# -*- coding: utf-8 -*-
"""图片分析端到端：种子存档 → 抓图（真 HTTP）→ 假模型 → 落库 → 报告。

这台机器上**没有模型凭据**，所以模型用假分析器注入；但图片走的是**真的本地 HTTP
服务器 + 真的 MediaDownloader**，不 mock 下载 —— 「URL 能不能变成模型读到的字节」
这条链只有真跑才算数。

这里守着四条最容易悄悄破掉的契约：
1. 同版本重跑零模型调用、零新增（Q3 + 成本）；
2. 换模型 → 新版本 → 新旧并存（Q3）；
3. 重建报告不构造分析器（R6）；
4. 证据坏掉时绝不产出「无图的风格结论」（Q1）。
"""

import json

import pytest
from sqlalchemy import select

import config as app_config
from config import db_config
from database import db_session
from database.models import XhsNote

from tests.media_server import start_server
from trend import store
from trend.images import build_image_downloader
from trend.llm import AnalyzerInfo, VisionRequest
from trend.models import TrendPostVision
from trend.runner import rebuild_report, run_analysis, run_pipeline
from trend.vision import STATUS_OK, VisionRead, VisionTerm, analysis_version
from trend.vocab import load_vocabulary

VOCAB = load_vocabulary()
MODEL = "fake-vision-1"


@pytest.fixture(scope="module")
def server():
    srv, _thread = start_server()
    srv.set_content("a1.jpg", b"\xff\xd8\xff-a1")
    srv.set_content("a2.jpg", b"\xff\xd8\xff-a2")
    srv.set_content("b1.jpg", b"\xff\xd8\xff-b1")
    yield srv
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def sqlite_env(tmp_path, monkeypatch):
    monkeypatch.setattr(app_config, "SAVE_DATA_OPTION", "sqlite")
    monkeypatch.setitem(db_config.sqlite_db_config, "db_path", str(tmp_path / "trend.db"))
    db_session._engines.clear()
    yield tmp_path
    db_session._engines.clear()


class ScriptedAnalyzer:
    """按 note_id 给固定读数的分析器。模拟真实分析器的对外契约。"""

    def __init__(self, *, terms_by_note=None, fail_notes=(), model=MODEL):
        self.calls: list[str] = []
        self.closed = False
        self._terms = terms_by_note or {}
        self._fail = set(fail_notes)
        self._info = AnalyzerInfo(
            model_id=model,
            prompt_version="prompt-test",
            analysis_version=analysis_version(
                prompt_template="p",
                schema_version="s1",
                vocabulary_version=VOCAB.version,
                model_id=model,
                score_formula_version="v1",
            ),
            available=True,
        )

    @property
    def info(self):
        return self._info

    async def analyze(self, request: VisionRequest) -> VisionRead:
        self.calls.append(request.note_id)
        if request.note_id in self._fail:
            return VisionRead(
                note_id=request.note_id,
                status="failed",
                evidence=request.images,
                error="脚本化的失败",
            )
        # 真实模型只能引用它**确实收到过**的图号 —— 这里照做，否则结论会被
        # （正确地）拒绝，测试就会变成在验证一个做不到的前提。
        sent = tuple(ref.index for ref in request.images)
        terms = tuple(
            VisionTerm(
                dimension=dimension,
                term=term,
                image_indices=(sent[0],),
                confidence=0.9,
                reason="图中可见",
            )
            for dimension, term in self._terms.get(request.note_id, ())
        )
        return VisionRead(
            note_id=request.note_id,
            status=STATUS_OK if terms else "no_style_signal",
            terms=terms,
            evidence=request.images,
        )

    async def aclose(self):
        self.closed = True


def _note(note_id, image_urls, likes="100"):
    return XhsNote(
        note_id=note_id,
        title=f"帖子 {note_id}",
        desc="d",
        liked_count=likes,
        collected_count="10",
        comment_count="5",
        share_count="1",
        tag_list=json.dumps("穿搭", ensure_ascii=False),
        # 真机落库形态：上游拼成逗号串，store 层再 json.dumps 包一层。
        image_list=json.dumps(",".join(image_urls), ensure_ascii=False),
        source_keyword="穿搭",
        creator_hash="hash",
        nickname="小*",
        time=1750000000,
        note_url=f"https://example.com/{note_id}",
    )


def _stub_runner():
    def _run(command, timeout=None):
        return 0, "", False

    return _run


async def _seed(server, tmp_path, image_urls_by_note):
    await store.init_trend_tables()
    async with db_session.get_session() as session:
        session.add_all(
            [_note(note_id, urls) for note_id, urls in image_urls_by_note.items()]
        )
    await run_pipeline(
        keywords=("穿搭",),
        top_n=10,
        crawl_executor=_stub_runner(),
        output_dir=tmp_path / "reports",
    )


def _downloader(tmp_path):
    return build_image_downloader(base_dir=tmp_path / "images", retry_base_delay=0.001)


# --------------------------------------------------------------------------- #
# 主链路
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_analysis_persists_reads_and_renders_image_backed_section(
    sqlite_env, server
):
    await _seed(
        server,
        sqlite_env,
        {
            "n1": [server.url("/ok/a1.jpg"), server.url("/ok/a2.jpg")],
            "n2": [server.url("/ok/b1.jpg")],
        },
    )
    analyzer = ScriptedAnalyzer(
        terms_by_note={"n1": [("风格", "韩系")], "n2": [("风格", "韩系")]}
    )

    result = await run_analysis(
        platform="xhs",
        max_images=4,
        analyzer=analyzer,
        downloader=_downloader(sqlite_env),
        vocabulary=VOCAB,
        output_dir=sqlite_env / "reports",
    )

    assert result.status == "succeeded"
    assert result.posts_analyzed == 2
    assert result.images_sent == 3
    assert result.findings == 1

    async with db_session.get_session() as session:
        rows = (await session.execute(select(TrendPostVision))).scalars().all()
    assert len(rows) == 2
    assert all(row.analysis_version == result.analysis_version for row in rows)

    markdown = result.report_markdown
    assert "## 风格结论（证据等级：image_backed）" in markdown
    assert "韩系" in markdown
    assert "送出图片 3 张" in markdown
    assert "[n1#1](http://127.0.0.1:" in markdown


@pytest.mark.asyncio
async def test_images_really_reach_the_analyzer(sqlite_env, server):
    """图不是「声称送了」—— 字节真的从 HTTP 服务器流到了分析器。"""
    await _seed(server, sqlite_env, {"n1": [server.url("/ok/a1.jpg")]})
    seen: list[bytes] = []

    class Recording(ScriptedAnalyzer):
        async def analyze(self, request):
            seen.extend(request.image_bytes)
            return await super().analyze(request)

    await run_analysis(
        platform="xhs",
        analyzer=Recording(terms_by_note={"n1": [("风格", "韩系")]}),
        downloader=_downloader(sqlite_env),
        vocabulary=VOCAB,
        output_dir=sqlite_env / "reports",
    )

    assert seen == [b"\xff\xd8\xff-a1"]


# --------------------------------------------------------------------------- #
# Q3 + 成本：同版本重跑
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_rerun_same_version_calls_no_model_and_adds_no_rows(sqlite_env, server):
    await _seed(server, sqlite_env, {"n1": [server.url("/ok/a1.jpg")]})

    first = ScriptedAnalyzer(terms_by_note={"n1": [("风格", "韩系")]})
    await run_analysis(
        platform="xhs",
        analyzer=first,
        downloader=_downloader(sqlite_env),
        vocabulary=VOCAB,
        output_dir=sqlite_env / "reports",
    )
    assert first.calls == ["n1"]

    second = ScriptedAnalyzer(terms_by_note={"n1": [("风格", "辣妹")]})
    result = await run_analysis(
        platform="xhs",
        analyzer=second,
        downloader=_downloader(sqlite_env),
        vocabulary=VOCAB,
        output_dir=sqlite_env / "reports",
    )

    assert second.calls == []  # 已读过，不再花模型的钱
    async with db_session.get_session() as session:
        rows = (await session.execute(select(TrendPostVision))).scalars().all()
    assert len(rows) == 1
    assert json.loads(rows[0].terms_json)[0]["term"] == "韩系"
    # 第二次的结论没被写进去，报告里仍是第一次的
    assert "韩系" in result.report_markdown
    assert "辣妹" not in result.report_markdown


@pytest.mark.asyncio
async def test_changing_the_model_yields_a_new_version_without_touching_the_old(
    sqlite_env, server
):
    await _seed(server, sqlite_env, {"n1": [server.url("/ok/a1.jpg")]})

    first = ScriptedAnalyzer(terms_by_note={"n1": [("风格", "韩系")]}, model="model-a")
    a = await run_analysis(
        platform="xhs",
        analyzer=first,
        downloader=_downloader(sqlite_env),
        vocabulary=VOCAB,
        output_dir=sqlite_env / "reports",
    )

    second = ScriptedAnalyzer(terms_by_note={"n1": [("风格", "辣妹")]}, model="model-b")
    b = await run_analysis(
        platform="xhs",
        analyzer=second,
        downloader=_downloader(sqlite_env),
        vocabulary=VOCAB,
        output_dir=sqlite_env / "reports",
    )

    assert a.analysis_version != b.analysis_version
    async with db_session.get_session() as session:
        rows = (await session.execute(select(TrendPostVision))).scalars().all()
    assert {row.analysis_version for row in rows} == {
        a.analysis_version,
        b.analysis_version,
    }
    # 旧版本的行原样保留
    old = [row for row in rows if row.analysis_version == a.analysis_version]
    assert json.loads(old[0].terms_json)[0]["term"] == "韩系"
    assert "辣妹" in b.report_markdown


# --------------------------------------------------------------------------- #
# R6：重建报告不碰模型
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_rebuild_report_never_constructs_an_analyzer(
    sqlite_env, server, monkeypatch
):
    await _seed(server, sqlite_env, {"n1": [server.url("/ok/a1.jpg")]})
    await run_analysis(
        platform="xhs",
        analyzer=ScriptedAnalyzer(terms_by_note={"n1": [("风格", "韩系")]}),
        downloader=_downloader(sqlite_env),
        vocabulary=VOCAB,
        output_dir=sqlite_env / "reports",
    )

    import trend.runner as runner_module

    def _explode(**_kwargs):
        raise AssertionError("重建报告路径不得构造分析器（SDD R6）")

    monkeypatch.setattr(runner_module, "build_analyzer", _explode)

    rebuilt = await rebuild_report(
        platform="xhs", output_dir=sqlite_env / "reports", vocabulary=VOCAB
    )

    assert "## 风格结论（证据等级：image_backed）" in rebuilt.report_markdown
    assert "韩系" in rebuilt.report_markdown


@pytest.mark.asyncio
async def test_rebuild_without_credentials_still_renders_the_stored_section(
    sqlite_env, server, monkeypatch
):
    """没有凭据也要能从库里重建出风格结论 —— 已产出的分析不该依赖「现在能不能调模型」。"""
    await _seed(server, sqlite_env, {"n1": [server.url("/ok/a1.jpg")]})
    await run_analysis(
        platform="xhs",
        analyzer=ScriptedAnalyzer(terms_by_note={"n1": [("风格", "韩系")]}),
        downloader=_downloader(sqlite_env),
        vocabulary=VOCAB,
        output_dir=sqlite_env / "reports",
    )

    monkeypatch.delenv("TREND_LLM_API_KEY", raising=False)
    monkeypatch.delenv("TREND_LLM_MODEL", raising=False)

    rebuilt = await rebuild_report(
        platform="xhs", output_dir=sqlite_env / "reports", vocabulary=VOCAB
    )

    assert "韩系" in rebuilt.report_markdown
    assert "本轮未运行图片分析" not in rebuilt.report_markdown


# --------------------------------------------------------------------------- #
# 无凭据：降级而不是失败
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_missing_credentials_records_skipped_and_still_writes_a_report(
    sqlite_env, server, monkeypatch
):
    monkeypatch.delenv("TREND_LLM_API_KEY", raising=False)
    monkeypatch.delenv("TREND_LLM_MODEL", raising=False)
    await _seed(server, sqlite_env, {"n1": [server.url("/ok/a1.jpg")]})

    result = await run_analysis(
        platform="xhs",
        downloader=_downloader(sqlite_env),
        vocabulary=VOCAB,
        output_dir=sqlite_env / "reports",
    )

    assert result.status == "skipped_no_credentials"
    assert result.posts_analyzed == 0
    assert result.report_path is not None
    assert "本轮未运行图片分析" in result.report_markdown

    record = await store.latest_style_analysis("xhs")
    assert record is not None
    assert record.status == "skipped_no_credentials"

    # 没有产生任何逐帖读数 —— 没做就是没做，不伪造
    async with db_session.get_session() as session:
        rows = (await session.execute(select(TrendPostVision))).scalars().all()
    assert rows == []


@pytest.mark.asyncio
async def test_pipeline_without_credentials_is_unaffected(
    sqlite_env, server, monkeypatch
):
    """普通 `trend run` 的行为不因「有没有模型凭据」而变。"""
    monkeypatch.delenv("TREND_LLM_API_KEY", raising=False)
    monkeypatch.delenv("TREND_LLM_MODEL", raising=False)
    await _seed(server, sqlite_env, {"n1": [server.url("/ok/a1.jpg")]})

    result = await run_pipeline(
        keywords=("穿搭",),
        top_n=10,
        crawl_executor=_stub_runner(),
        output_dir=sqlite_env / "reports",
    )

    assert result.notes_new == 0
    assert "风格结论：本轮未运行图片分析" in result.report_markdown


# --------------------------------------------------------------------------- #
# 图片缺失与证据完整性
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_post_whose_images_all_fail_is_recorded_as_no_images(sqlite_env, server):
    await _seed(server, sqlite_env, {"n1": [server.url("/status/404")]})
    analyzer = ScriptedAnalyzer(terms_by_note={"n1": [("风格", "韩系")]})

    result = await run_analysis(
        platform="xhs",
        analyzer=analyzer,
        downloader=_downloader(sqlite_env),
        vocabulary=VOCAB,
        output_dir=sqlite_env / "reports",
    )

    assert analyzer.calls == []  # 没图就不送模型
    assert result.posts_no_image == 1
    assert result.findings == 0
    assert "没有产生任何风格结论" in result.report_markdown


@pytest.mark.asyncio
async def test_one_failed_image_does_not_discard_the_others(sqlite_env, server):
    await _seed(
        server,
        sqlite_env,
        {"n1": [server.url("/status/404"), server.url("/ok/a2.jpg")]},
    )
    analyzer = ScriptedAnalyzer(terms_by_note={"n1": [("风格", "韩系")]})

    result = await run_analysis(
        platform="xhs",
        analyzer=analyzer,
        downloader=_downloader(sqlite_env),
        vocabulary=VOCAB,
        output_dir=sqlite_env / "reports",
    )

    # 只有第 2 张可下载；证据里的编号必须仍是原始序号 2，不能错位成 1
    async with db_session.get_session() as session:
        row = (await session.execute(select(TrendPostVision))).scalars().one()
    evidence = json.loads(row.evidence_json)
    assert [item["index"] for item in evidence] == [2]
    assert result.findings == 1


@pytest.mark.asyncio
async def test_a_failed_read_is_reported_as_unjudged(sqlite_env, server):
    await _seed(server, sqlite_env, {"n1": [server.url("/ok/a1.jpg")]})

    result = await run_analysis(
        platform="xhs",
        analyzer=ScriptedAnalyzer(fail_notes={"n1"}),
        downloader=_downloader(sqlite_env),
        vocabulary=VOCAB,
        output_dir=sqlite_env / "reports",
    )

    assert result.status == "failed"
    assert "未被判定" in result.report_markdown


@pytest.mark.asyncio
async def test_failed_reads_are_retried_on_the_next_run(sqlite_env, server):
    """真机教训：429 是暂时的。失败必须能在重跑时补上，而不是被冻结成永久缺失。

    首轮 20 条里 7 条撞限流；当时 `failed` 被当成「已读过」，重跑零调用直接跳过，
    只能靠换版本补救（那样会把已成功的十几条重读一遍、白花钱）。
    """
    await _seed(server, sqlite_env, {"n1": [server.url("/ok/a1.jpg")]})

    first = ScriptedAnalyzer(fail_notes={"n1"})
    a = await run_analysis(
        platform="xhs",
        analyzer=first,
        downloader=_downloader(sqlite_env),
        vocabulary=VOCAB,
        output_dir=sqlite_env / "reports",
    )
    assert a.status == "failed"
    assert a.posts_analyzed == 0

    # 端点恢复后重跑：必须**真的再送一次模型**，而不是跳过。
    second = ScriptedAnalyzer(terms_by_note={"n1": [("风格", "韩系")]})
    b = await run_analysis(
        platform="xhs",
        analyzer=second,
        downloader=_downloader(sqlite_env),
        vocabulary=VOCAB,
        output_dir=sqlite_env / "reports",
    )

    assert second.calls == ["n1"]
    assert b.status == "succeeded"
    assert b.posts_analyzed == 1
    assert b.findings == 1
    assert "韩系" in b.report_markdown

    # 仍然只有一行，且已被补读成功（不是插了第二行）
    async with db_session.get_session() as session:
        rows = (await session.execute(select(TrendPostVision))).scalars().all()
    assert len(rows) == 1
    assert rows[0].status == "ok"


@pytest.mark.asyncio
async def test_successful_reads_are_still_never_retried(sqlite_env, server):
    """补读失败的那条，不能顺手把成功的那条也重读一遍（成本）。"""
    await _seed(
        server,
        sqlite_env,
        {
            "n1": [server.url("/ok/a1.jpg")],
            "n2": [server.url("/ok/b1.jpg")],
        },
    )
    await run_analysis(
        platform="xhs",
        analyzer=ScriptedAnalyzer(
            terms_by_note={"n1": [("风格", "韩系")]}, fail_notes={"n2"}
        ),
        downloader=_downloader(sqlite_env),
        vocabulary=VOCAB,
        output_dir=sqlite_env / "reports",
    )

    second = ScriptedAnalyzer(
        terms_by_note={"n1": [("风格", "韩系")], "n2": [("风格", "辣妹")]}
    )
    await run_analysis(
        platform="xhs",
        analyzer=second,
        downloader=_downloader(sqlite_env),
        vocabulary=VOCAB,
        output_dir=sqlite_env / "reports",
    )

    assert second.calls == ["n2"]  # 只补失败的那条
    """把证据改坏（模拟数据事故）后重建：那条结论必须消失，而不是无图出场。"""
    await _seed(server, sqlite_env, {"n1": [server.url("/ok/a1.jpg")]})
    await run_analysis(
        platform="xhs",
        analyzer=ScriptedAnalyzer(terms_by_note={"n1": [("风格", "韩系")]}),
        downloader=_downloader(sqlite_env),
        vocabulary=VOCAB,
        output_dir=sqlite_env / "reports",
    )

    async with db_session.get_session() as session:
        await session.execute(TrendPostVision.__table__.update().values(evidence_json="[]"))

    rebuilt = await rebuild_report(
        platform="xhs", output_dir=sqlite_env / "reports", vocabulary=VOCAB
    )

    # 「韩系」在 text_only 区的说明文字里作为例子出现过，所以不能只断言词不在文中；
    # 要断言的是：它没有作为 image_backed 结论出现、证据链接也不该存在。
    assert "## 风格结论（证据等级：image_backed）" in rebuilt.report_markdown
    assert "没有产生任何风格结论" in rebuilt.report_markdown
    assert "[n1#1]" not in rebuilt.report_markdown


@pytest.mark.asyncio
async def test_cost_cap_limits_how_many_posts_are_sent(sqlite_env, server):
    """成本上限生效：只分析榜单前 N 条，其余帖子不发请求。"""
    await _seed(
        server,
        sqlite_env,
        {
            "n1": [server.url("/ok/a1.jpg")],
            "n2": [server.url("/ok/b1.jpg")],
        },
    )
    analyzer = ScriptedAnalyzer(
        terms_by_note={"n1": [("风格", "韩系")], "n2": [("风格", "辣妹")]}
    )

    result = await run_analysis(
        platform="xhs",
        vision_top_n=1,
        analyzer=analyzer,
        downloader=_downloader(sqlite_env),
        vocabulary=VOCAB,
        output_dir=sqlite_env / "reports",
    )

    assert result.posts_considered == 1
    assert len(analyzer.calls) == 1
