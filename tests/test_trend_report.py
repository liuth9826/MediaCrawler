# -*- coding: utf-8 -*-
"""报告渲染。

报告是这套东西唯一的产出物，也是「脱离模型重建」的终点，所以要断言到位：
尤其是那三条质量底线必须在报告里看得见。
"""

from trend.report import ReportData, ReportPost, ReportRun, render_report


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
    assert "风格结论：本切片不产出" in text
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
