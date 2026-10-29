# Sponsor tool feedback — template

**This file is a template with blanks to fill in. It intentionally contains no
invented feedback, no benchmark numbers and no performance claims.**

Record only what you actually observed while building and demonstrating
ReproLab. Where you have no observation, leave the field as `not measured` or
`no observation` rather than estimating. An empty field is more useful to a
sponsor than a guess.

---

## How to capture the factual parts

```bash
# Resolved model id, latency of one real completion, Tavily result count
curl -s http://127.0.0.1:8000/api/integrations/verify | python3 -m json.tool
```

Per-run figures come from the JSON manifest of a specific run
(`/api/runs/{id}/manifest.json`): exact commands, exit codes, durations and the
model actually used. Quote a run id with any number you report.

---

## Environment

| Field | Value |
|---|---|
| Date(s) of use | |
| ReproLab version / commit | |
| Host OS and CPU | |
| Docker version | |
| Network (direct / proxied) | |

---

## NVIDIA Nemotron

| Question | Answer |
|---|---|
| Which model id did you configure? | |
| Which model id was actually resolved and used? | |
| Did your account have the model you first configured? | |
| How reliably did it return schema-valid JSON on the first attempt? | |
| How often did a structured response need a retry? | |
| Did it respect the "one tool per turn" loop structure? | |
| Quality of its failure diagnoses, given real log output? | |
| Did it ever propose a command the validator rejected? If so, what kind? | |
| Did it ever claim a metric that could not be verified against the logs? | |

**Observations (free text):**

**Rough edges hit:**

---

## Nebius Token Factory

| Question | Answer |
|---|---|
| Time from sign-up to first successful API call | |
| Was the OpenAI-compatible client a drop-in? | |
| Did `GET /v1/models` return what the documentation described? | |
| Did published model ids match the live list exactly? | |
| Observed latency for a planning-sized request (note the run id) | |
| Any rate limiting or 5xx responses? Under what load? | |
| Was error messaging actionable (e.g. bad key, unavailable model)? | |
| Clarity of the documentation on base URL, auth and model ids | |

**Observations (free text):**

**Rough edges hit:**

> Note for this section: ReproLab resolves the configured model id against the
> live `/v1/models` list because published Nemotron ids differ in casing
> between Nebius pages. If you hit that, describe exactly which pages
> disagreed — that is concrete, actionable feedback.

---

## Tavily

| Question | Answer |
|---|---|
| Time from sign-up to first successful search | |
| Usefulness of `/search` for finding official project documentation | |
| Usefulness of `/extract` for paper PDFs | |
| Did error-text searches surface relevant troubleshooting? | |
| Relevance of results for narrow, technical queries | |
| Did `basic` versus `advanced` depth make a noticeable difference? | |
| Any results pointing at private or non-public addresses? | |
| Observed latency (note the run id) | |
| Credit consumption over the build | |

**Observations (free text):**

**Rough edges hit:**

---

## Overall

**What worked well:**

**What was hardest:**

**What would have saved the most time:**

**Would you use this stack again, and for what?**

---

## Things to be careful about when filling this in

- Do not report a latency or throughput number you did not measure.
- Quote a run id or a manifest alongside any number.
- Distinguish "I hit this once" from "this happened consistently".
- Distinguish a sponsor-tool limitation from a ReproLab bug.
- Never paste an API key, even redacted, into this file.
