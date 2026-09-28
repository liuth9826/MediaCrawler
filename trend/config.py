# -*- coding: utf-8 -*-
"""趋势层配置。

与仓库既有风格一致：模块级常量，可由 Typer flag 或环境变量覆盖。
"""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# 打分公式版本。公式一旦调整必须递增，使新旧排名可区分（SDD 质量底线 Q3）。
SCORE_FORMULA_VERSION = "v1"

# 综合传播分权重。口径：转播量 = 综合传播/互动表现，以转发为主（SDD §5）。
ENGAGEMENT_WEIGHTS: dict[str, float] = {
    "share": 5.0,
    "collected": 3.0,
    "comment": 2.0,
    "liked": 1.0,
}

DEFAULT_PLATFORM = "xhs"
DEFAULT_KEYWORDS: tuple[str, ...] = ("穿搭",)
DEFAULT_TOP_N = 30

# 趋势层强制 DB 存档后端：CSV/JSON/JSONL 不做去重，会破坏 R5「同一内容不重复分析」。
DEFAULT_SAVE_DATA_OPTION = "sqlite"

# 正文摘要长度上限，避免报告被超长正文撑爆。
DESC_EXCERPT_LEN = 280

# 整批不完整的判定：错误行数 / **本轮请求的目标量** 超过此比例即判 low。
# 分母用「本轮目标」而不是「库内总条目」或「绝对条数」，是因为前者才是本轮批次规模。
# 绝对条数不是尺度无关的 —— 实测同一失败率（7.5%）在 120 条批次被判 high、185 条批次
# 被判 low，只因为绝对错误数一个在 10 以下一个在 10 以上。
ERROR_RATE_THRESHOLD = float(os.getenv("TREND_ERROR_RATE_THRESHOLD", "0.25"))

# 推不出本轮目标量（未指定 --max-notes 且读不到目标程序配置）时的兜底绝对阈值。
ERROR_LINES_FALLBACK = int(os.getenv("TREND_ERROR_LINES_FALLBACK", "10"))

# 采集子进程超时（秒）。0 表示不限时。
CRAWL_TIMEOUT_SECONDS = float(os.getenv("TREND_CRAWL_TIMEOUT_SECONDS", "0")) or None

REPORT_DIR = Path(
    os.getenv("TREND_REPORT_DIR") or (PROJECT_ROOT / "data" / "trend_reports")
)
