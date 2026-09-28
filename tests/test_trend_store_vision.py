# -*- coding: utf-8 -*-
"""图片证据的落库与读回。

重点在三件容易做错、且做错了不会报错只会悄悄失真的事：
1. 图片 URL 要从**真机落库形态**（双重编码的 JSON 字符串）里读出来；
2. 同一版本重跑只插不改 —— 否则已引用出去的图片证据会被悄悄换掉（Q3）；
3. 坏行必须降级成 failed，不能被当成「读过且没看出风格」。
"""

import json

import pytest
from sqlalchemy import select

import config as app_config
from config import db_config
from database import db_session
from database.models import XhsNote

from trend import store
from trend.models import TrendPostVision, TrendStyleAnalysis
from trend.vision import (
    STATUS_FAILED,
    STATUS_NO_IMAGES,
    STATUS_NO_STYLE_SIGNAL,
    STATUS_OK,
    ImageRef,
    VisionRead,
    VisionTerm,
)

VERSION = "vision-test"


@pytest.fixture
def sqlite_env(tmp_path, monkeypatch):
    monkeypatch.setattr(app_config, "SAVE_DATA_OPTION", "sqlite")
    monkeypatch.setitem(db_config.sqlite_db_config, "db_path", str(tmp_path / "trend.db"))
    db_session._engines.clear()
    yield tmp_path
    db_session._engines.clear()


async def _seed_notes(rows) -> None:
    await store.init_trend_tables()
    async with db_session.get_session() as session:
        session.add_all(rows)


def _note(note_id, image_list):
    return XhsNote(
        note_id=note_id,
        title="t",
        desc="d",
        liked_count="1",
        collected_count="1",
        comment_count="1",
        share_count="1",
        tag_list="null",
        image_list=image_list,
        source_keyword="穿搭",
        creator_hash="hash",
        nickname="小*",
        time=1750000000,
        note_url="https://example.com/x",
    )


# --------------------------------------------------------------------------- #
# 建表
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_new_tables_are_created_on_a_fresh_db(sqlite_env):
    """新表必须能被既有建表路径建出来 —— 仓库没有 alembic，建表全靠 create_all。"""
    await store.init_trend_tables()

    async with db_session.get_session() as session:
        # 能查就说明表存在。
        await session.execute(select(TrendPostVision).limit(1))
        await session.execute(select(TrendStyleAnalysis).limit(1))


# --------------------------------------------------------------------------- #
# 图片 URL 回读
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_image_urls_are_parsed_from_real_double_encoded_storage(sqlite_env):
    """真机落库是 '"a,b"' 这种双重编码；只认 JSON 数组会把图片全丢掉。"""
    await _seed_notes(
        [_note("n1", json.dumps("http://a/1.jpg,http://a/2.jpg", ensure_ascii=False))]
    )

    urls = await store.fetch_note_image_urls("xhs", ["n1"])

    assert urls["n1"] == ("http://a/1.jpg", "http://a/2.jpg")


@pytest.mark.asyncio
async def test_missing_and_null_image_lists_yield_no_entry(sqlite_env):
    await _seed_notes(
        [
            _note("n1", "null"),
            _note("n2", json.dumps("", ensure_ascii=False)),
            _note("n3", None),
        ]
    )

    urls = await store.fetch_note_image_urls("xhs", ["n1", "n2", "n3"])

    assert urls == {}


@pytest.mark.asyncio
async def test_image_urls_preserve_source_order_and_drop_blanks(sqlite_env):
    """顺序即语义（封面在前），绝不能排序；空项要丢。"""
    await _seed_notes(
        [_note("n1", json.dumps("http://a/2.jpg,,http://a/1.jpg,http://a/2.jpg"))]
    )

    urls = await store.fetch_note_image_urls("xhs", ["n1"])

    assert urls["n1"] == ("http://a/2.jpg", "http://a/1.jpg")


@pytest.mark.asyncio
async def test_duplicate_note_rows_do_not_clobber_a_non_empty_image_list(sqlite_env):
    """note_id 无唯一约束，存档里可能有重复行；空行不得覆盖有图的行。"""
    await _seed_notes(
        [
            _note("n1", json.dumps("http://a/keep.jpg")),
            _note("n1", "null"),
        ]
    )

    urls = await store.fetch_note_image_urls("xhs", ["n1"])

    assert urls["n1"] == ("http://a/keep.jpg",)


@pytest.mark.asyncio
async def test_empty_note_ids_short_circuits(sqlite_env):
    await store.init_trend_tables()
    assert await store.fetch_note_image_urls("xhs", []) == {}


# --------------------------------------------------------------------------- #
# 逐帖读数：只插不改
# --------------------------------------------------------------------------- #


def _read(note_id, status=STATUS_OK, terms=(), indices=(1,)):
    return VisionRead(
        note_id=note_id,
        status=status,
        terms=tuple(terms),
        evidence=tuple(
            ImageRef(index=i, url=f"https://example.invalid/{note_id}/{i}.jpg")
            for i in indices
        ),
    )


def _term(term="韩系", indices=(1,)):
    return VisionTerm(
        dimension="风格",
        term=term,
        image_indices=tuple(indices),
        confidence=0.8,
        reason="图中可见",
    )


@pytest.mark.asyncio
async def test_vision_reads_round_trip(sqlite_env):
    await store.init_trend_tables()
    await store.persist_vision_reads(
        [_read("n1", terms=[_term(indices=(1, 2))], indices=(1, 2))],
        platform="xhs",
        analysis_version=VERSION,
        run_id="r1",
        model_id="glm-4v",
    )

    reads = await store.fetch_vision_reads("xhs", VERSION)

    assert len(reads) == 1
    assert reads[0].status == STATUS_OK
    assert reads[0].terms[0].term == "韩系"
    assert reads[0].terms[0].image_indices == (1, 2)
    assert [ref.url for ref in reads[0].evidence] == [
        "https://example.invalid/n1/1.jpg",
        "https://example.invalid/n1/2.jpg",
    ]


@pytest.mark.asyncio
async def test_persist_skips_existing_rows_instead_of_overwriting(sqlite_env):
    """终态行必须跳过，绝不覆盖 —— 否则已引用出去的证据会被悄悄换掉（Q3）。"""
    await store.init_trend_tables()
    first = await store.persist_vision_reads(
        [_read("n1", terms=[_term(term="韩系")])],
        platform="xhs",
        analysis_version=VERSION,
        run_id="r1",
        model_id="glm-4v",
    )
    assert first == (1, 0, 0)

    # 第二次带着**不同的结论**再来 —— 必须被忽略。
    second = await store.persist_vision_reads(
        [_read("n1", terms=[_term(term="辣妹")])],
        platform="xhs",
        analysis_version=VERSION,
        run_id="r2",
        model_id="glm-4v",
    )
    assert second == (0, 0, 1)

    reads = await store.fetch_vision_reads("xhs", VERSION)
    assert len(reads) == 1
    assert reads[0].terms[0].term == "韩系"


@pytest.mark.asyncio
async def test_a_failed_row_is_replaced_by_a_later_success(sqlite_env):
    """`failed` 是唯一的例外：它表示「没读到」，补读不是改写结论。

    真机教训：20 条里 7 条撞 429。若不给这条出路，一次限流就被冻成永久缺失。
    """
    await store.init_trend_tables()
    failed = VisionRead(note_id="n1", status=STATUS_FAILED, error="HTTP 429")
    await store.persist_vision_reads(
        [failed],
        platform="xhs",
        analysis_version=VERSION,
        run_id="r1",
        model_id="m",
    )

    result = await store.persist_vision_reads(
        [_read("n1", terms=[_term(term="韩系")])],
        platform="xhs",
        analysis_version=VERSION,
        run_id="r2",
        model_id="m",
    )

    assert result == (0, 1, 0)
    reads = await store.fetch_vision_reads("xhs", VERSION)
    assert len(reads) == 1
    assert reads[0].status == STATUS_OK
    assert reads[0].terms[0].term == "韩系"
    assert reads[0].error == ""


@pytest.mark.asyncio
async def test_a_successful_row_is_never_replaced_by_a_later_failure(sqlite_env):
    """反向也要守住：重跑时某条变成失败，不得把已有的成功结论抹掉。"""
    await store.init_trend_tables()
    await store.persist_vision_reads(
        [_read("n1", terms=[_term(term="韩系")])],
        platform="xhs",
        analysis_version=VERSION,
        run_id="r1",
        model_id="m",
    )

    result = await store.persist_vision_reads(
        [VisionRead(note_id="n1", status=STATUS_FAILED, error="HTTP 429")],
        platform="xhs",
        analysis_version=VERSION,
        run_id="r2",
        model_id="m",
    )

    assert result == (0, 0, 1)
    reads = await store.fetch_vision_reads("xhs", VERSION)
    assert reads[0].status == STATUS_OK
    assert reads[0].terms[0].term == "韩系"


@pytest.mark.asyncio
async def test_different_versions_coexist(sqlite_env):
    """升模型 → 新版本 → 新行，旧行原样保留（Q3）。"""
    await store.init_trend_tables()
    for version, model in (("vision-a", "m-a"), ("vision-b", "m-b")):
        await store.persist_vision_reads(
            [_read("n1", terms=[_term(term=model)])],
            platform="xhs",
            analysis_version=version,
            run_id="r1",
            model_id=model,
        )

    a = await store.fetch_vision_reads("xhs", "vision-a")
    b = await store.fetch_vision_reads("xhs", "vision-b")

    assert a[0].terms[0].term == "m-a"
    assert b[0].terms[0].term == "m-b"


@pytest.mark.asyncio
async def test_analyzed_note_ids_cover_terminal_statuses_but_not_failures(sqlite_env):
    """终态算「读完了」，**失败不算** —— 否则一次限流抖动会被冻结成永久缺失。

    这是真机数据推翻初版设计的地方：首轮 20 条里 7 条 429，重跑时它们被当成
    「已读过」直接跳过，只能靠换版本补救（那会把已成功的十几条重读一遍）。
    """
    await store.init_trend_tables()
    await store.persist_vision_reads(
        [
            _read("n1", status=STATUS_OK, terms=[_term()]),
            _read("n2", status=STATUS_NO_STYLE_SIGNAL, indices=(1,)),
            _read("n3", status=STATUS_NO_IMAGES, indices=()),
            _read("n4", status=STATUS_FAILED, indices=()),
        ],
        platform="xhs",
        analysis_version=VERSION,
        run_id="r1",
        model_id="m",
    )

    ids = await store.fetch_analyzed_note_ids("xhs", VERSION)

    assert ids == {"n1", "n2", "n3"}
    assert "n4" not in ids


@pytest.mark.asyncio
async def test_corrupt_row_degrades_to_failed_not_to_a_quiet_empty_read(sqlite_env):
    """坏行必须降级成 failed。被当成「读过且没看出风格」就把数据事故讲成了结论。"""
    await store.init_trend_tables()
    await store.persist_vision_reads(
        [_read("n1", terms=[_term()])],
        platform="xhs",
        analysis_version=VERSION,
        run_id="r1",
        model_id="m",
    )
    async with db_session.get_session() as session:
        await session.execute(
            TrendPostVision.__table__.update()
            .where(TrendPostVision.note_id == "n1")
            .values(terms_json="{ 不是合法 JSON")
        )

    reads = await store.fetch_vision_reads("xhs", VERSION)

    assert reads[0].status == STATUS_FAILED
    assert reads[0].terms == ()
    assert reads[0].error


@pytest.mark.asyncio
async def test_terms_without_image_indices_are_dropped_on_read(sqlite_env):
    """读回来的词条若没有图片编号，必须丢掉 —— 它一旦进来就是无证据结论。"""
    await store.init_trend_tables()
    await store.persist_vision_reads(
        [_read("n1", terms=[_term()])],
        platform="xhs",
        analysis_version=VERSION,
        run_id="r1",
        model_id="m",
    )
    async with db_session.get_session() as session:
        await session.execute(
            TrendPostVision.__table__.update()
            .where(TrendPostVision.note_id == "n1")
            .values(
                terms_json=json.dumps(
                    [
                        {"dimension": "风格", "term": "韩系", "image_indices": []},
                        {"dimension": "风格", "term": "辣妹"},
                        {"dimension": "风格", "term": "复古", "image_indices": [1]},
                    ],
                    ensure_ascii=False,
                )
            )
        )

    reads = await store.fetch_vision_reads("xhs", VERSION)

    assert [term.term for term in reads[0].terms] == ["复古"]


# --------------------------------------------------------------------------- #
# 批次台账
# --------------------------------------------------------------------------- #


async def _start(analysis_id, **overrides):
    kwargs = dict(
        run_id="r1",
        platform="xhs",
        analysis_version=VERSION,
        model_id="m",
        prompt_version="p",
        vocabulary_version="v",
        score_formula_version="v1",
        posts_considered=5,
    )
    kwargs.update(overrides)
    await store.start_style_analysis(analysis_id, **kwargs)


async def _finish(analysis_id, status="succeeded", **overrides):
    kwargs = dict(
        status=status,
        posts_analyzed=1,
        posts_failed=0,
        posts_no_image=0,
        images_sent=1,
        terms_rejected=0,
    )
    kwargs.update(overrides)
    await store.finish_style_analysis(analysis_id, **kwargs)


@pytest.mark.asyncio
async def test_style_analysis_ledger_round_trip(sqlite_env):
    await store.init_trend_tables()
    await _start("a1")
    await _finish(
        "a1", posts_analyzed=4, posts_failed=1, images_sent=9, terms_rejected=2
    )

    record = await store.latest_style_analysis("xhs")

    assert record is not None
    assert record.status == "succeeded"
    assert record.posts_analyzed == 4
    assert record.images_sent == 9
    assert record.terms_rejected == 2
    assert record.finished_ts is not None


@pytest.mark.asyncio
async def test_running_analysis_is_not_reported(sqlite_env):
    """中途崩掉留下的 running 行不得被拿去渲染 —— 半成品会被讲成结论。"""
    await store.init_trend_tables()
    await _start("a1")

    assert await store.latest_style_analysis("xhs") is None


@pytest.mark.asyncio
async def test_latest_prefers_newest_finished_execution(sqlite_env):
    await store.init_trend_tables()
    await _start("a1")
    await _finish("a1", status="failed", posts_analyzed=0, posts_failed=5)
    await _start("a2")
    await _finish("a2", posts_analyzed=5, images_sent=11)

    record = await store.latest_style_analysis("xhs")

    assert record is not None
    assert record.analysis_id == "a2"


@pytest.mark.asyncio
async def test_latest_can_filter_by_run_and_version(sqlite_env):
    await store.init_trend_tables()
    await _start("a1", run_id="r1", analysis_version="vision-a")
    await _finish("a1")
    await _start("a2", run_id="r2", analysis_version="vision-b")
    await _finish("a2", posts_analyzed=2, images_sent=2)

    assert (await store.latest_style_analysis("xhs")).analysis_id == "a2"
    assert (await store.latest_style_analysis("xhs", run_id="r1")).analysis_id == "a1"
    assert (
        await store.latest_style_analysis("xhs", analysis_version="vision-a")
    ).analysis_id == "a1"
