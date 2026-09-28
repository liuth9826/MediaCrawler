# -*- coding: utf-8 -*-
"""端到端：采集（打桩）→ 存档 → 打分 → 幂等落库 → 报告。

这是切片 1 的核心回归。整条链路必须在无登录、无网络、无模型的情况下跑通：
采集用注入的桩替代 subprocess，数据用 tmp sqlite，因此 CI 里可离线执行 ——
这同时满足 R6「可复跑」。
"""

import json

import pytest
from sqlalchemy import select

import config as app_config
from config import db_config
from database import db_session
from database.models import XhsNote

from trend import store
from trend.models import TrendPostScore
from trend.runner import rebuild_report, run_pipeline


def _stub_runner(returncode=0, stdout="", stderr=""):
    """替代真实执行器，使采集步骤不启动任何子进程。

    契约是 (command, timeout) -> (退出码, 合并输出, 是否超时)。
    """
    calls = []

    def _run(command, timeout=None):
        calls.append(command)
        return returncode, f"{stdout}\n{stderr}", False

    _run.calls = calls
    return _run


@pytest.fixture
def sqlite_env(tmp_path, monkeypatch):
    monkeypatch.setattr(app_config, "SAVE_DATA_OPTION", "sqlite")
    monkeypatch.setitem(db_config.sqlite_db_config, "db_path", str(tmp_path / "trend.db"))
    db_session._engines.clear()
    yield tmp_path
    db_session._engines.clear()


async def _seed_archive() -> None:
    """写入三条形态各异的帖子，覆盖真实脏值：中文单位、str(None)、null、毫秒时间戳。

    tag_list / image_list 刻意用**真机落库形态**：上游先把值拼成逗号分隔的字符串，
    再被 store 层 json.dumps 包一层，于是库里存的是 '"a,b,c"'（json.loads 出来是
    str 而非 list）。早先这里用的是自己编的 JSON 数组，测试因此全绿而真实路径是坏的。
    """
    await store.init_trend_tables()
    async with db_session.get_session() as session:
        session.add_all(
            [
                XhsNote(
                    note_id="n1",
                    title="春季通勤穿搭",
                    desc="三件套",
                    liked_count="1.2万",
                    collected_count="3000",
                    comment_count="120",
                    share_count="45",
                    tag_list=json.dumps("通勤穿搭,春季", ensure_ascii=False),
                    image_list=json.dumps(
                        "http://a/1.jpg,http://a/2.jpg", ensure_ascii=False
                    ),
                    source_keyword="穿搭",
                    creator_hash="hash-a",
                    nickname="小*",
                    time=1750000000000,
                    note_url="https://example.com/n1",
                ),
                XhsNote(
                    note_id="n2",
                    title="",
                    desc="极简风",
                    liked_count="2000",
                    collected_count="500",
                    comment_count="30",
                    share_count="0",
                    tag_list="null",
                    image_list="null",
                    source_keyword="穿搭",
                    creator_hash="hash-a",
                    nickname="小*",
                    time=1750000000,
                    note_url="https://example.com/n2",
                ),
                XhsNote(
                    note_id="n3",
                    title="高赞爆款",
                    desc="x",
                    liked_count="99999",
                    collected_count=str(None),
                    comment_count=str(None),
                    share_count=str(None),
                    tag_list=json.dumps("爆款", ensure_ascii=False),
                    image_list=json.dumps("http://a/only.jpg", ensure_ascii=False),
                    source_keyword="穿搭",
                    creator_hash="hash-b",
                    nickname="大*",
                    time=1750000000000,
                    note_url="https://example.com/n3",
                ),
            ]
        )


# --------------------------------------------------------------------------- #
# 主链路
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_pipeline_scores_archive_and_writes_report(sqlite_env):
    await _seed_archive()

    result = await run_pipeline(
        keywords=("穿搭",),
        top_n=10,
        crawl_executor=_stub_runner(),
        output_dir=sqlite_env / "reports",
    )

    assert result.notes_new == 3
    assert result.notes_updated == 0
    assert result.confidence == "high"
    assert result.report_path.exists()

    written = result.report_path.read_text(encoding="utf-8")
    assert "穿搭趋势报告" in written
    assert "春季通勤穿搭" in written
    assert "风格结论：仍然不产出" in written


@pytest.mark.asyncio
async def test_dirty_counts_and_tags_are_normalized(sqlite_env):
    await _seed_archive()
    await run_pipeline(
        keywords=("穿搭",),
        top_n=10,
        crawl_executor=_stub_runner(),
        output_dir=sqlite_env / "reports",
    )

    stored = {p.note_id: p for p in await store.fetch_scored_posts("xhs", limit=10)}

    assert stored["n1"].likes == 12000  # "1.2万"
    assert stored["n1"].shares == 45
    assert stored["n1"].image_count == 2
    assert stored["n1"].tag_list == ("通勤穿搭", "春季")
    assert stored["n1"].publish_time == 1750000000  # 毫秒 → 秒

    assert stored["n2"].tag_list == ()  # "null"
    assert stored["n2"].image_count == 0
    assert stored["n2"].publish_time == 1750000000  # 本来就是秒

    assert stored["n3"].collected == 0  # str(None) → "None"
    assert stored["n3"].comments == 0
    assert stored["n3"].shares == 0


@pytest.mark.asyncio
async def test_ranking_is_by_composite_score(sqlite_env):
    """n3（9.9万赞）> n1（转发+收藏+评论都高）> n2。"""
    await _seed_archive()
    await run_pipeline(
        keywords=("穿搭",),
        top_n=10,
        crawl_executor=_stub_runner(),
        output_dir=sqlite_env / "reports",
    )

    ordered = await store.fetch_scored_posts("xhs", limit=10)
    assert [post.note_id for post in ordered] == ["n3", "n1", "n2"]


# --------------------------------------------------------------------------- #
# R5：增量累积、重复运行不重复
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_rerun_is_idempotent_and_adds_no_duplicate_rows(sqlite_env):
    await _seed_archive()
    runner = _stub_runner()
    out = sqlite_env / "reports"

    first = await run_pipeline(keywords=("穿搭",), top_n=10, crawl_executor=runner, output_dir=out)
    second = await run_pipeline(keywords=("穿搭",), top_n=10, crawl_executor=runner, output_dir=out)

    assert (first.notes_new, first.notes_updated) == (3, 0)
    assert (second.notes_new, second.notes_updated) == (0, 3)
    assert await store.count_scored("xhs") == 3


@pytest.mark.asyncio
async def test_first_seen_run_id_survives_reruns(sqlite_env):
    await _seed_archive()
    runner = _stub_runner()
    out = sqlite_env / "reports"

    first = await run_pipeline(keywords=("穿搭",), top_n=10, crawl_executor=runner, output_dir=out)
    second = await run_pipeline(keywords=("穿搭",), top_n=10, crawl_executor=runner, output_dir=out)

    async with db_session.get_session() as session:
        rows = (await session.execute(select(TrendPostScore))).scalars().all()

    assert {row.first_seen_run_id for row in rows} == {first.run_id}
    assert {row.last_scored_run_id for row in rows} == {second.run_id}
    assert {row.score_formula_version for row in rows} == {"v1"}


# --------------------------------------------------------------------------- #
# R6 + Q2：降级与可信度
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_crawl_failure_marks_low_confidence_but_still_reports(sqlite_env):
    await _seed_archive()

    result = await run_pipeline(
        keywords=("穿搭",),
        top_n=10,
        crawl_executor=_stub_runner(returncode=1, stdout="Traceback: boom"),
        output_dir=sqlite_env / "reports",
    )

    assert result.confidence == "low"
    assert result.scored == 3  # 采集失败不得阻止出报告
    assert "整批采集不完整" in result.report_markdown


@pytest.mark.asyncio
async def test_skip_crawl_uses_existing_archive(sqlite_env):
    await _seed_archive()

    result = await run_pipeline(
        keywords=("穿搭",),
        top_n=10,
        skip_crawl=True,
        output_dir=sqlite_env / "reports",
    )

    assert result.confidence == "unknown"
    assert result.scored == 3


@pytest.mark.asyncio
async def test_unmatched_keyword_yields_low_confidence_and_empty_board(sqlite_env):
    await _seed_archive()

    result = await run_pipeline(
        keywords=("不存在的关键词",),
        top_n=10,
        crawl_executor=_stub_runner(),
        output_dir=sqlite_env / "reports",
    )

    assert result.confidence == "low"
    assert result.scored == 0
    assert "无可排序的帖子" in result.report_markdown


@pytest.mark.asyncio
async def test_rebuild_report_needs_no_crawl_and_no_model(sqlite_env):
    """R6：报告能脱离模型、脱离采集，纯从存档重建。"""
    await _seed_archive()
    runner = _stub_runner()
    first = await run_pipeline(
        keywords=("穿搭",),
        top_n=10,
        crawl_executor=runner,
        output_dir=sqlite_env / "reports",
    )
    runner.calls.clear()

    rebuilt = await rebuild_report(run_id=first.run_id, output_dir=sqlite_env / "rebuilt")

    assert runner.calls == []  # 重建不触发采集
    assert rebuilt.run_id == first.run_id
    assert rebuilt.scored == 3
    assert "春季通勤穿搭" in rebuilt.report_markdown
    assert "风格结论：仍然不产出" in rebuilt.report_markdown


# --------------------------------------------------------------------------- #
# 采集失败的记账与降级（R6 + Q2）
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_successful_run_is_recorded_as_succeeded(sqlite_env):
    await _seed_archive()
    result = await run_pipeline(
        keywords=("穿搭",),
        top_n=10,
        crawl_executor=_stub_runner(),
        output_dir=sqlite_env / "reports",
    )
    record = await store.get_run(result.run_id)
    assert record.status == "succeeded"


@pytest.mark.asyncio
async def test_failed_crawl_is_recorded_as_failed_not_succeeded(sqlite_env):
    """回归：status 过去无条件写成 succeeded，与该字段自己的契约矛盾，
    也会骗过按 status 过滤的消费者。"""
    await _seed_archive()
    result = await run_pipeline(
        keywords=("穿搭",),
        top_n=10,
        crawl_executor=_stub_runner(returncode=1, stdout="Traceback: boom"),
        output_dir=sqlite_env / "reports",
    )
    record = await store.get_run(result.run_id)
    assert record.status == "failed"
    assert record.confidence == "low"


@pytest.mark.asyncio
async def test_spawn_failure_still_produces_a_report(sqlite_env):
    """回归：采集子进程起不来（OSError）过去会抛出去，导致本轮没有报告、
    库里留下孤儿 running 行。"""
    await _seed_archive()

    def _exploding_runner(command, timeout=None):
        raise FileNotFoundError("main.py not found")

    result = await run_pipeline(
        keywords=("穿搭",),
        top_n=10,
        crawl_executor=_exploding_runner,
        output_dir=sqlite_env / "reports",
    )

    record = await store.get_run(result.run_id)
    assert record.status == "failed"
    assert result.confidence == "low"
    assert result.scored == 3  # 存档仍在，报告照出
    assert result.report_path.exists()
    assert "整批采集不完整" in result.report_markdown


@pytest.mark.asyncio
async def test_empty_archive_end_to_end_still_writes_a_report(sqlite_env):
    """真·空库（连表都要现建）也要能出报告，而不是崩在 _emit 上。"""
    result = await run_pipeline(
        keywords=("穿搭",),
        top_n=10,
        crawl_executor=_stub_runner(),
        output_dir=sqlite_env / "reports",
    )
    assert result.confidence == "low"
    assert result.notes_total == 0
    assert result.report_path.exists()
    assert "无可排序的帖子" in result.report_markdown


# --------------------------------------------------------------------------- #
# 存档脏值（xhs_note 侧既没有唯一约束，也没有 NOT NULL）
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_duplicate_note_id_in_archive_does_not_crash_the_run(sqlite_env):
    """回归：xhs_note.note_id 只有索引、没有唯一约束，并发采集会留下重复行。
    同一批次内两次 add 同一个 (platform, note_id) 会让 flush 撞唯一约束、
    整笔事务回滚 —— 本轮连报告都没有。"""
    await _seed_archive()
    async with db_session.get_session() as session:
        session.add(
            XhsNote(
                note_id="n1",  # 与既有记录重复
                title="重复的通勤穿搭",
                desc="dup",
                liked_count="1",
                collected_count="1",
                comment_count="1",
                share_count="1",
                tag_list="[]",
                image_list="[]",
                source_keyword="穿搭",
            )
        )

    result = await run_pipeline(
        keywords=("穿搭",),
        top_n=10,
        crawl_executor=_stub_runner(),
        output_dir=sqlite_env / "reports",
    )

    assert result.report_path.exists()
    assert await store.count_scored("xhs") == 3  # 去重后仍是 3 行


@pytest.mark.asyncio
async def test_notes_without_note_id_are_skipped_not_crashing(sqlite_env):
    """note_id 为 NULL 的行会被映射成 ""，不能因此撞唯一约束。"""
    await _seed_archive()
    async with db_session.get_session() as session:
        session.add(
            XhsNote(
                note_id=None,
                title="无 id 的行",
                desc="",
                liked_count="5",
                source_keyword="穿搭",
                tag_list="[]",
                image_list="[]",
            )
        )

    result = await run_pipeline(
        keywords=("穿搭",),
        top_n=10,
        crawl_executor=_stub_runner(),
        output_dir=sqlite_env / "reports",
    )

    assert result.notes_new == 3
    assert await store.count_scored("xhs") == 3


# --------------------------------------------------------------------------- #
# 边界守卫
# ---------------------------------------------------------------------------


def test_non_db_backend_is_rejected(monkeypatch):
    monkeypatch.setattr(app_config, "SAVE_DATA_OPTION", "json")
    with pytest.raises(store.UnsupportedBackendError):
        store.ensure_db_backend()


def test_unsupported_platform_is_rejected():
    with pytest.raises(store.UnsupportedPlatformError):
        store._note_model("dy")


@pytest.mark.asyncio
async def test_invalid_platform_is_rejected_before_any_row_is_written(sqlite_env):
    """安全回归：platform 会先进库、之后还会成为报告文件名的一部分，
    必须在写入任何数据之前拦下，且 rebuild_report 那条路径也不能例外。"""
    await store.init_trend_tables()  # 先建表，好让下面的检查可查

    with pytest.raises(store.UnsupportedPlatformError):
        await run_pipeline(
            keywords=("穿搭",),
            platform="../../tmp/pwn",
            top_n=10,
            crawl_executor=_stub_runner(),
            output_dir=sqlite_env / "reports",
        )

    assert await store.latest_run() is None  # 没有留下任何运行记录


def test_cli_keyword_split_handles_chinese_comma_and_blank():
    from trend.cli import _split_keywords

    assert _split_keywords("穿搭，通勤穿搭") == ("穿搭", "通勤穿搭")
    assert _split_keywords("  ") == ("穿搭",)


@pytest.mark.asyncio
async def test_pipeline_forces_db_backend_when_global_default_is_jsonl(tmp_path, monkeypatch):
    """回归：仓库默认后端是 jsonl，趋势层必须自己把后端设成 DB。

    否则用户执行 trend run 只会撞上 ensure_db_backend 的报错，而 CLI 上没有任何
    开关可以扳回来 —— 这是真机跑出来的问题。
    """
    monkeypatch.setattr(app_config, "SAVE_DATA_OPTION", "jsonl")
    monkeypatch.setitem(db_config.sqlite_db_config, "db_path", str(tmp_path / "trend.db"))
    db_session._engines.clear()
    try:
        result = await run_pipeline(
            keywords=("穿搭",),
            top_n=5,
            skip_crawl=True,
            output_dir=tmp_path / "reports",
        )
        assert app_config.SAVE_DATA_OPTION == "sqlite"
        assert result.confidence == "unknown"
    finally:
        db_session._engines.clear()


@pytest.mark.asyncio
async def test_explicit_non_db_backend_is_still_rejected(sqlite_env):
    with pytest.raises(store.UnsupportedBackendError):
        await run_pipeline(keywords=("穿搭",), save_data_option="csv")
