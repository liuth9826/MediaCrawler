# -*- coding: utf-8 -*-
"""趋势层命令行入口。

    uv run python -m trend run --keywords 穿搭
    uv run python -m trend report --run-id <ID>
"""

import asyncio
from typing import Optional

import typer

from trend.config import (
    DEFAULT_KEYWORDS,
    DEFAULT_PLATFORM,
    DEFAULT_SAVE_DATA_OPTION,
    DEFAULT_TOP_N,
)
from trend.crawler import CrawlOptions
from trend.runner import RunResult, rebuild_report, run_pipeline

app = typer.Typer(
    help="穿搭趋势分析：发现 → 采集 → 打分 → 报告",
    no_args_is_help=True,
)


def _split_keywords(raw: str) -> tuple[str, ...]:
    """支持中英文逗号，去空白，过滤空项。"""
    parts = [item.strip() for item in raw.replace("，", ",").split(",")]
    return tuple(item for item in parts if item) or DEFAULT_KEYWORDS


def _echo_result(result: RunResult) -> None:
    typer.echo(f"运行 ID：{result.run_id}")
    typer.echo(f"批次可信度：{result.confidence}")
    typer.echo(f"判定依据：{result.confidence_reason}")
    typer.echo(
        f"入库：新增 {result.notes_new} / 更新 {result.notes_updated} / 榜单 {result.scored}"
    )
    typer.echo(f"报告：{result.report_path}")


@app.command()
def run(
    keywords: str = typer.Option(
        ",".join(DEFAULT_KEYWORDS), "--keywords", "-k", help="关键词，逗号分隔"
    ),
    platform: str = typer.Option(DEFAULT_PLATFORM, "--platform", "-p", help="平台"),
    top_n: int = typer.Option(DEFAULT_TOP_N, "--top-n", help="榜单长度"),
    skip_crawl: bool = typer.Option(
        False, "--skip-crawl", help="跳过采集，仅对既有存档重新打分出报告"
    ),
    headless: bool = typer.Option(False, "--headless", help="采集时无头运行"),
    start: Optional[int] = typer.Option(
        None, "--start", help="起始页码，缺省用 main.py 的配置默认值"
    ),
    max_notes: Optional[int] = typer.Option(
        None,
        "--max-notes",
        help="本次最多采集条数，缺省用 main.py 的配置默认值（当前配置为 15）",
    ),
    lt: Optional[str] = typer.Option(
        None, "--lt", help="登录方式：qrcode | phone | cookie"
    ),
    cookies: Optional[str] = typer.Option(
        None,
        "--cookies",
        help="cookie 登录的 cookie 串（注意：会出现在进程列表与 shell 历史里）",
    ),
    save_data_option: str = typer.Option(
        DEFAULT_SAVE_DATA_OPTION,
        "--save-data-option",
        "-s",
        help="存档后端，只支持 DB 后端（sqlite/mysql/db/postgres）",
    ),
    out: str = typer.Option("", "--out", help="报告输出目录"),
) -> None:
    """跑一次完整流水线：采集（可选）→ 打分 → 出报告。"""
    result = asyncio.run(
        run_pipeline(
            platform=platform,
            keywords=_split_keywords(keywords),
            top_n=top_n,
            skip_crawl=skip_crawl,
            save_data_option=save_data_option,
            crawl_options=CrawlOptions(
                headless=headless,
                start_page=start,
                max_notes=max_notes,
                login_type=lt,
                cookies=cookies,
            ),
            output_dir=out or None,
        )
    )
    _echo_result(result)


@app.command()
def report(
    run_id: str = typer.Option("", "--run-id", help="运行 ID，缺省用最近一次"),
    platform: str = typer.Option(DEFAULT_PLATFORM, "--platform", "-p", help="平台"),
    top_n: int = typer.Option(DEFAULT_TOP_N, "--top-n", help="榜单长度"),
    save_data_option: str = typer.Option(
        DEFAULT_SAVE_DATA_OPTION,
        "--save-data-option",
        "-s",
        help="存档后端，只支持 DB 后端（sqlite/mysql/db/postgres）",
    ),
    out: str = typer.Option("", "--out", help="报告输出目录"),
) -> None:
    """仅从本地存档重建报告，不采集、不调用任何模型。"""
    result = asyncio.run(
        rebuild_report(
            run_id=run_id or None,
            platform=platform,
            top_n=top_n,
            save_data_option=save_data_option,
            output_dir=out or None,
        )
    )
    _echo_result(result)


if __name__ == "__main__":
    app()
