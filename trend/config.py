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

# 整批不完整的判定阈值：采集输出中的错误行数超过此**绝对值**即判 low（SDD 质量底线 Q2）。
# 刻意不用比率：一个采集子进程的输出推不出本轮批次规模，若拿「错误行 / 库内条目数」折算，
# 分母会随存档增长而稀释 —— 5000 条的归档里一次半失败批次只算出 2%，照样被判「完整」。
LOW_CONFIDENCE_ERROR_LINES = int(os.getenv("TREND_LOW_CONFIDENCE_ERROR_LINES", "10"))

# 采集子进程超时（秒）。0 表示不限时。
CRAWL_TIMEOUT_SECONDS = float(os.getenv("TREND_CRAWL_TIMEOUT_SECONDS", "0")) or None

REPORT_DIR = Path(
    os.getenv("TREND_REPORT_DIR") or (PROJECT_ROOT / "data" / "trend_reports")
)
