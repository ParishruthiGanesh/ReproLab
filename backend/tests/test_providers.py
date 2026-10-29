"""Provider response validation.

The model is reached over an OpenAI-compatible API; these tests drive that
transport with scripted responses so validation behaviour is deterministic.
"""

from __future__ import annotations

import json

import httpx
import pytest
from pydantic import BaseModel, Field

from reprolab.providers.nebius import (
    ModelNotAvailable,
    NebiusClient,
    NebiusError,
    StructuredResponseError,
    extract_json_object,
    strip_reasoning,
)
from reprolab.providers.tavily import TavilyClient, TavilyError


class Sample(BaseModel):
    name: str
    count: int = Field(ge=0)


def _models_payload(ids: list[str]) -> dict:
    return {
        "object": "list",
        "data": [{"id": i, "object": "model", "created": 1, "owned_by": "x"} for i in ids],
    }


def _completion(text: str) -> dict:
    return {
        "choices": [{"message": {"role": "assistant", "content": text}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def make_client(monkeypatch, *, models: list[str], replies: list[str], model: str,
                autoselect: bool = True) -> NebiusClient:
    queue = list(replies)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=_models_payload(models))
        if request.url.path.endswith("/chat/completions"):
            if not queue:
                return httpx.Response(500, text="no scripted reply left")
            return httpx.Response(200, json=_completion(queue.pop(0)))
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    client = NebiusClient(
        api_key="test-key-value", base_url="https://api.example/v1",
        model=model, autoselect=autoselect,
    )
    monkeypatch.setattr(
        client, "_client",
        lambda: httpx.Client(transport=transport, base_url=client.base_url),
    )
    return client


class TestReasoningIsNeverSurfaced:
    def test_think_blocks_are_stripped(self) -> None:
        raw = "<think>private deliberation</think>\n{\"a\": 1}"
        assert "private deliberation" not in strip_reasoning(raw)
        assert strip_reasoning(raw) == '{"a": 1}'

    def test_unclosed_reasoning_is_stripped(self) -> None:
        assert strip_reasoning("answer<think>trailing") == "answer"

    @pytest.mark.parametrize("tag", ["think", "thinking", "reasoning"])
    def test_all_reasoning_tags(self, tag: str) -> None:
        assert strip_reasoning(f"<{tag}>x</{tag}>ok") == "ok"


class TestJsonExtraction:
    def test_plain_object(self) -> None:
        assert extract_json_object('{"a": 1}') == {"a": 1}

    def test_fenced_block(self) -> None:
        assert extract_json_object('```json\n{"a": 2}\n```') == {"a": 2}

    def test_object_with_surrounding_prose(self) -> None:
        assert extract_json_object('Here it is:\n{"a": 3}\nhope that helps') == {"a": 3}

    def test_reasoning_plus_fence(self) -> None:
        assert extract_json_object('<think>hmm</think>```\n{"a": 4}\n```') == {"a": 4}

    def test_empty_response_raises(self) -> None:
        with pytest.raises(StructuredResponseError, match="empty"):
            extract_json_object("")

    def test_non_json_raises(self) -> None:
        with pytest.raises(StructuredResponseError, match="not valid JSON"):
            extract_json_object("I cannot help with that.")


class TestModelResolution:
    def test_exact_id_is_used(self, monkeypatch) -> None:
        client = make_client(
            monkeypatch, models=["nvidia/Nemotron-3_5-Lightning"], replies=[],
            model="nvidia/Nemotron-3_5-Lightning",
        )
        resolution = client.resolve_model()
        assert resolution.resolved == "nvidia/Nemotron-3_5-Lightning"
        assert resolution.substituted is False

    def test_case_mismatch_is_corrected(self, monkeypatch) -> None:
        # Published Nemotron ids differ in casing between Nebius pages, so the
        # live list is treated as the authority.
        client = make_client(
            monkeypatch, models=["nvidia/Nemotron-3.5-Super-VL"], replies=[],
            model="nvidia/nemotron-3.5-super-vl",
        )
        resolution = client.resolve_model()
        assert resolution.resolved == "nvidia/Nemotron-3.5-Super-VL"
        assert resolution.substituted is True
        assert "capitalisation" in resolution.note

    def test_unavailable_model_substitutes_another_nemotron(self, monkeypatch) -> None:
        client = make_client(
            monkeypatch,
            models=["nvidia/Nemotron-3-Ultra-550b-a55b", "meta/other-model"],
            replies=[], model="nvidia/Does-Not-Exist",
        )
        resolution = client.resolve_model()
        assert resolution.resolved == "nvidia/Nemotron-3-Ultra-550b-a55b"
        assert resolution.substituted is True
        assert resolution.available_nemotron == ("nvidia/Nemotron-3-Ultra-550b-a55b",)

    def test_autoselect_off_raises_with_guidance(self, monkeypatch) -> None:
        client = make_client(
            monkeypatch, models=["nvidia/Nemotron-3-Ultra-550b-a55b"], replies=[],
            model="nvidia/Missing", autoselect=False,
        )
        with pytest.raises(ModelNotAvailable, match="not in this account's model list"):
            client.resolve_model()

    def test_no_nemotron_available_is_an_error(self, monkeypatch) -> None:
        # ReproLab requires an NVIDIA open-source model; it does not silently
        # fall back to a non-NVIDIA one.
        client = make_client(
            monkeypatch, models=["meta/llama-3", "deepseek-ai/DeepSeek-R1"],
            replies=[], model="nvidia/Missing",
        )
        with pytest.raises(ModelNotAvailable, match="no NVIDIA Nemotron model"):
            client.resolve_model()

    def test_bad_api_key_is_reported_clearly(self, monkeypatch) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, text="unauthorized")

        client = NebiusClient(
            api_key="bad", base_url="https://api.example/v1", model="nvidia/x"
        )
        transport = httpx.MockTransport(handler)
        monkeypatch.setattr(
            client, "_client",
            lambda: httpx.Client(transport=transport, base_url=client.base_url),
        )
        with pytest.raises(NebiusError, match="rejected the API key"):
            client.list_models()


class TestStructuredResponses:
    def test_valid_response_parses(self, monkeypatch) -> None:
        client = make_client(
            monkeypatch, models=["nvidia/N"], model="nvidia/N",
            replies=['{"name": "ok", "count": 3}'],
        )
        result = client.structured(Sample, [{"role": "user", "content": "go"}])
        assert result.name == "ok" and result.count == 3

    def test_invalid_then_valid_retries(self, monkeypatch) -> None:
        client = make_client(
            monkeypatch, models=["nvidia/N"], model="nvidia/N",
            replies=['{"name": "ok", "count": -5}', '{"name": "ok", "count": 1}'],
        )
        result = client.structured(Sample, [{"role": "user", "content": "go"}])
        assert result.count == 1

    def test_persistently_invalid_raises_rather_than_half_parsing(
        self, monkeypatch
    ) -> None:
        client = make_client(
            monkeypatch, models=["nvidia/N"], model="nvidia/N",
            replies=["not json", "still not json", "{\"nope\": true}"],
        )
        with pytest.raises(StructuredResponseError, match="did not produce a valid"):
            client.structured(
                Sample, [{"role": "user", "content": "go"}], max_attempts=3
            )

    def test_prose_wrapped_json_is_accepted(self, monkeypatch) -> None:
        client = make_client(
            monkeypatch, models=["nvidia/N"], model="nvidia/N",
            replies=['<think>deliberating</think>Sure:\n```json\n{"name":"x","count":0}\n```'],
        )
        assert client.structured(Sample, [{"role": "user", "content": "go"}]).name == "x"

    def test_usage_is_tracked(self, monkeypatch) -> None:
        client = make_client(
            monkeypatch, models=["nvidia/N"], model="nvidia/N",
            replies=['{"name":"x","count":0}'],
        )
        client.structured(Sample, [{"role": "user", "content": "go"}])
        assert client.usage.calls >= 1
        assert client.usage.prompt_tokens == 10


class TestTavily:
    def _client(self, monkeypatch, handler) -> TavilyClient:
        client = TavilyClient(api_key="tvly-test-key-value", base_url="https://api.example")
        transport = httpx.MockTransport(handler)
        original = httpx.Client

        def factory(*args, **kwargs):
            kwargs["transport"] = transport
            return original(*args, **kwargs)

        monkeypatch.setattr(httpx, "Client", factory)
        return client

    def test_results_pointing_at_private_addresses_are_dropped(self, monkeypatch) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"title": "ok", "url": "https://example.com/a", "content": "c", "score": 0.9},
                        {"title": "ssrf", "url": "http://169.254.169.254/latest", "content": "c", "score": 0.8},
                        {"title": "local", "url": "http://127.0.0.1:8000/admin", "content": "c", "score": 0.7},
                    ]
                },
            )

        client = self._client(monkeypatch, handler)
        hits = client.search("anything")
        assert [h.url for h in hits] == ["https://example.com/a"]

    def test_failed_results_are_surfaced(self, monkeypatch) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "results": [],
                    "failed_results": [
                        {"url": "https://example.com/x", "error": "timeout"}
                    ],
                },
            )

        client = self._client(monkeypatch, handler)
        results = client.extract(["https://example.com/x"])
        assert any(not r.ok and r.error == "timeout" for r in results)

    def test_extract_rejects_private_url_without_calling_out(self, monkeypatch) -> None:
        called = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            called["n"] += 1
            return httpx.Response(200, json={"results": []})

        client = self._client(monkeypatch, handler)
        results = client.extract(["http://169.254.169.254/latest"])
        assert called["n"] == 0
        assert results and not results[0].ok

    def test_bad_key_message(self, monkeypatch) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, text="bad key")

        client = self._client(monkeypatch, handler)
        with pytest.raises(TavilyError, match="rejected the API key"):
            client.search("x")

    def test_missing_key_is_refused_at_construction(self) -> None:
        with pytest.raises(TavilyError, match="TAVILY_API_KEY"):
            TavilyClient(api_key="")
