"""`EmbeddingProvider` 适配层测试。

覆盖提供方接口契约、配置默认值，以及 `Ollama` 实现的请求格式和统一错误封装。
测试使用 `httpx.MockTransport`，不依赖真实的 `Ollama` 服务。
"""

import json

import httpx
import pytest
from pydantic import ValidationError

from app.core.config import settings
from app.infrastructure.models.embedding import (
    EmbeddingProvider,
    get_embedding_provider,
    reset_embedding_provider,
)
from app.infrastructure.models.errors import ModelTimeoutError, ModelUnavailableError
from app.infrastructure.models.ollama_embedding import OllamaEmbeddingProvider
from app.infrastructure.models.openai_compatible_embedding import (
    OpenAICompatibleEmbeddingProvider,
)


class FakeEmbeddingProvider(EmbeddingProvider):
    name = "fake"
    model = "fake-embedding:v1"
    dimensions = 4

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[float(i)] * self.dimensions for i, _ in enumerate(texts)]


async def test_fake_provider_contract() -> None:
    """接口契约：返回与输入等长、每条维度等于 dimensions；空输入返回空。"""
    provider = FakeEmbeddingProvider()

    vectors = await provider.embed(["甲", "乙", "丙"])

    assert len(vectors) == 3
    assert all(len(v) == provider.dimensions for v in vectors)
    assert await provider.embed([]) == []


def test_embedding_config_defaults() -> None:
    """配置字段默认值为 qwen3-embedding:0.6b 和 1024 维。

    断字段定义默认值而非当前环境值，与宿主 .env（可能已切智谱等）解耦。
    """
    from app.core.config import Settings

    assert Settings.model_fields["embedding_model"].default == "qwen3-embedding:0.6b"
    assert Settings.model_fields["embedding_dimensions"].default == 1024


def test_embedding_config_rejects_non_1024_dimensions() -> None:
    """数据库向量列固定 1024 维，启动时拒绝不兼容的环境配置。"""
    from app.core.config import Settings

    with pytest.raises(ValidationError, match="embedding_dimensions"):
        Settings(embedding_dimensions=1536)


@pytest.fixture(autouse=True)
def _reset_provider():
    reset_embedding_provider()
    yield
    reset_embedding_provider()


def _make_provider(handler, **kwargs) -> OllamaEmbeddingProvider:
    return OllamaEmbeddingProvider(
        "http://host.docker.internal:11434",
        "qwen3-embedding:0.6b",
        4,
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


async def test_ollama_embed_success() -> None:
    """批量 embed 成功：路径与请求体正确，返回浮点向量。"""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"embeddings": [[0.1, 0.2, 0.3, 0.4], [0.5, 0.6, 0.7, 0.8]]})

    provider = _make_provider(handler)
    vectors = await provider.embed(["部署步骤", "发布流程"])

    assert vectors == [[0.1, 0.2, 0.3, 0.4], [0.5, 0.6, 0.7, 0.8]]
    req = captured[0]
    assert req.url.path == "/api/embed"
    body = json.loads(req.content)
    assert body == {"model": "qwen3-embedding:0.6b", "input": ["部署步骤", "发布流程"]}


async def test_ollama_embed_empty_input_no_http() -> None:
    """空输入直接返回空，不发 HTTP 请求。"""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"embeddings": []})

    provider = _make_provider(handler)
    assert await provider.embed([]) == []
    assert calls == 0


async def test_ollama_embed_dimension_mismatch_no_retry() -> None:
    """返回维度与配置不符 → ModelUnavailableError，且不重试（配置错误重试无意义）。"""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"embeddings": [[0.1, 0.2]]})

    provider = _make_provider(handler, max_retries=2)
    with pytest.raises(ModelUnavailableError):
        await provider.embed(["测试"])
    assert calls == 1


async def test_ollama_embed_invalid_success_response_wrapped() -> None:
    """HTTP 200 但响应体非法时也统一为 ModelUnavailableError，且不重试。"""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=b"not json")

    provider = _make_provider(handler, max_retries=2)
    with pytest.raises(ModelUnavailableError) as exc_info:
        await provider.embed(["测试"])
    assert isinstance(exc_info.value.__cause__, ValueError)
    assert calls == 1


async def test_ollama_embed_unavailable_wrapped() -> None:
    """Ollama 停机（连接失败）→ ModelUnavailableError，重试 max_retries 次，不漏 httpx 异常。"""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("connection refused", request=request)

    provider = _make_provider(handler, max_retries=2)
    with pytest.raises(ModelUnavailableError) as exc_info:
        await provider.embed(["ping"])
    assert not isinstance(exc_info.value, httpx.HTTPError)
    assert calls == 3


async def test_ollama_embed_timeout_wrapped() -> None:
    """读取超时 → ModelTimeoutError，不漏 httpx.TimeoutException。"""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out", request=request)

    provider = _make_provider(handler, max_retries=0)
    with pytest.raises(ModelTimeoutError):
        await provider.embed(["ping"])


async def test_factory_returns_ollama_embedding(monkeypatch: pytest.MonkeyPatch) -> None:
    """EMBEDDING_PROVIDER=ollama（默认）时工厂返回 OllamaEmbeddingProvider（单例）。"""
    monkeypatch.setattr(settings, "embedding_provider", "ollama")  # 与宿主 .env 解耦
    provider = get_embedding_provider()
    assert isinstance(provider, OllamaEmbeddingProvider)
    assert isinstance(provider, EmbeddingProvider)
    assert provider.model == settings.embedding_model
    assert provider.dimensions == settings.embedding_dimensions
    assert get_embedding_provider() is provider


def _make_openai_provider(handler, **kwargs) -> OpenAICompatibleEmbeddingProvider:
    return OpenAICompatibleEmbeddingProvider(
        "https://open.bigmodel.cn/api/paas/v4",
        "test-api-key",
        "embedding-3",
        4,
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


async def test_openai_embed_success() -> None:
    """批量 embed 成功：路径/鉴权头/请求体（含 dimensions）正确，按 index 排序返回。"""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "data": [
                    {"embedding": [0.5, 0.6, 0.7, 0.8], "index": 1},
                    {"embedding": [0.1, 0.2, 0.3, 0.4], "index": 0},
                ]
            },
        )

    provider = _make_openai_provider(handler)
    vectors = await provider.embed(["部署步骤", "发布流程"])

    assert vectors == [[0.1, 0.2, 0.3, 0.4], [0.5, 0.6, 0.7, 0.8]]
    req = captured[0]
    assert req.url.path == "/api/paas/v4/embeddings"
    assert req.headers["Authorization"] == "Bearer test-api-key"
    body = json.loads(req.content)
    assert body == {
        "model": "embedding-3",
        "input": ["部署步骤", "发布流程"],
        "dimensions": 4,
    }


async def test_openai_embed_http_error_wrapped() -> None:
    """401/500 等非 2xx → ModelUnavailableError，不重试，不漏 httpx 异常。"""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(401, json={"error": {"message": "invalid api key"}})

    provider = _make_openai_provider(handler, max_retries=2)
    with pytest.raises(ModelUnavailableError) as exc_info:
        await provider.embed(["ping"])
    assert not isinstance(exc_info.value, httpx.HTTPError)
    assert calls == 1


async def test_openai_embed_dimension_mismatch_no_retry() -> None:
    """返回维度与 EMBEDDING_DIMENSIONS 不符 → ModelUnavailableError，不重试。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"embedding": [0.1, 0.2], "index": 0}]})

    provider = _make_openai_provider(handler, max_retries=2)
    with pytest.raises(ModelUnavailableError):
        await provider.embed(["测试"])


async def test_openai_embed_invalid_success_response_wrapped() -> None:
    """HTTP 200 但缺少 OpenAI data 字段时统一为 ModelUnavailableError。"""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"unexpected": []})

    provider = _make_openai_provider(handler, max_retries=2)
    with pytest.raises(ModelUnavailableError) as exc_info:
        await provider.embed(["测试"])
    assert isinstance(exc_info.value.__cause__, KeyError)
    assert calls == 1


async def test_openai_embed_timeout_wrapped() -> None:
    """超时 → ModelTimeoutError，不漏 httpx.TimeoutException。"""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out", request=request)

    provider = _make_openai_provider(handler, max_retries=0)
    with pytest.raises(ModelTimeoutError):
        await provider.embed(["ping"])


def test_openai_provider_requires_api_key() -> None:
    """缺 API Key 直接报错（配置错误尽早暴露）。"""
    with pytest.raises(RuntimeError, match="EMBEDDING_API_KEY"):
        OpenAICompatibleEmbeddingProvider(
            "https://open.bigmodel.cn/api/paas/v4", "", "embedding-3", 1024
        )


def test_factory_returns_openai_compatible(monkeypatch: pytest.MonkeyPatch) -> None:
    """EMBEDDING_PROVIDER=openai_compatible 时工厂返回对应实现。"""
    monkeypatch.setattr(settings, "embedding_provider", "openai_compatible")
    monkeypatch.setattr(settings, "embedding_api_key", "k")
    provider = get_embedding_provider()
    assert isinstance(provider, OpenAICompatibleEmbeddingProvider)
    assert provider.model == settings.embedding_model
