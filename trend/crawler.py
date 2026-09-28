# -*- coding: utf-8 -*-
"""以子进程方式复用既有爬虫。

不 import 爬虫对象：crawler.start() 必定拉起浏览器并走登录流程，放进本进程会让
分析流程与浏览器生命周期耦合，且无法在无登录态下回归。子进程隔离后，即使采集
失败或超时，已入库的存档仍然完整（SDD R6）。
"""

from __future__ import annotations

import subprocess
import sys
import threading
from dataclasses import dataclass
from typing import Callable, Sequence

from trend.config import (
    CRAWL_TIMEOUT_SECONDS,
    DEFAULT_SAVE_DATA_OPTION,
    PROJECT_ROOT,
)

ERROR_MARKERS = ("error", "traceback", "exception", "失败", "错误", "超时", "timeout")

ERROR_SAMPLE_MAX_LINES = 20
ERROR_SAMPLE_LEN = 2000

# 采集超时的约定退出码，与 shell 的 124 保持一致。
TIMEOUT_EXIT_CODE = 124
# 子进程根本起不来（解释器/脚本路径不对、无执行权限等）的约定退出码。
SPAWN_FAILURE_EXIT_CODE = 127


@dataclass(frozen=True)
class CrawlOutcome:
    exit_code: int
    error_line_count: int
    error_sample: str
    skipped: bool = False
    timed_out: bool = False
    spawn_failed: bool = False


@dataclass(frozen=True)
class CrawlOptions:
    """透传给采集子进程的可选项。

    每一项为 None / 空 时**不追加**对应参数，让目标程序用它自己的配置默认值 ——
    这样趋势层不会悄悄覆盖用户已有的设置，只在显式指定时才插手。

    之所以收成一个对象：这些选项要穿过 cli → runner → crawler 三层，逐个当 kwarg 传
    会把每层签名都撑开；收成对象后每层只需一个参数，且「哪些会透传」一目了然。
    """

    headless: bool = False
    save_data_option: str = DEFAULT_SAVE_DATA_OPTION
    start_page: int | None = None
    max_notes: int | None = None
    login_type: str | None = None
    cookies: str | None = None

    def to_argv(self) -> list[str]:
        argv = [
            "--save_data_option",
            self.save_data_option,
            # --headless 是取值选项（yes/no），不是裸开关。
            "--headless",
            "true" if self.headless else "false",
        ]
        if self.start_page is not None:
            argv += ["--start", str(self.start_page)]
        if self.max_notes is not None:
            argv += ["--crawler_max_notes_count", str(self.max_notes)]
        if self.login_type:
            argv += ["--lt", self.login_type]
        if self.cookies:
            argv += ["--cookies", self.cookies]
        return argv


def build_command(
    platform: str,
    keywords: Sequence[str],
    *,
    options: CrawlOptions | None = None,
) -> list[str]:
    """拼出等价于手工执行的采集命令。"""
    active = options if options is not None else CrawlOptions()
    return [
        sys.executable,
        str(PROJECT_ROOT / "main.py"),
        "--platform",
        platform,
        "--type",
        "search",
        "--keywords",
        ",".join(keywords),
        *active.to_argv(),
    ]


def count_error_lines(output: str) -> tuple[int, str]:
    """数出输出里的错误行，并留一段样本。"""
    count = 0
    samples: list[str] = []
    for line in output.splitlines():
        lowered = line.lower()
        if any(marker in lowered for marker in ERROR_MARKERS):
            count += 1
            if len(samples) < ERROR_SAMPLE_MAX_LINES:
                samples.append(line.strip())
    return count, "\n".join(samples)[:ERROR_SAMPLE_LEN]


# 执行器契约：跑完命令，返回 (退出码, 合并后的全部输出, 是否超时)。
# 抽成这个形状、而不是直接暴露 subprocess.run 的那套参数，是为了让测试用一个返回
# 三元组的桩就能替换掉执行过程，不必去模仿 subprocess.run 的重载签名。
Executor = Callable[[list[str], "float | None"], tuple[int, str, bool]]


def _streaming_executor(
    command: list[str], timeout: float | None
) -> tuple[int, str, bool]:
    """默认执行器：实时转发子进程输出，同时留一份用于统计错误行。

    必须实时转发。采集链路里包含「扫码登录」「逐条取详情」这类需要用户看着屏幕
    配合的步骤；把输出整段吞进管道再一次性打印，等于让用户全程失明（这正是之前
    排查卡点时无从下手的原因）。而错误行统计又不能不做 —— 它是批次可信度判定的
    唯一信号。所以一边 print 一边累积，两个都要。
    """
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        errors="replace",
    )
    collected: list[str] = []
    timed_out = False

    def _kill_on_timeout() -> None:
        nonlocal timed_out
        timed_out = True
        process.kill()

    timer = threading.Timer(timeout, _kill_on_timeout) if timeout else None
    if timer is not None:
        timer.start()

    try:
        if process.stdout is not None:
            for line in process.stdout:
                print(line, end="", flush=True)
                collected.append(line)
        process.wait()
    finally:
        if timer is not None:
            timer.cancel()

    return process.returncode, "".join(collected), timed_out


def run_crawl(
    platform: str,
    keywords: Sequence[str],
    *,
    options: CrawlOptions | None = None,
    timeout: float | None = CRAWL_TIMEOUT_SECONDS,
    executor: Executor | None = None,
) -> CrawlOutcome:
    """跑一次采集。

    `executor` 可注入以便测试 —— 默认实时转发输出地跑子进程，测试里换成桩即可在
    无浏览器、无登录态下回归整条链路。
    """
    command = build_command(platform, keywords, options=options)
    run_command = _streaming_executor if executor is None else executor
    effective_timeout = timeout or None

    try:
        exit_code, output, timed_out = run_command(command, effective_timeout)
    except OSError as exc:
        # 子进程起不来（路径不对、无执行权限、资源耗尽…）。这是最常见的失败类，
        # 必须转成结果对象而不是抛出 —— 抛出去会让本轮连报告都没有，且留下孤儿 running 行。
        return CrawlOutcome(
            exit_code=SPAWN_FAILURE_EXIT_CODE,
            error_line_count=1,
            error_sample=f"采集子进程无法启动：{exc}"[:ERROR_SAMPLE_LEN],
            spawn_failed=True,
        )

    count, sample = count_error_lines(output)
    if timed_out:
        return CrawlOutcome(
            exit_code=TIMEOUT_EXIT_CODE,
            error_line_count=count + 1,
            error_sample=(
                f"{sample}\n采集超时（{effective_timeout}s）"
            ).strip()[:ERROR_SAMPLE_LEN],
            timed_out=True,
        )
    return CrawlOutcome(
        exit_code=exit_code,
        error_line_count=count,
        error_sample=sample,
    )
