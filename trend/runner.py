# -*- coding: utf-8 -*-
"""编排：采集（可选）→ 读存档 → 打分 → 幂等落库 → 出报告。

报告装配与渲染都只读数据库，不碰分析器，因此「脱离模型重建报告」是结构性的，
不是靠约定（SDD R6）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Sequence

from trend import store
from trend.config import (
    DEFAULT_KEYWORDS,
    DEFAULT_PLATFORM,
    DEFAULT_SAVE_DATA_OPTION,
    DEFAULT_TOP_N,
    LOW_CONFIDENCE_ERROR_LINES,
    REPORT_DIR,
    SCORE_FORMULA_VERSION,
)
from trend.crawler import (
    ERROR_SAMPLE_LEN,
    SPAWN_FAILURE_EXIT_CODE,
    CrawlOutcome,
    Executor,
    run_crawl,
)
from trend.report import ReportData, ReportPost, ReportRun, render_report


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
    error_line_threshold: int = LOW_CONFIDENCE_ERROR_LINES,
) -> tuple[str, str]:
    """判定「整批是否完整」。返回 (confidence, reason)。

    要点（SDD 质量底线 Q2）：个别内容失败只体现为错误行数，只要未超阈值就不否定
    整批；只有退出码非 0、库内全空、关键词零匹配、或错误行数超阈值才判 low。

    阈值用绝对值而非比率：采集子进程的输出推不出本轮批次规模，拿「错误行 / 库内
    条目数」折算会随存档增长而稀释，让大归档里的半失败批次蒙混过关。
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
    if error_line_count > error_line_threshold:
        return "low", (
            f"采集输出错误行 {error_line_count} 条，超过阈值 {error_line_threshold} 条。"
            "批次规模无法从采集子进程推知，故不按比率折算 —— 超过绝对阈值即视为整批可疑。"
        )
    if notes_new <= 0:
        return "high", "本次无新增条目（既有存档已是最新）；错误行未超阈值，批次视为完整。"
    return "high", f"本次新增 {notes_new} 条，错误行 {error_line_count} 条，未超阈值，批次完整。"


def _crawl_or_record_failure(
    platform: str,
    keywords: Sequence[str],
    headless: bool,
    save_data_option: str,
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
            headless=headless,
            save_data_option=save_data_option,
            executor=crawl_executor,
        )
    except Exception as exc:  # noqa: BLE001 —— 采集期任何异常都要转成可记录的结果
        return CrawlOutcome(
            exit_code=SPAWN_FAILURE_EXIT_CODE,
            error_line_count=1,
            error_sample=f"采集阶段异常：{exc!r}"[:ERROR_SAMPLE_LEN],
            spawn_failed=True,
        )


async def build_report_data(*, run_id: str, platform: str, top_n: int) -> ReportData:
    """从存档装配报告数据。与重建共用同一条路径。"""
    run_record = await store.get_run(run_id)
    if run_record is None:
        raise KeyError(f"未找到运行记录 {run_id!r}")

    stored = await store.fetch_scored_posts(platform, limit=top_n)
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
        for index, item in enumerate(stored)
    )
    return ReportData(
        run=report_run,
        posts=posts,
        total_scored=await store.count_scored(platform),
        keyword_counts=await store.count_by_keyword(platform),
        top_n=top_n,
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
    *, run_id: str, platform: str, top_n: int, output_dir: str | Path | None
) -> RunResult:
    data = await build_report_data(run_id=run_id, platform=platform, top_n=top_n)
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
    headless: bool = False,
    save_data_option: str = DEFAULT_SAVE_DATA_OPTION,
    output_dir: str | Path | None = None,
    crawl_executor: Executor | None = None,
) -> RunResult:
    """跑一次完整流水线。`crawl_executor` 可注入以便测试。"""
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

    if skip_crawl:
        outcome = CrawlOutcome(
            exit_code=0, error_line_count=0, error_sample="", skipped=True
        )
    else:
        outcome = _crawl_or_record_failure(
            platform, active_keywords, headless, save_data_option, crawl_executor
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
        run_id=run_id, platform=platform, top_n=top_n, output_dir=output_dir
    )


async def rebuild_report(
    *,
    run_id: str | None = None,
    platform: str = DEFAULT_PLATFORM,
    top_n: int = DEFAULT_TOP_N,
    save_data_option: str = DEFAULT_SAVE_DATA_OPTION,
    output_dir: str | Path | None = None,
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
        run_id=run_id, platform=platform, top_n=top_n, output_dir=output_dir
    )
