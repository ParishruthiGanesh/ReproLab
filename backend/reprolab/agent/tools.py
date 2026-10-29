"""The agent's tools.

Every tool call is recorded with its inputs and outputs, is individually
budgeted, and validates its own arguments. The model names a tool and supplies
arguments; this module decides whether the call is permissible and performs it.
The model never touches the filesystem, the network or the runner directly.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ..db import Database
from ..providers.tavily import TavilyError
from ..security.paths import PathNotAllowed
from ..security.urls import UrlNotAllowed, validate_public_url
from ..runner.workspace import Workspace

MAX_TOOL_OUTPUT_CHARS = 9000


@dataclass
class ToolResult:
    ok: bool
    summary: str
    content: str
    data: dict[str, Any] = field(default_factory=dict)


class ToolBudgetExceeded(RuntimeError):
    """Raised when a tool's own call budget is exhausted."""


class ToolBox:
    """Dispatches validated tool calls for one run."""

    def __init__(
        self,
        *,
        db: Database,
        run_id: str,
        workspace: Workspace,
        tavily: Any | None,
        max_search_calls: int = 5,
        max_extract_calls: int = 3,
        max_file_reads: int = 8,
        max_download_bytes: int = 20_971_520,
        on_source: Callable[[str, str, str, str, str], None] | None = None,
    ) -> None:
        self.db = db
        self.run_id = run_id
        self.workspace = workspace
        self.tavily = tavily
        self.max_download_bytes = max_download_bytes
        self._budgets = {
            "search": max_search_calls,
            "extract": max_extract_calls,
            "read": max_file_reads,
        }
        self._used = {"search": 0, "extract": 0, "read": 0}
        self._seen_urls: set[str] = set()
        self._on_source = on_source

    # -- bookkeeping --------------------------------------------------------
    def _spend(self, bucket: str) -> None:
        if self._used[bucket] >= self._budgets[bucket]:
            raise ToolBudgetExceeded(
                f"the {bucket} budget for this run is exhausted "
                f"({self._budgets[bucket]} calls)"
            )
        self._used[bucket] += 1

    @property
    def remaining(self) -> dict[str, int]:
        return {k: self._budgets[k] - self._used[k] for k in self._budgets}

    def _record(
        self, tool: str, tool_input: Any, result: ToolResult, duration_ms: int
    ) -> None:
        self.db.add_tool_call(
            self.run_id,
            tool,
            tool_input,
            {"ok": result.ok, "summary": result.summary, "data": result.data},
            result.ok,
            duration_ms,
            result.summary,
        )

    def _add_source(
        self, kind: str, title: str, url: str, snippet: str, via: str
    ) -> None:
        self.db.add_source(self.run_id, kind, title, url, snippet, via)
        if url:
            self._seen_urls.add(url)
        if self._on_source is not None:
            try:
                self._on_source(kind, title, url, snippet, via)
            except Exception:  # pragma: no cover - defensive
                pass

    def note_url(self, url: str) -> None:
        if url:
            self._seen_urls.add(url)

    # -- dispatch -----------------------------------------------------------
    def call(self, tool: str, *, query: str = "", url: str = "", path: str = "") -> ToolResult:
        started = time.monotonic()
        tool_input = {"tool": tool, "query": query, "url": url, "path": path}
        try:
            if tool == "search_research_sources":
                result = self._search(query, depth="advanced", kind="research")
            elif tool == "search_documentation":
                result = self._search(query, depth="basic", kind="documentation")
            elif tool == "extract_source_content":
                result = self._extract(url, query)
            elif tool == "read_repository_file":
                result = self._read_file(path)
            elif tool == "inspect_repository_tree":
                result = self._tree()
            else:
                result = ToolResult(False, f"unknown tool {tool!r}", "")
        except (ToolBudgetExceeded, UrlNotAllowed, PathNotAllowed, TavilyError) as exc:
            result = ToolResult(False, f"{type(exc).__name__}: {exc}", str(exc))
        except Exception as exc:  # noqa: BLE001 - reported to the agent, not raised
            result = ToolResult(False, f"tool error: {exc}", str(exc))
        duration_ms = int((time.monotonic() - started) * 1000)
        self._record(tool, tool_input, result, duration_ms)
        return result

    # -- individual tools ---------------------------------------------------
    def _search(self, query: str, *, depth: str, kind: str) -> ToolResult:
        if self.tavily is None:
            return ToolResult(
                False,
                "web search is unavailable: TAVILY_API_KEY is not configured",
                "Tavily is not configured for this instance.",
            )
        query = (query or "").strip()
        if not query:
            return ToolResult(False, "search query was empty", "")
        self._spend("search")
        hits = self.tavily.search(query, max_results=5, search_depth=depth)
        lines = []
        for index, hit in enumerate(hits, 1):
            self._add_source(kind, hit.title, hit.url, hit.content, "tavily_search")
            lines.append(
                f"[{index}] {hit.title}\n    url: {hit.url}\n    {hit.content[:700]}"
            )
        body = "\n".join(lines) or "No results."
        return ToolResult(
            True,
            f"Tavily {kind} search for {query[:90]!r} returned {len(hits)} result(s)",
            body[:MAX_TOOL_OUTPUT_CHARS],
            {"result_count": len(hits), "urls": [h.url for h in hits]},
        )

    def _extract(self, url: str, query: str) -> ToolResult:
        if self.tavily is None:
            return ToolResult(
                False,
                "content extraction is unavailable: TAVILY_API_KEY is not configured",
                "Tavily is not configured for this instance.",
            )
        url = (url or "").strip()
        if not url:
            return ToolResult(False, "extract called without a url", "")
        # Re-validate even though the URL came from our own search results.
        validate_public_url(url)
        self._spend("extract")
        results = self.tavily.extract([url], query=query or "setup and evaluation instructions")
        for result in results:
            if result.ok and result.content.strip():
                self._add_source(
                    "documentation", url, result.url or url, result.content[:1200],
                    "tavily_extract",
                )
                return ToolResult(
                    True,
                    f"extracted {len(result.content)} characters from {url}",
                    result.content[:MAX_TOOL_OUTPUT_CHARS],
                    {"chars": len(result.content)},
                )
        error = next((r.error for r in results if r.error), "no content returned")
        return ToolResult(False, f"extraction failed for {url}: {error}", error)

    def _read_file(self, path: str) -> ToolResult:
        path = (path or "").strip()
        if not path:
            return ToolResult(False, "read_repository_file called without a path", "")
        self._spend("read")
        content = self.workspace.read_file(path)
        self._add_source(
            "repository_file", path, "", content[:600], "repository_clone"
        )
        return ToolResult(
            True,
            f"read {path} ({len(content)} characters)",
            f"--- {path} ---\n{content}"[:MAX_TOOL_OUTPUT_CHARS],
            {"path": path, "chars": len(content)},
        )

    def _tree(self) -> ToolResult:
        entries = self.workspace.tree()
        interesting = self.workspace.interesting_files()
        body = (
            f"Notable files present: {', '.join(interesting) or '(none)'}\n\n"
            "Repository files:\n" + "\n".join(entries)
        )
        return ToolResult(
            True,
            f"listed {len(entries)} repository file(s)",
            body[:MAX_TOOL_OUTPUT_CHARS],
            {"entry_count": len(entries), "notable": interesting},
        )
