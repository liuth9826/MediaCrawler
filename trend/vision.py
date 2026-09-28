# -*- coding: utf-8 -*-
"""图片证据的领域类型与聚合，**纯逻辑**：只依赖标准库与 trend.config。

为什么单独一个模块、且不 import 任何分析器/DB/HTTP：报告侧要能在**不接触模型**的
前提下重算并校验风格结论（SDD R6）。聚合放这里、放成纯函数，就使得「改聚合规则后
免模型重建报告」成立 —— 如果把聚合结果预先落库，规则就被冻在数据里了。

本模块承担质量底线 Q1 的**结构性**保证：`image_backed` 的结论必须携带图片引用，
这条不变量由 `validate_finding` 强制，而不是靠写代码的人自觉。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

# 证据等级（SDD Q1）。报告分区与校验器都以它为准。
VISION_EVIDENCE_LEVEL = "image_backed"
TEXT_ONLY_EVIDENCE_LEVEL = "text_only"

# 逐帖读取状态。`ok` 与 `no_style_signal` 都表示「确实看过了」，
# 后者是「看了但没看出」这一合法结果，必须与「失败」区分开 —— 否则
# 「模型读不懂图」会被讲成「这些图没有风格」。
STATUS_OK = "ok"
STATUS_NO_STYLE_SIGNAL = "no_style_signal"
STATUS_NO_IMAGES = "no_images"
STATUS_FAILED = "failed"

# 「读过」的状态集合。
_READ_STATUSES = frozenset({STATUS_OK, STATUS_NO_STYLE_SIGNAL})

# 终态：读完了、不必再读。
#
# `failed` **刻意不在其中**，这是真机数据逼出来的修正：首轮 20 条里有 7 条撞上
# HTTP 429 限流，若把 failed 也算「读过了」，重跑会直接跳过它们 —— 一次限流抖动就被
# 冻结成永久缺失，只能靠换模型/词表产生新版本才能补，而那样会把已成功的十几条
# 全部重读一遍。失败是暂时的，不该被当成结论。
TERMINAL_STATUSES = frozenset({STATUS_OK, STATUS_NO_STYLE_SIGNAL, STATUS_NO_IMAGES})


class EvidenceError(ValueError):
    """``image_backed`` 结论缺少图片引用 —— 违反 SDD Q1。"""


@dataclass(frozen=True)
class ImageRef:
    """送给模型的一张图。`index` 是给模型看的 1-based 编号。"""

    index: int
    url: str
    local_path: str | None = None


@dataclass(frozen=True)
class VisionTerm:
    """模型从某帖某图里读出的一条词条。

    `image_indices` 非空是构造期不变量：没有图片支撑的词条不该被构造出来
    （由 `parse_model_reply` 保证），这样「无证据的风格结论」在源头就不存在。
    """

    dimension: str
    term: str
    image_indices: tuple[int, ...]
    confidence: float
    reason: str


@dataclass(frozen=True)
class VisionRead:
    """模型对**一个帖子**的读取结果，也是落库的粒度。"""

    note_id: str
    status: str
    terms: tuple[VisionTerm, ...] = ()
    evidence: tuple[ImageRef, ...] = ()
    error: str = ""
    raw_excerpt: str = ""


@dataclass(frozen=True)
class VisionEvidenceRef:
    """结论到具体图片的引用 —— 报告里点得开的那条链。"""

    note_id: str
    image_index: int
    image_url: str


@dataclass(frozen=True)
class VisionFinding:
    """跨帖子聚合出的一条风格结论。"""

    dimension: str
    term: str
    post_count: int
    engagement_sum: float
    evidence: tuple[VisionEvidenceRef, ...]
    evidence_level: str = VISION_EVIDENCE_LEVEL


@dataclass(frozen=True)
class VisionAnalysis:
    version: str
    findings: tuple[VisionFinding, ...]
    posts_read: int
    posts_failed: int
    posts_no_image: int
    images_sent: int
    rejected_terms: int


def analysis_version(
    *,
    prompt_template: str,
    schema_version: str,
    vocabulary_version: str,
    model_id: str,
    score_formula_version: str,
) -> str:
    """由内容派生分析版本（SDD Q3）。

    照 `trend/vocab.py` 的做法哈希内容而非人工维护版本号：改提示词、改输出契约、
    改词表、换模型、改打分公式，版本都会自动变，不依赖人的记性。

    **API key 与 base_url 刻意不是输入**：它们是凭据，不是分析语义的一部分。
    换一把 key 不该产生一条「新版本」的结论 —— 那会把同一份分析记成两次。
    """
    canonical = json.dumps(
        {
            "prompt": hashlib.sha256(prompt_template.encode("utf-8")).hexdigest(),
            "schema": schema_version,
            "vocab": vocabulary_version,
            "model": model_id,
            "score": score_formula_version,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
    return f"vision-{digest}"


def validate_finding(finding: VisionFinding) -> None:
    """拒绝没有图片引用的 ``image_backed`` 结论（SDD Q1）。

    这是最后一道闸：聚合侧已经保证不产出这种结论，所以这里触发即意味着有 bug，
    应当变响而不是被静默放过。
    """
    if finding.evidence_level != VISION_EVIDENCE_LEVEL:
        return
    if not finding.evidence:
        raise EvidenceError(
            f"结论 {finding.dimension}/{finding.term} 声称 image_backed 却没有任何图片引用"
        )


def aggregate_vision_reads(
    reads: Sequence[VisionRead],
    *,
    version: str,
    scores: Mapping[str, float],
) -> VisionAnalysis:
    """把逐帖读数聚合成风格结论。纯函数、结果确定。

    `scores` 由调用方传入（调用方本来就已经取过一遍打分数据），避免这里再查一次库、
    也避免把分数冗余进逐帖读数表。

    只有 `ok` 的读数贡献词条；`evidence_json` 被改坏成空的行，其词条的图片编号
    无法解析，会被计入 `rejected_terms` 而不是变成一条没证据的结论。
    """
    # (dimension, term) -> note_id -> 证据引用
    buckets: dict[tuple[str, str], dict[str, list[VisionEvidenceRef]]] = {}
    posts_read = 0
    posts_failed = 0
    posts_no_image = 0
    images_sent = 0
    rejected_terms = 0

    for read in reads:
        images_sent += len(read.evidence)

        if read.status == STATUS_FAILED:
            posts_failed += 1
            continue
        if read.status == STATUS_NO_IMAGES:
            posts_no_image += 1
            continue
        if read.status not in _READ_STATUSES:
            # 未知状态一律当失败 —— 认不出来就不得声称「读过」。
            posts_failed += 1
            continue

        posts_read += 1
        if read.status != STATUS_OK:
            continue

        # index -> ImageRef，用于把模型给的编号解析回真实 URL。
        refs_by_index = {ref.index: ref for ref in read.evidence}
        for term in read.terms:
            resolved = [
                refs_by_index[i] for i in term.image_indices if i in refs_by_index
            ]
            if not resolved:
                # 词条声称的图一张都解析不出来：拒绝，而不是降级成无证据结论。
                rejected_terms += 1
                continue
            by_note = buckets.setdefault((term.dimension, term.term), {})
            collected = by_note.setdefault(read.note_id, [])
            for ref in resolved:
                collected.append(
                    VisionEvidenceRef(
                        note_id=read.note_id,
                        image_index=ref.index,
                        image_url=ref.url,
                    )
                )

    findings: list[VisionFinding] = []
    for (dimension, term_name), by_note in buckets.items():
        findings.append(
            VisionFinding(
                dimension=dimension,
                term=term_name,
                post_count=len(by_note),
                engagement_sum=sum(scores.get(note_id, 0.0) for note_id in by_note),
                evidence=_dedupe_evidence(by_note.values()),
            )
        )

    # 传播贡献优先 —— 这张榜要回答的是「什么在流行」，不是「什么被提得多」。
    findings.sort(key=lambda f: (-f.engagement_sum, -f.post_count, f.dimension, f.term))
    for finding in findings:
        validate_finding(finding)

    return VisionAnalysis(
        version=version,
        findings=tuple(findings),
        posts_read=posts_read,
        posts_failed=posts_failed,
        posts_no_image=posts_no_image,
        images_sent=images_sent,
        rejected_terms=rejected_terms,
    )


def _dedupe_evidence(
    groups: Iterable[list[VisionEvidenceRef]],
) -> tuple[VisionEvidenceRef, ...]:
    """去重并按 (note_id, image_index) 排序 —— 同一张图被同一帖引用多次只算一次。

    排序不只是为了好看：报告头部的「代表图」与测试断言都依赖这里的顺序确定。
    """
    unique: dict[tuple[str, int], VisionEvidenceRef] = {}
    for group in groups:
        for ref in group:
            unique.setdefault((ref.note_id, ref.image_index), ref)
    return tuple(unique[key] for key in sorted(unique))
