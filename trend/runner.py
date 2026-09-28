# -*- coding: utf-8 -*-
"""编排：采集（可选）→ 读存档 → 打分 → 幂等落库 → 出报告。

报告装配与渲染都只读数据库，不碰分析器，因此「脱离模型重建报告」是结构性的，
不是靠约定（SDD R6）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Mapping, Sequence

from media_downloader.downloader import MediaDownloader

from trend import store
from trend.config import (
    DEFAULT_KEYWORDS,
    DEFAULT_PLATFORM,
    DEFAULT_SAVE_DATA_OPTION,
    DEFAULT_TOP_N,
    ERROR_LINES_FALLBACK,
    ERROR_RATE_THRESHOLD,
    REPORT_DIR,
    SCORE_FORMULA_VERSION,
    VISION_MAX_IMAGES_PER_POST,
    VISION_TOP_N,
)
from trend.analysis import AnalysisPost, analyse
from trend.crawler import (
    ERROR_SAMPLE_LEN,
    SPAWN_FAILURE_EXIT_CODE,
    CrawlOptions,
    CrawlOutcome,
    Executor,
    requested_volume,
    run_crawl,
)
from trend.images import build_image_downloader, fetch_note_images
from trend.llm import StyleAnalyzer, VisionRequest, build_analyzer
from trend.report import (
    ReportData,
    ReportImageRef,
    ReportPost,
    ReportRun,
    ReportStyleAnalysis,
    ReportStyleFinding,
    ReportTagFrequency,
    ReportTextAnalysis,
    ReportTextFinding,
    render_report,
)
from trend.vision import (
    STATUS_NO_IMAGES,
    EvidenceError,
    ImageRef,
    VisionRead,
    aggregate_vision_reads,
    validate_finding,
)
from trend.vocab import Vocabulary, load_vocabulary


@dataclass(frozen=True)
class RunResult:
    run_id: str
    confidence: str
    confidence_reason: str
    notes_total: int
    notes_new: int
    notes_updated: int
    scored: int
    report_path: Path
    report_markdown: str


def new_run_id() -> str:
    return f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def assess_confidence(
    *,
    crawl_skipped: bool,
    exit_code: int | None,
    error_line_count: int,
    notes_total: int,
    notes_matched: int,
    notes_new: int,
    results_exhausted: bool = False,
    requested: int | None = None,
    error_rate_threshold: float = ERROR_RATE_THRESHOLD,
    error_lines_fallback: int = ERROR_LINES_FALLBACK,
) -> tuple[str, str]:
    """判定「整批是否完整」。返回 (confidence, reason)。

    要点（SDD 质量底线 Q2）：必须能区分「整批采集不完整」与「个别内容失败」。

    两条判据都是被真机数据逼出来的，不是推出来的：

    1. 爬虫报「结果取尽」时，批次范围由平台决定、而非被失败截断 —— 此时个别详情抓取
       失败不可能让整批不完整，错误行数只作明细，不判 low。
    2. 否则按「错误行 / **本轮请求目标量**」折算。分母必须是本轮目标：用库内总条目会随
       存档增长而稀释；用绝对条数则不是尺度无关的 —— 实测同一失败率（7.5%）在 120 条
       批次被判 high、在 185 条批次被判 low，只因为绝对错误数分别在 10 的两侧。
    """
    if crawl_skipped:
        return "unknown", "本次跳过采集，未评估本批次完整度；榜单基于既有存档。"
    if exit_code not in (0, None):
        return "low", f"采集子进程退出码 {exit_code}，整批采集可能不完整。"
    if notes_total <= 0:
        return "low", "库内无任何条目，无法产出趋势。"
    if notes_matched <= 0:
        return "low", (
            f"关键词未匹配到任何已入库条目（库内共 {notes_total} 条），本次无法产出榜单。"
        )

    exhausted_note = "爬虫报告搜索结果已取尽，批次范围完整"
    if results_exhausted:
        if notes_new <= 0:
            return "high", (
                f"{exhausted_note}，本次无新增；错误行 {error_line_count} 条属个别条目失败。"
            )
        return "high", (
            f"{exhausted_note}；本次新增 {notes_new} 条，错误行 {error_line_count} 条"
            "属个别条目失败，不否定整批。"
        )

    if requested and requested > 0:
        rate = error_line_count / requested
        if rate > error_rate_threshold:
            return "low", (
                f"采集输出错误行 {error_line_count} 条，占本轮请求目标 {requested} 条的 "
                f"{rate:.0%}，超过阈值 {error_rate_threshold:.0%}，整批采集可能不完整。"
            )
        if notes_new <= 0:
            return "high", (
                f"本次无新增条目（既有存档已是最新）；错误行 {error_line_count} 条占目标 "
                f"{requested} 条的 {rate:.0%}，未超阈值，批次完整。"
            )
        return "high", (
            f"本次新增 {notes_new} 条，错误行 {error_line_count} 条占目标 {requested} 条的 "
            f"{rate:.0%}，未超阈值，批次完整。"
        )

    if error_line_count > error_lines_fallback:
        return "low", (
            f"推不出本轮目标量（未指定 --max-notes），错误行 {error_line_count} 条"
            f"超过兜底阈值 {error_lines_fallback} 条，整批采集可能不完整。"
        )
    if notes_new <= 0:
        return "high", "本次无新增条目（既有存档已是最新）；错误行未超兜底阈值，批次视为完整。"
    return "high", (
        f"本次新增 {notes_new} 条，错误行 {error_line_count} 条未超兜底阈值，批次完整。"
    )


def _crawl_or_record_failure(
    platform: str,
    keywords: Sequence[str],
    options: CrawlOptions,
    crawl_executor: Executor | None,
) -> CrawlOutcome:
    """采集，并把任何采集期异常转成结果对象。

    run_crawl 自身已处理「超时」与「子进程起不来」两种常见失败；这里兜住其余意外。
    原则：采集出的任何问题都不得让本轮没有报告，也不得留下孤儿 running 行 ——
    失败要被记录下来、写进报告，而不是把异常抛给调用方。异常信息随 outcome 落库，
    所以这不是吞掉错误。
    """
    try:
        return run_crawl(
            platform,
            keywords,
            options=options,
            executor=crawl_executor,
        )
    except Exception as exc:  # noqa: BLE001 —— 采集期任何异常都要转成可记录的结果
        return CrawlOutcome(
            exit_code=SPAWN_FAILURE_EXIT_CODE,
            error_line_count=1,
            error_sample=f"采集阶段异常：{exc!r}"[:ERROR_SAMPLE_LEN],
            spawn_failed=True,
        )


async def build_report_data(
    *,
    run_id: str,
    platform: str,
    top_n: int,
    vocabulary: Vocabulary | None = None,
) -> ReportData:
    """从存档装配报告数据。与重建共用同一条路径。"""
    run_record = await store.get_run(run_id)
    if run_record is None:
        raise KeyError(f"未找到运行记录 {run_id!r}")

    # 取全量：词频分析必须跑在所有帖子上，只看榜单前 N 条会截断词频。
    all_posts = await store.fetch_scored_posts(platform)
    board = all_posts[:top_n]

    report_run = ReportRun(
        run_id=run_record.run_id,
        platform=run_record.platform or platform,
        keywords=run_record.keywords,
        confidence=run_record.confidence,
        confidence_reason=run_record.confidence_reason,
        crawl_skipped=run_record.crawl_skipped,
        exit_code=run_record.exit_code,
        notes_total=run_record.notes_total,
        notes_new=run_record.notes_new,
        notes_updated=run_record.notes_updated,
        error_line_count=run_record.error_line_count,
        score_formula_version=run_record.score_formula_version,
        generated_at=_now_text(),
    )
    posts = tuple(
        ReportPost(
            rank=index + 1,
            note_id=item.note_id,
            title=item.title,
            excerpt=item.desc_excerpt,
            nickname=item.nickname or "",
            note_url=item.note_url or "",
            source_keyword=item.source_keyword,
            likes=item.likes,
            collected=item.collected,
            comments=item.comments,
            shares=item.shares,
            raw_score=item.raw_score,
            composite_score=item.composite_score,
            tag_list=item.tag_list,
            image_count=item.image_count,
        )
        for index, item in enumerate(board)
    )
    return ReportData(
        run=report_run,
        posts=posts,
        total_scored=await store.count_scored(platform),
        keyword_counts=await store.count_by_keyword(platform),
        top_n=top_n,
        text_analysis=_build_text_analysis(all_posts, vocabulary),
        # 分数复用上面那次 fetch_scored_posts 的结果，不再查一次库。
        style_analysis=await _build_style_analysis(
            platform, scores={item.note_id: item.composite_score for item in all_posts}
        ),
    )


async def _build_style_analysis(
    platform: str, *, scores: Mapping[str, float]
) -> ReportStyleAnalysis | None:
    """从**已落库的逐帖读数**装配风格结论。

    这条路不碰任何分析器、不发任何请求 —— 它是 R6「报告能脱离模型重建」的落点。
    聚合是纯函数（`trend/vision.py`），所以调整聚合规则后仍可免模型重算。
    """
    record = await store.latest_style_analysis(platform)
    if record is None:
        return None

    reads = await store.fetch_vision_reads(platform, record.analysis_version)
    aggregated = aggregate_vision_reads(
        reads, version=record.analysis_version, scores=scores
    )

    findings: list[ReportStyleFinding] = []
    for finding in aggregated.findings:
        try:
            validate_finding(finding)
        except EvidenceError:
            # 最后一道 Q1 闸：没有图片引用的结论绝不进报告。
            continue
        findings.append(
            ReportStyleFinding(
                dimension=finding.dimension,
                term=finding.term,
                post_count=finding.post_count,
                engagement_sum=finding.engagement_sum,
                evidence=tuple(
                    ReportImageRef(
                        note_id=ref.note_id,
                        image_index=ref.image_index,
                        image_url=ref.image_url,
                    )
                    for ref in finding.evidence
                ),
            )
        )

    return ReportStyleAnalysis(
        analysis_version=record.analysis_version,
        model_id=record.model_id,
        prompt_version=record.prompt_version,
        vocabulary_version=record.vocabulary_version,
        status=record.status,
        posts_considered=record.posts_considered,
        posts_read=aggregated.posts_read,
        posts_failed=aggregated.posts_failed,
        posts_no_image=aggregated.posts_no_image,
        images_sent=aggregated.images_sent,
        rejected_terms=aggregated.rejected_terms,
        findings=tuple(findings),
    )


def _build_text_analysis(
    all_posts: Sequence[store.StoredPost], vocabulary: Vocabulary | None
) -> ReportTextAnalysis:
    """把规则分析结果映射成报告侧类型。

    映射而非直接传分析模块的对象 —— 这样 report.py 不必 import 分析模块，
    「报告渲染不依赖分析器」才是结构性保证而不是口头约定。
    """
    analysis = analyse(
        [
            AnalysisPost(
                note_id=item.note_id,
                title=item.title,
                desc=item.desc_excerpt,
                tags=item.tag_list,
                engagement=item.composite_score,
            )
            for item in all_posts
        ],
        vocabulary if vocabulary is not None else load_vocabulary(),
    )
    return ReportTextAnalysis(
        version=analysis.version,
        findings=tuple(
            ReportTextFinding(
                dimension=finding.dimension,
                term=finding.term,
                post_count=finding.post_count,
                engagement_sum=finding.engagement_sum,
                engagement_mean=finding.engagement_mean,
            )
            for finding in analysis.findings
        ),
        matched_posts=analysis.matched_posts,
        total_posts=analysis.total_posts,
        raw_tags=tuple(
            ReportTagFrequency(
                tag=item.tag,
                post_count=item.post_count,
                engagement_sum=item.engagement_sum,
            )
            for item in analysis.tag_frequencies
        ),
        distinct_tags=analysis.distinct_tags,
        singleton_tags=analysis.singleton_tags,
    )


def write_report(
    markdown: str,
    *,
    run_id: str,
    platform: str,
    output_dir: str | Path | None = None,
) -> Path:
    directory = Path(output_dir) if output_dir else REPORT_DIR
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{platform}-{run_id}.md"
    path.write_text(markdown, encoding="utf-8")
    return path


async def _emit(
    *,
    run_id: str,
    platform: str,
    top_n: int,
    output_dir: str | Path | None,
    vocabulary: Vocabulary | None = None,
) -> RunResult:
    data = await build_report_data(
        run_id=run_id, platform=platform, top_n=top_n, vocabulary=vocabulary
    )
    markdown = render_report(data)
    path = write_report(
        markdown,
        run_id=run_id,
        platform=data.run.platform or platform,
        output_dir=output_dir,
    )
    return RunResult(
        run_id=data.run.run_id,
        confidence=data.run.confidence,
        confidence_reason=data.run.confidence_reason,
        notes_total=data.run.notes_total,
        notes_new=data.run.notes_new,
        notes_updated=data.run.notes_updated,
        scored=len(data.posts),
        report_path=path,
        report_markdown=markdown,
    )


async def run_pipeline(
    *,
    platform: str = DEFAULT_PLATFORM,
    keywords: Sequence[str] | None = None,
    top_n: int = DEFAULT_TOP_N,
    skip_crawl: bool = False,
    save_data_option: str = DEFAULT_SAVE_DATA_OPTION,
    crawl_options: CrawlOptions | None = None,
    output_dir: str | Path | None = None,
    crawl_executor: Executor | None = None,
    vocabulary: Vocabulary | None = None,
) -> RunResult:
    """跑一次完整流水线。`crawl_executor` / `vocabulary` 可注入以便测试。"""
    active_keywords = tuple(keywords or DEFAULT_KEYWORDS)
    store.use_backend(save_data_option)
    # 前置校验：platform 会先落库，之后还会成为报告文件名的一部分。
    store.validate_platform(platform)
    await store.init_trend_tables()

    run_id = new_run_id()
    await store.start_run(
        run_id,
        platform,
        active_keywords,
        crawl_skipped=skip_crawl,
        formula_version=SCORE_FORMULA_VERSION,
    )

    # save_data_option 同时也决定本进程的存档后端，以它为准覆盖进采集参数 ——
    # 否则会出现子进程写一个库、分析读另一个库的静默错位。
    effective_options = replace(
        crawl_options or CrawlOptions(), save_data_option=save_data_option
    )

    if skip_crawl:
        outcome = CrawlOutcome(
            exit_code=0, error_line_count=0, error_sample="", skipped=True
        )
    else:
        outcome = _crawl_or_record_failure(
            platform, active_keywords, effective_options, crawl_executor
        )

    notes_total = await store.count_notes(platform)
    notes = await store.fetch_notes(platform, keywords=active_keywords)
    rows = [
        store.build_score_row(note, platform, formula_version=SCORE_FORMULA_VERSION)
        for note in notes
    ]
    notes_new, notes_updated = await store.persist_scores(rows, run_id)

    confidence, reason = assess_confidence(
        crawl_skipped=skip_crawl,
        exit_code=outcome.exit_code,
        error_line_count=outcome.error_line_count,
        notes_total=notes_total,
        notes_matched=len(notes),
        notes_new=notes_new,
        results_exhausted=outcome.results_exhausted,
        requested=None
        if skip_crawl
        else requested_volume(active_keywords, effective_options),
    )
    # 运行状态必须如实反映采集结果：否则失败的采集会以 succeeded 落库，
    # 与该字段自己的契约（running/succeeded/failed）矛盾，也会骗过按 status 过滤的消费者。
    crawl_ok = (
        outcome.exit_code == 0 and not outcome.timed_out and not outcome.spawn_failed
    )
    run_status = "succeeded" if (skip_crawl or crawl_ok) else "failed"

    await store.finish_run(
        run_id,
        status=run_status,
        confidence=confidence,
        confidence_reason=reason,
        exit_code=outcome.exit_code,
        notes_total=notes_total,
        notes_new=notes_new,
        notes_updated=notes_updated,
        error_line_count=outcome.error_line_count,
        error_sample=outcome.error_sample,
    )
    return await _emit(
        run_id=run_id,
        platform=platform,
        top_n=top_n,
        output_dir=output_dir,
        vocabulary=vocabulary,
    )


async def rebuild_report(
    *,
    run_id: str | None = None,
    platform: str = DEFAULT_PLATFORM,
    top_n: int = DEFAULT_TOP_N,
    save_data_option: str = DEFAULT_SAVE_DATA_OPTION,
    output_dir: str | Path | None = None,
    vocabulary: Vocabulary | None = None,
) -> RunResult:
    """只读存档重建报告：不采集、不调用任何模型（SDD R6）。"""
    store.use_backend(save_data_option)
    await store.init_trend_tables()

    if run_id is None:
        latest = await store.latest_run(platform)
        if latest is None:
            raise KeyError("库内没有任何运行记录，请先执行一次 `trend run`。")
        run_id = latest.run_id

    return await _emit(
        run_id=run_id,
        platform=platform,
        top_n=top_n,
        output_dir=output_dir,
        vocabulary=vocabulary,
    )


# --------------------------------------------------------------------------- #
# 图片分析（切片 5）：只读存档 + 抓图 + 调模型，不采集
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AnalysisResult:
    analysis_id: str
    run_id: str
    analysis_version: str
    status: str
    posts_considered: int
    posts_analyzed: int
    posts_failed: int
    posts_no_image: int
    images_sent: int
    rejected_terms: int
    findings: int
    report_path: Path | None
    report_markdown: str


async def _analyze_pending(
    *,
    platform: str,
    analysis_version: str,
    run_id: str,
    posts: Sequence[store.StoredPost],
    scores: Mapping[str, float],
    analyzer: StyleAnalyzer,
    downloader: MediaDownloader,
    max_images: int,
) -> tuple[str, dict[str, int], str]:
    """读还没读过的帖子，落库，然后按该版本的**全量**读数聚合。"""
    already = await store.fetch_analyzed_note_ids(platform, analysis_version)
    pending = [post for post in posts if post.note_id not in already]
    urls_by_note = await store.fetch_note_image_urls(
        platform, [post.note_id for post in pending]
    )

    reads: list[VisionRead] = []
    for post in pending:
        images = await fetch_note_images(
            post.note_id,
            urls_by_note.get(post.note_id, ()),
            downloader=downloader,
            max_images=max_images,
        )
        if not images:
            # 一张图都没下到：记「无图」，不送模型（无图请求会诱导它照提示词编）。
            reads.append(VisionRead(note_id=post.note_id, status=STATUS_NO_IMAGES))
            continue
        reads.append(
            await analyzer.analyze(
                VisionRequest(
                    note_id=post.note_id,
                    images=tuple(
                        ImageRef(
                            index=image.index, url=image.url, local_path=str(image.path)
                        )
                        for image in images
                    ),
                    image_bytes=tuple(image.data for image in images),
                )
            )
        )

    await store.persist_vision_reads(
        reads,
        platform=platform,
        analysis_version=analysis_version,
        run_id=run_id,
        model_id=analyzer.info.model_id,
    )

    # 用该版本的全量读数聚合，而不是只用本轮新增的 —— 否则增量运行的报告会只
    # 反映最新那几条帖子，历史结论凭空消失。
    all_reads = await store.fetch_vision_reads(platform, analysis_version)
    aggregated = aggregate_vision_reads(
        all_reads, version=analysis_version, scores=scores
    )

    if aggregated.posts_read == 0 and aggregated.posts_failed:
        status = "failed"
    elif aggregated.posts_failed:
        status = "partial"
    else:
        status = "succeeded"

    error_sample = next(
        (read.error[:ERROR_SAMPLE_LEN] for read in reads if read.error), ""
    )
    counts = {
        "posts_analyzed": aggregated.posts_read,
        "posts_failed": aggregated.posts_failed,
        "posts_no_image": aggregated.posts_no_image,
        "images_sent": aggregated.images_sent,
        "terms_rejected": aggregated.rejected_terms,
    }
    return status, counts, error_sample


async def run_analysis(
    *,
    run_id: str | None = None,
    platform: str = DEFAULT_PLATFORM,
    vision_top_n: int = VISION_TOP_N,
    max_images: int = VISION_MAX_IMAGES_PER_POST,
    save_data_option: str = DEFAULT_SAVE_DATA_OPTION,
    analyzer: StyleAnalyzer | None = None,
    downloader: MediaDownloader | None = None,
    vocabulary: Vocabulary | None = None,
    output_dir: str | Path | None = None,
    render: bool = True,
) -> AnalysisResult:
    """对**既有存档**做图片分析。

    刻意与 `run_pipeline` 分开：采集要动小号、要用户在场；分析只读库和图片。绑成
    一条命令会让「只是想重跑分析」也必须冒一次风控风险。

    无凭据不是错误，而是记一条 `skipped_no_credentials` 台账就收工（R6）。
    """
    store.use_backend(save_data_option)
    store.validate_platform(platform)
    await store.init_trend_tables()

    vocab = vocabulary if vocabulary is not None else load_vocabulary()

    if run_id is None:
        latest = await store.latest_run(platform)
        if latest is None:
            raise KeyError("库内没有任何运行记录，请先执行一次 `trend run`。")
        run_id = latest.run_id

    posts = await store.fetch_scored_posts(platform, limit=vision_top_n)
    scores = {post.note_id: post.composite_score for post in posts}

    owns_analyzer = analyzer is None
    active = analyzer or build_analyzer(
        vocabulary=vocab, score_formula_version=SCORE_FORMULA_VERSION
    )
    version = active.info.analysis_version
    analysis_id = uuid.uuid4().hex[:16]

    await store.start_style_analysis(
        analysis_id,
        run_id=run_id,
        platform=platform,
        analysis_version=version,
        model_id=active.info.model_id,
        prompt_version=active.info.prompt_version,
        vocabulary_version=vocab.version,
        score_formula_version=SCORE_FORMULA_VERSION,
        posts_considered=len(posts),
    )

    status = "skipped_no_credentials"
    error_sample = ""
    counts = {
        "posts_analyzed": 0,
        "posts_failed": 0,
        "posts_no_image": 0,
        "images_sent": 0,
        "terms_rejected": 0,
    }

    try:
        if active.info.available:
            status, counts, error_sample = await _analyze_pending(
                platform=platform,
                analysis_version=version,
                run_id=run_id,
                posts=posts,
                scores=scores,
                analyzer=active,
                downloader=downloader or build_image_downloader(platform=platform),
                max_images=max_images,
            )
        else:
            error_sample = str(getattr(active, "reason", "未配置模型凭据"))[
                :ERROR_SAMPLE_LEN
            ]
    finally:
        # 只关我们自己造的那个；调用方注入的分析器由调用方负责。
        if owns_analyzer:
            await active.aclose()

    await store.finish_style_analysis(
        analysis_id, status=status, error_sample=error_sample, **counts
    )

    report_path: Path | None = None
    report_markdown = ""
    if render:
        emitted = await _emit(
            run_id=run_id,
            platform=platform,
            top_n=DEFAULT_TOP_N,
            output_dir=output_dir,
            vocabulary=vocab,
        )
        report_path = emitted.report_path
        report_markdown = emitted.report_markdown

    built = await _build_style_analysis(platform, scores=scores)

    return AnalysisResult(
        analysis_id=analysis_id,
        run_id=run_id,
        analysis_version=version,
        status=status,
        posts_considered=len(posts),
        posts_analyzed=counts["posts_analyzed"],
        posts_failed=counts["posts_failed"],
        posts_no_image=counts["posts_no_image"],
        images_sent=counts["images_sent"],
        rejected_terms=counts["terms_rejected"],
        findings=len(built.findings) if built else 0,
        report_path=report_path,
        report_markdown=report_markdown,
    )
