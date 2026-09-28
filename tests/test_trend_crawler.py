# -*- coding: utf-8 -*-
"""采集命令拼装与子进程结果判定。

重点是命令必须与手工执行等价 —— 尤其 --headless 是取值选项而非裸开关，
拼错的后果是子进程直接报错退出，整批采集失败。
"""

import sys

import config as app_config

from trend.crawler import (
    SPAWN_FAILURE_EXIT_CODE,
    TIMEOUT_EXIT_CODE,
    CrawlOptions,
    build_command,
    count_error_lines,
    requested_volume,
    run_crawl,
)


def _value_of(command: list[str], flag: str) -> str:
    return command[command.index(flag) + 1]


def test_build_command_matches_documented_cli_surface():
    command = build_command(
        "xhs", ("穿搭", "通勤穿搭"), options=CrawlOptions(headless=True)
    )
    assert command[0] == sys.executable
    assert command[1].endswith("main.py")
    assert _value_of(command, "--platform") == "xhs"
    assert _value_of(command, "--type") == "search"
    assert _value_of(command, "--keywords") == "穿搭,通勤穿搭"
    assert _value_of(command, "--save_data_option") == "sqlite"


def test_headless_is_a_value_option_not_a_bare_flag():
    on = build_command("xhs", ("穿搭",), options=CrawlOptions(headless=True))
    off = build_command("xhs", ("穿搭",))
    assert _value_of(on, "--headless") == "true"
    assert _value_of(off, "--headless") == "false"


def test_count_error_lines_picks_up_chinese_and_english_markers():
    count, sample = count_error_lines("ok\nTraceback...\n失败: x\nfine\nERROR: boom")
    assert count == 3
    assert "Traceback" in sample
    assert "boom" in sample


def test_count_error_lines_on_clean_output():
    count, sample = count_error_lines("everything is fine")
    assert count == 0
    assert sample == ""


def test_run_crawl_with_injected_executor_avoids_any_subprocess():
    seen = {}

    def _executor(command, timeout=None):
        seen["command"] = command
        return 0, "done", False

    outcome = run_crawl("xhs", ("穿搭",), executor=_executor)
    assert outcome.exit_code == 0
    assert outcome.error_line_count == 0
    assert not outcome.timed_out
    assert _value_of(seen["command"], "--keywords") == "穿搭"


def test_run_crawl_surfaces_nonzero_exit_code():
    def _executor(command, timeout=None):
        return 2, "ERROR: boom", False

    outcome = run_crawl("xhs", ("穿搭",), executor=_executor)
    assert outcome.exit_code == 2
    assert outcome.error_line_count == 1


def test_run_crawl_passes_save_data_option_through_to_the_subprocess():
    seen = {}

    def _executor(command, timeout=None):
        seen["command"] = command
        return 0, "", False

    run_crawl(
        "xhs",
        ("穿搭",),
        options=CrawlOptions(save_data_option="postgres"),
        executor=_executor,
    )
    assert _value_of(seen["command"], "--save_data_option") == "postgres"


# --------------------------------------------------------------------------- #
# 透传参数：只显式指定时才传，其余交给目标程序自己的默认值
# --------------------------------------------------------------------------- #


def test_optional_crawl_flags_are_omitted_by_default():
    """不指定就不传 —— 否则会用趋势层的猜测覆盖用户已有的配置。"""
    command = build_command("xhs", ("穿搭",))
    for flag in ("--start", "--crawler_max_notes_count", "--lt", "--cookies"):
        assert flag not in command


def test_start_and_max_notes_are_passed_through():
    command = build_command(
        "xhs", ("穿搭",), options=CrawlOptions(start_page=3, max_notes=120)
    )
    assert _value_of(command, "--start") == "3"
    assert _value_of(command, "--crawler_max_notes_count") == "120"


def test_login_type_and_cookies_are_passed_through():
    command = build_command(
        "xhs",
        ("穿搭",),
        options=CrawlOptions(login_type="cookie", cookies="a1=xyz; web_session=abc"),
    )
    assert _value_of(command, "--lt") == "cookie"
    assert _value_of(command, "--cookies") == "a1=xyz; web_session=abc"


def test_start_page_zero_is_still_forwarded():
    """0 是合法页码，不能被当成「未指定」而丢掉。"""
    command = build_command("xhs", ("穿搭",), options=CrawlOptions(start_page=0))
    assert _value_of(command, "--start") == "0"


def test_empty_login_type_and_cookies_are_not_forwarded():
    command = build_command(
        "xhs", ("穿搭",), options=CrawlOptions(login_type="", cookies="")
    )
    assert "--lt" not in command
    assert "--cookies" not in command


# --------------------------------------------------------------------------- #
# 本轮请求目标量，以及「结果取尽」信号
# --------------------------------------------------------------------------- #


def test_requested_volume_is_per_keyword():
    assert requested_volume(("穿搭",), CrawlOptions(max_notes=200)) == 200
    assert requested_volume(("a", "b", "c"), CrawlOptions(max_notes=40)) == 120


def test_requested_volume_floors_to_one_page():
    """低于每页 20 条的上限会被爬虫抬到 20（小红书每页固定 20 条）。"""
    assert requested_volume(("穿搭",), CrawlOptions(max_notes=5)) == 20


def test_requested_volume_is_none_when_target_unreadable(monkeypatch):
    monkeypatch.setattr(app_config, "CRAWLER_MAX_NOTES_COUNT", 0)
    assert requested_volume(("穿搭",), CrawlOptions()) is None


def test_run_crawl_detects_the_results_exhausted_sentinel():
    def _executor(command, timeout=None):
        return 0, "INFO - [XiaoHongShuCrawler.search] No more content!\n", False

    assert run_crawl("xhs", ("穿搭",), executor=_executor).results_exhausted is True


def test_results_exhausted_does_not_false_positive_on_crawled_content():
    """连日志前缀一起匹配，避免被爬内容里的同名英文文字命中。"""

    def _executor(command, timeout=None):
        return 0, 'title: "no more content please, keep going"', False

    assert run_crawl("xhs", ("穿搭",), executor=_executor).results_exhausted is False


def test_run_crawl_reports_timeout_without_raising():
    def _executor(command, timeout=None):
        return 0, "partial", True

    outcome = run_crawl("xhs", ("穿搭",), timeout=1, executor=_executor)
    assert outcome.timed_out is True
    assert outcome.exit_code == TIMEOUT_EXIT_CODE
    assert "超时" in outcome.error_sample


def test_run_crawl_turns_spawn_failure_into_an_outcome():
    """回归：子进程起不来（OSError）过去会直接抛出，结果是本轮没有报告、
    库里留下孤儿 running 行。必须转成结果对象。"""

    def _executor(command, timeout=None):
        raise FileNotFoundError("no such file: main.py")

    outcome = run_crawl("xhs", ("穿搭",), executor=_executor)
    assert outcome.spawn_failed is True
    assert outcome.exit_code == SPAWN_FAILURE_EXIT_CODE
    assert "无法启动" in outcome.error_sample


# --------------------------------------------------------------------------- #
# 默认执行器：必须同时做到「实时转发输出」与「统计错误行」
# --------------------------------------------------------------------------- #


def test_streaming_executor_forwards_output_and_still_counts_errors(capsys):
    """回归：这两件事以前是二选一 —— 用 capture_output 拿到错误行统计，代价是
    用户全程看不见输出（扫码、翻页都失明）。现在必须两者兼得。"""
    from trend.crawler import _streaming_executor

    exit_code, output, timed_out = _streaming_executor(
        [sys.executable, "-c", "print('hello'); print('ERROR: boom')"], None
    )

    assert exit_code == 0
    assert timed_out is False
    assert "ERROR: boom" in output  # 供 count_error_lines 统计
    captured = capsys.readouterr().out
    assert "hello" in captured  # 用户看得见
    assert "ERROR: boom" in captured


def test_streaming_executor_kills_the_child_on_timeout():
    from trend.crawler import _streaming_executor

    exit_code, _, timed_out = _streaming_executor(
        [sys.executable, "-c", "import time; time.sleep(30)"], 1
    )

    assert timed_out is True
    assert exit_code != 0
