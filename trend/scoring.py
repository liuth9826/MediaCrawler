# -*- coding: utf-8 -*-
"""综合传播分。

口径（SDD §5）：`转播量` 按「综合传播/互动表现（以转发为主，含点赞、收藏、
评论）」统一理解，不再单独拆分。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping

from trend.config import ENGAGEMENT_WEIGHTS, SCORE_FORMULA_VERSION


@dataclass(frozen=True)
class ScoreResult:
    raw: float
    composite: float
    formula_version: str
    breakdown: dict[str, int]


def composite_score(
    counts: Mapping[str, object],
    *,
    weights: Mapping[str, float] | None = None,
    formula_version: str | None = None,
) -> ScoreResult:
    """计算加权原始分，以及其 log1p 压制后的综合分。

    取 log1p 压制的理由：避免单条爆款在跨帖子比较中压倒其余样本。
    原始 raw 同时保留，便于口径复核与公式改版后回溯。
    """
    active_weights = ENGAGEMENT_WEIGHTS if weights is None else weights
    version = SCORE_FORMULA_VERSION if formula_version is None else formula_version

    breakdown: dict[str, int] = {}
    for key in active_weights:
        raw_value = counts.get(key, 0)
        if raw_value is None or isinstance(raw_value, bool):
            value = 0
        elif isinstance(raw_value, (int, float)):
            value = int(raw_value)
        else:
            value = 0
        breakdown[key] = max(value, 0)

    raw = sum(breakdown[key] * float(weight) for key, weight in active_weights.items())

    return ScoreResult(
        raw=raw,
        composite=math.log1p(raw),
        formula_version=version,
        breakdown=breakdown,
    )
