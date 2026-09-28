# -*- coding: utf-8 -*-
"""规则/词表分析：只产出证据等级为 ``text_only`` 的**文本线索**。

刻意不做的事：不推断风格、不推断元素搭配、不下任何需要看图的结论。

为什么这条线划得这么死：质量底线是「无图片证据不下风格结论」（SDD Q1）。本模块
只看文字，一张图都没看过，所以它的输出**不得被称为风格结论**，在报告里也必须与
将来 ``image_backed`` 的结论分区展示。把「打了『韩系』标签的帖子互动很高」讲成
「韩系风格正在流行」，就是臆造。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from trend.vocab import Vocabulary

# 每个词条最多记录几条代表帖（按互动降序）。
TOP_POSTS_PER_FINDING = 3


@dataclass(frozen=True)
class AnalysisPost:
    """分析输入。刻意与存储层解耦，便于用构造出来的数据做单测。"""

    note_id: str
    title: str
    desc: str
    tags: tuple[str, ...]
    engagement: float


@dataclass(frozen=True)
class TextSignal:
    note_id: str
    dimension: str
    term: str


@dataclass(frozen=True)
class DimensionFinding:
    dimension: str
    term: str
    post_count: int
    engagement_sum: float
    engagement_mean: float
    top_note_ids: tuple[str, ...]


@dataclass(frozen=True)
class TagFrequency:
    """原始标签的频次，**未做任何归一化**。

    存在的意义是让词表的聚合成可核对：没有这张表，读者只能看到「风格:韩系 4 条」，
    看不到构成它的原始标签是 `韩系穿搭`×4，也就无法判断词表有没有把信号归歪。
    """

    tag: str
    post_count: int
    engagement_sum: float


@dataclass(frozen=True)
class TextAnalysis:
    version: str
    findings: tuple[DimensionFinding, ...]
    matched_posts: int
    total_posts: int
    tag_frequencies: tuple[TagFrequency, ...] = ()
    distinct_tags: int = 0
    singleton_tags: int = 0

    def by_dimension(self) -> dict[str, tuple[DimensionFinding, ...]]:
        grouped: dict[str, list[DimensionFinding]] = {}
        for finding in self.findings:
            grouped.setdefault(finding.dimension, []).append(finding)
        return {name: tuple(items) for name, items in grouped.items()}

    def top_tags(self, limit: int) -> tuple[TagFrequency, ...]:
        return self.tag_frequencies[:limit]


def _drop_subsumed(terms: list[str]) -> list[str]:
    """丢掉被同维度内更长词条覆盖的词条。

    「美式复古」与「复古」指同一串文字；若两个都计入，该维度的条数会被虚增，
    而虚增的恰恰是那个更泛的词。只在**同一维度内**做这件事 —— 跨维度不算重复，
    因为「工装裤」同时携带单品与风格两层含义，那是真实信息（见 match_post 文档）。
    """
    return [
        term
        for term in terms
        if not any(
            term.lower() != other.lower() and term.lower() in other.lower()
            for other in terms
        )
    ]


def match_post(
    *,
    note_id: str,
    title: str,
    desc: str,
    tags: Sequence[str],
    vocabulary: Vocabulary,
) -> list[TextSignal]:
    """在标签 + 标题 + 正文里找词条。

    一个帖子在「同一维度 + 同一词条」上只记一次。跨维度可以重复 —— 例如
    「美式复古工装裤」同时是单品（工装裤）与风格（工装／美式复古）的证据，
    这是同一串文字天然携带的两层含义，不是重复计数。
    """
    haystack = "\n".join([title or "", desc or "", *[t for t in tags if t]]).lower()
    if not haystack.strip():
        return []

    signals: list[TextSignal] = []
    for dimension, terms in vocabulary.dimensions.items():
        matched = [term for term in terms if term.lower() in haystack]
        for term in _drop_subsumed(matched):
            signals.append(TextSignal(note_id=note_id, dimension=dimension, term=term))
    return signals


def analyse(posts: Sequence[AnalysisPost], vocabulary: Vocabulary) -> TextAnalysis:
    """匹配并聚合。纯函数、结果确定 —— 报告可随时由存档重算，无需落库。"""
    buckets: dict[tuple[str, str], list[AnalysisPost]] = {}
    matched_note_ids: set[str] = set()

    for post in posts:
        for signal in match_post(
            note_id=post.note_id,
            title=post.title,
            desc=post.desc,
            tags=post.tags,
            vocabulary=vocabulary,
        ):
            buckets.setdefault((signal.dimension, signal.term), []).append(post)
            matched_note_ids.add(post.note_id)

    findings: list[DimensionFinding] = []
    for (dimension, term), group in buckets.items():
        engagement_sum = sum(post.engagement for post in group)
        top = sorted(group, key=lambda p: (-p.engagement, p.note_id))[
            :TOP_POSTS_PER_FINDING
        ]
        findings.append(
            DimensionFinding(
                dimension=dimension,
                term=term,
                post_count=len(group),
                engagement_sum=engagement_sum,
                engagement_mean=engagement_sum / len(group),
                top_note_ids=tuple(post.note_id for post in top),
            )
        )

    # 维度内按总传播贡献降序；条数、词条作为稳定的次级次序。
    findings.sort(key=lambda f: (f.dimension, -f.engagement_sum, -f.post_count, f.term))

    tag_frequencies = _count_raw_tags(posts)

    return TextAnalysis(
        version=vocabulary.version,
        findings=tuple(findings),
        matched_posts=len(matched_note_ids),
        total_posts=len(posts),
        tag_frequencies=tag_frequencies,
        distinct_tags=len(tag_frequencies),
        singleton_tags=sum(1 for item in tag_frequencies if item.post_count == 1),
    )


def _count_raw_tags(posts: Sequence[AnalysisPost]) -> tuple[TagFrequency, ...]:
    """逐字统计原始标签，不做归并 —— 这是词表输出的对照基准。

    帖子内重复的同一标签只计一次（真机上标签里有重复项）。
    """
    buckets: dict[str, list[AnalysisPost]] = {}
    for post in posts:
        for tag in {t.strip() for t in post.tags if t and t.strip()}:
            buckets.setdefault(tag, []).append(post)

    counted = [
        TagFrequency(
            tag=tag,
            post_count=len(group),
            engagement_sum=sum(item.engagement for item in group),
        )
        for tag, group in buckets.items()
    ]
    # 频次优先 —— 这张表要回答的是「哪些标签反复出现」，不是「哪个标签传播最好」。
    counted.sort(key=lambda f: (-f.post_count, -f.engagement_sum, f.tag))
    return tuple(counted)
