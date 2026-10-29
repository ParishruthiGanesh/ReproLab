"""NVIDIA Nemotron inference via Nebius Token Factory.

Token Factory is the *inference* layer: it serves the NVIDIA open-source model
that plans the experiment, reads retrieved material, diagnoses failures and
drafts the report. It never executes repository code -- that happens in the
isolated runner.

Two things are deliberate here:

* The configured model id is validated against the live ``GET /v1/models``
  list for the caller's own account, because published Nemotron ids differ in
  casing between Nebius pages and not every account has every model.
* Every model response that drives behaviour is parsed and validated against
  a Pydantic schema before anything acts on it. Unvalidated text never
  becomes a command, a path or a metric.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Type, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)

# Nemotron (and other reasoning models) may wrap private reasoning in these
# tags. ReproLab strips and discards it: hidden chain-of-thought is never
# stored, logged or displayed.
_REASONING_BLOCK = re.compile(
    r"<(think|thinking|reasoning)>.*?</\1>", re.DOTALL | re.IGNORECASE
)
_UNCLOSED_REASONING = re.compile(
    r"<(think|thinking|reasoning)>.*\Z", re.DOTALL | re.IGNORECASE
)
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class NebiusError(RuntimeError):
    """Nebius Token Factory could not serve the request."""


class ModelNotAvailable(NebiusError):
    """The configured model is not available to this account."""


class StructuredResponseError(NebiusError):
    """The model's response did not validate against the expected schema."""


@dataclass
class ModelResolution:
    requested: str
    resolved: str
    available_nemotron: tuple[str, ...]
    substituted: bool
    note: str = ""


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    calls: int = 0

    def add(self, data: dict[str, Any] | None) -> None:
        self.calls += 1
        if not data:
            return
        self.prompt_tokens += int(data.get("prompt_tokens") or 0)
        self.completion_tokens += int(data.get("completion_tokens") or 0)


def strip_reasoning(text: str) -> str:
    """Remove private reasoning blocks from a model response."""
    cleaned = _REASONING_BLOCK.sub("", text or "")
    cleaned = _UNCLOSED_REASONING.sub("", cleaned)
    return cleaned.strip()


def extract_json_object(text: str) -> Any:
    """Pull the first JSON value out of a model response.

    Handles fenced blocks and leading prose, because an OpenAI-compatible
    endpoint's JSON mode is advisory on some models.
    """
    cleaned = strip_reasoning(text)
    if not cleaned:
        raise StructuredResponseError("model returned an empty response")

    candidates: list[str] = []
    fence = _FENCE.search(cleaned)
    if fence:
        candidates.append(fence.group(1).strip())
    candidates.append(cleaned)
    for opener, closer in (("{", "}"), ("[", "]")):
        start = cleaned.find(opener)
        end = cleaned.rfind(closer)
        if start != -1 and end > start:
            candidates.append(cleaned[start : end + 1])

    last_error: Exception | None = None
    for candidate in candidates:
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except json.JSONDecodeError as exc:
            last_error = exc
            continue
    raise StructuredResponseError(
        f"model response is not valid JSON: {last_error}. "
        f"First 200 characters: {cleaned[:200]!r}"
    )


class NebiusClient:
    """Minimal OpenAI-compatible client for Nebius Token Factory."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        autoselect: bool = True,
        timeout: float = 180.0,
    ) -> None:
        if not api_key:
            raise NebiusError("NEBIUS_API_KEY is not set")
        self._api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.requested_model = model
        self.autoselect = autoselect
        self.timeout = timeout
        self._resolution: ModelResolution | None = None
        self.usage = Usage()

    # -- transport ----------------------------------------------------------
    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.base_url,
            timeout=self.timeout,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
                "User-Agent": "ReproLab/1.0",
            },
        )

    def list_models(self) -> list[dict[str, Any]]:
        """``GET /v1/models`` -- what this account can actually use."""
        try:
            with self._client() as client:
                response = client.get("/models")
        except httpx.HTTPError as exc:
            raise NebiusError(f"cannot reach Nebius Token Factory: {exc}") from exc
        if response.status_code in (401, 403):
            raise NebiusError(
                "Nebius Token Factory rejected the API key "
                f"(HTTP {response.status_code}). Check NEBIUS_API_KEY."
            )
        if response.status_code >= 400:
            raise NebiusError(
                f"GET /models failed with HTTP {response.status_code}: "
                f"{response.text[:300]}"
            )
        payload = response.json()
        data = payload.get("data")
        if not isinstance(data, list):
            raise NebiusError("GET /models returned no 'data' list")
        return [m for m in data if isinstance(m, dict) and m.get("id")]

    # -- model resolution ---------------------------------------------------
    def resolve_model(self, *, force: bool = False) -> ModelResolution:
        """Validate the configured model against the live catalogue.

        Published Nemotron ids differ in casing between Nebius pages, so the
        live list is the authority. If the exact id is missing, a
        case-insensitive match is tried first; only then (and only when
        autoselect is on) does ReproLab substitute another NVIDIA Nemotron
        model the account can use.
        """
        if self._resolution is not None and not force:
            return self._resolution

        models = self.list_models()
        ids = [str(m["id"]) for m in models]
        nemotron = tuple(sorted(i for i in ids if "nemotron" in i.lower()))

        if self.requested_model in ids:
            self._resolution = ModelResolution(
                requested=self.requested_model,
                resolved=self.requested_model,
                available_nemotron=nemotron,
                substituted=False,
            )
            return self._resolution

        lowered = {i.lower(): i for i in ids}
        exact_ci = lowered.get(self.requested_model.lower())
        if exact_ci:
            self._resolution = ModelResolution(
                requested=self.requested_model,
                resolved=exact_ci,
                available_nemotron=nemotron,
                substituted=True,
                note=(
                    f"NEBIUS_MODEL={self.requested_model!r} matched {exact_ci!r} "
                    "in the live model list apart from capitalisation."
                ),
            )
            return self._resolution

        if not self.autoselect:
            raise ModelNotAvailable(
                f"NEBIUS_MODEL={self.requested_model!r} is not in this account's "
                f"model list. Nemotron models available to you: "
                f"{', '.join(nemotron) or '(none)'}. Set NEBIUS_MODEL to one of "
                "them, or set NEBIUS_MODEL_AUTOSELECT=true."
            )
        if not nemotron:
            raise ModelNotAvailable(
                f"NEBIUS_MODEL={self.requested_model!r} is unavailable and this "
                "account has no NVIDIA Nemotron model. ReproLab requires an "
                "NVIDIA open-source model. Enable one in Nebius Token Factory."
            )
        # Prefer the closest name to what was asked for, then the first id.
        wanted = re.sub(r"[^a-z0-9]", "", self.requested_model.lower())
        best = min(
            nemotron,
            key=lambda i: (
                0 if re.sub(r"[^a-z0-9]", "", i.lower()) == wanted else 1,
                -_overlap(wanted, re.sub(r"[^a-z0-9]", "", i.lower())),
                len(i),
            ),
        )
        self._resolution = ModelResolution(
            requested=self.requested_model,
            resolved=best,
            available_nemotron=nemotron,
            substituted=True,
            note=(
                f"NEBIUS_MODEL={self.requested_model!r} is not available to this "
                f"account; using the Nemotron model {best!r} instead "
                "(NEBIUS_MODEL_AUTOSELECT=true)."
            ),
        )
        return self._resolution

    @property
    def model(self) -> str:
        return self.resolve_model().resolved

    # -- chat ---------------------------------------------------------------
    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.2,
        max_tokens: int = 2600,
        json_mode: bool = False,
        retries: int = 2,
    ) -> str:
        """One chat completion. Returns the response text, reasoning stripped."""
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}

        last_error: Exception | None = None
        for attempt in range(retries + 1):
            try:
                with self._client() as client:
                    response = client.post("/chat/completions", json=body)
            except httpx.HTTPError as exc:
                last_error = NebiusError(f"Nebius request failed: {exc}")
                time.sleep(min(2**attempt, 8))
                continue
            if response.status_code == 429 or response.status_code >= 500:
                last_error = NebiusError(
                    f"Nebius returned HTTP {response.status_code}: "
                    f"{response.text[:200]}"
                )
                time.sleep(min(2**attempt, 8))
                continue
            if response.status_code >= 400:
                raise NebiusError(
                    f"Nebius returned HTTP {response.status_code}: "
                    f"{response.text[:400]}"
                )
            payload = response.json()
            self.usage.add(payload.get("usage"))
            choices = payload.get("choices")
            if not isinstance(choices, list) or not choices:
                raise NebiusError("Nebius response contained no choices")
            message = choices[0].get("message") or {}
            content = message.get("content")
            if not isinstance(content, str):
                raise NebiusError("Nebius response had no text content")
            return strip_reasoning(content)
        raise last_error or NebiusError("Nebius request failed")

    # -- validated structured responses -------------------------------------
    def structured(
        self,
        schema: Type[T],
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.1,
        max_tokens: int = 2600,
        max_attempts: int = 3,
    ) -> T:
        """Get a response validated against ``schema``.

        On a validation failure the error is fed back to the model, bounded by
        ``max_attempts``. If it never validates, the caller gets an exception
        rather than a half-parsed object -- the agent then records a failure
        instead of acting on malformed output.
        """
        conversation = list(messages)
        schema_json = json.dumps(schema.model_json_schema(), indent=2)
        conversation.append(
            {
                "role": "system",
                "content": (
                    "Reply with a single JSON object and nothing else. No prose, "
                    "no markdown fences. It must validate against this JSON "
                    f"Schema:\n{schema_json}"
                ),
            }
        )
        last_error = ""
        for attempt in range(1, max_attempts + 1):
            raw = self.chat(
                conversation,
                temperature=temperature,
                max_tokens=max_tokens,
                json_mode=True,
            )
            try:
                data = extract_json_object(raw)
                return schema.model_validate(data)
            except (StructuredResponseError, ValidationError) as exc:
                last_error = str(exc)[:1200]
                if attempt == max_attempts:
                    break
                conversation.append({"role": "assistant", "content": raw[:4000]})
                conversation.append(
                    {
                        "role": "user",
                        "content": (
                            "That response did not validate. Error:\n"
                            f"{last_error}\n\nReturn only a corrected JSON object "
                            "matching the schema."
                        ),
                    }
                )
        raise StructuredResponseError(
            f"model did not produce a valid {schema.__name__} after "
            f"{max_attempts} attempts. Last error: {last_error}"
        )

    # -- diagnostics --------------------------------------------------------
    def healthcheck(self) -> dict[str, Any]:
        """Resolve the model and make one tiny real completion."""
        resolution = self.resolve_model(force=True)
        started = time.monotonic()
        reply = self.chat(
            [
                {
                    "role": "user",
                    "content": "Reply with the single word: ready",
                }
            ],
            max_tokens=16,
            temperature=0.0,
        )
        return {
            "ok": True,
            "requested_model": resolution.requested,
            "resolved_model": resolution.resolved,
            "substituted": resolution.substituted,
            "note": resolution.note,
            "available_nemotron_models": list(resolution.available_nemotron),
            "latency_ms": int((time.monotonic() - started) * 1000),
            "reply": reply[:120],
        }


def _overlap(a: str, b: str) -> int:
    """Length of the longest common prefix, used to rank similar model ids."""
    count = 0
    for x, y in zip(a, b):
        if x != y:
            break
        count += 1
    return count
