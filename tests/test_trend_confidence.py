# -*- coding: utf-8 -*-
"""批次可信度判定。

质量底线 Q2 的落点：必须能区分「整批采集不完整」与「个别内容失败」。

这条规则被真机数据推翻过两次：
- 第一版用「错误行 / 库内条目数」，分母是历史归档，会随存档增长而稀释；
- 第二版换成绝对条数，却仍不是尺度无关的 —— 同一失败率（7.5%）在 120 条批次被判
  high、在 185 条批次被判 low，只因为绝对错误数分别在阈值 10 的两侧。

现版本用「错误行 / 本轮请求目标量」折算，并额外用爬虫的「结果取尽」信号兜住自然
结束的批次。下面的参数化用例就是钉死「尺度无关」这一点的。
"""

import pytest

from trend.runner import assess_confidence


def _assess(**overrides):
    base = dict(
        crawl_skipped=False,
        exit_code=0,
        error_line_count=0,
        notes_total=100,
        notes_matched=100,
        notes_new=50,
        results_exhausted=False,
        requested=100,
    )
    base.update(overrides)
    return assess_confidence(**base)


# --------------------------------------------------------------------------- #
# 硬性判据
# --------------------------------------------------------------------------- #


def test_skip_crawl_is_unknown_not_high():
    level, reason = _assess(crawl_skipped=True, requested=None)
    assert level == "unknown"
    assert "跳过采集" in reason


def test_nonzero_exit_code_is_low():
    level, reason = _assess(exit_code=1)
    assert level == "low"
    assert "退出码 1" in reason


def test_empty_archive_is_low():
    level, reason = _assess(notes_total=0)
    assert level == "low"
    assert "无任何条目" in reason


def test_unmatched_keyword_is_low():
    level, reason = _assess(notes_matched=0)
    assert level == "low"
    assert "未匹配到" in reason


# --------------------------------------------------------------------------- #
# 自然结束的批次：个别失败不否定整批
# --------------------------------------------------------------------------- #


def test_results_exhausted_keeps_batch_high_despite_many_errors():
    """爬虫报「结果取尽」= 批次范围由平台决定，不是被失败截断。
    此时哪怕错误行很多，也只是个别条目失败，不该判整批不完整。"""
    level, reason = _assess(error_line_count=80, requested=200, results_exhausted=True)
    assert level == "high"
    assert "已取尽" in reason
    assert "个别条目失败" in reason


def test_results_exhausted_with_no_new_notes_is_still_high():
    level, _ = _assess(error_line_count=30, notes_new=0, results_exhausted=True)
    assert level == "high"


# --------------------------------------------------------------------------- #
# 核心回归：判据必须尺度无关
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "requested,errors",
    [(120, 9), (185, 14), (2000, 150), (40, 3)],
)
def test_same_failure_rate_gives_the_same_verdict(requested, errors):
    """回归：这四组的失败率都约 7.5%。旧版按绝对条数判定，9 与 14 会分列阈值 10 的
    两侧而得到相反结论。现在必须一致。"""
    level, _ = _assess(error_line_count=errors, requested=requested)
    assert level == "high"


def test_rate_over_threshold_is_low():
    level, reason = _assess(error_line_count=60, requested=200)
    assert level == "low"
    assert "占本轮请求目标 200 条" in reason
    assert "超过阈值" in reason


def test_rate_threshold_is_configurable():
    level, _ = _assess(error_line_count=60, requested=200, error_rate_threshold=0.9)
    assert level == "high"


def test_rerun_with_no_new_notes_is_still_high():
    """重复运行同一关键词时 0 新增是正常的，不是失败信号。"""
    level, reason = _assess(notes_new=0, error_line_count=0)
    assert level == "high"
    assert "无新增" in reason


# --------------------------------------------------------------------------- #
# 推不出目标量时的兜底
# --------------------------------------------------------------------------- #


def test_falls_back_to_absolute_threshold_when_target_unknown():
    level, reason = _assess(requested=None, error_line_count=14)
    assert level == "low"
    assert "推不出本轮目标量" in reason


def test_fallback_threshold_is_configurable():
    level, _ = _assess(requested=None, error_line_count=14, error_lines_fallback=50)
    assert level == "high"


def test_under_fallback_threshold_is_high():
    level, reason = _assess(requested=None, error_line_count=3)
    assert level == "high"
    assert "兜底阈值" in reason
