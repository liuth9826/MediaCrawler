# -*- coding: utf-8 -*-
"""趋势侧持久化与读取。

只走 DB 后端：CSV/JSON/JSONL 不做事去重，会破坏「同一内容不重复分析」（SDD §6）。
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Sequence

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from database.db_session import get_session

import config as app_config

from trend import models  # noqa: F401 —— 导入即为注册 ORM 元数据，顺序不可省
from trend.config import DESC_EXCERPT_LEN
from trend.models import TrendCrawlRun, TrendPostScore
from trend.parsing import normalize_epoch_seconds, parse_engagement_count, parse_string_list
from trend.scoring import composite_score

DB_BACKENDS = ("sqlite", "mysql", "db", "postgres")


class UnsupportedBackendError(RuntimeError):
    """当前存档后端不满足趋势层要求。"""


class UnsupportedPlatformError(NotImplementedError):
    """趋势层尚未打通该平台。"""


@asynccontextmanager
async def _session() -> AsyncIterator[AsyncSession]:
    """给 get_session() 补上类型，并把「非 DB 后端」转成明确报错。

    上游 database.db_session.get_session 的标注不完整，类型检查推不出 session
    类型，于是每个 `async with` 都得手写注解。它还会对 json/csv/jsonl 后端 yield
    None —— 若只靠 use_backend 前置守卫，任何一条绕过守卫的调用路径都会退化成
    `None.execute` 的 AttributeError，把真正的病因盖掉。
    """
    session: AsyncSession | None
    async with get_session() as session:
        if session is None:
            raise UnsupportedBackendError(
                "当前存档后端不提供 SQL 会话（json/csv/jsonl）。趋势层只支持 DB 后端。"
            )
        yield session


def _note_model(platform: str) -> Any:
    """平台 → 帖子表 ORM。切片 1 只打通小红书（见 SDD §9）。"""
    if platform == "xhs":
        from database.models import XhsNote

        return XhsNote
    raise UnsupportedPlatformError(
        f"趋势层暂只支持 xhs，收到 {platform!r}。其余平台见 docs/穿搭趋势-SDD.md 的切片计划。"
    )


def validate_platform(platform: str) -> None:
    """在写入任何数据之前校验平台。

    必须前置：run_pipeline 会把 platform 落库，之后它还会成为报告文件名的一部分
    （write_report）。若拖到 _note_model 才校验，脏 platform 已经进库；而
    rebuild_report 那条路径压根不调用 _note_model，于是能从库里取出脏值去拼路径。
    """
    _note_model(platform)


def ensure_db_backend() -> None:
    backend = app_config.SAVE_DATA_OPTION
    if backend not in DB_BACKENDS:
        raise UnsupportedBackendError(
            f"趋势层需要 DB 存档后端（{'/'.join(DB_BACKENDS)}），当前为 {backend!r}。"
            "CSV/JSON/JSONL 后端不做去重，会破坏「同一内容不重复分析」。"
        )


def use_backend(option: str) -> None:
    """把存档后端写进配置并校验。

    跟随仓库既有约定（cmd_arg 也是就地改 config 全局量）。之所以必须显式设置：
    仓库默认后端是 jsonl，而趋势层只认 DB 后端，不设就会直接撞上 ensure_db_backend
    的报错，用户却没有任何开关可扳。
    """
    app_config.SAVE_DATA_OPTION = option
    ensure_db_backend()


async def init_trend_tables() -> None:
    """建表。trend.models 已在本模块顶部导入，元数据已注册。"""
    from database.db_session import create_tables

    ensure_db_backend()
    await create_tables(app_config.SAVE_DATA_OPTION)


# --------------------------------------------------------------------------- #
# 从既有存档读取帖子
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class NoteRow:
    note_id: str
    creator_hash: str | None
    nickname: str | None
    title: str
    desc: str
    note_url: str | None
    publish_time: int | None
    liked_count: int
    collected_count: int
    comment_count: int
    share_count: int
    tag_list: tuple[str, ...]
    image_count: int
    source_keyword: str


def _parse_tags(raw: object) -> tuple[str, ...]:
    return tuple(parse_string_list(raw))


def _to_note_row(record: Any) -> NoteRow:
    return NoteRow(
        note_id=str(getattr(record, "note_id", "") or ""),
        creator_hash=getattr(record, "creator_hash", None),
        nickname=getattr(record, "nickname", None),
        title=(getattr(record, "title", None) or "").strip(),
        desc=(getattr(record, "desc", None) or "").strip(),
        note_url=getattr(record, "note_url", None),
        publish_time=normalize_epoch_seconds(getattr(record, "time", None)),
        liked_count=parse_engagement_count(getattr(record, "liked_count", None)),
        collected_count=parse_engagement_count(getattr(record, "collected_count", None)),
        comment_count=parse_engagement_count(getattr(record, "comment_count", None)),
        share_count=parse_engagement_count(getattr(record, "share_count", None)),
        tag_list=_parse_tags(getattr(record, "tag_list", None)),
        image_count=len(parse_string_list(getattr(record, "image_list", None))),
        source_keyword=(getattr(record, "source_keyword", None) or "").strip(),
    )


async def fetch_notes(
    platform: str, *, keywords: Sequence[str] | None = None
) -> list[NoteRow]:
    """从既有存档读出帖子，可按来源关键词过滤。"""
    model = _note_model(platform)
    async with _session() as session:
        statement = select(model)
        if keywords:
            statement = statement.where(model.source_keyword.in_(tuple(keywords)))
        result = await session.execute(statement)
        records = result.scalars().all()
    return [_to_note_row(record) for record in records]


async def count_notes(platform: str) -> int:
    model = _note_model(platform)
    async with _session() as session:
        result = await session.execute(select(func.count()).select_from(model))
        return int(result.scalar() or 0)


# --------------------------------------------------------------------------- #
# 打分与幂等落库
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PostScoreRow:
    platform: str
    note_id: str
    source_keyword: str
    nickname: str | None
    creator_hash: str | None
    title: str
    desc_excerpt: str
    note_url: str | None
    publish_time: int | None
    liked_count: int
    collected_count: int
    comment_count: int
    share_count: int
    raw_score: float
    composite_score: float
    score_formula_version: str
    tag_list: tuple[str, ...]
    image_count: int


def build_score_row(note: NoteRow, platform: str, *, formula_version: str) -> PostScoreRow:
    """由存档行算出打分行。纯函数，便于单测。"""
    result = composite_score(
        {
            "liked": note.liked_count,
            "collected": note.collected_count,
            "comment": note.comment_count,
            "share": note.share_count,
        },
        formula_version=formula_version,
    )
    return PostScoreRow(
        platform=platform,
        note_id=note.note_id,
        source_keyword=note.source_keyword,
        nickname=note.nickname,
        creator_hash=note.creator_hash,
        title=note.title,
        desc_excerpt=note.desc[:DESC_EXCERPT_LEN],
        note_url=note.note_url,
        publish_time=note.publish_time,
        liked_count=note.liked_count,
        collected_count=note.collected_count,
        comment_count=note.comment_count,
        share_count=note.share_count,
        raw_score=result.raw,
        composite_score=result.composite,
        score_formula_version=result.formula_version,
        tag_list=note.tag_list,
        image_count=note.image_count,
    )


def _to_orm(row: PostScoreRow, run_id: str, now: int) -> TrendPostScore:
    return TrendPostScore(
        platform=row.platform,
        note_id=row.note_id,
        source_keyword=row.source_keyword,
        nickname=row.nickname,
        creator_hash=row.creator_hash,
        title=row.title,
        desc_excerpt=row.desc_excerpt,
        note_url=row.note_url,
        publish_time=row.publish_time,
        liked_count=row.liked_count,
        collected_count=row.collected_count,
        comment_count=row.comment_count,
        share_count=row.share_count,
        raw_score=row.raw_score,
        composite_score=row.composite_score,
        score_formula_version=row.score_formula_version,
        tag_list=json.dumps(list(row.tag_list), ensure_ascii=False),
        image_count=row.image_count,
        first_seen_run_id=run_id,
        last_scored_run_id=run_id,
        created_ts=now,
        updated_ts=now,
    )


_UPDATE_FIELDS = (
    "source_keyword",
    "nickname",
    "creator_hash",
    "title",
    "desc_excerpt",
    "note_url",
    "publish_time",
    "liked_count",
    "collected_count",
    "comment_count",
    "share_count",
    "raw_score",
    "composite_score",
    "score_formula_version",
    "image_count",
)


def _dedupe_by_note_id(rows: Sequence[PostScoreRow]) -> list[PostScoreRow]:
    """按 (platform, note_id) 去重、保留最后一个，并丢掉没有 note_id 的行。

    必须做：xhs_note.note_id 只有索引、没有唯一约束（database/models.py:198），
    并发采集会在存档里留下同 id 的重复行，NULL note_id 又会被映射成 ""。
    若不去重，同一批次内两次 session.add 同一个键，flush 时撞唯一约束、
    整笔事务回滚 —— 本轮连报告都产不出来。
    """
    deduped: dict[tuple[str, str], PostScoreRow] = {}
    for row in rows:
        if not row.note_id:
            continue
        deduped[(row.platform, row.note_id)] = row
    return list(deduped.values())


async def persist_scores(rows: Sequence[PostScoreRow], run_id: str) -> tuple[int, int]:
    """幂等写入。返回 (新增数, 更新数)。

    先一次性取出已存在的 (platform, note_id)，再分流插入/更新，沿用仓库既有的
    「先查后写」风格（store/xhs/_store_impl.py:126），保持跨
    sqlite/mysql/postgres 的可移植性。

    已知限制：两个 trend run 进程并发跑同一批数据时，「先查后写」之间存在竞态，
    双方都会判定为不存在而同时 INSERT，撞唯一约束。本地 CLI 串行使用不触发；
    真正并发安全需要 ON CONFLICT / MERGE 这类方言相关的 upsert，留待后续切片。
    """
    deduped = _dedupe_by_note_id(rows)
    if not deduped:
        return 0, 0

    now = int(time.time())
    async with _session() as session:
        existing = await _load_existing(session, deduped)
        inserted = 0
        updated = 0
        for row in deduped:
            if (row.platform, row.note_id) in existing:
                await _update_row(session, row, run_id, now)
                updated += 1
            else:
                session.add(_to_orm(row, run_id, now))
                inserted += 1
        await session.flush()
    return inserted, updated


async def _load_existing(
    session: Any, rows: Sequence[PostScoreRow]
) -> set[tuple[str, str]]:
    """返回本批涉及的、已存在于库中的 (platform, note_id)。"""
    by_platform: dict[str, set[str]] = {}
    for row in rows:
        by_platform.setdefault(row.platform, set()).add(row.note_id)
    if not by_platform:
        return set()

    found: set[tuple[str, str]] = set()
    for platform_value, note_ids in by_platform.items():
        result = await session.execute(
            select(TrendPostScore.platform, TrendPostScore.note_id).where(
                TrendPostScore.platform == platform_value,
                TrendPostScore.note_id.in_(tuple(note_ids)),
            )
        )
        found.update((row[0], row[1]) for row in result.all())
    return found


async def _update_row(session: Any, row: PostScoreRow, run_id: str, now: int) -> None:
    values = {field: getattr(row, field) for field in _UPDATE_FIELDS}
    values["tag_list"] = json.dumps(list(row.tag_list), ensure_ascii=False)
    values["last_scored_run_id"] = run_id
    values["updated_ts"] = now
    # first_seen_run_id / created_ts 刻意不更新：首次出现时间必须稳定。
    statement = (
        update(TrendPostScore)
        .where(
            TrendPostScore.platform == row.platform,
            TrendPostScore.note_id == row.note_id,
        )
        .values(**values)
    )
    await session.execute(statement)


# --------------------------------------------------------------------------- #
# 运行记账
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    platform: str
    keywords: tuple[str, ...]
    status: str
    confidence: str
    confidence_reason: str
    crawl_skipped: bool
    exit_code: int | None
    notes_total: int
    notes_new: int
    notes_updated: int
    error_line_count: int
    score_formula_version: str
    started_ts: int | None
    finished_ts: int | None


def _to_run_record(record: Any) -> RunRecord:
    raw_keywords = getattr(record, "keywords", None) or ""
    return RunRecord(
        run_id=record.run_id,
        platform=record.platform or "",
        keywords=tuple(item for item in raw_keywords.split(",") if item),
        status=record.status or "",
        confidence=record.confidence or "unknown",
        confidence_reason=record.confidence_reason or "",
        crawl_skipped=bool(record.crawl_skipped),
        exit_code=record.exit_code,
        notes_total=record.notes_total or 0,
        notes_new=record.notes_new or 0,
        notes_updated=record.notes_updated or 0,
        error_line_count=record.error_line_count or 0,
        score_formula_version=record.score_formula_version or "",
        started_ts=record.started_ts,
        finished_ts=record.finished_ts,
    )


async def start_run(
    run_id: str,
    platform: str,
    keywords: Sequence[str],
    *,
    crawl_skipped: bool,
    formula_version: str,
) -> None:
    async with _session() as session:
        session.add(
            TrendCrawlRun(
                run_id=run_id,
                platform=platform,
                keywords=",".join(keywords),
                status="running",
                crawl_skipped=crawl_skipped,
                started_ts=int(time.time()),
                score_formula_version=formula_version,
            )
        )


async def finish_run(
    run_id: str,
    *,
    status: str,
    confidence: str,
    confidence_reason: str,
    exit_code: int | None,
    notes_total: int,
    notes_new: int,
    notes_updated: int,
    error_line_count: int,
    error_sample: str = "",
) -> None:
    async with _session() as session:
        statement = (
            update(TrendCrawlRun)
            .where(TrendCrawlRun.run_id == run_id)
            .values(
                status=status,
                confidence=confidence,
                confidence_reason=confidence_reason,
                exit_code=exit_code,
                notes_total=notes_total,
                notes_new=notes_new,
                notes_updated=notes_updated,
                error_line_count=error_line_count,
                error_sample=error_sample,
                finished_ts=int(time.time()),
            )
        )
        await session.execute(statement)


async def get_run(run_id: str) -> RunRecord | None:
    async with _session() as session:
        result = await session.execute(
            select(TrendCrawlRun).where(TrendCrawlRun.run_id == run_id)
        )
        record = result.scalars().first()
    return _to_run_record(record) if record else None


async def latest_run(platform: str | None = None) -> RunRecord | None:
    async with _session() as session:
        statement = select(TrendCrawlRun).order_by(TrendCrawlRun.id.desc())
        if platform:
            statement = statement.where(TrendCrawlRun.platform == platform)
        result = await session.execute(statement.limit(1))
        record = result.scalars().first()
    return _to_run_record(record) if record else None


# --------------------------------------------------------------------------- #
# 读取打分结果（报告重建的唯一数据来源）
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class StoredPost:
    note_id: str
    title: str
    desc_excerpt: str
    nickname: str | None
    note_url: str | None
    source_keyword: str
    publish_time: int | None
    likes: int
    collected: int
    comments: int
    shares: int
    raw_score: float
    composite_score: float
    tag_list: tuple[str, ...]
    image_count: int


def _to_stored_post(record: Any) -> StoredPost:
    return StoredPost(
        note_id=record.note_id,
        title=(record.title or "").strip(),
        desc_excerpt=(record.desc_excerpt or "").strip(),
        nickname=record.nickname,
        note_url=record.note_url,
        source_keyword=(record.source_keyword or "").strip(),
        publish_time=record.publish_time,
        likes=record.liked_count or 0,
        collected=record.collected_count or 0,
        comments=record.comment_count or 0,
        shares=record.share_count or 0,
        raw_score=record.raw_score or 0.0,
        composite_score=record.composite_score or 0.0,
        tag_list=_parse_tags(record.tag_list),
        image_count=record.image_count or 0,
    )


async def fetch_scored_posts(
    platform: str, *, limit: int | None = None
) -> list[StoredPost]:
    """按综合传播分降序取帖子。``limit=None`` 表示取全部。

    之所以允许不设上限：词频分析必须跑在**全量**帖子上，只看榜单前 N 条会把词频截断。
    榜单本身仍只取前 N 条，那是渲染层的事。
    """
    async with _session() as session:
        statement = (
            select(TrendPostScore)
            .where(TrendPostScore.platform == platform)
            .order_by(TrendPostScore.composite_score.desc(), TrendPostScore.note_id.asc())
        )
        if limit is not None:
            statement = statement.limit(int(limit))
        result = await session.execute(statement)
        records = result.scalars().all()
    return [_to_stored_post(record) for record in records]


async def count_scored(platform: str) -> int:
    async with _session() as session:
        result = await session.execute(
            select(func.count())
            .select_from(TrendPostScore)
            .where(TrendPostScore.platform == platform)
        )
        return int(result.scalar() or 0)


async def count_by_keyword(platform: str) -> tuple[tuple[str, int], ...]:
    async with _session() as session:
        result = await session.execute(
            select(TrendPostScore.source_keyword, func.count())
            .where(TrendPostScore.platform == platform)
            .group_by(TrendPostScore.source_keyword)
            .order_by(func.count().desc())
        )
        return tuple((row[0] or "", int(row[1])) for row in result.all())
