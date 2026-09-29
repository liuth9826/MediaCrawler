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


# --------------------------------------------------------------------------- #
# 图片分析（切片 5）：凭据、成本上限、缓存位置
# --------------------------------------------------------------------------- #

# 凭据**一律外部注入**（.env / 环境变量），代码里不硬编码任何 key。
# 缺凭据不是错误：R6 要求模型可缺席，此时整条链路降级为「未运行」而不是失败。
LLM_API_KEY_ENV = "TREND_LLM_API_KEY"
LLM_BASE_URL_ENV = "TREND_LLM_BASE_URL"
LLM_MODEL_ENV = "TREND_LLM_MODEL"
DEFAULT_LLM_BASE_URL = "https://api.openai.com/v1"

# 成本上限：只分析榜单前 N 条、每帖最多送 K 张图。
# 请求量直接等于模型账单，所以要有一个**默认**上限，而不是靠调用方记得传参数。
VISION_TOP_N = int(os.getenv("TREND_VISION_TOP_N", "20"))
VISION_MAX_IMAGES_PER_POST = int(os.getenv("TREND_VISION_MAX_IMAGES", "4"))
VISION_MAX_TERMS_PER_POST = int(os.getenv("TREND_VISION_MAX_TERMS", "6"))

VISION_REQUEST_TIMEOUT = float(os.getenv("TREND_LLM_TIMEOUT", "120"))
# 重试默认值由真机 429 定：原先 2 次重试（等 ~1s、~2s）在 20 条的批次里丢了 7 条。
# 现在 4 次、指数退避到 16s，并优先遵守响应里的 Retry-After。
VISION_MAX_RETRIES = int(os.getenv("TREND_LLM_RETRIES", "4"))
VISION_RETRY_WAIT = float(os.getenv("TREND_LLM_RETRY_WAIT", "2.0"))
# Retry-After 可能给得很大；上限防止一条响应把整轮卡死。
VISION_RETRY_AFTER_MAX = float(os.getenv("TREND_LLM_RETRY_AFTER_MAX", "30"))

# 图片缓存。落在 data/ 下，已被 .gitignore 忽略。
VISION_IMAGE_DIR = Path(
    os.getenv("TREND_IMAGE_DIR") or (PROJECT_ROOT / "data" / "trend_images")
)

# 报告分区的「判断」维度：这些维度回答「是什么风格 / 怎么搭的」，才是趋势判断。
#
# 其余维度（单品 / 场景 / 呈现 / 季节 / 品牌）是**背景描述** —— 它们在穿搭内容里普遍
# 存在，真机上正是它们占满了榜首（内搭 10 帖、裙子 9 帖、开衫 9 帖…）。没有历史基线时
# 「出现 9 次」说明不了「在流行」，所以必须在报告里与判断分开呈现，避免被读成趋势。
#
# 放在 config 而不是词表里，是因为这是**呈现策略**，而 report.py 只 import config、
# 不 import 词表（保持 R6 的纯度）。有测试钉死这里每个名字都存在于已发布词表中。
VISION_JUDGEMENT_DIMENSIONS: tuple[str, ...] = ("风格", "手法")
