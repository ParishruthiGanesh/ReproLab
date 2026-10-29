# ReproLab

**An AI research engineering agent that takes a research paper and its public
code repository, prepares an experiment, attempts to run it, diagnoses
failures, and produces a reproducibility report backed by actual execution
evidence.**

Built for the Nebius x NVIDIA Global AI Hackathon — Coding and Agentic
Engineering track.

Licensed under the [MIT License](LICENSE).

---

## What problem this solves

A paper reports a number. Its repository is public. Whether that code actually
runs — and whether anything you can run from it supports the number — are
different questions, and answering them by hand costs an afternoon per paper.

ReproLab automates the mechanical part and, crucially, **refuses to overstate
the result**. It keeps five outcomes strictly separate:

| Outcome | Means |
|---|---|
| Repository inspected | The code was fetched and read |
| Environment installed | Dependencies resolved and installed |
| Smoke test passed | A documented import or test ran successfully |
| Small experiment completed | A documented command produced output |
| **A specific reported result reproduced under matching conditions** | **The paper's claim was checked** |

A successful install is not a reproduction. ReproLab v1 runs small CPU-only
checks and **never claims the last row** — it says so explicitly in every
report.

---

## The four components and what each one does

### NVIDIA Nemotron — the reasoning
An NVIDIA open-source model does the research-engineering work: reading the
paper and repository, identifying dependencies, datasets, weights and
documented commands, writing the experiment plan, diagnosing a real failure
from its actual log output, and drafting the report narrative.

Every model response that drives behaviour is validated against a Pydantic
schema before anything acts on it. A response that will not validate is
retried a bounded number of times and then becomes a recorded failure — never
a half-parsed object.

### Nebius Token Factory — the inference provider
Serves Nemotron over an OpenAI-compatible API (`/v1/chat/completions`).

ReproLab validates the configured model id against the **live `GET /v1/models`
list for your own account** at run time, because published Nemotron ids differ
in casing between Nebius's own pages and not every account has every model. If
the exact id is missing, a case-insensitive match is tried first; only then
does it substitute another Nemotron model your account can actually use, and
it records that substitution in the run.

> **Architecture note.** Token Factory provides *model inference only*. It does
> not execute repository code. Nothing downloaded from a paper's repository is
> ever sent there to run.

### Tavily — the retrieval
Used in three concrete places, not as decoration:

1. **Before planning** — find the paper's own pages and the project's official
   installation and evaluation documentation.
2. **During research** — resolve specific setup questions the agent raises.
3. **After a real failure** — search on the *actual error text produced by the
   runner*, so a repair is informed rather than guessed.

Tavily's extraction also recovers readable text from PDFs and
JavaScript-rendered pages that a direct fetch cannot parse.

Every result is recorded as a source with its URL, and treated as untrusted
data — never as instructions.

### The execution runner — a separate, isolated container
Repository code runs in a disposable Docker container, one per command:

- non-root (`uid 1000`), `--cap-drop ALL`, `--security-opt no-new-privileges`
- read-only root filesystem; only the run's workspace and a `tmpfs` are writable
- no Docker socket, no host mounts beyond the workspace, never privileged
- CPU, memory, PID, wall-clock, log-size and output caps, enforced by ReproLab
- **no application credentials are passed in** — not the Nebius key, not the
  Tavily key, nothing from the orchestrator's environment
- **dependency acquisition is separated from experiment execution**: setup
  commands get network access; experiment commands run with `--network none`

If Docker is unavailable, ReproLab reports the run as blocked and says what to
install. **It never falls back to running untrusted code on the host.**

---

## Setup

### Prerequisites

- Python 3.11+
- Node.js 20+
- Docker Engine or Docker Desktop, with the daemon running
- A Nebius Token Factory API key — <https://tokenfactory.nebius.com>
- A Tavily API key — <https://app.tavily.com>

Check Docker first; without it ReproLab cannot execute anything:

```bash
docker info
```

### 1. Configure credentials

```bash
cp .env.example .env
```

Edit `.env` and set:

| Variable | What it is |
|---|---|
| `NEBIUS_API_KEY` | Your Nebius Token Factory key |
| `NEBIUS_BASE_URL` | `https://api.tokenfactory.nebius.com/v1` |
| `NEBIUS_MODEL` | An NVIDIA Nemotron id, e.g. `nvidia/Nemotron-3_5-Lightning` |
| `TAVILY_API_KEY` | Your Tavily key |

`.env` is git-ignored. Never put a key in source, and never commit one.

If `NEBIUS_MODEL` is not available to your account, ReproLab reports the
mismatch and — with `NEBIUS_MODEL_AUTOSELECT=true` — uses another Nemotron
model you do have.

Behind a TLS-intercepting corporate proxy, also set
`REPROLAB_RUNNER_CA_BUNDLE=/path/to/ca-bundle.crt` so the dependency-install
phase can reach PyPI.

### 2. Start the backend

```bash
cd backend
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
./run_backend.sh
```

The backend serves the API **and** the background worker on
<http://127.0.0.1:8000>. Confirm your configuration:

```bash
curl -s http://127.0.0.1:8000/api/health | python3 -m json.tool
```

### 3. Start the frontend

```bash
cd frontend
npm install
npm run dev
```

Open <http://localhost:3000>.

### 4. Verify the integrations with real calls

```bash
curl -s http://127.0.0.1:8000/api/integrations/verify | python3 -m json.tool
```

This makes one real Nemotron completion, one real Tavily search, and one real
Docker check. It reports the resolved model id and never returns key values.
The same button exists under **How it works** in the UI.

---

## Running a check

1. **New experiment** — paper URL, repository URL, an optional plain-English
   goal, setup check or small experiment, and a runtime budget.
2. **Plan review** — the objective, required resources, the exact commands with
   their network phase, known blockers, and the limits that will be enforced.
   Nothing has executed yet. **You approve.**
3. **Live run** — status and elapsed time, progress timeline, source panel,
   streaming container logs, repair attempts with diffs, and a cancel button
   that really terminates the container.
4. **Final report** — what was attempted, the outcome ladder, environment and
   repository commit, exact commands and exit codes, measurements with evidence
   references, missing resources, original versus modified implementation, and
   downloadable Markdown and JSON.

Runs are tracked by the backend, not the browser. **Refreshing or closing the
page does not stop or lose a run.**

### Curated repositories

By default this instance only *executes* code from a hand-checked list
(`backend/reprolab/curated.py`) — otherwise a public deployment would be an
open remote-code-execution service. Any public repository can still be
inspected.

| Entry | Expected result |
|---|---|
| [BM25S](https://github.com/xhluca/bm25s) (MIT, [arXiv:2407.03618](https://arxiv.org/abs/2407.03618)) | Succeeds: CPU-only, no downloads at experiment time |
| BM25S on BEIR / Natural Questions | **Blocked**: evaluation corpus is not available |
| [Llama 2](https://github.com/meta-llama/llama) ([arXiv:2307.09288](https://arxiv.org/abs/2307.09288)) | **Blocked**: approval-gated weights, GPU required |

Two of the three are *expected* to be blocked. That is the correct, useful
answer — not a defect.

To run arbitrary public repositories, set
`REPROLAB_ALLOW_ARBITRARY_REPOS=true` **and** `REPROLAB_API_TOKEN` on a private
instance you control. That setting turns ReproLab into a remote code execution
service for anyone who can reach it.

---

## How numbers get into a report

They are never taken from the model.

1. ReproLab parses output shapes it recognises on its own (for example
   pytest's summary line).
2. The model may additionally *point at* a log line it believes holds a result.
3. ReproLab re-finds that line in the real log and parses the number itself.
4. If the quoted line is not present verbatim, the claim is **discarded** and
   listed in the report under "discarded measurement claims".

If nothing can be verified, the report says *no reliable metric was extracted*
rather than inventing one.

---

## Tests

```bash
cd backend
./run_tests.sh                      # all 174 tests, including real containers
./run_tests.sh -m "not docker"      # skip tests needing a Docker daemon
```

Covered: URL and path restrictions (SSRF, metadata endpoints, traversal,
symlink escape), secret redaction, command validation, provider response
validation and model resolution, runner timeout / cancellation / log cap /
memory cap / network isolation / credential absence against **real
containers**, agent repair limits, the blocked-vs-failed-vs-completed
distinction, evidence references in reports, and a deterministic offline
fixture.

The fixture in `backend/tests/fixtures/fixture_repo/` is a synthetic
repository used to exercise the machinery. It is clearly labelled as a fixture
and is **not** a research reproduction.

---

## Documentation

- [Architecture](docs/ARCHITECTURE.md) — components, data flow, security model
- [Demo guide](docs/DEMO.md) — reproducible walkthrough with expected output
- [Three-minute demo script](docs/DEMO_SCRIPT.md)
- [Devpost description draft](docs/DEVPOST.md)
- [Integration summary](docs/INTEGRATIONS.md) — exactly how NVIDIA, Nebius and Tavily are used
- [Sponsor tool feedback template](docs/FEEDBACK.md)

---

## Limitations

ReproLab v1 deliberately does not attempt:

- universal reproduction of every paper
- full model training
- automatic GPU provisioning
- verifying a published numerical claim (it reports this as unverified)

It targets lightweight Python research repositories, on CPU, in minutes.

---

## License

[MIT](LICENSE).
