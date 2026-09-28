# -*- coding: utf-8 -*-
"""词表加载与版本化。

版本号必须由内容派生 —— 否则「词表升级后新旧结果可区分」就依赖人记得手动改版本号，
那是不可靠的（SDD 质量底线 Q3）。
"""

import json

import pytest

from trend.vocab import (
    MATCHER_VERSION,
    VOCAB_PATH,
    VocabularyError,
    load_vocabulary,
    vocabulary_version,
)


def _write(tmp_path, payload):
    path = tmp_path / "vocab.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_shipped_vocabulary_loads_and_is_grounded_in_real_tags():
    vocab = load_vocabulary()
    assert vocab.term_count() > 0
    # 词条提炼自真实采集到的标签，几个高频项必须在
    assert "韩系" in vocab.dimensions["风格"]
    assert "通勤" in vocab.dimensions["场景"]
    assert "外套" in vocab.dimensions["单品"]
    assert "优衣库" in vocab.dimensions["品牌"]


def test_version_is_derived_from_content_not_maintained_by_hand():
    """同样内容 → 同样版本；顺序无关；改一个词条 → 版本必变。"""
    base = vocabulary_version({"风格": ("韩系", "辣妹")})
    reordered = vocabulary_version({"风格": ("辣妹", "韩系")})
    changed = vocabulary_version({"风格": ("韩系", "辣妹", "复古")})

    assert base == reordered
    assert base != changed
    assert MATCHER_VERSION in base


def test_matcher_version_is_part_of_the_version_string():
    """改了匹配逻辑而词表没变时，版本号也必须能区分。"""
    assert vocabulary_version({"风格": ("a",)}).endswith(f"-{MATCHER_VERSION}")


def test_terms_are_trimmed_deduped_and_sorted(tmp_path):
    vocab = load_vocabulary(
        _write(tmp_path, {"dimensions": {"风格": [" 韩系 ", "韩系", "辣妹"]}})
    )
    assert vocab.dimensions["风格"] == ("辣妹", "韩系")


def test_missing_file_raises_instead_of_silently_emptying(tmp_path):
    """静默成空词表最危险：报告会「正常」产出但一条线索都没有，看起来像
    「没有趋势」，实际是词表没读到。"""
    with pytest.raises(VocabularyError):
        load_vocabulary(tmp_path / "nope.json")


def test_invalid_json_raises(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(VocabularyError):
        load_vocabulary(path)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"dimensions": {}},
        {"dimensions": "不是对象"},
        {"dimensions": {"风格": "不是列表"}},
        {"dimensions": {"风格": []}},
        {"dimensions": {"风格": ["   "]}},
    ],
)
def test_malformed_vocabulary_raises(tmp_path, payload):
    with pytest.raises(VocabularyError):
        load_vocabulary(_write(tmp_path, payload))


def test_shipped_vocab_lives_inside_the_package():
    assert VOCAB_PATH.exists()
    assert VOCAB_PATH.name == "fashion.json"
