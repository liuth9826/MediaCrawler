# -*- coding: utf-8 -*-
"""图片证据的聚合与版本化。

这一层是 SDD 质量底线 Q1 的结构性落点：结论可以**只看图**得出，但**不能不附图**。
所以测试的重点不是「聚合算得对不对」这么简单，而是「没有图片引用的结论还能不能
混出去」—— 答案必须是不能，且要由数据结构强制、而不是靠调用方自觉。
"""

import pytest

from trend.vision import (
    STATUS_FAILED,
    STATUS_NO_IMAGES,
    STATUS_NO_STYLE_SIGNAL,
    STATUS_OK,
    EvidenceError,
    ImageRef,
    VisionEvidenceRef,
    VisionFinding,
    VisionRead,
    VisionTerm,
    aggregate_vision_reads,
    analysis_version,
    validate_finding,
)


def _term(term="韩系", dimension="风格", indices=(1,), confidence=0.8):
    return VisionTerm(
        dimension=dimension,
        term=term,
        image_indices=tuple(indices),
        confidence=confidence,
        reason="图中为韩系通勤搭配",
    )


def _read(note_id, status=STATUS_OK, terms=(), indices=(1,)):
    return VisionRead(
        note_id=note_id,
        status=status,
        terms=tuple(terms),
        evidence=tuple(
            ImageRef(index=i, url=f"https://example.invalid/{note_id}/{i}.jpg")
            for i in indices
        ),
    )


# --------------------------------------------------------------------------- #
# 版本化（Q3）
# --------------------------------------------------------------------------- #


def test_version_is_derived_from_content_and_stable():
    base = dict(
        prompt_template="看这些图，返回 JSON",
        schema_version="s1",
        vocabulary_version="vocab-abc-m1",
        model_id="glm-4v",
        score_formula_version="v1",
    )
    assert analysis_version(**base) == analysis_version(**base)


@pytest.mark.parametrize(
    "field,changed",
    [
        ("prompt_template", "换了个提示词"),
        ("schema_version", "s2"),
        ("vocabulary_version", "vocab-def-m1"),
        ("model_id", "qwen-vl-max"),
        ("score_formula_version", "v2"),
    ],
)
def test_version_changes_when_any_semantic_input_changes(field, changed):
    """五项语义输入任一变化都必须产生新版本，否则新旧结果无法区分。"""
    base = dict(
        prompt_template="看这些图，返回 JSON",
        schema_version="s1",
        vocabulary_version="vocab-abc-m1",
        model_id="glm-4v",
        score_formula_version="v1",
    )
    changed_inputs = {**base, field: changed}
    assert analysis_version(**base) != analysis_version(**changed_inputs)


def test_version_excludes_credentials(monkeypatch):
    """换 key 或换网关不该被记成「新版本」—— 凭据不是分析语义的一部分。

    这条要钉死：一旦有人把 env 塞进哈希，同一份分析会被记成两次，Q3 的
    「新旧可区分」就退化成「每次跑都是新的」。
    """
    inputs = dict(
        prompt_template="看这些图，返回 JSON",
        schema_version="s1",
        vocabulary_version="vocab-abc-m1",
        model_id="glm-4v",
        score_formula_version="v1",
    )
    before = analysis_version(**inputs)
    monkeypatch.setenv("TREND_LLM_API_KEY", "sk-first-key")
    monkeypatch.setenv("TREND_LLM_BASE_URL", "https://first.invalid/v1")
    after_first = analysis_version(**inputs)
    monkeypatch.setenv("TREND_LLM_API_KEY", "sk-second-key")
    monkeypatch.setenv("TREND_LLM_BASE_URL", "https://second.invalid/v1")

    assert before == after_first == analysis_version(**inputs)
    assert "sk-first-key" not in before


# --------------------------------------------------------------------------- #
# Q1：没有图片引用就不能成为结论
# --------------------------------------------------------------------------- #


def test_validate_rejects_image_backed_finding_without_evidence():
    finding = VisionFinding(
        dimension="风格", term="韩系", post_count=3, engagement_sum=1.0, evidence=()
    )
    with pytest.raises(EvidenceError):
        validate_finding(finding)


def test_validate_accepts_finding_with_evidence():
    finding = VisionFinding(
        dimension="风格",
        term="韩系",
        post_count=1,
        engagement_sum=1.0,
        evidence=(
            VisionEvidenceRef(
                note_id="n1", image_index=1, image_url="https://example.invalid/1"
            ),
        ),
    )
    validate_finding(finding)


def test_term_whose_image_indices_cannot_be_resolved_is_rejected_not_downgraded():
    """词条声称的编号在送出的图里找不到 → 拒绝并计数，绝不降级成「无证据的结论」。

    这是「模型编了一个它没看过的图号」的防线。
    """
    read = _read("n1", terms=[_term(indices=(7,))], indices=(1, 2))

    analysis = aggregate_vision_reads([read], version="v", scores={})

    assert analysis.findings == ()
    assert analysis.rejected_terms == 1


def test_read_with_terms_but_empty_evidence_yields_no_claim():
    """evidence 被改坏成空（报告重建路径上的真实风险）时，不得产出无证据结论。"""
    read = VisionRead(
        note_id="n1",
        status=STATUS_OK,
        terms=(_term(indices=(1,)),),
        evidence=(),
    )

    analysis = aggregate_vision_reads([read], version="v", scores={})

    assert analysis.findings == ()
    assert analysis.rejected_terms == 1


# --------------------------------------------------------------------------- #
# 聚合
# --------------------------------------------------------------------------- #


def test_only_ok_reads_contribute_terms():
    """「没看出风格」「读失败」「没图」都不贡献词条，但要各自计数。"""
    reads = [
        _read("n1", status=STATUS_OK, terms=[_term()]),
        _read("n2", status=STATUS_NO_STYLE_SIGNAL, terms=[_term()]),
        _read("n3", status=STATUS_FAILED, terms=[_term()]),
        _read("n4", status=STATUS_NO_IMAGES, terms=[_term()], indices=()),
    ]

    analysis = aggregate_vision_reads(reads, version="v", scores={})

    assert len(analysis.findings) == 1
    assert analysis.findings[0].post_count == 1
    assert analysis.posts_read == 2  # ok + no_style_signal
    assert analysis.posts_failed == 1
    assert analysis.posts_no_image == 1


def test_unknown_status_is_treated_as_failure_not_as_read():
    """认不出来的状态不得被当成「读过」—— 否则统计口径会被脏数据撑大。"""
    reads = [VisionRead(note_id="n1", status="something_new")]

    analysis = aggregate_vision_reads(reads, version="v", scores={})

    assert analysis.posts_read == 0
    assert analysis.posts_failed == 1


def test_unknown_status_read_does_not_contribute_terms():
    reads = [
        VisionRead(
            note_id="n1",
            status="something_new",
            terms=(_term(),),
            evidence=(ImageRef(index=1, url="https://example.invalid/1"),),
        )
    ]

    analysis = aggregate_vision_reads(reads, version="v", scores={})

    assert analysis.findings == ()


def test_engagement_uses_supplied_scores_and_defaults_to_zero():
    """分数由调用方传入，缺分记为 0 —— 结论仍成立，只是贡献算不出来。"""
    reads = [
        _read("n1", terms=[_term()]),
        _read("n2", terms=[_term()]),
    ]

    analysis = aggregate_vision_reads(reads, version="v", scores={"n1": 3.5, "n2": 1.5})
    assert analysis.findings[0].engagement_sum == pytest.approx(5.0)

    missing = aggregate_vision_reads(reads, version="v", scores={})
    assert missing.findings[0].engagement_sum == 0.0
    assert missing.findings[0].post_count == 2


def test_same_post_and_term_counted_once_but_evidence_union_kept():
    """同一帖同一词条只计一次帖子数，但两张图的证据都要留下。"""
    read = _read("n1", terms=[_term(indices=(1, 2))], indices=(1, 2))

    analysis = aggregate_vision_reads([read], version="v", scores={})

    assert analysis.findings[0].post_count == 1
    assert [ref.image_index for ref in analysis.findings[0].evidence] == [1, 2]


def test_evidence_is_deduped_and_sorted():
    reads = [
        _read("n2", terms=[_term(indices=(1,))], indices=(1,)),
        _read("n1", terms=[_term(indices=(1,))], indices=(1,)),
        _read("n1", terms=[_term(indices=(1,))], indices=(1,)),
    ]

    analysis = aggregate_vision_reads(reads, version="v", scores={})

    evidence = analysis.findings[0].evidence
    assert [(ref.note_id, ref.image_index) for ref in evidence] == [("n1", 1), ("n2", 1)]


def test_findings_ranked_by_engagement_then_count_then_term():
    reads = [
        _read("n1", terms=[_term(term="辣妹")]),
        _read("n2", terms=[_term(term="辣妹")]),
        _read("n3", terms=[_term(term="韩系")]),
    ]

    analysis = aggregate_vision_reads(
        reads, version="v", scores={"n1": 1.0, "n2": 1.0, "n3": 1.0}
    )

    # 辣妹 2 条 2.0 分，韩系 1 条 1.0 分。
    assert [f.term for f in analysis.findings] == ["辣妹", "韩系"]


def test_aggregation_is_deterministic_regardless_of_input_order():
    reads = [
        _read("n1", terms=[_term(term="韩系")], indices=(1,)),
        _read("n2", terms=[_term(term="辣妹")], indices=(1,)),
    ]

    forward = aggregate_vision_reads(reads, version="v", scores={"n1": 1.0, "n2": 1.0})
    backward = aggregate_vision_reads(
        list(reversed(reads)), version="v", scores={"n1": 1.0, "n2": 1.0}
    )

    assert forward == backward


def test_images_sent_counts_every_attached_image_including_failed_reads():
    """图确实送出去了就得算 —— 失败可能发生在收到回复之后。"""
    reads = [
        _read("n1", status=STATUS_OK, terms=[_term()], indices=(1, 2)),
        _read("n2", status=STATUS_FAILED, indices=(1,)),
    ]

    analysis = aggregate_vision_reads(reads, version="v", scores={})

    assert analysis.images_sent == 3


def test_empty_input_yields_empty_analysis():
    analysis = aggregate_vision_reads([], version="vision-x", scores={})

    assert analysis.findings == ()
    assert analysis.version == "vision-x"
    assert analysis.posts_read == 0
