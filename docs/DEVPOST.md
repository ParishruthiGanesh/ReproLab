# Devpost description — draft

*Draft for the Nebius x NVIDIA Global AI Hackathon, Coding and Agentic
Engineering track. Fill the bracketed placeholders before submitting; they are
left blank rather than guessed.*

---

## Inspiration

Reproducibility in machine learning is discussed as a cultural problem, but
day to day it is a *labour* problem. A paper reports a number. Its repository
is public. Finding out whether that code even installs — let alone whether
anything you can run from it supports the number — costs an afternoon of
dependency archaeology per paper.

What made this worth building as an agent is that the work is genuinely
agentic: read a paper, read a repository, find the official docs, infer what
the project expects of its environment, run something, read the stack trace,
try a bounded fix, and write down honestly what happened.

The part that worried us, and that shaped the whole design, is that an agent
doing this is *strongly incentivised to overclaim*. "I installed the package"
easily becomes "the paper reproduces." We decided early that ReproLab's main
contribution would be refusing to do that.

## What it does

Give ReproLab a paper URL and its public GitHub repository. It:

1. clones the repository and retrieves the paper;
2. uses Tavily to find the project's official installation and evaluation
   documentation;
3. reads the repository to identify dependencies, datasets, weights,
   documented commands and hardware requirements;
4. writes an experiment plan — and **stops for human approval** before any
   downloaded code runs;
5. executes the approved commands in a disposable, network-restricted
   container;
6. diagnoses real failures from real exit codes and logs, attempting at most
   two bounded repairs;
7. produces a reproducibility report where every measurement points at a
   recorded execution.

Two task types: a **setup check** (install and run a documented smoke test)
and a **small experiment** (run a documented evaluation or inference command
on a small sample).

**The part we care about most.** The report keeps five outcomes strictly
separate: repository inspected, environment installed, smoke test passed,
small experiment completed, and *a specific reported result reproduced under
matching conditions*. ReproLab v1 runs small CPU-only checks and **never
claims the last one** — it states plainly that the paper's quantitative claims
remain unverified, and lists exactly how its conditions differed.

Numbers never come from the model. The model may *point at* a log line it
believes holds a result; ReproLab then re-finds that line in the real log and
parses the number itself. If the quoted line is not present verbatim, the
claim is **discarded** and the report says so. "No reliable metric was
extracted" is a perfectly good answer, and ReproLab gives it.

And "blocked" is a first-class result. If a paper needs approval-gated
weights, a dataset that is not publicly downloadable, or a GPU, ReproLab
reports it as **Blocked** with the missing requirement named — explicitly not
a failure of the code. Conflating "blocked", "failed" and "completed" is
precisely how reproducibility tooling misleads people.

## How we built it

**NVIDIA Nemotron** does every reasoning step: choosing the next research
tool, writing the plan, diagnosing failures, locating results in logs, and
drafting the report narrative. Every response is validated against a Pydantic
schema before anything acts on it; an invalid response is retried a bounded
number of times and then becomes a recorded failure, never a half-parsed
object.

**Nebius Token Factory** serves the model over its OpenAI-compatible API. We
deliberately do not hardcode a model id: ReproLab resolves `NEBIUS_MODEL`
against the live `GET /v1/models` list for the caller's own account, because
published Nemotron ids differ in casing between Nebius's own pages and not
every account has every model. Mismatches and substitutions are recorded in
the run.

**Tavily** does retrieval in three concrete places: finding the paper's pages
and the project's official documentation before planning; resolving specific
setup questions; and — the one we found most valuable — searching on the
*actual error text produced by the runner* after a failure, so a repair is
informed rather than guessed. Its extraction also recovers text from paper
PDFs that a direct fetch cannot parse.

**The execution runner** is a separate, disposable Docker container per
command. This boundary is the architectural point: Token Factory provides
model inference; it never executes repository code.

Stack: Next.js and TypeScript, FastAPI, SQLite, a threaded background worker,
and server-sent events with polling as the fallback. Runs live in the
database, not in a request, so refreshing the browser loses nothing.

## Challenges we ran into

**Making an agent say "I don't know."** The default behaviour of a capable
model given logs and a paper is to produce a confident number. The fix was
structural, not prompt-based: ReproLab verifies every claimed value against
the recorded log and discards what it cannot find.

**Killing a container that is flooding stdout.** Our log-size cap worked, but
`docker kill` would hang: Docker will not accept the kill until its attach
stream is drained, and we had stopped reading. The fix was to kill
asynchronously and keep draining the stream while discarding output. We only
found this because we tested the limit for real.

**CORS between `localhost` and `127.0.0.1`.** Different origins. Caught by
driving the real UI in a browser rather than trusting that the build passed.

**Deciding what to do without Docker.** The tempting fallback is to run the
code on the host with rlimits. We didn't build it. If Docker is unavailable,
ReproLab reports the run as blocked and explains what to install — it never
silently runs untrusted code on the host.

## Accomplishments we're proud of

- An agent that produces **"blocked"** and **"no reliable metric was
  extracted"** as confident, correct answers.
- A security boundary verified against real containers, not asserted in a
  README: network isolation, non-root execution, absence of application
  credentials, working cancellation, and timeout, memory and log caps are each
  covered by a test that runs a real container.
- 174 passing tests, including a deterministic offline fixture that is clearly
  labelled as a fixture rather than dressed up as a reproduction.
- An example report in the UI that is labelled as an example everywhere it
  appears and is excluded from every statistic.

## What we learned

That the hard part of an agent like this is not getting it to act, but
bounding what it is allowed to conclude. Almost every design decision —
schema-validated responses, a command allowlist, metric re-verification, the
separated outcome ladder — exists to keep a capable model from being
accidentally persuasive about something it did not establish.

We also learned to distrust published identifier strings. Resolving the model
id against the live account model list turned out to be essential, not
defensive over-engineering.

## What's next

- GPU-backed runs behind explicit, metered opt-in, which would make the top
  rung of the outcome ladder reachable for some papers.
- Dataset provenance: fetching a declared corpus during the setup phase, with
  checksums, so an evaluation can be attempted with a recorded data identity.
- Comparing a measured value against the paper's stated value, with an
  explicit statement of the conditions that do and do not match.
- Batch mode over a conference's accepted papers, producing per-paper setup
  reports.

## Try it

- Repository: `https://github.com/ParishruthiGanesh/ReproLab`
- Licence: MIT
- Setup and demo: `README.md` and `docs/DEMO.md`
- Demo video: `[LINK]`

---

### Fields to fill before submitting

| Field | Value |
|---|---|
| Demo video URL | |
| Nemotron model id actually used | |
| Live deployment URL (optional) | |
| Team members | |

**Do not add performance numbers here unless you measured them**, and quote a
run id alongside any you do add.
