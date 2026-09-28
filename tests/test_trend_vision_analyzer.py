# -*- coding: utf-8 -*-
"""分析器接缝：图确实送出去了吗、模型的胡话被挡住了吗。

这台机器上**没有模型凭据**，所以这些测试全部靠注入假 provider 跑通 —— 这正是
把 HTTP 做成可注入边界的目的。两个最要紧的断言：

1. provider 收到的图片 part 数量正确（否则「模型看过图」只是口头声明）；
2. 不带有效图片编号的词条被拒绝并计数（否则 Q1 就只是文档里的一句话）。
"""

import json
from types import SimpleNamespace

import httpx
import pytest

from trend.llm import (
    PROMPT_TEMPLATE,
    NullAnalyzer,
    OpenAICompatProvider,
    ProviderError,
    RetryableProviderError,
    VisionRequest,
    VisionStyleAnalyzer,
    build_analyzer,
    build_user_text,
    format_vocabulary,
    parse_model_reply,
    prompt_version,
)
from trend.vocab import Vocabulary
from trend.vision import ImageRef

VOCAB = Vocabulary(
    dimensions={"风格": ("韩系", "辣妹", "复古"), "单品": ("工装裤",)},
    version="vocab-test-m1",
)


def _images(count=2):
    return tuple(
        ImageRef(index=i, url=f"https://example.invalid/{i}.jpg")
        for i in range(1, count + 1)
    )


class FakeVisionProvider:
    """记录请求、按脚本回复。模拟真实 provider 的契约。"""

    def __init__(self, reply="{}", *, error=None, replies=None):
        self._reply = reply
        self._error = error
        self._replies = list(replies or [])
        self.calls = []
        self.closed = False

    async def complete(self, *, system, user_text, images, model):
        self.calls.append(
            {
                "system": system,
                "user_text": user_text,
                "images": list(images),
                "model": model,
            }
        )
        if self._error is not None:
            raise self._error
        if self._replies:
            return self._replies.pop(0)
        return self._reply

    async def aclose(self):
        self.closed = True


def _analyzer(provider, max_terms=6):
    return VisionStyleAnalyzer(
        provider,
        vocabulary=VOCAB,
        model_id="glm-4v",
        score_formula_version="v1",
        max_terms=max_terms,
    )


def _request(count=2, note_id="n1"):
    return VisionRequest(
        note_id=note_id,
        images=_images(count),
        image_bytes=tuple(f"bytes-{i}".encode() for i in range(1, count + 1)),
    )


# --------------------------------------------------------------------------- #
# 提示词与版本
# --------------------------------------------------------------------------- #


def test_user_text_lists_the_vocabulary_and_has_no_stray_placeholders():
    text = build_user_text(image_count=3, vocabulary=VOCAB, max_terms=4)

    assert "韩系" in text and "工装裤" in text
    assert "3 张图片" in text
    assert "最多返回 4 个词条" in text
    # JSON 示例里的双花括号必须被 .format 还原成单花括号。
    assert '{"style_terms"' in text
    assert "{{" not in text and "}}" not in text


def test_prompt_version_is_content_derived():
    assert prompt_version() == prompt_version()
    assert prompt_version().startswith("prompt-")


def test_format_vocabulary_covers_every_dimension():
    text = format_vocabulary(VOCAB)
    assert "风格: 韩系、辣妹、复古" in text
    assert "单品: 工装裤" in text


# --------------------------------------------------------------------------- #
# 图确实送出去了
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_provider_receives_one_image_blob_per_sent_image():
    provider = FakeVisionProvider(reply='{"style_terms": []}')

    await _analyzer(provider).analyze(_request(count=3))

    assert len(provider.calls) == 1
    assert len(provider.calls[0]["images"]) == 3
    assert provider.calls[0]["images"][0] == b"bytes-1"
    assert provider.calls[0]["model"] == "glm-4v"


@pytest.mark.asyncio
async def test_analyzer_never_sends_a_request_without_images():
    """无图请求会诱导模型照着提示词编 —— 必须直接短路，不调用 provider。"""
    provider = FakeVisionProvider()

    read = await _analyzer(provider).analyze(
        VisionRequest(note_id="n1", images=(), image_bytes=())
    )

    assert read.status == "no_images"
    assert provider.calls == []


# --------------------------------------------------------------------------- #
# Q1：没有有效图片编号的词条必须被拒
# --------------------------------------------------------------------------- #


def test_term_without_image_indices_is_rejected():
    raw = json.dumps(
        {"style_terms": [{"dimension": "风格", "term": "韩系", "image_indices": []}]}
    )

    terms, rejected = parse_model_reply(
        raw, vocabulary=VOCAB, sent_indices=[1, 2], max_terms=6
    )

    assert terms == ()
    assert rejected == 1


def test_term_with_out_of_range_image_index_is_rejected():
    """模型编了一个它没看过的图号 —— 这是幻觉最典型的形态。"""
    raw = json.dumps(
        {"style_terms": [{"dimension": "风格", "term": "韩系", "image_indices": [9]}]}
    )

    terms, rejected = parse_model_reply(
        raw, vocabulary=VOCAB, sent_indices=[1, 2], max_terms=6
    )

    assert terms == ()
    assert rejected == 1


def test_out_of_range_indices_are_dropped_but_valid_ones_kept():
    raw = json.dumps(
        {"style_terms": [{"dimension": "风格", "term": "韩系", "image_indices": [1, 9]}]}
    )

    terms, rejected = parse_model_reply(
        raw, vocabulary=VOCAB, sent_indices=[1, 2], max_terms=6
    )

    assert rejected == 0
    assert terms[0].image_indices == (1,)


def test_term_outside_the_vocabulary_is_rejected():
    """词表是闭集：模型不能发明词条或维度。"""
    raw = json.dumps(
        {
            "style_terms": [
                {"dimension": "风格", "term": "赛博朋克", "image_indices": [1]},
                {"dimension": "不存在的维度", "term": "韩系", "image_indices": [1]},
            ]
        }
    )

    terms, rejected = parse_model_reply(
        raw, vocabulary=VOCAB, sent_indices=[1], max_terms=6
    )

    assert terms == ()
    assert rejected == 2


def test_valid_term_is_accepted_with_clamped_confidence():
    raw = json.dumps(
        {
            "style_terms": [
                {
                    "dimension": "风格",
                    "term": "韩系",
                    "image_indices": [2, 1],
                    "confidence": 7.5,
                    "reason": "  上身是  宽松外套 ",
                }
            ]
        }
    )

    terms, rejected = parse_model_reply(
        raw, vocabulary=VOCAB, sent_indices=[1, 2], max_terms=6
    )

    assert rejected == 0
    assert terms[0].image_indices == (1, 2)  # 排序确定
    assert terms[0].confidence == 1.0  # 夹到 [0,1]
    assert terms[0].reason == "上身是 宽松外套"


def test_duplicate_terms_are_collapsed_without_being_counted_as_rejected():
    raw = json.dumps(
        {
            "style_terms": [
                {"dimension": "风格", "term": "韩系", "image_indices": [1]},
                {"dimension": "风格", "term": "韩系", "image_indices": [2]},
            ]
        }
    )

    terms, rejected = parse_model_reply(
        raw, vocabulary=VOCAB, sent_indices=[1, 2], max_terms=6
    )

    assert len(terms) == 1
    assert rejected == 0


def test_max_terms_caps_the_output():
    raw = json.dumps(
        {
            "style_terms": [
                {"dimension": "风格", "term": "韩系", "image_indices": [1]},
                {"dimension": "风格", "term": "辣妹", "image_indices": [1]},
                {"dimension": "风格", "term": "复古", "image_indices": [1]},
            ]
        }
    )

    terms, _ = parse_model_reply(raw, vocabulary=VOCAB, sent_indices=[1], max_terms=2)

    assert len(terms) == 2


# --------------------------------------------------------------------------- #
# 畸形回复
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "raw",
    [
        "这不是 JSON",
        "",
        "[1, 2, 3]",
        '{"style_terms": "不是数组"}',
        "null",
    ],
)
@pytest.mark.asyncio
async def test_malformed_reply_becomes_a_failed_read_not_an_exception(raw):
    provider = FakeVisionProvider(reply=raw)

    read = await _analyzer(provider).analyze(_request())

    assert read.status == "failed"
    assert read.terms == ()
    assert read.error
    # 原文留档，便于事后判断是模型的问题还是解析的问题（空回复时即空串）。
    assert read.raw_excerpt == " ".join(raw.split())


@pytest.mark.asyncio
async def test_code_fenced_reply_is_tolerated():
    provider = FakeVisionProvider(
        reply='```json\n{"style_terms": [{"dimension": "风格", "term": "韩系",'
        ' "image_indices": [1]}]}\n```'
    )

    read = await _analyzer(provider).analyze(_request())

    assert read.status == "ok"
    assert read.terms[0].term == "韩系"


@pytest.mark.asyncio
async def test_explicit_no_style_signal_is_not_a_failure():
    """「看了但没看出」是合法结果，必须与「读取失败」区分开。"""
    provider = FakeVisionProvider(reply='{"style_terms": [], "no_style_signal": true}')

    read = await _analyzer(provider).analyze(_request())

    assert read.status == "no_style_signal"
    assert read.terms == ()
    assert read.error == ""


@pytest.mark.asyncio
async def test_provider_failure_never_raises():
    provider = FakeVisionProvider(error=ProviderError("端点拒绝"))

    read = await _analyzer(provider).analyze(_request())

    assert read.status == "failed"
    assert "端点拒绝" in read.error


@pytest.mark.asyncio
async def test_evidence_records_what_was_actually_sent():
    provider = FakeVisionProvider(
        reply='{"style_terms": [{"dimension": "风格", "term": "韩系", "image_indices": [1]}]}'
    )

    read = await _analyzer(provider).analyze(_request(count=2))

    assert [ref.index for ref in read.evidence] == [1, 2]


# --------------------------------------------------------------------------- #
# build_analyzer：凭据只在这里读
# --------------------------------------------------------------------------- #


def test_missing_credentials_yield_null_analyzer(monkeypatch):
    monkeypatch.delenv("TREND_LLM_API_KEY", raising=False)
    monkeypatch.delenv("TREND_LLM_MODEL", raising=False)

    analyzer = build_analyzer(vocabulary=VOCAB, score_formula_version="v1")

    assert isinstance(analyzer, NullAnalyzer)
    assert analyzer.info.available is False


def test_key_without_model_is_still_unavailable(monkeypatch):
    """半套凭据不算配好 —— 没有模型名根本发不出请求。"""
    monkeypatch.setenv("TREND_LLM_API_KEY", "sk-x")
    monkeypatch.delenv("TREND_LLM_MODEL", raising=False)

    analyzer = build_analyzer(vocabulary=VOCAB, score_formula_version="v1")

    assert analyzer.info.available is False


def test_credentials_produce_a_real_analyzer(monkeypatch):
    monkeypatch.setenv("TREND_LLM_API_KEY", "sk-x")
    monkeypatch.setenv("TREND_LLM_MODEL", "glm-4v")
    monkeypatch.setenv("TREND_LLM_BASE_URL", "https://gateway.invalid/v1")

    analyzer = build_analyzer(vocabulary=VOCAB, score_formula_version="v1")

    assert isinstance(analyzer, VisionStyleAnalyzer)
    assert analyzer.info.model_id == "glm-4v"
    assert analyzer.info.available is True


def test_analysis_version_ignores_credential_changes(monkeypatch):
    """换 key / 换网关不得改变 analysis_version（Q3）。"""
    monkeypatch.setenv("TREND_LLM_MODEL", "glm-4v")
    monkeypatch.setenv("TREND_LLM_API_KEY", "sk-one")
    monkeypatch.setenv("TREND_LLM_BASE_URL", "https://one.invalid/v1")
    first = build_analyzer(vocabulary=VOCAB, score_formula_version="v1").info

    monkeypatch.setenv("TREND_LLM_API_KEY", "sk-two")
    monkeypatch.setenv("TREND_LLM_BASE_URL", "https://two.invalid/v1")
    second = build_analyzer(vocabulary=VOCAB, score_formula_version="v1").info

    assert first.analysis_version == second.analysis_version


def test_analysis_version_changes_with_model(monkeypatch):
    monkeypatch.setenv("TREND_LLM_API_KEY", "sk-x")
    monkeypatch.setenv("TREND_LLM_MODEL", "glm-4v")
    first = build_analyzer(vocabulary=VOCAB, score_formula_version="v1").info

    monkeypatch.setenv("TREND_LLM_MODEL", "qwen-vl-max")
    second = build_analyzer(vocabulary=VOCAB, score_formula_version="v1").info

    assert first.analysis_version != second.analysis_version


# --------------------------------------------------------------------------- #
# HTTP 层
# --------------------------------------------------------------------------- #


def _provider(handler, *, retry_wait=0.001, **kwargs):
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(
        transport=transport, base_url="https://gateway.invalid/v1"
    )
    return OpenAICompatProvider(
        base_url="https://gateway.invalid/v1",
        api_key="sk-test",
        retry_wait=retry_wait,
        client=client,
        **kwargs,
    )


def _ok(reply='{"style_terms": []}'):
    return httpx.Response(200, json={"choices": [{"message": {"content": reply}}]})


@pytest.mark.asyncio
async def test_request_body_shape_matches_openai_chat_completions():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return _ok()

    provider = _provider(handler)
    await provider.complete(
        system="sys", user_text="hello", images=[b"\xff\xd8\xff_jpeg"], model="glm-4v"
    )
    await provider.aclose()

    assert seen["url"].endswith("/chat/completions")
    assert seen["auth"] == "Bearer sk-test"
    body = seen["body"]
    assert body["model"] == "glm-4v"
    assert body["temperature"] == 0
    content = body["messages"][1]["content"]
    assert content[0] == {"type": "text", "text": "hello"}
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


@pytest.mark.asyncio
async def test_extracts_text_from_parts_style_content():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": [{"type": "text", "text": '{"a": 1}'}]}}
                ]
            },
        )

    provider = _provider(handler)
    text = await provider.complete(system="s", user_text="u", images=[b"x"], model="m")
    await provider.aclose()

    assert text == '{"a": 1}'


@pytest.mark.asyncio
async def test_fatal_status_is_not_retried():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(403, text="forbidden")

    provider = _provider(handler, max_retries=3)
    with pytest.raises(ProviderError):
        await provider.complete(system="s", user_text="u", images=[b"x"], model="m")
    await provider.aclose()

    assert len(calls) == 1


@pytest.mark.asyncio
async def test_server_error_is_retried_then_succeeds():
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(500, text="boom")
        return _ok('{"style_terms": []}')

    provider = _provider(handler, max_retries=3)
    text = await provider.complete(system="s", user_text="u", images=[b"x"], model="m")
    await provider.aclose()

    assert len(calls) == 3
    assert text == '{"style_terms": []}'


@pytest.mark.asyncio
async def test_retries_exhausted_surfaces_as_provider_error():
    def handler(request):
        return httpx.Response(503, text="unavailable")

    provider = _provider(handler, max_retries=1)
    with pytest.raises(ProviderError):
        await provider.complete(system="s", user_text="u", images=[b"x"], model="m")
    await provider.aclose()


@pytest.mark.asyncio
async def test_unexpected_response_shape_is_a_provider_error():
    def handler(request):
        return httpx.Response(200, json={"unexpected": True})

    provider = _provider(handler, max_retries=0)
    with pytest.raises(ProviderError):
        await provider.complete(system="s", user_text="u", images=[b"x"], model="m")
    await provider.aclose()


@pytest.mark.asyncio
async def test_transport_failure_is_retried():
    calls = []

    def handler(request):
        calls.append(1)
        raise httpx.ConnectError("no route")

    provider = _provider(handler, max_retries=2)
    # 重试耗尽后统一收敛成 ProviderError —— 调用方只需认识一种失败。
    with pytest.raises(ProviderError):
        await provider.complete(system="s", user_text="u", images=[b"x"], model="m")
    await provider.aclose()

    assert len(calls) == 3


@pytest.mark.asyncio
async def test_retry_after_header_is_parsed_from_a_429():
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) < 2:
            return httpx.Response(429, headers={"Retry-After": "0"}, text="slow down")
        return _ok('{"style_terms": []}')

    provider = _provider(handler, max_retries=3, retry_wait=30.0)
    text = await provider.complete(system="s", user_text="u", images=[b"x"], model="m")
    await provider.aclose()

    assert len(calls) == 2
    assert text == '{"style_terms": []}'


class _RetryState:
    """tenacity 的 RetryCallState 在测试里只需要这两样。"""

    def __init__(self, exc, attempt):
        self.outcome = SimpleNamespace(exception=lambda: exc)
        self.attempt_number = attempt


def _provider_for_wait(retry_wait=2.0):
    return OpenAICompatProvider(
        base_url="https://gateway.invalid/v1", api_key="sk-test", retry_wait=retry_wait
    )


def test_wait_prefers_the_servers_retry_after_over_guessed_backoff():
    """限流窗口端点自己知道，猜不如问。"""
    provider = _provider_for_wait(retry_wait=2.0)

    honoured = provider._wait_seconds(
        _RetryState(RetryableProviderError("429", retry_after=7.0), attempt=1)
    )

    assert honoured == 7.0


def test_retry_after_is_capped_so_one_response_cannot_stall_the_run():
    provider = _provider_for_wait()

    capped = provider._wait_seconds(
        _RetryState(RetryableProviderError("429", retry_after=99999.0), attempt=1)
    )

    assert capped == 30.0  # VISION_RETRY_AFTER_MAX


def test_wait_falls_back_to_exponential_backoff_without_the_header():
    provider = _provider_for_wait(retry_wait=2.0)

    first = provider._wait_seconds(_RetryState(RetryableProviderError("x"), attempt=1))
    third = provider._wait_seconds(_RetryState(RetryableProviderError("x"), attempt=3))

    assert first == 2.0  # 2 * 2^0
    assert third == 8.0  # 2 * 2^2


def test_transport_errors_use_backoff_not_retry_after():
    """网络类异常没有响应头，不能假装有。"""
    provider = _provider_for_wait(retry_wait=2.0)

    wait = provider._wait_seconds(
        _RetryState(httpx.ConnectError("no route"), attempt=2)
    )

    assert wait == 4.0  # 2 * 2^1


# --------------------------------------------------------------------------- #
# 提示词契约
# --------------------------------------------------------------------------- #


def test_prompt_requires_image_indices_and_forbids_invention():
    """提示词是 Q1 的第一道闸，这几句不能被静默改掉。"""
    assert "image_indices" in PROMPT_TEMPLATE
    assert "确实看到" in PROMPT_TEMPLATE
    assert "编造" in PROMPT_TEMPLATE
