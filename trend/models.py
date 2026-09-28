# -*- coding: utf-8 -*-
"""趋势侧 ORM 表。

复用 database.models.Base，使 database.db_session.create_tables 一并建表。
本模块必须在 create_tables 之前被导入，否则表不会注册进元数据。
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Float,
    Integer,
    String,
    Text,
    UniqueConstraint,
)

from database.models import Base


class TrendCrawlRun(Base):
    """一次采集运行的记账。承载「整批不完整 vs 个别失败」的判定依据（SDD Q2）。"""

    __tablename__ = "trend_crawl_run"

    id = Column(Integer, primary_key=True, comment="主键ID")
    run_id = Column(String(64), nullable=False, unique=True, index=True, comment="运行ID")
    platform = Column(String(32), comment="平台")
    keywords = Column(Text, comment="关键词(逗号分隔)")
    status = Column(String(32), comment="running/succeeded/failed")
    confidence = Column(String(16), comment="high/low/unknown —— 整批是否完整")
    confidence_reason = Column(Text, comment="判定依据")
    crawl_skipped = Column(Boolean, default=False, comment="是否跳过采集")
    exit_code = Column(Integer, comment="采集子进程退出码")
    notes_total = Column(Integer, comment="采集后库内条目数")
    notes_new = Column(Integer, comment="本次新增条目数")
    notes_updated = Column(Integer, comment="本次更新条目数")
    error_line_count = Column(Integer, comment="采集输出中的错误行数")
    error_sample = Column(Text, comment="错误样本(截断)")
    started_ts = Column(BigInteger, comment="开始时间戳")
    finished_ts = Column(BigInteger, comment="结束时间戳")
    score_formula_version = Column(String(32), comment="打分公式版本")


class TrendPostScore(Base):
    """帖子级综合传播分。

    唯一约束是 (platform, note_id) 而非单列 note_id：同一帖子在不同平台可能出现，
    单列唯一会让后一个平台的记录被当成「已存在」而不计入该平台的榜单。
    """

    __tablename__ = "trend_post_score"
    __table_args__ = (
        UniqueConstraint("platform", "note_id", name="uq_trend_post_score_platform_note"),
    )

    id = Column(Integer, primary_key=True, comment="主键ID")
    platform = Column(String(32), index=True, comment="平台")
    note_id = Column(String(255), nullable=False, index=True, comment="帖子ID")
    source_keyword = Column(Text, default="", comment="来源关键词")
    nickname = Column(Text, comment="博主昵称(已脱敏)")
    creator_hash = Column(String(64), index=True, comment="博主匿名哈希")
    title = Column(Text, comment="标题")
    desc_excerpt = Column(Text, comment="正文摘要")
    note_url = Column(Text, comment="帖子URL")
    publish_time = Column(BigInteger, comment="发布时间戳(秒)")
    liked_count = Column(Integer, comment="点赞数")
    collected_count = Column(Integer, comment="收藏数")
    comment_count = Column(Integer, comment="评论数")
    share_count = Column(Integer, comment="转发数")
    raw_score = Column(Float, comment="加权原始分")
    composite_score = Column(Float, index=True, comment="log1p 压制后的综合传播分")
    score_formula_version = Column(String(32), comment="打分公式版本")
    tag_list = Column(Text, comment="标签(JSON)")
    image_count = Column(Integer, comment="图片数")
    first_seen_run_id = Column(String(64), comment="首次出现的运行ID")
    last_scored_run_id = Column(String(64), comment="最近一次打分的运行ID")
    created_ts = Column(BigInteger, comment="创建时间戳")
    updated_ts = Column(BigInteger, comment="更新时间戳")
