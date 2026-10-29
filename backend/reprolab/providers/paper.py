"""Paper retrieval.

Papers arrive as a URL. ReproLab fetches them through the SSRF-checked HTTP
path, converts HTML to readable text locally, and falls back to Tavily's
extraction for PDFs and pages that do not yield useful text. arXiv links are
normalised to their abstract page, which carries the title and abstract as
plain HTML.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from urllib.parse import urlparse

from ..security.redact import redact
from ..security.urls import fetch_text, validate_public_url

_SCRIPT_STYLE = re.compile(
    r"<(script|style|noscript|svg)\b[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE
)
_TAGS = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t]+")
_BLANKS = re.compile(r"\n{3,}")
_ARXIV_ID = re.compile(r"arxiv\.org/(?:abs|pdf|html)/([0-9]{4}\.[0-9]{4,5}(?:v\d+)?)", re.I)

MAX_PAPER_CHARS = 20_000


@dataclass
class PaperContent:
    url: str
    final_url: str
    title: str
    text: str
    source: str  # "http", "tavily_extract", or "unavailable"
    error: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.text.strip())


def html_to_text(raw: str) -> str:
    stripped = _SCRIPT_STYLE.sub(" ", raw)
    stripped = re.sub(r"<br\s*/?>", "\n", stripped, flags=re.IGNORECASE)
    stripped = re.sub(r"</(p|div|h[1-6]|li|tr)>", "\n", stripped, flags=re.IGNORECASE)
    stripped = _TAGS.sub(" ", stripped)
    text = html.unescape(stripped)
    text = _WS.sub(" ", text)
    text = "\n".join(line.strip() for line in text.splitlines())
    return _BLANKS.sub("\n\n", text).strip()


def extract_title(raw_html: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", raw_html, re.DOTALL | re.IGNORECASE)
    if not match:
        return ""
    return _WS.sub(" ", html.unescape(_TAGS.sub("", match.group(1)))).strip()[:300]


def canonical_paper_url(url: str) -> str:
    """Point arXiv PDF/HTML links at the abstract page, which parses cleanly."""
    arxiv = _ARXIV_ID.search(url or "")
    if arxiv:
        return f"https://arxiv.org/abs/{arxiv.group(1)}"
    return url


def fetch_paper(
    url: str, *, max_bytes: int, tavily=None, goal: str = ""
) -> PaperContent:
    """Fetch a paper's readable text. Never raises; reports failure instead."""
    try:
        validate_public_url(url, resolve=False)
    except Exception as exc:
        return PaperContent(url, url, "", "", "unavailable", error=str(exc))

    target = canonical_paper_url(url)
    is_pdf = urlparse(target).path.lower().endswith(".pdf")

    if not is_pdf:
        try:
            final_url, body, content_type = fetch_text(target, max_bytes=max_bytes)
            raw = body.decode("utf-8", errors="replace")
            if "pdf" not in content_type.lower():
                text = html_to_text(raw)
                if len(text) > 400:
                    return PaperContent(
                        url=url,
                        final_url=final_url,
                        title=extract_title(raw),
                        text=redact(text)[:MAX_PAPER_CHARS],
                        source="http",
                    )
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            last_error = str(exc)
        else:
            last_error = "page yielded too little readable text"
    else:
        last_error = "paper URL is a PDF"

    # Tavily's extractor handles PDFs and JavaScript-rendered pages.
    if tavily is not None:
        try:
            results = tavily.extract(
                [target], query=goal or "paper abstract, method, reported results"
            )
            for result in results:
                if result.ok and len(result.content.strip()) > 200:
                    return PaperContent(
                        url=url,
                        final_url=result.url or target,
                        title="",
                        text=redact(result.content)[:MAX_PAPER_CHARS],
                        source="tavily_extract",
                    )
            failures = [r.error for r in results if not r.ok and r.error]
            if failures:
                last_error = f"{last_error}; Tavily extract: {failures[0]}"
        except Exception as exc:  # noqa: BLE001
            last_error = f"{last_error}; Tavily extract failed: {exc}"

    return PaperContent(
        url=url, final_url=target, title="", text="", source="unavailable",
        error=last_error[:500],
    )
