# -*- coding: utf-8 -*-
"""报告渲染。纯函数。

这是「报告能脱离模型单独重建」的结构性保证（SDD R6）：本模块只接收已装配好的
数据结构，不 import 数据库、不 import 分析器，因此只要库里有分数行，报告就能
离线重建，且渲染路径上永远不会触发模型调用。
"""

from __future__ import annotations

from dataclasses import dataclass

from trend.config import ENGAGEMENT_WEIGHTS

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
        _render_style_claim_status(),
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


def _render_style_claim_status() -> str:
    return "\n".join(
        [
            "## 风格结论：仍然不产出",
            "",
            "本报告**没有任何风格 / 元素结论** —— 图片证据链路尚未接入，系统一张图都没看过。",
            "",
            "这是质量底线「无图片证据不下风格结论」的直接结果。本报告中的文本统计一律标为 "
            "`text_only`，**不构成风格判断**；等图片证据打通（切片 5），`image_backed` 的"
            "结论会单独成区表述。",
        ]
    )


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
