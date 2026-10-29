"use client";

import { useEffect, useState } from "react";
import { api, type Health } from "@/lib/api";

export default function AboutPage() {
  const [health, setHealth] = useState<Health | null>(null);
  const [verify, setVerify] = useState<Record<
    string,
    Record<string, unknown>
  > | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    api.health().then(setHealth).catch(() => undefined);
  }, []);

  async function runVerify() {
    setBusy(true);
    setError("");
    try {
      setVerify(await api.verifyIntegrations());
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <div className="page-head">
        <h1>How ReproLab works</h1>
        <p>
          Four parts with clearly separated jobs. The distinction that matters
          most: Token Factory serves model inference; it never runs repository
          code.
        </p>
      </div>

      <div className="grid grid-2">
        <div className="stack">
          <div className="card">
            <h3>NVIDIA Nemotron</h3>
            <p className="small">
              The open-source NVIDIA model that does the reasoning work: reading
              the paper and repository, identifying dependencies and datasets,
              writing the experiment plan, diagnosing a real failure from its
              actual log output, and drafting the report narrative. Every
              response that drives behaviour is validated against a schema
              before anything acts on it.
            </p>
          </div>
          <div className="card">
            <h3>Nebius Token Factory</h3>
            <p className="small">
              The inference provider that serves Nemotron over an
              OpenAI-compatible API. ReproLab validates the configured model id
              against the live model list for your own account at run time,
              because published ids differ in casing between Nebius pages and
              not every account has every model.
            </p>
            <div className="notice info" style={{ marginTop: "0.8rem", marginBottom: 0 }}>
              <strong>What Token Factory does not do</strong>
              It does not execute repository code. Nothing downloaded from a
              paper&apos;s repository is sent there to run.
            </div>
          </div>
          <div className="card">
            <h3>Tavily</h3>
            <p className="small">
              Retrieval. ReproLab uses it in three places: finding the
              paper&apos;s own pages and the project&apos;s official
              documentation before planning; resolving specific setup questions;
              and searching on the <em>real error text</em> from the runner when
              something fails, so a repair is informed rather than guessed. It
              also extracts readable text from PDFs the direct fetch cannot
              parse.
            </p>
          </div>
          <div className="card">
            <h3>The execution runner</h3>
            <p className="small">
              A separate, disposable container per command. Non-root, all
              capabilities dropped, no new privileges, read-only root
              filesystem, no Docker socket, and hard CPU, memory, process,
              runtime, log-size and output caps enforced by ReproLab rather than
              requested of the model. Dependency installation gets network
              access; the experiment phase runs with{" "}
              <code>--network none</code>. No application credentials are ever
              passed in.
            </p>
          </div>
        </div>

        <div className="stack">
          <div className="card">
            <h3>Outcomes are kept separate</h3>
            <p className="card-note">
              Most reproducibility tooling blurs these. ReproLab does not.
            </p>
            <ul className="plain small">
              <li>The repository was inspected.</li>
              <li>The environment installed.</li>
              <li>A smoke test passed.</li>
              <li>A small experiment completed.</li>
              <li>
                <strong>
                  A specific reported result was reproduced under matching
                  conditions.
                </strong>
              </li>
            </ul>
            <p className="small" style={{ marginTop: "0.7rem" }}>
              A successful install or a small inference run does not verify a
              paper&apos;s accuracy claim. ReproLab v1 runs small CPU-only
              checks and never claims the last step.
            </p>
          </div>

          <div className="card">
            <h3>Measurements must be verifiable</h3>
            <p className="small">
              Numbers never come from the model. It points at a log line it
              believes holds a result; ReproLab then re-finds that line in the
              real log and parses the number itself. If the quoted line is not
              present verbatim, the claim is discarded and listed as such in the
              report.
            </p>
          </div>

          {health ? (
            <div className="card">
              <h3>This instance</h3>
              <dl className="kv">
                <dt>Version</dt>
                <dd className="mono">{health.version}</dd>
                <dt>Nebius</dt>
                <dd>
                  {health.integrations.nebius_token_factory.configured
                    ? "configured"
                    : "not configured"}
                </dd>
                <dt>Tavily</dt>
                <dd>
                  {health.integrations.tavily.configured
                    ? "configured"
                    : "not configured"}
                </dd>
                <dt>Runner</dt>
                <dd>
                  {health.runner.available ? "available" : "unavailable"}
                </dd>
                <dt>Repository policy</dt>
                <dd>
                  {health.policy.curated_repositories_only
                    ? "curated repositories only"
                    : "arbitrary public repositories enabled"}
                </dd>
                <dt>Allowed programs</dt>
                <dd className="mono small">
                  {health.policy.allowed_programs.join(", ")}
                </dd>
              </dl>
              <div className="actions" style={{ marginTop: "0.9rem" }}>
                <button onClick={runVerify} disabled={busy}>
                  {busy ? "Verifying…" : "Verify integrations now"}
                </button>
              </div>
              <p className="small muted" style={{ marginTop: "0.5rem" }}>
                Makes one real call to each provider. No key values are ever
                returned.
              </p>
              {error ? (
                <div className="notice bad" style={{ marginTop: "0.8rem" }}>
                  {error}
                </div>
              ) : null}
              {verify ? (
                <pre style={{ marginTop: "0.8rem" }}>
                  <code>{JSON.stringify(verify, null, 2)}</code>
                </pre>
              ) : null}
            </div>
          ) : null}
        </div>
      </div>
    </>
  );
}
