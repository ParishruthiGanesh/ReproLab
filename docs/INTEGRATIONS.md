# Integration summary

A factual account of how each sponsor technology is used. Nothing here is a
performance claim.

---

## NVIDIA Nemotron (open-source model)

**Role:** every reasoning step in the agent loop.

| Where | What the model does | Schema it must satisfy |
|---|---|---|
| Research loop | Chooses one tool per turn, or stops | `ToolAction` |
| Planning | Writes the experiment plan a human reviews | `ExperimentPlan` |
| Diagnosis | Explains a real failure from its actual log output and decides blocked / repairable / failed | `FailureDiagnosis` |
| Metric reading | Points at log lines that contain printed results | `MetricExtraction` |
| Reporting | Drafts the report's narrative sections | `ReportAssessment` |

**Constraints applied to the model's output**

- Every response is parsed and validated against a Pydantic schema. Invalid
  responses are fed back with the validation error, bounded by
  `max_structured_retries`, then become a recorded failure. A half-parsed
  object never reaches the rest of the system.
- Commands it proposes are re-validated by ReproLab's command validator and
  then approved by a human before execution.
- Numbers it reports are re-verified against the recorded logs; unverifiable
  claims are discarded and listed as such.
- Reasoning blocks (`<think>…</think>`) are stripped and discarded. ReproLab
  does not request, store or display hidden chain-of-thought — only concise
  action summaries.
- Execution limits are enforced in application code. The model is told about
  them so its plans are realistic; it is never trusted to honour them.

**Why an open NVIDIA model fits this task:** the work is structured
extraction and judgement over long, messy context — a README, a paper, a
repository tree and a stack trace at once — emitted as strict JSON. Long
context and reliable structured output are what the job needs.

---

## Nebius Token Factory

**Role:** the inference provider that serves Nemotron.

- Endpoint: `POST {NEBIUS_BASE_URL}/chat/completions`, OpenAI-compatible.
- Default base URL: `https://api.tokenfactory.nebius.com/v1`.
- Model discovery: `GET {NEBIUS_BASE_URL}/models`.
- Auth: `Authorization: Bearer $NEBIUS_API_KEY`, read from the environment and
  never logged, echoed, or passed to an experiment container.

**Model resolution.** ReproLab does not hardcode a model id. At run time it
fetches the live model list for the caller's account and resolves
`NEBIUS_MODEL` against it:

1. exact match → use it;
2. case-insensitive match → use the live spelling and record the correction;
3. otherwise, with `NEBIUS_MODEL_AUTOSELECT=true`, substitute the closest
   Nemotron model the account actually has, and record the substitution;
4. if the account has no Nemotron model at all, fail with a clear message
   rather than silently using a non-NVIDIA model.

This exists because published Nemotron ids differ in casing between Nebius's
own documentation pages (for example `nvidia/Nemotron-3_5-Lightning` in the
model catalogue versus `nvidia/Nemotron-3.5-Lightning` on the endpoints page),
and because accounts differ in which models they can reach.

**Architecture boundary.** Token Factory provides model inference only.
ReproLab does not and cannot use it to execute repository code: downloaded
code runs in a separate container runner. Any claim that Token Factory "runs
the experiment" would be false.

---

## Tavily

**Role:** retrieval of material ReproLab was not given.

| Call site | Endpoint | Purpose |
|---|---|---|
| `research_sources` | `POST /search` (`advanced`) | The paper's own pages and the project's official installation and evaluation documentation, before planning |
| `documentation` | `POST /search` (`basic`) | A specific setup or evaluation question raised during research |
| `troubleshoot` | `POST /search` | Keyed on the **real error text produced by the runner**, after an observed failure, to inform a bounded repair |
| `extract` | `POST /extract` | Readable text from a PDF or JavaScript-rendered page the direct fetch cannot parse — including the paper itself |

Auth: `Authorization: Bearer $TAVILY_API_KEY`.

**How results are treated**

- Every result URL is re-validated against the SSRF policy before it is kept
  or extracted; results pointing at private or metadata addresses are dropped
  without being shown to the model.
- Results are recorded as sources, with URL and retrieval method, and appear
  in the run's source panel and in the final report.
- Content is passed to the model explicitly labelled as untrusted data. It
  cannot change ReproLab's instructions or widen what may be executed.
- Per-run call budgets bound how much retrieval a single run can perform.

Tavily is load-bearing: without it, the agent sees only the repository and
the paper URL, and troubleshooting a failure has nothing but the stack trace
to work from. Runs still proceed when `TAVILY_API_KEY` is absent — the UI and
the run timeline both state that external documentation was not retrieved.

---

## The execution runner

**Role:** the only place repository code ever runs.

Not a sponsor technology, but the component that makes the other three
honest. A disposable Docker container per command: non-root, all capabilities
dropped, no new privileges, read-only root filesystem, no Docker socket, hard
CPU/memory/PID/time/log/file-size caps, cancellation that terminates the
container, and **no application credentials inside**.

Dependency acquisition and experiment execution are separated: setup commands
have network access, experiment commands run with `--network none`.

If Docker is unavailable, ReproLab reports the run as blocked and explains the
minimum setup. It never runs untrusted repository code on the host.

---

## Credential handling

| Rule | Where enforced |
|---|---|
| Keys come only from the environment | `reprolab/config.py` |
| `.env` is git-ignored; `.env.example` holds placeholders only | `.gitignore`, `.env.example` |
| Keys are never written to logs, reports, manifests or the UI | `reprolab/security/redact.py`, applied at every persistence and display point |
| Keys are never passed to an experiment container | `reprolab/runner/docker_runner.py` — explicit environment allowlist |
| `/api/health` reports configured-or-not, never values | `reprolab/main.py` |

Covered by tests: `tests/test_security.py::TestSecretRedaction` and
`tests/test_runner.py::TestIsolation::test_no_application_credentials_reach_the_container`.
