# -*- coding: utf-8 -*-
"""互动数与时间戳解析。

覆盖的是真实数据里出现过的脏值形态：str(None) 落库成 "None"（见
store/xhs/_store_impl.py:151-154）、中文单位计数、毫秒时间戳。
"""

import json

import pytest

from trend.parsing import (
    normalize_epoch_seconds,
    parse_engagement_count,
    parse_string_list,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        # 缺失值：既有落库路径把 None 变成字符串 "None"
        (None, 0),
        ("None", 0),
        ("none", 0),
        ("null", 0),
        ("", 0),
        ("   ", 0),
        ("nan", 0),
        # 普通数值与千分位
        (1234, 1234),
        ("1234", 1234),
        ("1,234", 1234),
        ("1，234", 1234),
        (12.9, 12),
        # 中文单位
        ("1.2万", 12000),
        ("3万", 30000),
        ("1亿", 100000000),
        ("10万+", 100000),
        ("2.5w", 25000),
        ("8k", 8000),
        ("5千", 5000),
        # 浮点精度：1.005 * 10000 在二进制下是 10049.999…，截断会少 1
        ("1.005万", 10050),
        # 脏数据
        (-5, 0),
        ("-5", 0),
        ("赞", 0),
        (True, 0),
        ("1.2.3", 1),
    ],
)
def test_parse_engagement_count(raw, expected):
    assert parse_engagement_count(raw) == expected


def test_parse_engagement_count_custom_default():
    assert parse_engagement_count(None, default=-1) == -1


@pytest.mark.parametrize(
    "raw,expected",
    [
        (1750000000, 1750000000),  # 秒
        ("1750000000", 1750000000),
        (1750000000000, 1750000000),  # 毫秒 → 秒
        ("1750000000000", 1750000000),
        (None, None),
        ("None", None),
        (0, None),
        ("", None),
        ("not-a-time", None),
    ],
)
def test_normalize_epoch_seconds(raw, expected):
    assert normalize_epoch_seconds(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        # 真实形态（小红书）：json.dumps 包了一个逗号分隔的字符串
        (json.dumps("通勤穿搭,春季穿搭", ensure_ascii=False), ["通勤穿搭", "春季穿搭"]),
        # 同一形态，但带 json.dumps 默认的 \uXXXX 转义 —— 上游就是这么存的
        ('"\\u901a\\u52e4\\u7a7f\\u642d,\\u6625\\u5b63"', ["通勤穿搭", "春季"]),
        # JSON 数组（其他平台的形态）
        ('["a", "b"]', ["a", "b"]),
        (["a", "b"], ["a", "b"]),
        # 被包了一层的数组，也应能剥开
        (json.dumps('["a","b"]'), ["a", "b"]),
        # 裸 CSV
        ("a,b,c", ["a", "b", "c"]),
        ("a，b", ["a", "b"]),
        ("  a , b  ", ["a", "b"]),
        # 单项
        (json.dumps("http://a/1.jpg", ensure_ascii=False), ["http://a/1.jpg"]),
        # 空值形态
        (None, []),
        ("null", []),
        ("", []),
        ("   ", []),
        ("[]", []),
        (0, []),
        (True, []),
        ({"a": 1}, []),
    ],
)
def test_parse_string_list(raw, expected):
    assert parse_string_list(raw) == expected


def test_parse_string_list_handles_the_real_xhs_tag_value():
    """回归：真机库里 tag_list 存的是 json.dumps("标签1,标签2,...")，
    于是 json.loads 出来是 str 而不是 list。旧实现只认 list，导致全部标签
    被静默丢掉 —— 而标签正是后续文本分析切片的主要输入。"""
    stored = json.dumps(
        "不费力气穿搭,ootd每日穿搭,初夏穿搭,松弛感穿搭", ensure_ascii=False
    )
    assert stored.startswith('"')  # 确认确实是被包了一层
    assert parse_string_list(stored) == [
        "不费力气穿搭",
        "ootd每日穿搭",
        "初夏穿搭",
        "松弛感穿搭",
    ]


def test_parse_string_list_handles_the_real_xhs_image_value():
    stored = json.dumps("http://a/1.jpg,http://a/2.jpg", ensure_ascii=False)
    assert parse_string_list(stored) == ["http://a/1.jpg", "http://a/2.jpg"]
