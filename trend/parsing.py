# -*- coding: utf-8 -*-
"""把平台侧「形状不规整」的值规范化成 Python 原生值。

存在理由：既有落库路径用 str(...) 存互动数（store/xhs/_store_impl.py:151-154），
因此缺失值会变成字符串 "None"；平台侧也可能给 "1.2万" / "1亿" 这类带中文单位的
计数；时间戳可能是毫秒而非秒。这些差异都在本模块收敛掉。
"""

from __future__ import annotations

import json
import re

_UNIT_MULTIPLIERS: tuple[tuple[str, int], ...] = (
    ("亿", 100_000_000),
    ("萬", 10_000),
    ("万", 10_000),
    ("w", 10_000),
    ("W", 10_000),
    ("k", 1_000),
    ("K", 1_000),
    ("千", 1_000),
)

_NON_NUMERIC = frozenset({"", "none", "null", "nan", "nil", "-", "unknown", "未知", "无"})

_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")

# 秒级时间戳不会超过此上界（约公元 5138 年），超过即视为毫秒。
_SECONDS_UPPER_BOUND = 100_000_000_000


def parse_engagement_count(value: object, *, default: int = 0) -> int:
    """把平台互动数解析为非负整数。

    接受 int / float / str / None。无法解析时返回 default。
    绝不返回负数 —— 负数计数只可能来自脏数据。
    """
    if value is None or isinstance(value, bool):
        return default

    if isinstance(value, (int, float)):
        return max(int(value), 0)

    if not isinstance(value, str):
        return default

    text = value.strip()
    if text.lower() in _NON_NUMERIC:
        return default

    match = _NUMBER_RE.search(text.replace(",", "").replace("，", ""))
    if not match:
        return default

    try:
        number = float(match.group())
    except ValueError:
        return default

    # 用 round 而非 int：1.005 万在二进制浮点下是 10049.999…，截断会少 1。
    return max(round(number * _find_multiplier(text)), 0)


def _find_multiplier(text: str) -> int:
    for unit, multiplier in _UNIT_MULTIPLIERS:
        if unit in text:
            return multiplier
    return 1


# 明确表示「没有内容」的取值，直接判空。
_EMPTY_LISTISH = frozenset({"", "null", "none", "nan"})

# 剥 JSON 字符串包装的最大层数，防御性上界，避免畸形输入导致死循环。
_MAX_UNWRAP_DEPTH = 3


def parse_string_list(raw: object) -> list[str]:
    """把 tag_list / image_list 解析成字符串列表。

    真实落库形态有两种，都必须处理 —— 只认一种会**静默丢数据**：

    1. JSON 数组：`json.dumps(list)` 的直接结果，如 `'["a", "b"]'`
    2. **被包了一层的 JSON 字符串**：上游先把值拼成 `"a,b,c"` 再 `json.dumps`，
       于是库里存的是 `'"a,b,c"'`；`json.loads` 得到的是字符串而非列表。

    小红书侧的真机数据是第 2 种（已对真实库核实：库里是
    `'"不费力气穿搭,ootd每日穿搭,..."'`）。若只认第 1 种，全部标签与图片都会被
    丢掉 —— 而标签正是后续文本分析切片的主要输入。
    """
    if isinstance(raw, (list, tuple)):
        return [str(item) for item in raw if item is not None]
    if not isinstance(raw, str):
        return []

    text = raw.strip()
    for _ in range(_MAX_UNWRAP_DEPTH):
        if text.lower() in _EMPTY_LISTISH:
            return []
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except (ValueError, TypeError):
                return []
            if not isinstance(parsed, list):
                return []
            return [str(item) for item in parsed if item is not None]
        if len(text) > 1 and text.startswith('"') and text.endswith('"'):
            try:
                inner = json.loads(text)
            except (ValueError, TypeError):
                return []
            if not isinstance(inner, str):
                return []
            text = inner
            continue
        break

    # 走到这里说明是逗号分隔的裸文本 —— 小红书侧的真实形态。
    return [
        part.strip() for part in text.replace("，", ",").split(",") if part.strip()
    ]


def normalize_epoch_seconds(value: object) -> int | None:
    """把可能是毫秒的时间戳统一成秒；无法解析时返回 None。"""
    seconds = parse_engagement_count(value, default=0)
    if seconds <= 0:
        return None
    if seconds >= _SECONDS_UPPER_BOUND:
        return seconds // 1000
    return seconds
