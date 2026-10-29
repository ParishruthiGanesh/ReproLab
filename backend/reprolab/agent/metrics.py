"""Verified metric extraction.

Numbers never come from the model. The model points at a log line it believes
contains a result; ReproLab then re-finds that line in the real log and parses
the number itself. If the quoted line is not in the log, the candidate is
discarded and recorded as unverifiable.

This is the difference between "measured" and "narrated".
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .schemas import MetricCandidate

_NUMBER = re.compile(r"[-+]?\d{1,12}(?:[.,]\d+)?(?:[eE][-+]?\d+)?")

# Deterministic parsers for well-known output shapes, applied without the
# model's involvement.
_DETERMINISTIC = (
    (
        "pytest_summary",
        re.compile(
            r"^(?:=+\s*)?(?P<body>(?:\d+\s+(?:passed|failed|error|errors|skipped|"
            r"xfailed|xpassed|warnings?|deselected)(?:,\s*)?)+)",
            re.IGNORECASE,
        ),
    ),
)
_PYTEST_PAIR = re.compile(
    r"(\d+)\s+(passed|failed|errors?|skipped|xfailed|xpassed|deselected)",
    re.IGNORECASE,
)


@dataclass
class VerifiedMetric:
    name: str
    value: float | None
    raw_value: str
    unit: str
    parser: str
    provenance: str
    source_file: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "value": self.value,
            "raw_value": self.raw_value,
            "unit": self.unit,
            "parser": self.parser,
            "provenance": self.provenance,
            "source_file": self.source_file,
        }


def _normalise(line: str) -> str:
    return " ".join(line.split()).strip().lower()


def _parse_number(raw: str) -> float | None:
    match = _NUMBER.search(raw.replace(",", ""))
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def deterministic_metrics(log_text: str, *, source_file: str = "") -> list[VerifiedMetric]:
    """Parse output shapes ReproLab recognises on its own.

    Currently pytest's summary line, which is an unambiguous machine-readable
    statement of how many tests ran.
    """
    found: list[VerifiedMetric] = []
    seen: set[tuple[str, str]] = set()
    for line in log_text.splitlines():
        trimmed = line.strip().strip("= ").strip()
        for parser_name, pattern in _DETERMINISTIC:
            match = pattern.match(trimmed)
            if not match:
                continue
            for count, label in _PYTEST_PAIR.findall(match.group("body")):
                name = f"pytest_{label.lower().rstrip('s')}"
                key = (name, count)
                if key in seen:
                    continue
                seen.add(key)
                found.append(
                    VerifiedMetric(
                        name=name,
                        value=float(count),
                        raw_value=count,
                        unit="tests",
                        parser=parser_name,
                        provenance=line.strip()[:500],
                        source_file=source_file,
                    )
                )
    return found


def verify_candidates(
    candidates: list[MetricCandidate], log_text: str, *, source_file: str = ""
) -> tuple[list[VerifiedMetric], list[dict[str, str]]]:
    """Confirm each candidate against the real log.

    Returns ``(verified, rejected)``. A candidate is accepted only if its
    quoted line appears in the log (whitespace-normalised) and a number can be
    parsed from the quote by ReproLab itself.
    """
    log_lines = {_normalise(line): line.strip() for line in log_text.splitlines() if line.strip()}
    verified: list[VerifiedMetric] = []
    rejected: list[dict[str, str]] = []

    for candidate in candidates:
        quote = _normalise(candidate.evidence_line)
        if not quote:
            rejected.append(
                {"name": candidate.name, "reason": "no evidence line supplied"}
            )
            continue
        actual = log_lines.get(quote)
        if actual is None:
            # Allow the quote to be a contiguous substring of a real log line,
            # but require a real match -- never a paraphrase.
            actual = next(
                (original for norm, original in log_lines.items() if quote in norm),
                None,
            )
        if actual is None:
            rejected.append(
                {
                    "name": candidate.name,
                    "reason": (
                        "quoted line is not present verbatim in the execution log"
                    ),
                    "claimed_line": candidate.evidence_line[:200],
                }
            )
            continue
        value = _parse_number(candidate.raw_value) or _parse_number(actual)
        if value is None:
            rejected.append(
                {
                    "name": candidate.name,
                    "reason": "no number could be parsed from the quoted line",
                    "claimed_line": candidate.evidence_line[:200],
                }
            )
            continue
        # The claimed value must actually occur in the matched line.
        if candidate.raw_value.strip() and candidate.raw_value.strip().replace(
            ",", ""
        ) not in actual.replace(",", ""):
            rejected.append(
                {
                    "name": candidate.name,
                    "reason": (
                        f"claimed value {candidate.raw_value!r} does not occur in "
                        "the matched log line"
                    ),
                    "claimed_line": actual[:200],
                }
            )
            continue
        verified.append(
            VerifiedMetric(
                name=candidate.name[:120],
                value=value,
                raw_value=candidate.raw_value[:120],
                unit=candidate.unit[:40],
                parser="model_located_reprolab_verified",
                provenance=actual[:500],
                source_file=source_file,
            )
        )
    return verified, rejected


def dedupe(metrics: list[VerifiedMetric]) -> list[VerifiedMetric]:
    """Prefer deterministically-parsed metrics over model-located ones."""
    best: dict[tuple[str, str], VerifiedMetric] = {}
    for metric in metrics:
        key = (metric.name.lower(), metric.raw_value)
        existing = best.get(key)
        if existing is None or (
            existing.parser != "pytest_summary" and metric.parser == "pytest_summary"
        ):
            best[key] = metric
    return list(best.values())
