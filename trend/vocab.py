# -*- coding: utf-8 -*-
"""词表加载与版本化。

版本号是**内容哈希**，不是人工维护的字符串：改词表 → 版本自动变；改匹配逻辑 →
递增 MATCHER_VERSION 也会变。这样「词表/提示词升级后新旧结果可区分」不依赖人的记性
（SDD 质量底线 Q3）。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

VOCAB_PATH = Path(__file__).resolve().parent / "vocabulary" / "fashion.json"

# 匹配逻辑本身的版本。改了匹配方式必须递增 —— 否则词表没变、结果变了，版本号却相同。
MATCHER_VERSION = "m1"


class VocabularyError(ValueError):
    """词表内容不合法。"""


@dataclass(frozen=True)
class Vocabulary:
    dimensions: dict[str, tuple[str, ...]]
    version: str

    def term_count(self) -> int:
        return sum(len(terms) for terms in self.dimensions.values())


def load_vocabulary(path: str | Path | None = None) -> Vocabulary:
    """读取词表。文件缺失或结构不对时明确报错，不静默降级成空词表。

    静默成空词表是危险的：报告会「正常」产出但一条线索都没有，看起来像
    「没有趋势」，实际是词表没读到。
    """
    source = Path(path) if path is not None else VOCAB_PATH
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except OSError as exc:
        raise VocabularyError(f"词表读取失败：{source} —— {exc}") from exc
    except ValueError as exc:
        raise VocabularyError(f"词表不是合法 JSON：{source} —— {exc}") from exc

    if not isinstance(raw, dict):
        raise VocabularyError(f"词表顶层必须是对象：{source}")

    dimensions_raw = raw.get("dimensions")
    if not isinstance(dimensions_raw, dict) or not dimensions_raw:
        raise VocabularyError(f"词表缺少非空的 dimensions：{source}")

    dimensions: dict[str, tuple[str, ...]] = {}
    for name, terms in dimensions_raw.items():
        if not isinstance(terms, list):
            raise VocabularyError(f"维度 {name!r} 的词条必须是列表：{source}")
        cleaned = tuple(
            sorted({str(term).strip() for term in terms if str(term).strip()})
        )
        if not cleaned:
            raise VocabularyError(f"维度 {name!r} 去掉空白后没有任何词条：{source}")
        dimensions[str(name)] = cleaned

    return Vocabulary(dimensions=dimensions, version=vocabulary_version(dimensions))


def vocabulary_version(dimensions: dict[str, tuple[str, ...]]) -> str:
    """由词表内容派生版本号。

    对规范序列化后的内容取哈希，所以只是调整 JSON 排版不会改版本 —— 只有词条真的
    变了才变。这是有意的：版本应当反映语义，而不是文件字节。
    """
    # 词条顺序不影响匹配结果，因此也不应影响版本号 —— 版本要反映语义，不是排版。
    # （sort_keys 只排字典键，管不到列表内的顺序，所以这里显式 sorted。）
    canonical = json.dumps(
        {name: sorted(terms) for name, terms in sorted(dimensions.items())},
        ensure_ascii=False,
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
    return f"vocab-{digest}-{MATCHER_VERSION}"
