# ReproLab architecture

## Components

```
┌──────────────────────────────────────────────────────────────────────┐
│  Browser — Next.js + TypeScript                                      │
│  New experiment · Plan review · Live run · Final report · History    │
└───────────────┬──────────────────────────────────────────────────────┘
                │ REST + Server-Sent Events (polling as fallback)
┌───────────────▼──────────────────────────────────────────────────────┐
│  FastAPI backend                                                     │
│  Validates input · enforces policy · serves run state                │
│  SQLite  ◄── the single source of truth for every run                │
└───────────────┬──────────────────────────────────────────────────────┘
                │ claims queued / approved runs
┌───────────────▼──────────────────────────────────────────────────────┐
│  Background worker (threads)                                         │
│  Drives the bounded agent loop, independent of any HTTP request      │
└──┬────────────────────┬───────────────────────────┬──────────────────┘
   │                    │                           │
┌──▼───────────────┐ ┌──▼──────────────┐ ┌──────────▼──────────────────┐
│ Nebius Token     │ │ Tavily          │ │ Execution runner            │
│ Factory          │ │                 │ │                             │
│ serves NVIDIA    │ │ paper pages,    │ │ disposable Docker container │
│ Nemotron         │ │ official docs,  │ │ non-root · no credentials   │
│                  │ │ error-specific  │ │ setup: network              │
│ INFERENCE ONLY   │ │ troubleshooting │ │ experiment: --network none  │
│ never runs code  │ │                 │ │                             │
└──────────────────┘ └─────────────────┘ └─────────────────────────────┘
```

**The separation that matters:** Token Factory serves model inference.
Repository code executes in the runner, a different process with a different
trust boundary. Token Factory never runs arbitrary repository code, and the
runner never holds an API key.

## Run lifecycle

```
queued
  │  worker claims the run
researching ──── clone repo · fetch paper · Tavily search · read files
  │              bounded loop: at most N tool calls
  │              ↓
  │         ExperimentPlan (schema-validated)
  │              ↓
  │         every command re-parsed by the command validator
  │
  ├── plan is blocked ─────────────────► Blocked  (no code executed)
  │
awaiting_approval ◄── HUMAN GATE. Nothing has executed yet.
  │  approve                    │  decline
  │                             └────────► Not attempted
running ──── bootstrap venv · setup commands · experiment commands
  │
  ├── failure ──► diagnose (schema-validated)
  │                 ├── missing resource ──────► Blocked
  │                 ├── repairable & budget left ─► repairing ─┐
  │                 └── otherwise ──────────────► Failed       │
  │                                                            │
  │◄───────────────── retry the original command ──────────────┘
  │                   (at most max_repairs attempts, enforced in code)
reporting ── collect artifacts · parse + verify metrics · write report
  │
done ──► Completed | Failed | Blocked | Cancelled | Not attempted
```

Cancellation is checked throughout and actually kills the container.

## Why run state lives in SQLite

Starting a run inserts a row and returns. The worker polls for queued and
approved runs and drives them on its own threads. The browser only ever reads
state. So a refresh, a closed tab, or a lost connection changes nothing about
whether a run proceeds — and history survives a restart.

A backend restart mid-run is detected on startup: such runs are marked
`Failed` with an explanation, rather than left showing "Active" forever,
because the disposable workspace does not survive the restart.

## Status vocabulary

| Status | Meaning |
|---|---|
| `Completed` | Every planned command ran and exited successfully |
| `Failed` | A command failed for a reason ReproLab could not repair within its bounds |
| `Blocked` | A required resource could not be obtained here. **Not a failure of the code** |
| `Cancelled` | A user stopped the run; execution was terminated |
| `Not attempted` | The plan was declined, or the run never reached execution |

The blocked/failed distinction is the one most worth getting right: gated
weights, an unavailable dataset, or a GPU requirement say nothing bad about
the repository.

## Security model

Everything retrieved — paper text, repository files, web pages, container
logs — is **untrusted data**. It cannot change ReproLab's instructions,
request credentials, or widen what may be executed. The application enforces
this structurally; the prompts state it as well, but the enforcement does not
depend on the model complying.

### Input boundary
- URLs: scheme, port and credential checks, DNS resolution, and rejection of
  loopback, private, link-local, reserved and cloud-metadata addresses
  (including IPv4-mapped IPv6 forms). **Every redirect hop is re-validated**,
  because a public host can redirect to `169.254.169.254`.
- Repositories: canonicalised `github.com/owner/repo` only; shallow clone with
  hooks disabled and submodules not fetched; size-capped.
- Downloads: byte caps enforced by ReproLab, not trusted to `Content-Length`.

### Command boundary
Model output never reaches a shell. Each proposed command is:
1. parsed into an argv list (no shell is ever invoked),
2. checked against a program allowlist, with per-program subcommand rules,
3. checked for shell metacharacters, absolute paths and `..` traversal,
4. **approved by a human**,
5. re-parsed again at execution time rather than trusted from the database.

ReproLab's own bootstrap commands are argv literals in its source, so they do
not pass through the untrusted-input allowlist — but they run in exactly the
same isolated container.

### Execution boundary
| Control | Implementation |
|---|---|
| Disposable | Fresh container per command; `--rm`; workspace deleted with the run |
| Non-root | `--user 1000:1000` |
| No privileges | `--cap-drop ALL`, `--security-opt no-new-privileges`, never `--privileged` |
| No host access | Only the run's workspace is mounted; no Docker socket |
| Read-only root | `--read-only` plus `tmpfs` for `/tmp` and `/run` |
| CPU / memory | `--cpus`, `--memory`, `--memory-swap` |
| Processes | `--pids-limit` |
| Wall clock | Watchdog kills the container at the step timeout |
| Log size | Reader stops and kills the container at the cap, then drains the stream so termination is not blocked |
| File size | `--ulimit fsize` |
| Network | `bridge` for setup, `none` for the experiment phase |
| Credentials | An explicit environment allowlist; no application secret is ever passed in |
| Cancellation | `docker kill` on the named container, plus a polled cancel check |

### Path boundary
Every path from the model or a repository is resolved inside the run's
workspace and rejected if it escapes — including through symlinks. Patch
destinations additionally refuse `.git`, `conftest.py`, `sitecustomize.py`,
`.pth` files and similar, which would change how later commands behave.

### Output boundary
Secret redaction runs over every string that is persisted, streamed or
displayed: known environment values plus credential shapes (JWTs, `tvly-`,
`sk-`, `ghp_`, `AKIA`, `hf_`, Authorization headers, URL credentials, and
`key: value` spellings including JSON-quoted keys).

### Deployment posture
By default only curated repositories may be **executed**; any public
repository may be inspected. `REPROLAB_ALLOW_ARBITRARY_REPOS=true` is for a
private, authenticated instance only — it makes ReproLab a remote code
execution service for anyone who can reach it. `REPROLAB_API_TOKEN` gates
every write endpoint.

## Evidence model

Every executed command becomes a step with an evidence id (`ev-01`, `ev-02`,
…) carrying its argv, phase, network mode, exit code, duration, truncation
flags and a log file. The report and the JSON manifest reference those ids.

Metrics are produced in two ways, and both are verified by ReproLab:

1. **Deterministic parsers** for output shapes ReproLab recognises itself.
2. **Model-located, ReproLab-verified**: the model quotes a log line; ReproLab
   re-finds that line in the real log, confirms the claimed value occurs in
   it, and parses the number itself. Failures are recorded as discarded
   claims with the reason.

The model never supplies a number that reaches a report.

## Bounds

All enforced in application code, and described to the model only so its plans
are realistic:

| Bound | Default |
|---|---|
| Total runtime per run | 900 s (user-selectable 300–900) |
| Per-command timeout | 420 s |
| CPU / memory | 2 cores / 4096 MB, no GPU |
| Captured log per command | 1 MB |
| Workspace size | 2048 MB |
| Research tool calls | 10, with per-tool sub-budgets |
| Plan commands | 8 setup + 8 experiment |
| Repair attempts | 2 |
| Structured-response retries | 2 |
