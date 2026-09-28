# -*- coding: utf-8 -*-
"""综合传播分。

口径：转播量 = 综合传播/互动表现，以转发为主（SDD §5）。
"""

import math

import pytest

from trend.config import ENGAGEMENT_WEIGHTS, SCORE_FORMULA_VERSION
from trend.scoring import composite_score


def test_share_weight_dominates_a_like_count():
    """以转发为主：等量的转发必须比点赞更值钱。"""
    share_heavy = composite_score({"liked": 0, "collected": 0, "comment": 0, "share": 100})
    like_heavy = composite_score({"liked": 100, "collected": 0, "comment": 0, "share": 0})
    assert share_heavy.raw > like_heavy.raw


def test_formula_matches_documented_v1():
    """v1 公式：5×转发 + 3×收藏 + 2×评论 + 1×点赞，再取 log1p。"""
    result = composite_score({"liked": 10, "collected": 20, "comment": 30, "share": 40})
    assert result.raw == 10 * 1 + 20 * 3 + 30 * 2 + 40 * 5
    assert result.composite == pytest.approx(math.log1p(result.raw))
    assert result.formula_version == SCORE_FORMULA_VERSION
    assert result.breakdown == {"share": 40, "collected": 20, "comment": 30, "liked": 10}


def test_log1p_dampening_keeps_outliers_comparable():
    """压制后，10 倍传播量的帖子得分差远小于 10 倍，避免单条爆款压倒样本。"""
    small = composite_score({"share": 100})
    large = composite_score({"share": 1000})
    assert large.raw == 10 * small.raw
    assert large.composite < 2 * small.composite


def test_dirty_counts_never_negative_and_never_crash():
    result = composite_score(
        {"liked": None, "collected": "x", "comment": -3, "share": None}
    )
    assert result.breakdown["liked"] == 0
    assert result.breakdown["collected"] == 0
    assert result.breakdown["comment"] == 0
    assert result.breakdown["share"] == 0
    assert result.raw == 0


def test_formula_version_is_recorded_so_old_and_new_are_distinguishable():
    """Q3 可版本化：换了公式，结果自带不同版本号，新旧可区分。"""
    result = composite_score({"liked": 1}, formula_version="v2-test")
    assert result.formula_version == "v2-test"
    assert result.formula_version != SCORE_FORMULA_VERSION


def test_default_weights_match_documented_ordering():
    assert ENGAGEMENT_WEIGHTS["share"] > ENGAGEMENT_WEIGHTS["collected"]
    assert ENGAGEMENT_WEIGHTS["collected"] > ENGAGEMENT_WEIGHTS["comment"]
    assert ENGAGEMENT_WEIGHTS["comment"] > ENGAGEMENT_WEIGHTS["liked"]
