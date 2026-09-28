# -*- coding: utf-8 -*-
"""规则/词表分析。

要害两条：
1. 输出只能是 ``text_only`` 的文本线索，不得冒称风格结论（质量底线 Q1）
2. 聚合必须确定 —— 否则报告无法由存档稳定重建（R6）
"""

import pytest

from trend.analysis import AnalysisPost, analyse, match_post
from trend.vocab import Vocabulary


@pytest.fixture
def vocab() -> Vocabulary:
    return Vocabulary(
        dimensions={
            "风格": ("韩系", "辣妹", "工装"),
            "单品": ("外套", "工装裤"),
            "季节": ("秋冬",),
        },
        version="vocab-test-m1",
    )


def _post(note_id="n1", title="", desc="", tags=(), engagement=10.0) -> AnalysisPost:
    return AnalysisPost(
        note_id=note_id,
        title=title,
        desc=desc,
        tags=tuple(tags),
        engagement=engagement,
    )


# --------------------------------------------------------------------------- #
# 匹配
# --------------------------------------------------------------------------- #


def test_matches_across_tags_title_and_desc(vocab):
    signals = match_post(
        note_id="n", title="秋冬穿搭", desc="", tags=("韩系穿搭",), vocabulary=vocab
    )
    assert [s.term for s in signals] == ["韩系", "秋冬"]


def test_matching_is_case_insensitive_for_latin_terms():
    lower = Vocabulary(dimensions={"风格": ("lolita",)}, version="v")
    signals = match_post(
        note_id="n", title="Lolita洋装", desc="", tags=(), vocabulary=lower
    )
    assert [s.term for s in signals] == ["lolita"]


def test_one_phrase_can_be_evidence_in_two_dimensions(vocab):
    """「工装裤」既是单品，也含风格词「工装」—— 同一串文字天然的两层含义，
    不是重复计数。"""
    by_dimension = {
        s.dimension: s.term
        for s in match_post(
            note_id="n", title="", desc="", tags=("工装裤",), vocabulary=vocab
        )
    }
    assert by_dimension == {"风格": "工装", "单品": "工装裤"}


def test_a_post_is_recorded_once_per_term(vocab):
    """同一词条在标签与标题里各出现一次，也只记一次。"""
    signals = match_post(
        note_id="n", title="外套", desc="外套", tags=("外套",), vocabulary=vocab
    )
    assert [s.term for s in signals] == ["外套"]


def test_longer_term_subsumes_shorter_within_a_dimension():
    """回归：真机上「美式复古工装裤」让「美式复古」与「复古」同时命中，
    「复古」的条数被虚增。同一维度内应只保留更具体的那个。"""
    overlapping = Vocabulary(dimensions={"风格": ("美式复古", "复古")}, version="v")
    signals = match_post(
        note_id="n", title="美式复古工装裤", desc="", tags=(), vocabulary=overlapping
    )
    assert [s.term for s in signals] == ["美式复古"]


def test_shorter_term_still_matches_when_the_longer_one_absent():
    overlapping = Vocabulary(dimensions={"风格": ("美式复古", "复古")}, version="v")
    signals = match_post(
        note_id="n", title="复古穿搭", desc="", tags=(), vocabulary=overlapping
    )
    assert [s.term for s in signals] == ["复古"]


def test_subsumption_does_not_over_reach():
    """去重只能吃掉真子串。「秋冬」与「冬季」互不包含，两者都应留下 ——
    否则会把季节维度砍得只剩一个词。"""
    seasonal = Vocabulary(dimensions={"季节": ("秋冬", "冬季")}, version="v")
    signals = match_post(
        note_id="n", title="秋冬外套 冬季内搭", desc="", tags=(), vocabulary=seasonal
    )
    assert sorted(s.term for s in signals) == ["冬季", "秋冬"]


def test_no_text_yields_no_signals(vocab):
    assert match_post(note_id="n", title="", desc="", tags=(), vocabulary=vocab) == []
    assert match_post(note_id="n", title="   ", desc="", tags=(), vocabulary=vocab) == []


# --------------------------------------------------------------------------- #
# 聚合
# --------------------------------------------------------------------------- #


def test_aggregation_counts_posts_and_sums_engagement(vocab):
    posts = [
        _post("a", tags=("韩系穿搭",), engagement=10.0),
        _post("b", tags=("韩系穿搭", "外套"), engagement=12.0),
        _post("c", title="辣妹穿搭", engagement=11.0),
    ]
    by_term = {(f.dimension, f.term): f for f in analyse(posts, vocab).findings}

    assert by_term[("风格", "韩系")].post_count == 2
    assert by_term[("风格", "韩系")].engagement_sum == pytest.approx(22.0)
    assert by_term[("风格", "韩系")].engagement_mean == pytest.approx(11.0)
    assert by_term[("单品", "外套")].post_count == 1


def test_findings_are_ordered_by_engagement_within_a_dimension(vocab):
    posts = [
        _post("a", tags=("韩系穿搭",), engagement=20.0),
        _post("b", tags=("辣妹穿搭",), engagement=5.0),
    ]
    style = analyse(posts, vocab).by_dimension()["风格"]
    assert [f.term for f in style] == ["韩系", "辣妹"]


def test_top_note_ids_are_the_highest_engagement_first(vocab):
    posts = [
        _post("low", tags=("外套",), engagement=1.0),
        _post("high", tags=("外套",), engagement=99.0),
    ]
    (finding,) = analyse(posts, vocab).findings
    assert finding.top_note_ids[0] == "high"


def test_unmatched_posts_are_counted_not_hidden(vocab):
    """覆盖度必须如实反映 —— 未命中的帖子不能被悄悄忽略。"""
    result = analyse([_post("a", tags=("韩系穿搭",)), _post("b", title="完全无关")], vocab)
    assert result.matched_posts == 1
    assert result.total_posts == 2


def test_analysis_is_deterministic(vocab):
    """同输入同输出，否则报告无法由存档稳定重建（R6）。"""
    posts = [
        _post("b", tags=("外套",), engagement=9.0),
        _post("a", tags=("外套",), engagement=9.0),
    ]
    assert analyse(posts, vocab) == analyse(posts, vocab)


def test_analysis_records_the_vocabulary_version(vocab):
    assert analyse([_post("a", tags=("外套",))], vocab).version == "vocab-test-m1"


def test_empty_input_is_handled(vocab):
    result = analyse([], vocab)
    assert result.findings == ()
    assert result.matched_posts == 0
    assert result.total_posts == 0
    assert result.by_dimension() == {}
    assert result.tag_frequencies == ()


# --------------------------------------------------------------------------- #
# 原始标签词频：词表聚类的对照基准
# --------------------------------------------------------------------------- #


def test_raw_tag_frequencies_are_counted_verbatim_not_merged(vocab):
    """必须逐字统计。若也做归并，就没有东西可以核对词表归得对不对了。"""
    posts = [
        _post("a", tags=("韩系穿搭", "外套")),
        _post("b", tags=("韩系穿搭",)),
        _post("c", tags=("松弛感穿搭",)),
    ]
    result = analyse(posts, vocab)
    counted = {item.tag: item.post_count for item in result.tag_frequencies}

    assert counted["韩系穿搭"] == 2
    assert counted["外套"] == 1
    assert counted["松弛感穿搭"] == 1  # 未被归并掉
    assert result.distinct_tags == 3
    assert result.singleton_tags == 2


def test_raw_tags_include_ones_the_vocabulary_ignores(vocab):
    """`fyp` 这类噪声标签词表不认，但原始词频必须照实列出。"""
    result = analyse([_post("a", tags=("fyp",))], vocab)
    assert [item.tag for item in result.tag_frequencies] == ["fyp"]
    assert result.findings == ()


def test_raw_tag_frequency_ignores_blanks_and_duplicates_within_a_post(vocab):
    result = analyse([_post("a", tags=("外套", "外套", "", "   "))], vocab)
    assert [(i.tag, i.post_count) for i in result.tag_frequencies] == [("外套", 1)]


def test_raw_tags_sorted_by_frequency_first(vocab):
    posts = [_post("a", tags=("少",)), _post("b", tags=("多",)), _post("c", tags=("多",))]
    assert [i.tag for i in analyse(posts, vocab).tag_frequencies] == ["多", "少"]


def test_posts_without_tags_contribute_no_raw_frequencies(vocab):
    result = analyse([_post("a", title="无标签", tags=())], vocab)
    assert result.tag_frequencies == ()
    assert result.distinct_tags == 0
    assert result.singleton_tags == 0


def test_top_tags_caps_the_list_without_mutating_the_data(vocab):
    posts = [_post(str(i), tags=(f"标签{i}",)) for i in range(5)]
    result = analyse(posts, vocab)
    assert len(result.top_tags(2)) == 2
    assert len(result.tag_frequencies) == 5
