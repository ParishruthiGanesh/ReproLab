"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import {
  api,
  ApiError,
  type CuratedRepo,
  type Health,
  type TaskType,
} from "@/lib/api";

const BUDGETS = [
  { value: 300, label: "5 minutes", hint: "Setup checks on small repositories" },
  { value: 600, label: "10 minutes", hint: "Default — install plus a small experiment" },
  { value: 900, label: "15 minutes", hint: "Slower dependency installs" },
];

export default function NewExperimentPage() {
  const router = useRouter();
  const [health, setHealth] = useState<Health | null>(null);
  const [curated, setCurated] = useState<CuratedRepo[]>([]);
  const [curatedOnly, setCuratedOnly] = useState(true);
  const [loadError, setLoadError] = useState("");

  const [paperUrl, setPaperUrl] = useState("");
  const [repoUrl, setRepoUrl] = useState("");
  const [goal, setGoal] = useState("");
  const [taskType, setTaskType] = useState<TaskType>("setup_check");
  const [budget, setBudget] = useState(600);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    api
      .health()
      .then(setHealth)
      .catch((e: Error) =>
        setLoadError(
          `Cannot reach the ReproLab backend: ${e.message}. Start it with ` +
            `./backend/run_backend.sh`,
        ),
      );
    api
      .curated()
      .then((data) => {
        setCurated(data.repositories);
        setCuratedOnly(data.curated_only);
      })
      .catch(() => undefined);
  }, []);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setError("");
    setSubmitting(true);
    try {
      const created = await api.createRun({
        paper_url: paperUrl.trim(),
        repo_url: repoUrl.trim(),
        goal: goal.trim(),
        task_type: taskType,
        runtime_budget_seconds: budget,
      });
      router.push(`/runs/${created.id}`);
    } catch (e) {
      setError(
        e instanceof ApiError ? e.message : (e as Error).message ?? "unknown error",
      );
      setSubmitting(false);
    }
  }

  function preset(repo: CuratedRepo, type: TaskType) {
    setPaperUrl(repo.paper_url);
    setRepoUrl(repo.repo_url);
    setTaskType(type);
    setGoal("");
    setError("");
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  const nebius = health?.integrations.nebius_token_factory;
  const tavily = health?.integrations.tavily;
  const runnerAvailable = health?.runner?.available === true;

  return (
    <>
      <div className="page-head">
        <h1>New reproducibility check</h1>
        <p>
          Give ReproLab a paper and its public repository. It researches both,
          proposes an experiment plan for your approval, then runs the approved
          commands in an isolated container and reports what it actually
          measured.
        </p>
      </div>

      {loadError ? (
        <div className="notice bad">
          <strong>Backend unreachable</strong>
          {loadError}
        </div>
      ) : null}

      {health && (!nebius?.configured || !tavily?.configured || !runnerAvailable) ? (
        <div className="notice warn">
          <strong>Configuration incomplete</strong>
          <ul className="plain">
            {!nebius?.configured ? (
              <li>
                <code>NEBIUS_API_KEY</code> is not set, so no NVIDIA model is
                available to plan an experiment.
              </li>
            ) : null}
            {!tavily?.configured ? (
              <li>
                <code>TAVILY_API_KEY</code> is not set, so external
                documentation will not be retrieved.
              </li>
            ) : null}
            {!runnerAvailable ? (
              <li>
                No isolated runner:{" "}
                {String(health.runner?.error ?? "Docker is unavailable")}{" "}
                ReproLab will report runs as blocked rather than execute
                repository code on the host.
              </li>
            ) : null}
          </ul>
          Copy <code>.env.example</code> to <code>.env</code> and fill it in,
          then restart the backend.
        </div>
      ) : null}

      <div className="grid grid-2">
        <form className="card" onSubmit={submit}>
          <h2>Experiment</h2>

          <div className="field">
            <label htmlFor="paper">Paper URL</label>
            <input
              id="paper"
              type="url"
              required
              placeholder="https://arxiv.org/abs/2407.03618"
              value={paperUrl}
              onChange={(e) => setPaperUrl(e.target.value)}
            />
            <span className="hint">
              An arXiv abstract page, a project page, or a direct PDF link.
            </span>
          </div>

          <div className="field">
            <label htmlFor="repo">Public GitHub repository URL</label>
            <input
              id="repo"
              type="url"
              required
              placeholder="https://github.com/xhluca/bm25s"
              value={repoUrl}
              onChange={(e) => setRepoUrl(e.target.value)}
            />
            <span className="hint">
              {curatedOnly
                ? "This instance executes code only from its curated list below. Other repositories are refused before anything is downloaded."
                : "Arbitrary public repositories are enabled on this instance."}
            </span>
          </div>

          <div className="field">
            <label htmlFor="goal">Goal (optional, plain English)</label>
            <textarea
              id="goal"
              rows={3}
              placeholder="Check that the retrieval example runs and prints ranked results."
              value={goal}
              onChange={(e) => setGoal(e.target.value)}
            />
          </div>

          <div className="field">
            <label>Task type</label>
            <div className="radio-row">
              <label
                className={`radio-card${taskType === "setup_check" ? " selected" : ""}`}
              >
                <input
                  type="radio"
                  name="task"
                  checked={taskType === "setup_check"}
                  onChange={() => setTaskType("setup_check")}
                />
                <strong>Setup check</strong>
                <span>
                  Inspect the repository, install dependencies, run a meaningful
                  import or the documented smoke test.
                </span>
              </label>
              <label
                className={`radio-card${taskType === "small_experiment" ? " selected" : ""}`}
              >
                <input
                  type="radio"
                  name="task"
                  checked={taskType === "small_experiment"}
                  onChange={() => setTaskType("small_experiment")}
                />
                <strong>Small experiment</strong>
                <span>
                  Also run a documented evaluation or inference command on a
                  small sample, where the materials are available.
                </span>
              </label>
            </div>
          </div>

          <div className="field">
            <label htmlFor="budget">Runtime budget</label>
            <select
              id="budget"
              value={budget}
              onChange={(e) => setBudget(Number(e.target.value))}
            >
              {BUDGETS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label} — {option.hint}
                </option>
              ))}
            </select>
            <span className="hint">
              Enforced by ReproLab, not by the model. Commands exceeding it are
              terminated.
            </span>
          </div>

          {error ? (
            <div className="notice bad">
              <strong>Cannot start this run</strong>
              {error}
            </div>
          ) : null}

          <div className="actions">
            <button className="primary" type="submit" disabled={submitting}>
              {submitting ? "Starting…" : "Start analysis"}
            </button>
            <span className="muted small">
              Nothing is executed until you approve the plan.
            </span>
          </div>
        </form>

        <div className="stack">
          <div className="card">
            <h3>How a run proceeds</h3>
            <ol className="plain">
              <li>Clone the repository and retrieve the paper.</li>
              <li>
                Search with Tavily for the project&apos;s official documentation.
              </li>
              <li>
                Read repository files to identify dependencies, data, weights and
                documented commands.
              </li>
              <li>
                Produce an experiment plan — <strong>you approve it</strong>.
              </li>
              <li>Run the approved commands in an isolated container.</li>
              <li>
                Diagnose any failure, attempt at most{" "}
                {String(health?.limits?.max_repairs ?? 2)} bounded repairs.
              </li>
              <li>Report, with every measurement tied to recorded evidence.</li>
            </ol>
          </div>

          {health ? (
            <div className="card">
              <h3>This instance</h3>
              <dl className="kv">
                <dt>Inference</dt>
                <dd>
                  {nebius?.configured ? (
                    <>
                      Nebius Token Factory
                      <br />
                      <span className="mono">{nebius.requested_model}</span>
                      <br />
                      <span className="muted small">
                        validated against your live model list at run time
                      </span>
                    </>
                  ) : (
                    <span className="muted">not configured</span>
                  )}
                </dd>
                <dt>Retrieval</dt>
                <dd>
                  {tavily?.configured ? (
                    "Tavily"
                  ) : (
                    <span className="muted">not configured</span>
                  )}
                </dd>
                <dt>Runner</dt>
                <dd>
                  {runnerAvailable ? (
                    <>
                      Docker {String(health.runner.docker_server_version ?? "")}
                      <br />
                      <span className="muted small">
                        {String(health.limits.container_cpus)} CPU,{" "}
                        {String(health.limits.container_memory_mb)} MB, no GPU
                      </span>
                    </>
                  ) : (
                    <span className="muted">unavailable</span>
                  )}
                </dd>
              </dl>
            </div>
          ) : null}
        </div>
      </div>

      <div className="card">
        <h2>Curated repositories</h2>
        <p className="card-note">
          Each entry is a real paper with a real public repository, checked by
          hand for licence, size and documented commands. Two are expected to be
          reported as blocked — that is the correct result, not a defect.
        </p>
        <div className="grid grid-3">
          {curated.map((repo) => (
            <div key={repo.slug} className="card" style={{ marginBottom: 0 }}>
              <h3>{repo.name}</h3>
              <p className="small muted" style={{ marginTop: "0.3rem" }}>
                {repo.demo_label}
              </p>
              <dl className="kv" style={{ margin: "0.6rem 0" }}>
                <dt>Licence</dt>
                <dd>{repo.license}</dd>
                <dt>Size</dt>
                <dd>~{repo.approx_size_mb} MB</dd>
              </dl>
              <p className="small">{repo.notes}</p>
              {repo.expected_blockers.length > 0 ? (
                <ul className="plain small">
                  {repo.expected_blockers.map((blocker) => (
                    <li key={blocker} className="muted">
                      {blocker}
                    </li>
                  ))}
                </ul>
              ) : null}
              <div className="actions" style={{ marginTop: "0.7rem" }}>
                <button type="button" onClick={() => preset(repo, "setup_check")}>
                  Setup check
                </button>
                <button
                  type="button"
                  onClick={() => preset(repo, "small_experiment")}
                >
                  Small experiment
                </button>
              </div>
              <p className="small" style={{ marginTop: "0.5rem" }}>
                <a href={repo.paper_url} target="_blank" rel="noreferrer">
                  paper
                </a>{" "}
                ·{" "}
                <a href={repo.repo_url} target="_blank" rel="noreferrer">
                  repository
                </a>
              </p>
            </div>
          ))}
        </div>
        <p className="small muted" style={{ marginTop: "0.9rem" }}>
          Looking for the output format first? See the{" "}
          <Link href="/example">labelled example report</Link>.
        </p>
      </div>
    </>
  );
}
