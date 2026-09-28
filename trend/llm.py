# -*- coding: utf-8 -*-
"""模型分析层：把「看图」这一步封在一个可替换的接缝后面。

分层理由（SDD §6「分析后端 = 接口 + NullAnalyzer」）：

- `VisionProvider` 是可注入的 HTTP 边界。测试注入假 provider 就能在**没有任何凭据**的
  机器上验证「图确实被送出去了、模型的胡话确实被挡住了」。
- `NullAnalyzer` 是「无凭据」这条路径的正式实现，不是特例分支。R6 要求模型可缺席，
  所以缺席必须是**一等公民**：报告照出，只是声明本轮没做图片分析。
- `build_analyzer` 是全仓库**唯一**读模型凭据、也是**唯一**实例化 httpx 的地方。
  默认它必须让「重建报告」这条路径碰不到 —— 那是 R6 的结构性保证。

Q1 在这里落成两段代码：提示词要求每个词条必须带图片编号，`parse_model_reply` 再把
不带编号/编号越界/不在词表里的词条**逐条丢掉并计数**。所以「没看图的风格结论」
根本走不到报告层。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import dataclass
from typing import Any, Protocol, Sequence

import httpx
from tenacity import (
    AsyncRetrying,
    RetryCallState,
    retry_if_exception_type,
    stop_after_attempt,
)

from trend.config import (
    DEFAULT_LLM_BASE_URL,
    LLM_API_KEY_ENV,
    LLM_BASE_URL_ENV,
    LLM_MODEL_ENV,
    VISION_MAX_RETRIES,
    VISION_MAX_TERMS_PER_POST,
    VISION_REQUEST_TIMEOUT,
    VISION_RETRY_AFTER_MAX,
    VISION_RETRY_WAIT,
)
from trend.vision import (
    STATUS_FAILED,
    STATUS_NO_IMAGES,
    STATUS_NO_STYLE_SIGNAL,
    STATUS_OK,
    ImageRef,
    VisionRead,
    VisionTerm,
    analysis_version,
)
from trend.vocab import Vocabulary

# 输出契约的版本。改了返回结构或解析规则必须递增 —— 否则版本号不变而结果变了。
VISION_SCHEMA_VERSION = "s1"

SYSTEM_PROMPT = (
    "你是穿搭图片分析助手。你**只能**依据给定的图片作答，不得依据品牌印象、"
    "流行常识或文字标签推测图片里没有的东西。没有任何图片支持某个词条时，就不要输出它。"
    "宁可少报，绝不编造。"
)

# 这段文字会被哈希进 analysis_version，所以改动它会自动产生新版本（Q3）。
# 注意：花括号是 .format 的占位符，JSON 示例里的花括号因此必须写成双花括号。
PROMPT_TEMPLATE = """下面是一个穿搭帖子的 {image_count} 张图片（编号从 1 开始，顺序即送图顺序）。

请只从给定词表中挑选你在这几张图里**确实看到**的词条，并给出支撑它的图片编号。
看不清、拿不准、词表里没有对应的，就不要写。编造比漏报严重得多。

词表（维度: 词条）：
{vocabulary}

严格只返回如下 JSON，不要加 Markdown 代码块，不要加任何解释文字：
{{"style_terms": [{{"dimension": "<词表中的维度>", "term": "<词表中的词条>", "image_indices": [1], "confidence": 0.8, "reason": "不超过40字的图中所见"}}], "no_style_signal": false}}

最多返回 {max_terms} 个词条，按把握从大到小。若这几张图确实没有任何能与词表对应的
穿搭信号，返回 {{"style_terms": [], "no_style_signal": true}}。
"""

REASON_MAX_LEN = 60
ERROR_MAX_LEN = 300
RAW_EXCERPT_MAX_LEN = 500

# 这些状态码重试没有意义：请求本身就被拒了（鉴权、参数、路径）。
_FATAL_STATUS = frozenset({400, 401, 403, 404, 405, 410, 422})

_MIME_BY_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


class ModelReplyError(ValueError):
    """模型回复无法解析成约定的结构。"""


class ProviderError(RuntimeError):
    """模型调用失败，且重试无意义。"""


class RetryableProviderError(RuntimeError):
    """模型调用失败，但值得重试（网络、限流、5xx）。

    `retry_after` 来自响应头。限流端点通常知道该等多久，比自己猜的退避更准。
    """

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


@dataclass(frozen=True)
class AnalyzerInfo:
    model_id: str
    prompt_version: str
    analysis_version: str
    available: bool


@dataclass(frozen=True)
class VisionRequest:
    """一次「看这个帖子的这几张图」的请求。"""

    note_id: str
    images: tuple[ImageRef, ...]
    image_bytes: tuple[bytes, ...]


class VisionProvider(Protocol):
    """可注入的 HTTP 边界 —— 测试用它换成假实现，整条链路即可离线回归。"""

    async def complete(
        self,
        *,
        system: str,
        user_text: str,
        images: Sequence[bytes],
        model: str,
    ) -> str: ...

    async def aclose(self) -> None: ...


class StyleAnalyzer(Protocol):
    @property
    def info(self) -> AnalyzerInfo: ...

    async def analyze(self, request: VisionRequest) -> VisionRead: ...

    async def aclose(self) -> None: ...


# --------------------------------------------------------------------------- #
# 提示词装配
# --------------------------------------------------------------------------- #


def format_vocabulary(vocabulary: Vocabulary) -> str:
    lines = [
        f"{dimension}: {'、'.join(terms)}"
        for dimension, terms in vocabulary.dimensions.items()
    ]
    return "\n".join(lines)


def build_user_text(*, image_count: int, vocabulary: Vocabulary, max_terms: int) -> str:
    return PROMPT_TEMPLATE.format(
        image_count=image_count,
        vocabulary=format_vocabulary(vocabulary),
        max_terms=max_terms,
    )


def prompt_version() -> str:
    digest = hashlib.sha256(PROMPT_TEMPLATE.encode("utf-8")).hexdigest()[:12]
    return f"prompt-{digest}-{VISION_SCHEMA_VERSION}"


# --------------------------------------------------------------------------- #
# 回复解析（Q1 的第二道闸）
# --------------------------------------------------------------------------- #


def _strip_code_fence(text: str) -> str:
    """容忍模型顺手加的 ```json 围栏 —— 提示词里禁了，但不能指望它一定听话。"""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _collapse(text: str) -> str:
    return " ".join(text.split())


def parse_model_reply(
    raw: str,
    *,
    vocabulary: Vocabulary,
    sent_indices: Sequence[int],
    max_terms: int,
) -> tuple[tuple[VisionTerm, ...], int]:
    """解析模型回复。返回 (词条, 被拒条数)。

    逐条校验、**不合格即丢**：词条必须在词表内（闭集）、必须带至少一个**确实送出过**
    的图片编号。这就是 Q1 在词条级的结构性保证 —— 没有有效图片编号的风格词条，
    永远变不成结论。

    顶层结构坏掉才抛 `ModelReplyError`（那是一次失败的读取）；单条词条坏掉只是拒绝。
    """
    try:
        payload: Any = json.loads(_strip_code_fence(raw))
    except ValueError as exc:
        raise ModelReplyError(f"模型回复不是合法 JSON：{exc}") from exc

    if not isinstance(payload, dict):
        raise ModelReplyError("模型回复的顶层必须是 JSON 对象")

    items = payload.get("style_terms")
    if not isinstance(items, list):
        raise ModelReplyError("模型回复缺少 style_terms 数组")

    allowed: dict[str, frozenset[str]] = {
        dimension: frozenset(terms) for dimension, terms in vocabulary.dimensions.items()
    }
    valid_indices = {int(index) for index in sent_indices}

    terms: list[VisionTerm] = []
    rejected = 0
    seen: set[tuple[str, str]] = set()

    for item in items:
        if not isinstance(item, dict):
            rejected += 1
            continue

        dimension = str(item.get("dimension") or "").strip()
        term = str(item.get("term") or "").strip()
        if term not in allowed.get(dimension, frozenset()):
            # 词表是闭集：模型不能发明「赛博朋克」这种维度或词条。
            rejected += 1
            continue

        indices = item.get("image_indices")
        if not isinstance(indices, list):
            rejected += 1
            continue
        try:
            requested = [int(index) for index in indices]
        except (TypeError, ValueError):
            rejected += 1
            continue
        # 越界编号一律剔除；一个都不剩就等于这条词条没有证据。
        resolved = tuple(
            sorted({index for index in requested if index in valid_indices})
        )
        if not resolved:
            rejected += 1
            continue

        key = (dimension, term)
        if key in seen:
            continue  # 重复词条是冗余，不算「拒绝」。
        seen.add(key)

        try:
            confidence = float(item.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0

        terms.append(
            VisionTerm(
                dimension=dimension,
                term=term,
                image_indices=resolved,
                confidence=min(max(confidence, 0.0), 1.0),
                reason=_collapse(str(item.get("reason") or ""))[:REASON_MAX_LEN],
            )
        )
        if len(terms) >= max_terms:
            break

    return tuple(terms), rejected


# --------------------------------------------------------------------------- #
# 真实 provider
# --------------------------------------------------------------------------- #


def _guess_mime(data: bytes) -> str:
    for magic, mime in _MIME_BY_MAGIC:
        if data.startswith(magic):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _data_uri(data: bytes) -> str:
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{_guess_mime(data)};base64,{encoded}"


def _parse_retry_after(response: httpx.Response) -> float | None:
    """读 `Retry-After`（秒）。HTTP-date 形式不解析，退回指数退避即可。"""
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        return max(float(raw.strip()), 0.0)
    except ValueError:
        return None


def _extract_content(payload: Any) -> str:
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ProviderError(f"响应结构不符合 OpenAI 兼容格式：{exc}") from exc

    if isinstance(content, list):
        # 有的兼容端点把 content 返回成 parts 数组。
        content = "".join(
            str(part.get("text") or "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    if not isinstance(content, str):
        raise ProviderError("响应 content 不是字符串")
    return content


class OpenAICompatProvider:
    """OpenAI 兼容的 `/chat/completions` 客户端（图片走 base64 data URI）。

    刻意**不依赖** `response_format={"type":"json_object"}`：不是所有兼容端点都支持它，
    依赖它会得到一个「本地能跑、换端点就 400」的实现。约束写在提示词里，解析侧防御。
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout: float = VISION_REQUEST_TIMEOUT,
        max_retries: int = VISION_MAX_RETRIES,
        retry_wait: float = VISION_RETRY_WAIT,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._model_owned = client is None
        # 凭据随**每个请求**发，不依赖 client 的默认头 —— 否则注入 client（测试、
        # 复用连接池）时鉴权头会被悄悄丢掉，表现为「本地测试 401」。
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=timeout
        )
        self._max_retries = max(0, max_retries)
        self._retry_wait = retry_wait

    async def complete(
        self,
        *,
        system: str,
        user_text: str,
        images: Sequence[bytes],
        model: str,
    ) -> str:
        payload = {
            "model": model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_text},
                        *[
                            {
                                "type": "image_url",
                                "image_url": {"url": _data_uri(data)},
                            }
                            for data in images
                        ],
                    ],
                },
            ],
        }

        retryer = AsyncRetrying(
            stop=stop_after_attempt(self._max_retries + 1),
            wait=self._wait_seconds,
            retry=retry_if_exception_type((RetryableProviderError, httpx.TransportError)),
            reraise=True,
        )
        try:
            async for attempt in retryer:
                with attempt:
                    return await self._post(payload)
        except (RetryableProviderError, httpx.TransportError) as exc:
            # 重试耗尽。统一收敛成 ProviderError —— 调用方只需要认识一种失败。
            raise ProviderError(f"模型调用失败（已重试）：{exc}") from exc
        raise ProviderError("模型调用未产生结果")  # pragma: no cover —— 循环必返回或抛出

    def _wait_seconds(self, retry_state: RetryCallState) -> float:
        """优先听端点的 Retry-After，没有才退回指数退避。

        限流窗口是端点自己知道的，猜不如问。但 Retry-After 也可能给得离谱，所以夹一个上限。
        """
        outcome = retry_state.outcome
        retry_after: float | None = None
        if outcome is not None:
            raw = getattr(outcome.exception(), "retry_after", None)
            if raw is not None:
                try:
                    retry_after = float(raw)
                except (TypeError, ValueError):
                    retry_after = None
        if retry_after is not None:
            return min(max(retry_after, 0.0), VISION_RETRY_AFTER_MAX)
        attempt = int(retry_state.attempt_number)
        base: float = float(self._retry_wait)
        backoff: float = base * (2 ** (attempt - 1))
        ceiling: float = base * 8
        return min(backoff, ceiling)

    async def _post(self, payload: dict[str, Any]) -> str:
        try:
            response = await self._client.post(
                "/chat/completions", json=payload, headers=self._headers
            )
        except httpx.TransportError:
            raise
        except httpx.HTTPError as exc:
            raise RetryableProviderError(f"请求失败：{exc}") from exc

        if response.status_code in _FATAL_STATUS:
            raise ProviderError(
                f"模型端点拒绝了请求（HTTP {response.status_code}）"
                f"：{response.text[:ERROR_MAX_LEN]}"
            )
        if response.status_code >= 400:
            raise RetryableProviderError(
                f"模型端点返回 HTTP {response.status_code}",
                retry_after=_parse_retry_after(response),
            )

        try:
            return _extract_content(response.json())
        except ValueError as exc:
            raise ProviderError(f"响应不是合法 JSON：{exc}") from exc

    async def aclose(self) -> None:
        if self._model_owned:
            await self._client.aclose()


# --------------------------------------------------------------------------- #
# 分析器
# --------------------------------------------------------------------------- #


class VisionStyleAnalyzer:
    """真实的「看图」分析器。`analyze` **永不抛异常**（对齐 MediaDownloader.download）。"""

    def __init__(
        self,
        provider: VisionProvider,
        *,
        vocabulary: Vocabulary,
        model_id: str,
        score_formula_version: str,
        max_terms: int = VISION_MAX_TERMS_PER_POST,
    ) -> None:
        self._provider = provider
        self._vocabulary = vocabulary
        self._model_id = model_id
        self._max_terms = max_terms
        self._info = AnalyzerInfo(
            model_id=model_id,
            prompt_version=prompt_version(),
            analysis_version=analysis_version(
                prompt_template=PROMPT_TEMPLATE,
                schema_version=VISION_SCHEMA_VERSION,
                vocabulary_version=vocabulary.version,
                model_id=model_id,
                score_formula_version=score_formula_version,
            ),
            available=True,
        )

    @property
    def info(self) -> AnalyzerInfo:
        return self._info

    async def analyze(self, request: VisionRequest) -> VisionRead:
        if not request.image_bytes:
            # 无图请求会诱导模型从提示词里编 —— 干脆不送。
            return VisionRead(note_id=request.note_id, status=STATUS_NO_IMAGES)

        user_text = build_user_text(
            image_count=len(request.images),
            vocabulary=self._vocabulary,
            max_terms=self._max_terms,
        )

        try:
            raw = await self._provider.complete(
                system=SYSTEM_PROMPT,
                user_text=user_text,
                images=request.image_bytes,
                model=self._model_id,
            )
        except Exception as exc:  # noqa: BLE001 —— 模型侧任何异常都转成可记录的结果
            return VisionRead(
                note_id=request.note_id,
                status=STATUS_FAILED,
                evidence=request.images,
                error=f"{type(exc).__name__}: {_collapse(str(exc))}"[:ERROR_MAX_LEN],
            )

        try:
            terms, _rejected = parse_model_reply(
                raw,
                vocabulary=self._vocabulary,
                sent_indices=[ref.index for ref in request.images],
                max_terms=self._max_terms,
            )
        except ModelReplyError as exc:
            return VisionRead(
                note_id=request.note_id,
                status=STATUS_FAILED,
                evidence=request.images,
                error=str(exc)[:ERROR_MAX_LEN],
                raw_excerpt=_collapse(raw)[:RAW_EXCERPT_MAX_LEN],
            )

        return VisionRead(
            note_id=request.note_id,
            status=STATUS_OK if terms else STATUS_NO_STYLE_SIGNAL,
            terms=terms,
            evidence=request.images,
            raw_excerpt=_collapse(raw)[:RAW_EXCERPT_MAX_LEN],
        )

    async def aclose(self) -> None:
        await self._provider.aclose()


class NullAnalyzer:
    """无凭据时的正式实现（R6）。

    `available=False` 让编排层直接记一条 `skipped_no_credentials` 就收工 ——
    不下载图片、不产生任何逐帖读数，也不把「没做」写成「做了但没发现」。
    """

    def __init__(
        self, *, vocabulary: Vocabulary, score_formula_version: str, reason: str = ""
    ) -> None:
        self._reason = reason or "未配置模型凭据（TREND_LLM_API_KEY / TREND_LLM_MODEL）"
        self._info = AnalyzerInfo(
            model_id="",
            prompt_version=prompt_version(),
            analysis_version=analysis_version(
                prompt_template=PROMPT_TEMPLATE,
                schema_version=VISION_SCHEMA_VERSION,
                vocabulary_version=vocabulary.version,
                model_id="",
                score_formula_version=score_formula_version,
            ),
            available=False,
        )

    @property
    def info(self) -> AnalyzerInfo:
        return self._info

    @property
    def reason(self) -> str:
        return self._reason

    async def analyze(self, request: VisionRequest) -> VisionRead:
        # 编排层本就不该调到这里；真调到了也要留下痕迹，而不是伪造一个读数。
        return VisionRead(
            note_id=request.note_id, status=STATUS_FAILED, error=self._reason
        )

    async def aclose(self) -> None:
        return None


def build_analyzer(
    *,
    vocabulary: Vocabulary,
    score_formula_version: str,
    max_terms: int = VISION_MAX_TERMS_PER_POST,
) -> StyleAnalyzer:
    """按环境变量造分析器。**全仓库唯一读模型凭据的地方。**

    缺 key 或缺 model 一律返回 `NullAnalyzer` —— 这不是错误路径，是 R6 要求的降级。
    重建报告那条路径永远不调用本函数（有测试钉死）。
    """
    api_key = (os.getenv(LLM_API_KEY_ENV) or "").strip()
    model_id = (os.getenv(LLM_MODEL_ENV) or "").strip()
    if not api_key or not model_id:
        return NullAnalyzer(
            vocabulary=vocabulary, score_formula_version=score_formula_version
        )

    base_url = (os.getenv(LLM_BASE_URL_ENV) or "").strip() or DEFAULT_LLM_BASE_URL
    provider = OpenAICompatProvider(base_url=base_url, api_key=api_key)
    return VisionStyleAnalyzer(
        provider,
        vocabulary=vocabulary,
        model_id=model_id,
        score_formula_version=score_formula_version,
        max_terms=max_terms,
    )
