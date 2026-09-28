# -*- coding: utf-8 -*-
"""批次可信度判定。

这是质量底线 Q2 的落点：能区分「整批采集不完整」与「个别内容失败」，
后者不否定整批。每条分支都要有断言，因为它直接决定报告头部怎么写。
"""

from trend.runner import assess_confidence


def test_skip_crawl_is_unknown_not_high():
    level, reason = assess_confidence(
        crawl_skipped=True,
        exit_code=0,
        error_line_count=0,
        notes_total=10,
        notes_matched=5,
        notes_new=0,
    )
    assert level == "unknown"
    assert "跳过采集" in reason


def test_nonzero_exit_code_is_low():
    level, reason = assess_confidence(
        crawl_skipped=False,
        exit_code=1,
        error_line_count=0,
        notes_total=10,
        notes_matched=5,
        notes_new=5,
    )
    assert level == "low"
    assert "退出码 1" in reason


def test_empty_archive_is_low():
    level, reason = assess_confidence(
        crawl_skipped=False,
        exit_code=0,
        error_line_count=0,
        notes_total=0,
        notes_matched=0,
        notes_new=0,
    )
    assert level == "low"
    assert "无任何条目" in reason


def test_unmatched_keyword_is_low():
    level, reason = assess_confidence(
        crawl_skipped=False,
        exit_code=0,
        error_line_count=0,
        notes_total=10,
        notes_matched=0,
        notes_new=0,
    )
    assert level == "low"
    assert "未匹配到" in reason


def test_error_lines_over_absolute_threshold_is_low_even_with_a_huge_archive():
    """回归：旧口径用「错误行 / 库内条目数」折算，5000 条的归档会把 100 条错误稀释
    成 2%，让半失败批次被判为「完整」。改成绝对阈值后必须判 low。"""
    level, reason = assess_confidence(
        crawl_skipped=False,
        exit_code=0,
        error_line_count=100,
        notes_total=5000,
        notes_matched=5000,
        notes_new=100,
    )
    assert level == "low"
    assert "超过阈值" in reason


def test_individual_failures_below_threshold_keep_batch_high():
    """质量底线 Q2 的核心：100 条里错 1 条，不得否定整批。"""
    level, reason = assess_confidence(
        crawl_skipped=False,
        exit_code=0,
        error_line_count=1,
        notes_total=100,
        notes_matched=100,
        notes_new=40,
    )
    assert level == "high"
    assert "批次完整" in reason


def test_rerun_with_no_new_notes_is_still_high():
    """重复运行同一关键词时 0 新增是正常的，不是失败信号。"""
    level, reason = assess_confidence(
        crawl_skipped=False,
        exit_code=0,
        error_line_count=0,
        notes_total=50,
        notes_matched=50,
        notes_new=0,
    )
    assert level == "high"
    assert "无新增" in reason


def test_threshold_is_configurable():
    level, _ = assess_confidence(
        crawl_skipped=False,
        exit_code=0,
        error_line_count=5,
        notes_total=10,
        notes_matched=10,
        notes_new=10,
        error_line_threshold=50,
    )
    assert level == "high"
