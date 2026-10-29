# Reproducible demo guide

Three runs. One succeeds, two are blocked — and being blocked is the correct
answer for those two, not a defect.

## Before you start

```bash
docker info                         # the daemon must be running
cd backend && ./run_backend.sh      # terminal 1
cd frontend && npm run dev          # terminal 2
```

Confirm configuration:

```bash
curl -s http://127.0.0.1:8000/api/integrations/verify | python3 -m json.tool
```

Expect `verified: true` for `nebius_token_factory`, `tavily` and `runner`.
The response names the **resolved** model id, which may differ from the one
you configured if your account does not have it.

---

## Run 1 — expected to succeed

**BM25S** — *BM25S: Orders of magnitude faster lexical search via eager sparse
scoring*, [arXiv:2407.03618](https://arxiv.org/abs/2407.03618), MIT licensed.

| Field | Value |
|---|---|
| Paper URL | `https://arxiv.org/abs/2407.03618` |
| Repository URL | `https://github.com/xhluca/bm25s` |
| Goal | `Check that the documented retrieval example runs and prints ranked results.` |
| Task type | **Small experiment** |
| Runtime budget | 10 minutes |

Or press **Small experiment** on the BM25S card.

### What to expect

1. The repository is cloned and the commit recorded.
2. Tavily retrieves the paper and the project's documentation; sources appear
   in the panel.
3. The agent reads `README.md` and the packaging files.
4. A plan appears **and the run stops for approval**. It should propose
   something close to:

   | Phase | Command |
   |---|---|
   | setup (network) | `pip install .` |
   | setup (network) | `pip install pytest PyStemmer` |
   | experiment (no network) | `pytest tests/core/... -q` |
   | experiment (no network) | `python examples/save_and_reload_end_to_end.py` |

   Exact commands vary between runs — the agent plans from what it reads.
5. Approve. Logs stream. Reference timings on 2 CPU cores:

   | Step | Typical |
   |---|---|
   | `python -m venv` bootstrap | ~6 s |
   | `pip install .` | ~18 s |
   | `pip install pytest PyStemmer` | ~7 s |
   | `pytest` subset | ~1 s |
   | example script | ~1 s |

6. **Final report**: `Completed`, with the ladder showing repository
   inspected, environment installed, smoke test passed and small experiment
   completed — and **"a specific reported result reproduced under matching
   conditions" explicitly NO**.
7. Measurements carry evidence ids. A `pytest_passed` count is parsed
   deterministically from the pytest summary line.

### What this does and does not show

It shows the published code installs from source and its documented example
produces ranked output on a CPU in under a minute. It does **not** verify the
paper's throughput claims: those are measured on BEIR corpora this run never
touched. The report says so under "what remains unverified".

---

## Run 2 — expected to be blocked (missing dataset)

Same paper and repository, but the **BM25S on BEIR / Natural Questions** card.

The paper's headline numbers are measured on BEIR datasets. Those corpora are
not in the repository, and the experiment phase has no network by design.

Expect `Blocked`, with `blocked_category` of `missing_dataset`, the missing
resources named, and — importantly — the report stating this is **not a
failure of the code**. Depending on what the agent reads, it may report this
at plan time (no code executed) or after installing and hitting the real
error.

---

## Run 3 — expected to be blocked (gated weights and GPU)

**Llama 2 reference implementation**,
[arXiv:2307.09288](https://arxiv.org/abs/2307.09288).

Every documented command needs checkpoint weights obtained through an approval
form and a signed download URL, and is launched with `torchrun` across CUDA
GPUs.

Expect `Blocked` naming both blockers. You may also see commands **rejected by
ReproLab's validator** — `torchrun` is not on the program allowlist, and
`./download.sh` is a shell script, which ReproLab never executes. Those
rejections are shown in the plan and in the report.

---

## Also worth demonstrating

**Cancellation really works.** Start Run 1, approve, and press **Cancel run**
during `pip install`. The container is killed, the step records as
`cancelled`, and the run finishes as `Cancelled` — not left hanging.

**Runs survive the browser.** Approve Run 1, then hard-refresh or close and
reopen the tab. Progress continues; the timeline and logs are still there.

**A non-curated repository is refused before anything is downloaded:**

```bash
curl -s -X POST http://127.0.0.1:8000/api/runs \
  -H 'content-type: application/json' \
  -d '{"paper_url":"https://arxiv.org/abs/1234.5678",
       "repo_url":"https://github.com/someone/unreviewed",
       "task_type":"setup_check"}'
# HTTP 403 — curated-repository mode
```

**SSRF protection:**

```bash
curl -s -X POST http://127.0.0.1:8000/api/runs \
  -H 'content-type: application/json' \
  -d '{"paper_url":"http://169.254.169.254/latest/meta-data/",
       "repo_url":"https://github.com/xhluca/bm25s",
       "task_type":"setup_check"}'
# HTTP 400 — cloud instance metadata address
```

---

## Determinism

The agent plans from what it reads, so exact commands and wording vary between
runs. What is stable:

- the **status** for each of the three runs (Completed, Blocked, Blocked);
- the outcome ladder never claiming reproduction;
- every measurement carrying an evidence id;
- unverifiable metric claims being discarded rather than reported.

For a byte-identical check, use the offline fixture:

```bash
cd backend && ./run_tests.sh tests/test_fixture_experiment.py -v
```

That fixture is a synthetic repository for testing ReproLab's machinery. It is
**not** a research reproduction, and it says so in its own README.
