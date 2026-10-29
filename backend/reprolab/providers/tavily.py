"""Tavily retrieval.

Tavily is how ReproLab finds material it was not given: the paper's project
page, the library's official installation and evaluation documentation, and
the known errors behind a specific failure message. Three call sites:

1. ``research_sources``   -- before planning, to locate the paper's own pages
                             and the project's official docs.
2. ``documentation``      -- to resolve concrete setup questions (which extras
                             a package needs, what the documented smoke test
                             is).
3. ``troubleshoot``       -- after a real failure, keyed on the actual error
                             text from the runner, to inform a repair.

Everything Tavily returns is untrusted web content. It is recorded as a
*source* and quoted to the model as data, never as instructions.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx

from ..security.redact import redact
from ..security.urls import UrlNotAllowed, validate_public_url

MAX_SNIPPET_CHARS = 1500
MAX_EXTRACT_CHARS = 18_000


class TavilyError(RuntimeError):
    """Tavily could not serve the request."""


@dataclass
class SearchHit:
    title: str
    url: str
    content: str
    score: float
    published_date: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "url": self.url,
            "content": self.content,
            "score": self.score,
            "published_date": self.published_date,
        }


@dataclass
class ExtractResult:
    url: str
    content: str
    ok: bool
    error: str = ""


class TavilyClient:
    """Minimal Tavily REST client with SSRF-checked result URLs."""

    def __init__(
        self, *, api_key: str, base_url: str = "https://api.tavily.com", timeout: float = 60.0
    ) -> None:
        if not api_key:
            raise TavilyError("TAVILY_API_KEY is not set")
        self._api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.calls = 0

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        try:
            with httpx.Client(
                base_url=self.base_url,
                timeout=self.timeout,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                    "User-Agent": "ReproLab/1.0",
                },
            ) as client:
                response = client.post(path, json=body)
        except httpx.HTTPError as exc:
            raise TavilyError(f"cannot reach Tavily: {exc}") from exc
        if response.status_code in (401, 403):
            raise TavilyError(
                f"Tavily rejected the API key (HTTP {response.status_code}). "
                "Check TAVILY_API_KEY."
            )
        if response.status_code == 429:
            raise TavilyError("Tavily rate limit reached (HTTP 429)")
        if response.status_code >= 400:
            raise TavilyError(
                f"Tavily {path} failed with HTTP {response.status_code}: "
                f"{redact(response.text[:300])}"
            )
        payload = response.json()
        if not isinstance(payload, dict):
            raise TavilyError(f"Tavily {path} returned an unexpected payload")
        return payload

    # -- search -------------------------------------------------------------
    def search(
        self,
        query: str,
        *,
        max_results: int = 5,
        search_depth: str = "basic",
        include_domains: list[str] | None = None,
        exclude_domains: list[str] | None = None,
    ) -> list[SearchHit]:
        """``POST /search``. Result URLs are SSRF-validated before being kept."""
        query = (query or "").strip()
        if not query:
            raise TavilyError("search query is empty")
        body: dict[str, Any] = {
            "query": query[:380],
            "max_results": max(1, min(int(max_results), 10)),
            "search_depth": search_depth if search_depth in {"basic", "advanced"} else "basic",
            "include_answer": False,
            "include_raw_content": False,
            "include_images": False,
        }
        if include_domains:
            body["include_domains"] = include_domains[:10]
        if exclude_domains:
            body["exclude_domains"] = exclude_domains[:10]

        payload = self._post("/search", body)
        raw_results = payload.get("results")
        if not isinstance(raw_results, list):
            raise TavilyError("Tavily /search returned no 'results' list")

        hits: list[SearchHit] = []
        for item in raw_results:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "")
            try:
                validate_public_url(url, resolve=False)
            except UrlNotAllowed:
                # A result pointing at a private address is dropped, not shown.
                continue
            hits.append(
                SearchHit(
                    title=redact(str(item.get("title") or ""))[:300],
                    url=url,
                    content=redact(str(item.get("content") or ""))[:MAX_SNIPPET_CHARS],
                    score=float(item.get("score") or 0.0),
                    published_date=str(item.get("published_date") or "")[:40],
                )
            )
        return hits

    # -- extract ------------------------------------------------------------
    def extract(
        self, urls: list[str], *, query: str = "", extract_depth: str = "basic"
    ) -> list[ExtractResult]:
        """``POST /extract``. Each URL is SSRF-validated before submission."""
        safe: list[str] = []
        rejected: list[ExtractResult] = []
        for url in urls[:5]:
            try:
                safe.append(validate_public_url(url).url)
            except UrlNotAllowed as exc:
                rejected.append(ExtractResult(url=url, content="", ok=False, error=str(exc)))
        if not safe:
            return rejected

        body: dict[str, Any] = {
            "urls": safe,
            "extract_depth": extract_depth if extract_depth in {"basic", "advanced"} else "basic",
            "format": "markdown",
            "include_images": False,
        }
        if query:
            body["query"] = query[:380]
        payload = self._post("/extract", body)

        results: list[ExtractResult] = list(rejected)
        for item in payload.get("results") or []:
            if not isinstance(item, dict):
                continue
            results.append(
                ExtractResult(
                    url=str(item.get("url") or ""),
                    content=redact(str(item.get("raw_content") or ""))[:MAX_EXTRACT_CHARS],
                    ok=True,
                )
            )
        # A 200 response can still report per-URL failures.
        for item in payload.get("failed_results") or []:
            if not isinstance(item, dict):
                continue
            results.append(
                ExtractResult(
                    url=str(item.get("url") or ""),
                    content="",
                    ok=False,
                    error=redact(str(item.get("error") or "extraction failed"))[:300],
                )
            )
        return results

    # -- task-shaped helpers -----------------------------------------------
    def research_sources(
        self, *, paper_url: str, repo: str, goal: str, max_results: int = 5
    ) -> list[SearchHit]:
        """Find the paper's own pages and the project's official documentation."""
        topic = goal.strip() or "reproduce the published experiment"
        query = (
            f"{repo} official documentation installation and evaluation "
            f"instructions, paper {paper_url}, {topic}"
        )
        return self.search(query, max_results=max_results, search_depth="advanced")

    def documentation(self, question: str, *, max_results: int = 4) -> list[SearchHit]:
        return self.search(
            f"official documentation: {question}", max_results=max_results
        )

    def troubleshoot(
        self, *, error_text: str, repo: str, max_results: int = 4
    ) -> list[SearchHit]:
        """Search on the real error text produced by the runner."""
        condensed = " ".join((error_text or "").split())[:280]
        return self.search(
            f"{repo} error: {condensed}", max_results=max_results
        )

    def healthcheck(self) -> dict[str, Any]:
        started = time.monotonic()
        hits = self.search("Tavily API status", max_results=1)
        return {
            "ok": True,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "results": len(hits),
            "first_url": hits[0].url if hits else "",
        }
