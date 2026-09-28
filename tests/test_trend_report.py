# -*- coding: utf-8 -*-
"""报告渲染。

报告是这套东西唯一的产出物，也是「脱离模型重建」的终点，所以要断言到位：
尤其是那三条质量底线必须在报告里看得见。
"""

from trend.report import (
    ReportData,
    ReportPost,
    ReportRun,
    ReportTagFrequency,
    ReportTextAnalysis,
    ReportTextFinding,
    render_report,
)


def _run(**overrides) -> ReportRun:
    base = dict(
        run_id="20260928-101010-abcd1234",
        platform="xhs",
        keywords=("穿搭",),
        confidence="high",
        confidence_reason="本次新增 3 条，错误行 0 条，未超阈值，批次完整。",
        crawl_skipped=False,
        exit_code=0,
        notes_total=3,
        notes_new=3,
        notes_updated=0,
        error_line_count=0,
        score_formula_version="v1",
        generated_at="2026-09-28 10:10:10",
    )
    base.update(overrides)
    return ReportRun(**base)


def _post(**overrides) -> ReportPost:
    base = dict(
        rank=1,
        note_id="n1",
        title="通勤穿搭",
        excerpt="",
        nickname="小*",
        note_url="https://example.com/n1",
        source_keyword="穿搭",
        likes=12000,
        collected=3000,
        comments=120,
        shares=45,
        raw_score=21465.0,
        composite_score=9.97,
        tag_list=("通勤穿搭",),
        image_count=2,
    )
    base.update(overrides)
    return ReportPost(**base)


def _data(posts=(_post(),), keyword_counts=None, **overrides) -> ReportData:
    base = dict(
        run=_run(),
        posts=posts,
        total_scored=len(posts),
        keyword_counts=keyword_counts or (("穿搭", len(posts)),),
        top_n=30,
    )
    base.update(overrides)
    return ReportData(**base)


# --------------------------------------------------------------------------- #
# 质量底线 Q1：不臆造 —— 没有图片证据就不出风格结论
# --------------------------------------------------------------------------- #


def test_report_declares_no_style_conclusions_in_this_slice():
    text = render_report(_data())
    assert "风格结论：仍然不产出" in text
    assert "无图片证据不下风格结论" in text
    # 两类证据等级必须被点名，后续切片才能分区展示
    assert "text_only" in text
    assert "image_backed" in text


# --------------------------------------------------------------------------- #
# 质量底线 Q2：可信度可判定
# --------------------------------------------------------------------------- #


def test_low_confidence_warns_the_whole_batch_is_incomplete():
    text = render_report(
        _data(
            run=_run(
                confidence="low",
                confidence_reason="采集子进程退出码 1，整批采集可能不完整。",
            )
        )
    )
    assert "批次可信度：低（low）" in text
    assert "整批采集不完整" in text


def test_individual_failure_does_not_deny_the_batch():
    """核心断言：阈值内的个别失败仍是 high，且报告写明不否定整批。"""
    text = render_report(_data(run=_run(confidence="high", error_line_count=1)))
    assert "批次可信度：高（high）" in text
    assert "不否定整批" in text


def test_skipped_crawl_reports_unknown_confidence():
    text = render_report(
        _data(
            run=_run(
                confidence="unknown",
                confidence_reason="本次跳过采集，未评估本批次完整度；榜单基于既有存档。",
                crawl_skipped=True,
            )
        )
    )
    assert "批次可信度：未知（unknown）" in text
    assert "本次跳过采集" in text
    assert "报告完整度取决于上一次采集批次" in text


# --------------------------------------------------------------------------- #
# 质量底线 Q3：可版本化
# --------------------------------------------------------------------------- #


def test_report_states_score_formula_version():
    text = render_report(_data())
    assert "打分公式版本" in text
    assert "`v1`" in text


# --------------------------------------------------------------------------- #
# 榜单渲染
# --------------------------------------------------------------------------- #


def test_post_row_has_link_counts_and_score():
    text = render_report(_data())
    assert "[通勤穿搭](https://example.com/n1)" in text
    assert "12,000" in text
    assert "9.97" in text
    assert "通勤穿搭" in text


def test_pipe_and_newline_in_title_are_escaped():
    text = render_report(_data(posts=(_post(title="a|b\nc"),)))
    assert "a\\|b c" in text


def test_long_title_is_truncated():
    text = render_report(_data(posts=(_post(title="穿" * 200),)))
    assert "…" in text


def test_empty_posts_render_without_crash():
    text = render_report(_data(posts=()))
    assert "无可排序的帖子" in text


def test_keyword_breakdown_only_shown_for_multiple_keywords():
    single = render_report(_data())
    assert "按来源关键词" not in single

    multi = render_report(_data(keyword_counts=(("穿搭", 2), ("通勤穿搭", 1))))
    assert "按来源关键词" in multi
    assert "通勤穿搭：1" in multi


# --------------------------------------------------------------------------- #
# 不可信文本注入（标题/URL 来自被爬平台）
# --------------------------------------------------------------------------- #


def test_markdown_link_breakout_in_title_is_neutralized():
    """标题里的 `](` 不得撑破链接语法并注入任意 Markdown。"""
    text = render_report(_data(posts=(_post(title="正常](http://evil)"),)))
    assert "](http://evil)" not in text
    assert "\\]" in text


def test_non_http_url_is_not_rendered_as_a_link():
    for bad in ("javascript:alert(1)", "data:text/html,x", "  ", ""):
        text = render_report(_data(posts=(_post(note_url=bad),)))
        assert "javascript:" not in text
        assert "](data:" not in text


def test_parenthesis_in_url_is_percent_encoded():
    text = render_report(_data(posts=(_post(note_url="https://e.com/a)b"),)))
    assert "https://e.com/a%29b" in text


def test_meta_table_keeps_intentional_code_spans():
    """转义不能误伤本模块自己写的反引号代码片段。"""
    text = render_report(_data())
    assert "`v1`" in text
    assert "`20260928-101010-abcd1234`" in text


# --------------------------------------------------------------------------- #
# 文本线索（text_only）：Q1 要求它自证不是风格结论
# --------------------------------------------------------------------------- #


def _text_analysis(**overrides) -> ReportTextAnalysis:
    base = dict(
        version="vocab-abc123-m1",
        findings=(
            ReportTextFinding(
                dimension="风格",
                term="韩系",
                post_count=5,
                engagement_sum=55.3,
                engagement_mean=11.06,
            ),
            ReportTextFinding(
                dimension="单品",
                term="外套",
                post_count=10,
                engagement_sum=108.0,
                engagement_mean=10.8,
            ),
        ),
        matched_posts=18,
        total_posts=20,
    )
    base.update(overrides)
    return ReportTextAnalysis(**base)


def test_text_clues_state_they_are_not_style_conclusions():
    text = render_report(_data(text_analysis=_text_analysis()))
    assert "文本线索（证据等级：text_only）" in text
    assert "不是风格结论" in text
    assert "**不是**「韩系风格正在流行」" in text


def test_text_clues_record_the_vocabulary_version():
    """Q3：报告必须声明用的是哪一版词表。"""
    text = render_report(_data(text_analysis=_text_analysis()))
    assert "词表版本" in text
    assert "vocab-abc123-m1" in text


def test_text_clues_are_grouped_by_dimension_ranked_by_engagement():
    text = render_report(_data(text_analysis=_text_analysis()))
    assert "### 风格" in text
    assert "### 单品" in text
    assert "| 韩系 | 5 | 55.30 | 11.06 |" in text


def test_text_clues_report_coverage_honestly():
    text = render_report(_data(text_analysis=_text_analysis()))
    assert "20 条帖子中 18 条命中词条" in text


def test_no_text_clues_section_when_analysis_is_absent():
    assert "文本线索" not in render_report(_data())


def test_no_text_clues_section_when_nothing_matched():
    text = render_report(
        _data(text_analysis=_text_analysis(findings=(), matched_posts=0))
    )
    assert "文本线索" not in text


# --------------------------------------------------------------------------- #
# 原始标签词频：词表聚类的对照基准
# --------------------------------------------------------------------------- #


def _with_raw_tags(**overrides) -> ReportTextAnalysis:
    base = dict(
        raw_tags=(
            ReportTagFrequency(tag="韩系穿搭", post_count=4, engagement_sum=44.54),
            ReportTagFrequency(tag="每日穿搭", post_count=3, engagement_sum=33.0),
        ),
        distinct_tags=133,
        singleton_tags=123,
    )
    base.update(overrides)
    return _text_analysis(**base)


def test_raw_tag_table_precedes_the_dimension_tables():
    """先给证据、再给归一化结果 —— 顺序反了就失去了可核对性。"""
    text = render_report(_data(text_analysis=_with_raw_tags()))
    assert "原始标签词频（未归一化，供核对）" in text
    assert text.index("原始标签词频") < text.index("### 风格")


def test_raw_tag_table_states_the_singleton_share():
    """这一行是词表存在的理由，必须出现在报告里。"""
    text = render_report(_data(text_analysis=_with_raw_tags()))
    assert "共 133 个不同标签，其中" in text
    assert "123 个只出现 1 次" in text


def test_raw_tag_table_lists_tags_verbatim():
    text = render_report(_data(text_analysis=_with_raw_tags()))
    assert "| 韩系穿搭 | 4 | 44.54 |" in text


def test_raw_tag_table_caps_rows_and_states_the_remainder():
    many = tuple(
        ReportTagFrequency(tag=f"标签{i}", post_count=1, engagement_sum=1.0)
        for i in range(20)
    )
    text = render_report(
        _data(
            text_analysis=_with_raw_tags(
                raw_tags=many, distinct_tags=20, singleton_tags=20
            )
        )
    )
    assert "（仅列前 15 个；其余 5 个频次更低）" in text


def test_raw_tag_table_absent_when_there_are_no_tags():
    text = render_report(
        _data(text_analysis=_text_analysis(findings=(), matched_posts=0))
    )
    assert "原始标签词频" not in text


def test_text_clues_section_appears_for_raw_tags_even_with_no_vocabulary_match():
    """一条词表词条都没命中时，「133 个标签里 123 个只出现一次」本身仍是结论。"""
    text = render_report(
        _data(
            text_analysis=_with_raw_tags(findings=(), matched_posts=0)
        )
    )
    assert "原始标签词频" in text
