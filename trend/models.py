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


class TrendStyleAnalysis(Base):
    """一次图片分析的批次台账。

    与 `TrendCrawlRun` 同构：start 时写一行 running，finish 时补状态与计数。
    每次执行都**追加**一行，不覆盖历史 —— Q3 的「新旧可区分」因此是结构性的。
    """

    __tablename__ = "trend_style_analysis"

    id = Column(Integer, primary_key=True, comment="主键ID")
    analysis_id = Column(
        String(64), nullable=False, unique=True, index=True, comment="分析执行ID"
    )
    analysis_version = Column(String(64), index=True, comment="分析版本(内容哈希)")
    run_id = Column(String(64), index=True, comment="关联的采集运行ID")
    platform = Column(String(32), index=True, comment="平台")
    model_id = Column(String(128), comment="模型标识")
    prompt_version = Column(String(64), comment="提示词版本")
    vocabulary_version = Column(String(64), comment="词表版本")
    score_formula_version = Column(String(32), comment="打分公式版本")
    status = Column(
        String(32),
        comment="running/succeeded/partial/failed/skipped_no_credentials",
    )
    posts_considered = Column(Integer, comment="进入候选的帖子数")
    posts_analyzed = Column(Integer, comment="成功读取的帖子数")
    posts_failed = Column(Integer, comment="读取失败的帖子数")
    posts_no_image = Column(Integer, comment="无可用图片的帖子数")
    images_sent = Column(Integer, comment="送出的图片张数")
    terms_rejected = Column(Integer, comment="被拒绝的词条数")
    error_sample = Column(Text, comment="错误样本(截断)")
    started_ts = Column(BigInteger, comment="开始时间戳")
    finished_ts = Column(BigInteger, comment="结束时间戳")
    created_ts = Column(BigInteger, comment="创建时间戳")


class TrendPostVision(Base):
    """模型对**单个帖子**的读取结果。

    这是 Q1 的证据载体，也是「免模型重建报告」的数据来源：报告侧只读这张表 +
    `trend_post_score`，不碰任何模型。

    唯一键是 (platform, note_id, analysis_version)，**不含** analysis_id：
    - 含 analysis_id（每次执行都变）会让同版本重跑插入重复行，聚合时双计；
    - 不含它则同版本重跑直接跳过已读的帖子，零模型调用；存档增长时只增量读新帖（R5）；
    - 换模型/提示词/词表 → 新 analysis_version → 新行，旧行原样保留（Q3）。
    """

    __tablename__ = "trend_post_vision"
    __table_args__ = (
        UniqueConstraint(
            "platform",
            "note_id",
            "analysis_version",
            name="uq_trend_post_vision_platform_note_version",
        ),
    )

    id = Column(Integer, primary_key=True, comment="主键ID")
    platform = Column(String(32), index=True, comment="平台")
    note_id = Column(String(255), index=True, comment="帖子ID")
    analysis_version = Column(String(64), index=True, comment="分析版本(内容哈希)")
    run_id = Column(String(64), comment="首次写入该行的运行ID")
    model_id = Column(String(128), comment="模型标识")
    status = Column(String(32), comment="ok/no_style_signal/no_images/failed")
    terms_json = Column(Text, comment="模型读出的词条(JSON)")
    evidence_json = Column(Text, comment="实际送出的图片(JSON)")
    images_sent = Column(Integer, comment="送出的图片张数")
    raw_excerpt = Column(Text, comment="模型原始回复(截断，仅供审计)")
    error = Column(Text, comment="错误信息")
    created_ts = Column(BigInteger, comment="创建时间戳")


class TrendScoreSnapshot(Base):
    """每次运行后的**帖子分数快照**。

    存在的唯一理由：`trend_post_score` 每帖只有一行、每次采集原地覆盖互动数与综合分，
    所以「上周这条帖多少赞」这类历史**不留痕**。没有快照，隔天再跑一次也攒不出时间
    序列 —— 等再久都答不了「什么在涨」（SDD R4 的另一半）。

    粒度是 (platform, note_id, run_id)：同一帖在不同运行里各留一行，差值就是变化量。
    同一次运行重复写入会被唯一约束挡住（走「先查后写」跳过），所以重跑同一个 run_id
    不会灌重复数据。
    """

    __tablename__ = "trend_score_snapshot"
    __table_args__ = (
        UniqueConstraint(
            "platform",
            "note_id",
            "run_id",
            name="uq_trend_score_snapshot_platform_note_run",
        ),
    )

    id = Column(Integer, primary_key=True, comment="主键ID")
    platform = Column(String(32), index=True, comment="平台")
    note_id = Column(String(255), index=True, comment="帖子ID")
    run_id = Column(String(64), index=True, comment="所属运行ID")
    liked_count = Column(Integer, comment="点赞数(本次采集所见)")
    collected_count = Column(Integer, comment="收藏数(本次采集所见)")
    comment_count = Column(Integer, comment="评论数(本次采集所见)")
    share_count = Column(Integer, comment="转发数(本次采集所见)")
    raw_score = Column(Float, comment="加权原始分")
    composite_score = Column(Float, comment="log1p 压制后的综合传播分")
    score_formula_version = Column(String(32), comment="打分公式版本")
    created_ts = Column(BigInteger, comment="快照时间戳")
