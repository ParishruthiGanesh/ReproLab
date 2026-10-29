"""Pydantic schemas for every structured model response.

Nothing the model says is acted on until it validates here. Commands in
particular are validated twice: once as a schema field, then again by
``runner.commands.parse_command`` before execution -- and then a human
approves them.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

TaskType = Literal["setup_check", "small_experiment"]


class ToolAction(BaseModel):
    """One step of the research loop: call a tool, or stop and plan."""

    thought_summary: str = Field(
        default="",
        max_length=400,
        description=(
            "One short sentence stating what you are doing and why. A concise "
            "action summary for the run timeline -- not your reasoning."
        ),
    )
    tool: Literal[
        "search_research_sources",
        "extract_source_content",
        "read_repository_file",
        "inspect_repository_tree",
        "search_documentation",
        "finish_research",
    ]
    query: str = Field(default="", max_length=400)
    url: str = Field(default="", max_length=2048)
    path: str = Field(default="", max_length=512)

    @field_validator("thought_summary", "query", "path", "url", mode="before")
    @classmethod
    def _coerce(cls, value: object) -> str:
        if value is None:
            return ""
        return str(value)


class PlannedCommand(BaseModel):
    """A command the agent proposes. Re-validated before it can be executed."""

    command: str = Field(max_length=600)
    phase: Literal["setup", "experiment"]
    purpose: str = Field(default="", max_length=300)
    workdir: str = Field(default=".", max_length=200)

    @field_validator("command")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("command must not be empty")
        return value.strip()

    @field_validator("workdir", mode="before")
    @classmethod
    def _default_workdir(cls, value: object) -> str:
        text = str(value or ".").strip()
        return text or "."


class RequiredResource(BaseModel):
    name: str = Field(max_length=200)
    kind: Literal[
        "dependency", "dataset", "model_weights", "hardware", "credential", "other"
    ]
    available: bool = Field(
        description="True only if it can be obtained inside the stated limits."
    )
    detail: str = Field(default="", max_length=600)


class ExperimentPlan(BaseModel):
    """The plan a human reviews before any downloaded code is executed."""

    objective: str = Field(max_length=1200)
    paper_claim_summary: str = Field(
        default="",
        max_length=1500,
        description="What the paper itself reports, in its own terms.",
    )
    what_this_run_can_show: str = Field(
        default="",
        max_length=1200,
        description=(
            "What a successful run would and would not establish. Installing a "
            "package or running a small inference does not verify an accuracy claim."
        ),
    )
    required_resources: list[RequiredResource] = Field(default_factory=list, max_length=20)
    setup_commands: list[PlannedCommand] = Field(default_factory=list, max_length=8)
    experiment_commands: list[PlannedCommand] = Field(default_factory=list, max_length=8)
    expected_outputs: list[str] = Field(default_factory=list, max_length=12)
    blockers: list[str] = Field(default_factory=list, max_length=12)
    estimated_runtime_seconds: int = Field(default=300, ge=1, le=7200)
    is_blocked: bool = Field(
        default=False,
        description=(
            "True if the experiment cannot proceed: missing weights, private "
            "data, unsupported hardware, or resources beyond the limits."
        ),
    )
    blocked_reason: str = Field(default="", max_length=1000)
    confidence: Literal["low", "medium", "high"] = "medium"


class FailureDiagnosis(BaseModel):
    """Diagnosis of a real, observed failure."""

    summary: str = Field(max_length=800)
    category: Literal[
        "missing_dependency",
        "missing_dataset",
        "missing_model_weights",
        "unsupported_hardware",
        "resource_limit",
        "code_error",
        "configuration_error",
        "network_restriction",
        "unknown",
    ]
    is_blocked: bool = Field(
        description=(
            "True if no bounded repair can fix this because a required resource "
            "is unobtainable here. A blocked run is not a failed run."
        )
    )
    evidence_quote: str = Field(
        default="",
        max_length=1000,
        description="The exact line(s) from the execution log this rests on.",
    )
    repair_possible: bool = False
    repair_rationale: str = Field(default="", max_length=800)
    repair_commands: list[PlannedCommand] = Field(default_factory=list, max_length=4)
    patch_path: str = Field(default="", max_length=512)
    patch_find: str = Field(default="", max_length=4000)
    patch_replace: str = Field(default="", max_length=4000)


class MetricCandidate(BaseModel):
    """A metric the model believes appears in the logs, pending verification.

    The value is never taken from the model: ReproLab re-finds the quoted line
    in the actual log and parses the number itself. If the quote is not present
    verbatim, the candidate is discarded.
    """

    name: str = Field(max_length=120)
    raw_value: str = Field(max_length=120)
    unit: str = Field(default="", max_length=40)
    evidence_line: str = Field(
        max_length=600,
        description="The exact log line containing this value, copied verbatim.",
    )


class MetricExtraction(BaseModel):
    metrics: list[MetricCandidate] = Field(default_factory=list, max_length=12)
    no_reliable_metric_reason: str = Field(default="", max_length=600)


class ReportAssessment(BaseModel):
    """The narrative parts of the final report.

    Statuses and evidence are computed by ReproLab, not by the model.
    """

    headline: str = Field(max_length=300)
    what_was_attempted: str = Field(max_length=2000)
    what_the_paper_reports: str = Field(default="", max_length=2000)
    what_reprolab_measured: str = Field(default="", max_length=2000)
    differences_from_paper: list[str] = Field(default_factory=list, max_length=12)
    remains_unverified: list[str] = Field(default_factory=list, max_length=12)
    limitations: list[str] = Field(default_factory=list, max_length=12)
    missing_resources: list[str] = Field(default_factory=list, max_length=12)
