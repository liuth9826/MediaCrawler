# -*- coding: utf-8 -*-
"""趋势层命令行入口。

    uv run python -m trend run --keywords 穿搭
    uv run python -m trend report --run-id <ID>
"""

import asyncio
from typing import Optional

import typer
from dotenv import load_dotenv

from trend.config import (
    DEFAULT_KEYWORDS,
    DEFAULT_PLATFORM,
    DEFAULT_SAVE_DATA_OPTION,
    DEFAULT_TOP_N,
    VISION_MAX_IMAGES_PER_POST,
    VISION_TOP_N,
)
from trend.crawler import CrawlOptions
from trend.runner import (
    AnalysisResult,
    RunResult,
    rebuild_report,
    run_analysis,
    run_pipeline,
)

# 模型凭据从 .env 读（已 gitignore）。load_dotenv 默认**不覆盖**已有环境变量，
# 所以显式导出的环境变量优先级更高。只在趋势入口做这件事，不影响其他平台。
load_dotenv()

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


def _echo_analysis(result: AnalysisResult) -> None:
    typer.echo(f"分析 ID：{result.analysis_id}")
    typer.echo(f"关联运行：{result.run_id}")
    typer.echo(f"分析版本：{result.analysis_version}")
    typer.echo(f"状态：{result.status}")
    typer.echo(
        f"读取：成功 {result.posts_analyzed} / 失败 {result.posts_failed} / "
        f"无图 {result.posts_no_image}（候选 {result.posts_considered}）"
    )
    typer.echo(
        f"送出图片 {result.images_sent} 张；丢弃词条 {result.rejected_terms} 条；"
        f"产出结论 {result.findings} 条"
    )
    if result.report_path is not None:
        typer.echo(f"报告：{result.report_path}")


@app.command()
def analyze(
    run_id: str = typer.Option("", "--run-id", help="关联的运行 ID，缺省用最近一次"),
    platform: str = typer.Option(DEFAULT_PLATFORM, "--platform", "-p", help="平台"),
    vision_top_n: int = typer.Option(
        VISION_TOP_N, "--vision-top-n", help="只分析榜单前 N 条（成本上限）"
    ),
    max_images: int = typer.Option(
        VISION_MAX_IMAGES_PER_POST, "--max-images", help="每帖最多送几张图（成本上限）"
    ),
    save_data_option: str = typer.Option(
        DEFAULT_SAVE_DATA_OPTION,
        "--save-data-option",
        "-s",
        help="存档后端，只支持 DB 后端（sqlite/mysql/db/postgres）",
    ),
    out: str = typer.Option("", "--out", help="报告输出目录"),
) -> None:
    """对既有存档做图片分析：抓图 → 调模型 → 落库 → 出报告。**不采集。**

    需要模型凭据（`TREND_LLM_API_KEY` / `TREND_LLM_MODEL`，可选 `TREND_LLM_BASE_URL`）。
    没有凭据时不会报错，只记一条「因缺凭据跳过」并照常出报告 —— 质量底线 Q1 宁可
    声明「本轮未运行图片分析」，也不给没有图片支撑的风格结论。
    """
    result = asyncio.run(
        run_analysis(
            run_id=run_id or None,
            platform=platform,
            vision_top_n=vision_top_n,
            max_images=max_images,
            save_data_option=save_data_option,
            output_dir=out or None,
        )
    )
    _echo_analysis(result)


if __name__ == "__main__":
    app()
