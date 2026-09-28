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
        "",
        _render_posts(data),
        "",
        _render_footer(),
    ]
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
    return "\n".join(lines)


def _render_style_claim_status() -> str:
    return "\n".join(
        [
            "## 风格结论：本切片不产出",
            "",
            "本切片只产出基于互动数据的帖子榜，尚未接入分析层，因此**报告中没有任何风格结论**。",
            "",
            "这是刻意的。按质量底线「无图片证据不下风格结论」，在图片证据链路打通之前，"
            "系统不输出任何风格 / 元素判断。后续切片会在此分区展示两类结论，并明确区分：",
            "",
            "- `text_only` —— 仅由标签与正文推出，不作为风格结论",
            "- `image_backed` —— 模型确实读取过图片",
        ]
    )


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
