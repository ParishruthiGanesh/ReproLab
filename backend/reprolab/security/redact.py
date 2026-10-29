"""Secret redaction.

Logs, reports and manifests are written to disk and shown in a browser.
Nothing derived from the environment may carry a credential, so every string
headed for persistence or display passes through ``redact``.
"""

from __future__ import annotations

import os
import re

PLACEHOLDER = "[REDACTED]"

# Environment variables whose values must never be echoed.
SENSITIVE_ENV_VARS = (
    "NEBIUS_API_KEY",
    "TAVILY_API_KEY",
    "REPROLAB_API_TOKEN",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "HF_TOKEN",
    "HUGGING_FACE_HUB_TOKEN",
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_ACCESS_KEY_ID",
    "AWS_SESSION_TOKEN",
)

# Shapes of credentials that may appear in third-party output even when they
# are not in our own environment.
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\btvly-[A-Za-z0-9_\-]{8,}", re.I), PLACEHOLDER),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"), PLACEHOLDER),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"), PLACEHOLDER),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), PLACEHOLDER),
    (re.compile(r"\bhf_[A-Za-z0-9]{16,}"), PLACEHOLDER),
    # JWT-shaped tokens (Nebius keys are JWTs).
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"), PLACEHOLDER),
    # Authorization headers and common key=value spellings.
    (
        re.compile(r"(?i)\b(authorization)\s*:\s*(bearer\s+)?\S+"),
        r"\1: " + PLACEHOLDER,
    ),
    (
        # Matches bare `api_key=...` and JSON-quoted `"api_key": "..."` alike:
        # the key name may be followed by a closing quote before the separator.
        re.compile(
            r"(?i)\b([a-z0-9_\-]*(?:api[_\-]?key|secret|password|passwd|token))"
            r"([\"\']?\s*[:=]\s*)([\"\'])?[^\s\"\',}]{6,}"
        ),
        r"\1\2\3" + PLACEHOLDER,
    ),
    # Credentials embedded in a URL.
    (re.compile(r"(?i)\b(https?://)([^/\s:@]+):([^/\s@]+)@"), r"\1\2:" + PLACEHOLDER + "@"),
)

_MIN_ENV_VALUE_LENGTH = 6


def _env_secrets() -> list[str]:
    values: list[str] = []
    for name in SENSITIVE_ENV_VARS:
        value = os.environ.get(name)
        if value and len(value.strip()) >= _MIN_ENV_VALUE_LENGTH:
            values.append(value.strip())
    # Longest first, so a prefix never masks a longer secret.
    return sorted(set(values), key=len, reverse=True)


def redact(text: str, *, extra_secrets: tuple[str, ...] = ()) -> str:
    """Return ``text`` with known secrets and credential-shaped tokens masked."""
    if not text:
        return text
    result = text
    for secret in sorted(
        {s for s in extra_secrets if s and len(s) >= _MIN_ENV_VALUE_LENGTH}
        | set(_env_secrets()),
        key=len,
        reverse=True,
    ):
        result = result.replace(secret, PLACEHOLDER)
    for pattern, replacement in _PATTERNS:
        result = pattern.sub(replacement, result)
    return result


def redact_bytes(data: bytes, *, extra_secrets: tuple[str, ...] = ()) -> bytes:
    text = data.decode("utf-8", errors="replace")
    return redact(text, extra_secrets=extra_secrets).encode("utf-8")


def redact_obj(obj: object, *, extra_secrets: tuple[str, ...] = ()) -> object:
    """Recursively redact strings inside dicts/lists/tuples."""
    if isinstance(obj, str):
        return redact(obj, extra_secrets=extra_secrets)
    if isinstance(obj, dict):
        return {
            key: redact_obj(value, extra_secrets=extra_secrets)
            for key, value in obj.items()
        }
    if isinstance(obj, (list, tuple)):
        rendered = [redact_obj(item, extra_secrets=extra_secrets) for item in obj]
        return type(obj)(rendered) if isinstance(obj, tuple) else rendered
    return obj
