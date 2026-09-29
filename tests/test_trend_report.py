# -*- coding: utf-8 -*-
"""报告渲染。

报告是这套东西唯一的产出物，也是「脱离模型重建」的终点，所以要断言到位：
尤其是那三条质量底线必须在报告里看得见。
"""

from trend.config import VISION_JUDGEMENT_DIMENSIONS
from trend.report import (
    ReportData,
    ReportImageRef,
    ReportPost,
    ReportRun,
    ReportStyleAnalysis,
    ReportStyleFinding,
    ReportTagFrequency,
    ReportTextAnalysis,
    ReportTextFinding,
    render_report,
)
from trend.vocab import load_vocabulary


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


def _style_analysis(**overrides) -> ReportStyleAnalysis:
    base = dict(
        analysis_version="vision-abc123",
        model_id="glm-4v",
        prompt_version="prompt-def456-s1",
        vocabulary_version="vocab-ghi789-m1",
        status="succeeded",
        posts_considered=12,
        posts_read=10,
        posts_failed=1,
        posts_no_image=1,
        images_sent=9,
        rejected_terms=2,
        findings=(
            ReportStyleFinding(
                dimension="风格",
                term="韩系",
                post_count=4,
                engagement_sum=12.5,
                evidence=(
                    ReportImageRef(
                        note_id="n1",
                        image_index=1,
                        image_url="https://example.invalid/1.jpg",
                    ),
                ),
            ),
        ),
    )
    base.update(overrides)
    return ReportStyleAnalysis(**base)


# --------------------------------------------------------------------------- #
# 质量底线 Q1：不臆造 —— 没有图片证据就不出风格结论
# --------------------------------------------------------------------------- #


def test_report_declares_style_analysis_not_run_instead_of_claiming_a_result():
    """没跑过图片分析时，报告必须说「未运行」，而不是说「没有风格」。

    把「未执行」讲成「没有」，就是把一个流程状态讲成了一个关于穿搭的结论。
    """
    text = render_report(_data())
    assert "风格结论：本轮未运行图片分析" in text
    assert "无图片证据不下风格结论" in text
    # 两类证据等级必须被点名，读者才知道两种结论不是一回事
    assert "text_only" in text
    assert "image_backed" in text


def test_skipped_for_missing_credentials_is_reported_as_not_run():
    text = render_report(
        _data(
            style_analysis=ReportStyleAnalysis(
                analysis_version="vision-x",
                model_id="",
                prompt_version="prompt-x",
                vocabulary_version="vocab-x",
                status="skipped_no_credentials",
                posts_considered=0,
                posts_read=0,
                posts_failed=0,
                posts_no_image=0,
                images_sent=0,
                rejected_terms=0,
            )
        )
    )

    assert "本轮未运行图片分析" in text
    assert "缺少模型凭据" in text


def test_image_backed_section_lists_findings_with_clickable_evidence():
    text = render_report(_data(style_analysis=_style_analysis()))

    assert "## 风格结论（证据等级：image_backed）" in text
    assert "不可混读" in text
    assert "韩系" in text
    assert "[n1#1](https://example.invalid/1.jpg)" in text
    # 覆盖率要报出来，只报结论不报覆盖是另一种失真
    assert "候选 12 条" in text
    assert "送出图片 9 张" in text


def test_image_backed_section_precedes_the_text_only_section():
    text = render_report(
        _data(text_analysis=_text_analysis(), style_analysis=_style_analysis())
    )

    assert text.index("## 风格结论（证据等级：image_backed）") < text.index(
        "## 文本线索（证据等级：text_only）"
    )


def test_failed_reads_are_declared_as_unjudged_not_absent():
    text = render_report(_data(style_analysis=_style_analysis(posts_failed=3)))

    assert "读取失败 3 条" in text
    assert "未被判定" in text


def test_empty_findings_explain_why_instead_of_showing_a_blank_table():
    text = render_report(
        _data(style_analysis=_style_analysis(findings=(), posts_read=0, posts_failed=4))
    )

    assert "没有产生任何风格结论" in text
    assert "不是「没有风格」，是没读到" in text


def test_unsafe_image_url_is_rendered_as_plain_text_not_a_link():
    """图片 URL 同样来自被爬平台，`javascript:` 之流不得变成可点链接。"""
    text = render_report(
        _data(
            style_analysis=_style_analysis(
                findings=(
                    ReportStyleFinding(
                        dimension="风格",
                        term="韩系",
                        post_count=1,
                        engagement_sum=1.0,
                        evidence=(
                            ReportImageRef(
                                note_id="n1",
                                image_index=1,
                                image_url="javascript:alert(1)",
                            ),
                        ),
                    ),
                )
            )
        )
    )

    assert "javascript:" not in text
    assert "n1#1" in text


def test_hostile_finding_text_is_escaped_in_the_table():
    """维度/词条来自模型，同样是不可信文本。"""
    text = render_report(
        _data(
            style_analysis=_style_analysis(
                findings=(
                    ReportStyleFinding(
                        dimension="风格|注入",
                        term="韩系](http://evil.invalid)",
                        post_count=1,
                        engagement_sum=1.0,
                        evidence=(
                            ReportImageRef(
                                note_id="n1",
                                image_index=1,
                                image_url="https://example.invalid/1.jpg",
                            ),
                        ),
                    ),
                )
            )
        )
    )

    assert "http://evil.invalid)" not in text


def test_findings_are_split_into_judgement_and_background():
    """真机教训：上榜的先是「内搭 10 帖 / 裙子 9 帖」这类背景词。

    它们不是趋势判断 —— 穿搭内容里普遍存在。混在一张表里排前几名，读者会把
    「出现次数多」读成「在流行」。
    """
    text = render_report(
        _data(
            style_analysis=_style_analysis(
                findings=(
                    ReportStyleFinding(
                        dimension="单品",
                        term="内搭",
                        post_count=10,
                        engagement_sum=104.81,
                        evidence=(
                            ReportImageRef(
                                note_id="n2",
                                image_index=1,
                                image_url="https://example.invalid/2.jpg",
                            ),
                        ),
                    ),
                    ReportStyleFinding(
                        dimension="风格",
                        term="学院风",
                        post_count=4,
                        engagement_sum=41.03,
                        evidence=(
                            ReportImageRef(
                                note_id="n1",
                                image_index=1,
                                image_url="https://example.invalid/1.jpg",
                            ),
                        ),
                    ),
                )
            )
        )
    )

    assert "### 趋势判断（风格）" in text
    assert "### 背景描述（单品）" in text
    # 判断必须排在背景之前，否则读者先看到的是灌水那半张表
    assert text.index("### 趋势判断") < text.index("### 背景描述")
    assert "不是趋势判断" in text


def test_background_section_is_omitted_when_there_are_only_judgements():
    text = render_report(_data(style_analysis=_style_analysis()))

    assert "### 趋势判断（风格）" in text
    assert "### 背景描述" not in text


def test_background_only_run_states_that_no_judgement_was_made():
    """只有背景维度命中时必须说清楚 —— 否则读者会把它读成「本次的趋势就是这些」。

    这与「把未运行说成没有」是同一类失真：没有判断，就要说没有判断。
    """
    text = render_report(
        _data(
            style_analysis=_style_analysis(
                findings=(
                    ReportStyleFinding(
                        dimension="单品",
                        term="内搭",
                        post_count=10,
                        engagement_sum=104.81,
                        evidence=(
                            ReportImageRef(
                                note_id="n1",
                                image_index=1,
                                image_url="https://example.invalid/1.jpg",
                            ),
                        ),
                    ),
                )
            )
        )
    )

    assert "没有得出任何风格 / 手法判断" in text
    assert "### 趋势判断" not in text
    assert "### 背景描述（单品）" in text


def test_judgement_heading_is_derived_from_the_data_not_hardcoded():
    """小标题跟着实际出现的维度走 —— 硬编码维度名会在词表改名后撒谎。"""
    text = render_report(
        _data(
            style_analysis=_style_analysis(
                findings=(
                    ReportStyleFinding(
                        dimension="手法",
                        term="叠穿",
                        post_count=14,
                        engagement_sum=147.35,
                        evidence=(
                            ReportImageRef(
                                note_id="n1",
                                image_index=1,
                                image_url="https://example.invalid/1.jpg",
                            ),
                        ),
                    ),
                )
            )
        )
    )

    heading = next(line for line in text.splitlines() if line.startswith("### 趋势判断"))
    assert heading == "### 趋势判断（手法）"


def test_evidence_scope_disclaimer_is_present():
    """没有基线时，「出现次数多」说明不了「在流行」—— 这句话必须在场。"""
    text = render_report(_data(style_analysis=_style_analysis()))

    assert "没有历史基线时" in text
    assert "出现次数多不等于「在流行」" in text


def test_every_judgement_dimension_exists_in_the_shipped_vocabulary():
    """分区策略写在 config、维度名却定义在词表 —— 用测试把两者绑住。

    否则词表把「手法」改名后，config 里的旧名会静默失配：所有结论都被当成背景描述，
    报告里再没有趋势判断，而且不会报错。
    """
    vocab = load_vocabulary()

    missing = [d for d in VISION_JUDGEMENT_DIMENSIONS if d not in vocab.dimensions]

    assert missing == []


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


# --------------------------------------------------------------------------- #
# 口径警示：关键词是查询的一部分，不是中立的抽样框
# --------------------------------------------------------------------------- #


def test_single_keyword_does_not_trigger_the_query_mix_warning():
    text = render_report(_data(text_analysis=_text_analysis()))
    assert "口径警示" not in text


def test_multiple_keywords_warn_that_distributions_reflect_the_query_mix():
    """真机上就是这里出过循环论证：搜了「韩系穿搭」，然后报告「韩系占 30%」。"""
    text = render_report(
        _data(
            keyword_counts=(("穿搭", 60), ("韩系穿搭", 40), ("通勤穿搭", 20)),
            text_analysis=_text_analysis(),
        )
    )
    assert "口径警示" in text
    assert "3 个关键词" in text
    assert "不是发现" in text
    assert "不要合并" in text
    assert "同一个内容池" in text


def test_query_mix_warning_precedes_the_distributions():
    """警示必须在读者读到分布之前出现，否则等于没写。"""
    text = render_report(
        _data(
            keyword_counts=(("穿搭", 60), ("韩系穿搭", 40)),
            text_analysis=_text_analysis(),
        )
    )
    assert text.index("口径警示") < text.index("文本线索")


def test_text_clues_always_state_the_corpus_scope():
    """即便只有一个关键词，那里也只是「搜索返回了什么」，不是平台整体分布。"""
    text = render_report(_data(text_analysis=_text_analysis()))
    assert "不等于平台整体分布" in text
