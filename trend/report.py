# -*- coding: utf-8 -*-
"""报告渲染。纯函数。

这是「报告能脱离模型单独重建」的结构性保证（SDD R6）：本模块只接收已装配好的
数据结构，不 import 数据库、不 import 分析器，因此只要库里有分数行，报告就能
离线重建，且渲染路径上永远不会触发模型调用。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from trend.config import (
    ENGAGEMENT_WEIGHTS,
    VISION_COMMON_TERM_RATE,
    VISION_JUDGEMENT_DIMENSIONS,
    VISION_MIN_POSTS_FOR_JUDGEMENT,
    VISION_MIN_READS_FOR_RATE_RULE,
)

MAX_CELL_LEN = 60

# 原始标签词频表列出多少行。长尾很长（真机上 92% 的标签只出现一次），
# 全列出来只会淹没重点，剩下的用一行说明代替。
RAW_TAG_ROWS = 15

_CONFIDENCE_LABELS = {
    "high": "高（high）",
    "low": "低（low）",
    "unknown": "未知（unknown）",
}


@dataclass(frozen=True)
class ReportPost:
    rank: int
    note_id: str
    title: str
    excerpt: str
    nickname: str
    note_url: str
    source_keyword: str
    likes: int
    collected: int
    comments: int
    shares: int
    raw_score: float
    composite_score: float
    tag_list: tuple[str, ...]
    image_count: int


@dataclass(frozen=True)
class ReportTextFinding:
    dimension: str
    term: str
    post_count: int
    engagement_sum: float
    engagement_mean: float


@dataclass(frozen=True)
class ReportTagFrequency:
    tag: str
    post_count: int
    engagement_sum: float


@dataclass(frozen=True)
class ReportTextAnalysis:
    """报告侧看到的文本线索。刻意用 report.py 自己的类型，不 import 分析模块 ——
    这样「报告渲染不依赖分析器」是一条结构性约束，而不是约定。"""

    version: str
    findings: tuple[ReportTextFinding, ...]
    matched_posts: int
    total_posts: int
    raw_tags: tuple[ReportTagFrequency, ...] = ()
    distinct_tags: int = 0
    singleton_tags: int = 0

    def by_dimension(self) -> dict[str, tuple[ReportTextFinding, ...]]:
        grouped: dict[str, list[ReportTextFinding]] = {}
        for finding in self.findings:
            grouped.setdefault(finding.dimension, []).append(finding)
        return {name: tuple(items) for name, items in grouped.items()}


@dataclass(frozen=True)
class ReportImageRef:
    """结论 → 具体图片的引用。报告里点得开的那条链。"""

    note_id: str
    image_index: int
    image_url: str


@dataclass(frozen=True)
class ReportStyleFinding:
    """一条 `image_backed` 风格结论。

    `evidence` 非空是该等级的**定义**，不是约定 —— Q1 的校验在 runner 映射之前完成，
    报告侧只负责渲染，不负责判断证据够不够。
    """

    dimension: str
    term: str
    post_count: int
    engagement_sum: float
    evidence: tuple[ReportImageRef, ...]
    evidence_level: str = "image_backed"


@dataclass(frozen=True)
class ReportStyleAnalysis:
    """图片分析的整体结果，含覆盖率与失败数 —— 只报结论不报覆盖率是另一种失真。"""

    analysis_version: str
    model_id: str
    prompt_version: str
    vocabulary_version: str
    status: str
    posts_considered: int
    posts_read: int
    posts_failed: int
    posts_no_image: int
    images_sent: int
    rejected_terms: int
    findings: tuple[ReportStyleFinding, ...] = ()


@dataclass(frozen=True)
class ReportRun:
    run_id: str
    platform: str
    keywords: tuple[str, ...]
    confidence: str
    confidence_reason: str
    crawl_skipped: bool
    exit_code: int | None
    notes_total: int
    notes_new: int
    notes_updated: int
    error_line_count: int
    score_formula_version: str
    generated_at: str


@dataclass(frozen=True)
class ReportData:
    run: ReportRun
    posts: tuple[ReportPost, ...]
    total_scored: int
    keyword_counts: tuple[tuple[str, int], ...]
    top_n: int
    text_analysis: ReportTextAnalysis | None = None
    style_analysis: ReportStyleAnalysis | None = None


# 能改写 Markdown 结构或注入 HTML 的字符。标题/标签/昵称都来自被爬平台，属不可信文本。
# 只列真正有害的：方括号与圆括号能撑破链接语法，尖括号在 HTML 渲染器里是标签，
# 竖线会撑破表格，反斜杠是转义符本身。星号/下划线只是外观，反引号本模块自己要用。
_MARKDOWN_ESCAPES = str.maketrans({char: f"\\{char}" for char in "\\[]()<>|"})


def _fmt_int(value: int | None) -> str:
    return f"{value or 0:,}"


def _cell(value: object, *, limit: int = MAX_CELL_LEN) -> str:
    """表格单元格：折叠换行、转义 Markdown 控制字符、截断。

    不只是转义竖线 —— `](` 这类序列能撑破链接语法、注入任意 Markdown（乃至
    `javascript:` 链接），而标题正是来自被爬平台的不可信文本。
    """
    text = "" if value is None else str(value)
    text = " ".join(text.split())
    text = text.translate(_MARKDOWN_ESCAPES)
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text or "—"


def _safe_url(url: str | None) -> str | None:
    """只放行 http/https，并中和会截断链接语法的括号。

    note_url 由平台侧字段拼成，属不可信输入；`javascript:` 之类的伪协议必须挡掉。
    返回 None 表示不应把它渲染成链接。
    """
    if not url:
        return None
    text = "".join(url.split())
    if not text.lower().startswith(("http://", "https://")):
        return None
    return text.replace("(", "%28").replace(")", "%29")


def formula_text() -> str:
    parts = " + ".join(
        f"{weight:g}×{name}" for name, weight in ENGAGEMENT_WEIGHTS.items()
    )
    return f"log1p({parts})"


def render_report(data: ReportData) -> str:
    blocks = [
        f"# 穿搭趋势报告 · {data.run.platform}",
        "",
        _render_run_meta(data.run),
        "",
        _render_confidence(data.run),
        "",
        _render_metrics(data),
        "",
        _render_style_section(data),
    ]

    text_clues = _render_text_clues(data)
    if text_clues:
        blocks += ["", text_clues]

    blocks += ["", _render_posts(data), "", _render_footer()]
    return "\n".join(blocks).rstrip() + "\n"


def _render_run_meta(run: ReportRun) -> str:
    keywords = "、".join(run.keywords) if run.keywords else "—"
    crawl_state = "已跳过（仅对既有存档重新打分）" if run.crawl_skipped else "已执行"
    rows = (
        ("运行 ID", f"`{run.run_id}`"),
        ("平台", run.platform),
        ("关键词", keywords),
        ("生成时间", run.generated_at),
        ("打分公式版本", f"`{run.score_formula_version}`"),
        ("采集", crawl_state),
    )
    lines = ["## 运行信息", "", "| 项 | 值 |", "| --- | --- |"]
    lines += [f"| {_cell(key)} | {_cell(value)} |" for key, value in rows]
    return "\n".join(lines)


def _render_confidence(run: ReportRun) -> str:
    label = _CONFIDENCE_LABELS.get(run.confidence, run.confidence)
    lines = [
        f"## 批次可信度：{label}",
        "",
        run.confidence_reason or "（无判定依据）",
        "",
    ]
    if run.confidence == "low":
        lines += [
            "**本批次整批采集不完整。** 下方榜单只代表已成功入库的那部分，请勿当作全量结论。",
            "",
        ]
    if run.confidence == "unknown":
        lines += [
            "> 本次未执行采集，报告完整度取决于上一次采集批次。"
            "如需重新评估批次完整度，请执行一次带采集的运行。",
            "",
        ]
    lines += [
        "> 判定口径：能区分「整批采集不完整」与「个别内容失败」。"
        "个别条目失败只在采集明细中体现，**不否定整批**。",
    ]
    return "\n".join(lines)


def _render_metrics(data: ReportData) -> str:
    run = data.run
    lines = [
        "## 采集与入选",
        "",
        f"- 库内帖子总数：{_fmt_int(run.notes_total)}",
        f"- 本次新增：{_fmt_int(run.notes_new)}",
        f"- 本次更新：{_fmt_int(run.notes_updated)}",
        f"- 采集输出错误行：{_fmt_int(run.error_line_count)}",
        f"- 已打分帖子：{_fmt_int(data.total_scored)}（榜单取前 {_fmt_int(data.top_n)}）",
    ]
    if len(data.keyword_counts) > 1:
        lines += ["", "按来源关键词：", ""]
        lines += [
            f"- {_cell(keyword or '（空）', limit=40)}：{_fmt_int(count)}"
            for keyword, count in data.keyword_counts
        ]
        lines += ["", *_render_query_mix_warning(data.keyword_counts)]
    return "\n".join(lines)


def _render_query_mix_warning(
    keyword_counts: tuple[tuple[str, int], ...],
) -> list[str]:
    """多关键词时的口径警示。

    关键词是**查询**的一部分，不是中立的抽样框。把「穿搭 / 通勤穿搭 / 韩系穿搭」这类
    异质（而且互相包含）的查询合并统计，得到的分布反映的是查询配比 —— 搜了韩系就会
    得到更多韩系帖子，那是查询的结果，不是发现。真机上就靠这一点识破过一次循环论证。
    """
    keywords = "、".join(f"`{keyword or '（空）'}`" for keyword, _ in keyword_counts)
    return [
        f"> **口径警示：本次用了 {len(keyword_counts)} 个关键词（{keywords}），"
        "而本报告的统计是对**整个存档聚合**的。**",
        ">",
        "> 因此下方各项分布反映的是**这些查询的配比**，而不是平台的整体分布。"
        "搜了「韩系穿搭」自然就得到更多韩系帖子 —— 那是查询的结果，不是发现。",
        ">",
        "> 要读整体趋势，请用**单一宽泛关键词 + 深翻页**采集，让平台自身的排序决定构成。"
        "窄关键词只适合回答它自己那个问题，且应单独看、**不要合并**。",
        ">",
        "> 另外避免使用互相包含的关键词（如 `穿搭` 与 `通勤穿搭`）—— "
        "它们查回来的是同一个内容池。",
    ]


def _render_style_section(data: ReportData) -> str:
    """风格结论区。三种形态：没跑过 / 跑了但缺凭据 / 跑了有结果。

    「没跑」与「跑了没发现」必须分开讲 —— 把前者写成后者，就是把一次未执行的
    分析讲成一个关于穿搭的结论（这正是 Q1 要防的那类失真）。
    """
    analysis = data.style_analysis
    if analysis is None or analysis.status == "skipped_no_credentials":
        return _render_style_not_run(analysis)
    return _render_style_conclusions(analysis)


def _render_style_not_run(analysis: ReportStyleAnalysis | None) -> str:
    reason = (
        "本报告生成时尚未运行过图片分析。"
        if analysis is None
        else "上一次图片分析因缺少模型凭据而跳过，没有读取任何图片。"
    )
    return "\n".join(
        [
            "## 风格结论：本轮未运行图片分析",
            "",
            f"{reason}因此本报告**没有任何风格 / 元素结论**。",
            "",
            "这是质量底线「无图片证据不下风格结论」的直接结果：一张图都没看过，就不给"
            "风格判断。下方的文字统计一律标为 `text_only`，**不构成风格判断**。",
            "",
            "接入方式：配置 `TREND_LLM_API_KEY` 与 `TREND_LLM_MODEL`（可选 "
            "`TREND_LLM_BASE_URL`）后执行 `uv run python -m trend analyze`；"
            "届时风格结论会以 `image_backed` 等级单独成区展示。",
        ]
    )


def _render_style_meta(analysis: ReportStyleAnalysis) -> list[str]:
    """区块头：版本、覆盖率、口径。

    与下面的结论分档分开 —— 这一段回答「这次分析是怎么做的、可信度如何」，
    那一段回答「得出了什么」，改动理由不同。
    """
    lines = [
        "## 风格结论（证据等级：image_backed）",
        "",
        "本节结论**来自模型读取帖子图片**。它与下方「文本线索（text_only）」的证据等级"
        "不同，**不可混读**。",
        "",
        f"- 分析版本：`{_cell(analysis.analysis_version, limit=48)}`"
        "（提示词 / 词表 / 模型 / 打分公式任一变化都会换新版本）",
        f"- 模型：`{_cell(analysis.model_id, limit=48)}`　"
        f"提示词：`{_cell(analysis.prompt_version, limit=48)}`　"
        f"词表：`{_cell(analysis.vocabulary_version, limit=48)}`",
        f"- 覆盖度：候选 {_fmt_int(analysis.posts_considered)} 条 → 成功读取 "
        f"{_fmt_int(analysis.posts_read)} 条 / 读取失败 {_fmt_int(analysis.posts_failed)} 条 / "
        f"无可用图片 {_fmt_int(analysis.posts_no_image)} 条",
        f"- 送出图片 {_fmt_int(analysis.images_sent)} 张；丢弃词条 "
        f"{_fmt_int(analysis.rejected_terms)} 条（不在词表内、或没有有效图片编号，"
        "这类词条不会成为结论）",
        "- 每条结论都附支撑它的图片编号，编号对应帖子 `image_list` 的顺序",
        "- 口径：表内「帖子数」是**本次候选集内**的命中次数（只分析了榜单前 N 条），"
        "既不是平台整体分布、也不是时间趋势 —— **没有历史基线时，出现次数多不等于"
        "「在流行」**。要判断「在涨」，需要两次相隔时间的运行做对比",
        "- 排序：按**总传播贡献**（命中帖子的综合分之和）降序；帖子数用于区分"
        "「靠条数堆起来」与「靠单条高传播」",
        f"- 降级规则：命中率 ≥ {VISION_COMMON_TERM_RATE:.0%} 的词条不计入趋势判断 —— "
        "出现在大多数帖子里说明它是内容池的底色而不是信号，已移到下方背景描述",
        f"- 门槛：进入趋势判断需要至少 {VISION_MIN_POSTS_FOR_JUDGEMENT} 帖支撑；"
        "不足的先列在「待观察」，**不算结论**",
        "",
    ]

    if analysis.posts_failed:
        lines += [
            f"**注意**：有 {_fmt_int(analysis.posts_failed)} 条帖子读取失败，"
            "它们的风格**未被判定** —— 这不等于它们没有风格。",
            "",
        ]
    return lines


def _render_style_conclusions(analysis: ReportStyleAnalysis) -> str:
    lines = _render_style_meta(analysis)

    if not analysis.findings:
        lines += ["**本次没有产生任何风格结论。**", "", _empty_reason(analysis)]
        return "\n".join(lines)

    buckets: dict[str, list[ReportStyleFinding]] = {
        _JUDGEMENT: [],
        _TENTATIVE: [],
        _BACKGROUND: [],
    }
    for finding in analysis.findings:
        buckets[_classify(finding, analysis)].append(finding)
    judgement = buckets[_JUDGEMENT]
    tentative = buckets[_TENTATIVE]
    background = buckets[_BACKGROUND]

    if not judgement:
        reasons: list[str] = []
        if tentative:
            reasons.append(f"「待观察」命中帖数不足 {VISION_MIN_POSTS_FOR_JUDGEMENT} 帖")
        if background:
            reasons.append("「背景描述」是画面里的常见元素、或命中率过高")
        if reasons:
            # 一条判断都没有时必须说清楚 —— 否则读者会把「待观察」或「背景描述」
            # 当成本次的趋势。这与「把未运行说成没有」是同一类失真。
            lines += [
                f"**本次没有得出任何趋势判断** —— {'；'.join(reasons)}，都不构成趋势结论。",
                "",
            ]
    if judgement:
        lines += _render_finding_section(
            f"趋势判断（{'、'.join(_dimension_names(judgement))}）", judgement
        )
    if tentative:
        lines += _render_finding_section(
            f"待观察（{'、'.join(_dimension_names(tentative))}）",
            tentative,
            blurb=(
                f"命中帖数少于 {VISION_MIN_POSTS_FOR_JUDGEMENT} 帖 —— "
                "**单帖命中是轶事，不是趋势**。列在这里是为了不丢掉线索，"
                "不是让你据此下判断；样本够了它们会自己升上来。"
            ),
        )
    if background:
        lines += _render_finding_section(
            f"背景描述（{'、'.join(_dimension_names(background))}）",
            background,
            blurb=(
                "这里有两种东西，都不是趋势判断：**描述性维度**（画面里有什么），"
                "以及**命中率过高的词条**（出现于大多数帖子，属于这个内容池的底色）。"
                "出现次数多不代表在流行。列在这里，是为了说明上面那些判断来自什么样的样本。"
            ),
        )
    return "\n".join(lines)


def _render_finding_section(
    heading: str, findings: Sequence[ReportStyleFinding], *, blurb: str = ""
) -> list[str]:
    block = [f"### {heading}", ""]
    if blurb:
        block += [blurb, ""]
    return [*block, *_findings_table(findings), ""]


_JUDGEMENT = "judgement"
_TENTATIVE = "tentative"
_BACKGROUND = "background"


def _classify(finding: ReportStyleFinding, analysis: ReportStyleAnalysis) -> str:
    """把一条结论分档：趋势判断 / 待观察 / 背景描述。

    两道关卡缺一不可：维度得是判断性的，支撑帖子数也得够。命中率规则**只在分母足够
    大时才套用** —— 比例在小样本上会失真（3 帖里 2 帖 = 67%，会被误判成「太普遍」）。
    """
    if finding.dimension not in VISION_JUDGEMENT_DIMENSIONS:
        return _BACKGROUND
    if finding.post_count < VISION_MIN_POSTS_FOR_JUDGEMENT:
        return _TENTATIVE
    if (
        analysis.posts_read >= VISION_MIN_READS_FOR_RATE_RULE
        and finding.post_count / analysis.posts_read >= VISION_COMMON_TERM_RATE
    ):
        return _BACKGROUND
    return _JUDGEMENT


def _dimension_names(findings: Sequence[ReportStyleFinding]) -> list[str]:
    """按出现顺序去重的维度名 —— 小标题跟着数据走，不硬编码维度列表。"""
    names: dict[str, None] = {}
    for finding in findings:
        names.setdefault(finding.dimension, None)
    return list(names)


def _findings_table(findings: Sequence[ReportStyleFinding]) -> list[str]:
    rows = [
        "| 维度 | 词条 | 帖子数 | 总传播贡献 | 证据图 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for finding in findings:
        rows.append(
            "| {dimension} | {term} | {count} | {engagement} | {evidence} |".format(
                dimension=_cell(finding.dimension, limit=20),
                term=_cell(finding.term, limit=40),
                count=_fmt_int(finding.post_count),
                engagement=f"{finding.engagement_sum:.2f}",
                evidence=_render_evidence(finding.evidence),
            )
        )
    return rows


def _empty_reason(analysis: ReportStyleAnalysis) -> str:
    """空结果也要给出原因，而不是留一个空区块。"""
    if analysis.posts_read == 0 and analysis.posts_failed:
        return "本次没有任何一条帖子被成功读取（原因见上方覆盖率）—— 这不是「没有风格」，是没读到。"
    if analysis.posts_read == 0 and analysis.posts_no_image:
        return "候选帖子都拿不到可用图片，无从判断。"
    return (
        "模型看过图，但没有在词表范围内发现可判定的风格信号。"
        "这不等于「没有趋势」，只是这一批图里没有。"
    )


def _render_evidence(refs: tuple[ReportImageRef, ...], *, limit: int = 3) -> str:
    """把证据渲染成可点的图号链接。非法协议（如 `javascript:`）退化成纯文本。"""
    parts: list[str] = []
    for ref in refs[:limit]:
        label = _cell(f"{ref.note_id}#{ref.image_index}", limit=40)
        url = _safe_url(ref.image_url)
        parts.append(f"[{label}]({url})" if url else label)
    if len(refs) > limit:
        parts.append(f"…共 {_fmt_int(len(refs))} 张")
    return "、".join(parts) or "—"


def _render_text_clues(data: ReportData) -> str:
    analysis = data.text_analysis
    # 只要有原始标签就值得出这一节 —— 即使一条词表词条都没命中，
    # 「133 个标签里 123 个只出现一次」本身就是对趋势的一个回答。
    if analysis is None or (not analysis.findings and not analysis.raw_tags):
        return ""

    lines = [
        "## 文本线索（证据等级：text_only）",
        "",
        "这是**标签与文案的词频 × 互动加权**，不是风格结论。",
        "",
        "换个说法：下面「韩系」「通勤」这些词的意思是「有 N 条帖子在标签或文案里含该词，"
        "它们合计贡献了多少传播分」，而**不是**「韩系风格正在流行」。后一个判断需要看图，"
        "本切片没看。",
        "",
        f"- 词表版本：`{_cell(analysis.version, limit=48)}`",
        f"- 覆盖度：{_fmt_int(analysis.total_posts)} 条帖子中 "
        f"{_fmt_int(analysis.matched_posts)} 条命中词条"
        "（未命中的帖子既无标签、文案里也没有词表词条）",
        "- 维度内排序：按**总传播贡献**（命中帖子的综合分之和）降序；"
        "均值用于区分「靠条数堆起来」与「靠单条高传播」",
        "- 范围：本节分布来自**本次搜索的返回结果**，回答的是「搜索返回了什么」，"
        "不等于平台整体分布",
        "- 本节刻意**不分区**：它的定位是原始对照表（词表把信号归得对不对，拿它核），"
        "整节已声明不是风格结论，再拆成「趋势判断 / 待观察 / 背景描述」只是把一句"
        "免责声明拆成三段",
        "",
    ]

    lines += _render_raw_tags(analysis)

    for dimension, findings in analysis.by_dimension().items():
        lines += [
            f"### {_cell(dimension, limit=20)}",
            "",
            "| 词条 | 帖子数 | 总传播贡献 | 均值 |",
            "| --- | --- | --- | --- |",
        ]
        lines += [
            f"| {_cell(item.term, limit=30)} | {_fmt_int(item.post_count)} "
            f"| {item.engagement_sum:.2f} | {item.engagement_mean:.2f} |"
            for item in findings
        ]
        lines.append("")

    return "\n".join(lines).rstrip()


def _render_raw_tags(analysis: ReportTextAnalysis) -> list[str]:
    """原始标签词频 —— 词表聚类的对照基准。

    放在维度表之前是有意的：先给证据，再给归一化结果，读者才能核对词表有没有把
    信号归歪（真机上就靠这张表发现过「复古」被「美式复古」重复计入）。
    """
    if not analysis.raw_tags:
        return []

    shown = analysis.raw_tags[:RAW_TAG_ROWS]
    hidden = analysis.distinct_tags - len(shown)
    lines = [
        "### 原始标签词频（未归一化，供核对）",
        "",
        f"共 {_fmt_int(analysis.distinct_tags)} 个不同标签，其中 "
        f"**{_fmt_int(analysis.singleton_tags)} 个只出现 1 次**。",
        "",
        "这正是词表需要存在的原因：单个标签太碎，读不出趋势。"
        "下表未经任何归并，可与下方的维度表逐条对照 —— "
        "它能暴露词表可能归错的地方。",
        "",
        "| 标签 | 帖子数 | 总传播贡献 |",
        "| --- | --- | --- |",
    ]
    lines += [
        f"| {_cell(item.tag, limit=40)} | {_fmt_int(item.post_count)} "
        f"| {item.engagement_sum:.2f} |"
        for item in shown
    ]
    if hidden > 0:
        lines += ["", f"（仅列前 {len(shown)} 个；其余 {_fmt_int(hidden)} 个频次更低）"]
    lines.append("")
    return lines


def _render_posts(data: ReportData) -> str:
    lines = [
        f"## 帖子榜 Top {_fmt_int(data.top_n)}",
        "",
        f"按综合传播分降序。综合分 = `{formula_text()}`；转发权重最高，"
        "故口径以转发为主，含点赞、收藏、评论。",
        "",
    ]
    if not data.posts:
        lines.append("（无可排序的帖子。）")
        return "\n".join(lines)

    lines += [
        "| # | 综合分 | 转发 | 收藏 | 评论 | 点赞 | 图 | 标题 | 博主 | 标签 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for post in data.posts:
        title_cell = _cell(post.title or post.excerpt)
        safe_url = _safe_url(post.note_url)
        if safe_url:
            title_cell = f"[{title_cell}]({safe_url})"
        tags = "、".join(post.tag_list) if post.tag_list else "—"
        lines.append(
            "| {rank} | {score:.2f} | {shares} | {collected} | {comments} | {likes} "
            "| {images} | {title} | {nick} | {tags} |".format(
                rank=post.rank,
                score=post.composite_score,
                shares=_fmt_int(post.shares),
                collected=_fmt_int(post.collected),
                comments=_fmt_int(post.comments),
                likes=_fmt_int(post.likes),
                images=_fmt_int(post.image_count),
                title=title_cell,
                nick=_cell(post.nickname),
                tags=_cell(tags),
            )
        )
    return "\n".join(lines)


def _render_footer() -> str:
    return "\n".join(
        [
            "---",
            "",
            "本报告由 `trend` 从本地存档直接重建，渲染过程不依赖任何模型。",
            "重建命令：`uv run python -m trend report --run-id <ID>`",
        ]
    )
