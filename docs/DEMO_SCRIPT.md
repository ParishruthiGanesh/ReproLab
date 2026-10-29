# Three-minute demo script

**Total: 2:55.** Have the backend and frontend already running, the BM25S run
**already finished** in one tab, and a fresh **New experiment** tab open.
Never wait for an install on camera.

---

### 0:00 — 0:25 · The problem

> "A paper reports a number. Its repository is public. Whether the code runs,
> and whether anything you can run from it supports that number, are different
> questions — and answering them by hand costs an afternoon per paper.
>
> ReproLab is an AI research engineering agent that does the mechanical part
> and, more importantly, refuses to overstate the result."

---

### 0:25 — 0:50 · Start a real run

*On the New experiment tab.* Click the **BM25S** card's **Small experiment**.

> "A paper, its public repository, a goal, and a runtime budget. BM25S — a
> real paper, arXiv 2407.03618, MIT licensed.
>
> NVIDIA Nemotron, served by Nebius Token Factory, now reads the paper and the
> repository. Tavily finds the project's official documentation. Note what it
> hasn't done: it hasn't run anything."

---

### 0:50 — 1:25 · The approval gate

*Switch to the finished run tab, scrolled to the plan.*

> "Here's the plan it produced. The objective. The resources it needs. And the
> exact commands.
>
> Look at the phase column. Setup commands get network access so dependencies
> can install. Experiment commands run with **no network at all**.
>
> Every one of these was parsed into an argument list and checked against an
> allowlist. There's no shell — no pipes, no `&&`, no `python -c`. And none of
> it runs until a human presses approve.
>
> This is the distinction that matters: **Token Factory serves the model.
> Repository code executes somewhere else entirely** — a disposable container,
> non-root, capabilities dropped, read-only root filesystem, and **not one of
> my API keys inside it**."

---

### 1:25 — 1:55 · Real execution

*Scroll to the steps table and logs.*

> "Approved — and these are real exit codes from real containers. Install from
> source. Thirteen tests pass, network-isolated. The documented example prints
> ranked retrieval results. About thirty seconds, CPU only.
>
> Every step has an evidence id, and every log is kept."

*Hover the cancel button.*

> "And cancel actually kills the container — it's not a flag the model is
> asked to respect."

---

### 1:55 — 2:30 · The report — the honest part

*Scroll to the outcome ladder.*

> "Here's what makes this scientific rather than a demo.
>
> Repository inspected — yes. Environment installed — yes. Smoke test passed —
> yes. Small experiment completed — yes.
>
> **A specific reported result reproduced under matching conditions — no.**
>
> ReproLab will not claim that. The paper's throughput numbers are measured on
> BEIR corpora this run never touched, on different hardware. The report says
> that, under 'what remains unverified'."

*Point at the discarded claim.*

> "And this is my favourite part. Numbers never come from the model. The model
> points at a log line; ReproLab re-finds that line in the actual log and
> parses the number itself. This claim couldn't be located verbatim — so it
> was **discarded**, and the report says so rather than quietly reporting it."

---

### 2:30 — 2:55 · Blocked is a real answer

*Open the Llama 2 blocked report.*

> "Not every paper can be checked. Llama 2 needs approval-gated weights and
> GPUs. ReproLab reports that as **Blocked** — with the missing requirement
> named, and stated plainly as *not a failure of the code*.
>
> Blocked, failed, and completed are different answers, and conflating them is
> how reproducibility tooling misleads people.
>
> NVIDIA Nemotron does the reasoning. Nebius Token Factory serves it. Tavily
> finds the documentation and the error-specific troubleshooting. An isolated
> runner does the execution. MIT licensed, 174 tests passing."

---

## Delivery notes

- **Pre-cache the Docker image** (`docker pull python:3.11-slim`) and
  pre-warm one BM25S run. Live installs are the main way this demo runs long.
- If the live run is still planning at 1:25, stay on the finished tab. The
  live run is only there to show the agent starting.
- **Do not show a terminal with `.env` open.** Ever.
- If asked "did it reproduce the paper?" — the answer is **no, and that is the
  point**: it verified the code installs and runs, and said explicitly that
  the paper's numbers remain unverified.

## One-sentence pitch

> ReproLab turns a paper and its repository into an execution-backed
> reproducibility report — and is rigorous about the difference between "the
> code ran" and "the result reproduced."
