"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useParams } from "next/navigation";
import Link from "next/link";
import {
  api,
  formatDuration,
  type RunDetail,
  type TimelineEvent,
} from "@/lib/api";
import { StatusBadge } from "@/components/StatusBadge";
import { Timeline } from "@/components/Timeline";
import { PlanReview } from "@/components/PlanReview";
import { SourcePanel } from "@/components/SourcePanel";
import { FinalReport } from "@/components/FinalReport";

const PHASE_LABEL: Record<string, string> = {
  queued: "Queued",
  researching: "Researching the paper and repository",
  awaiting_approval: "Waiting for your approval",
  approved: "Approved — starting the runner",
  running: "Executing in the isolated runner",
  repairing: "Attempting a bounded repair",
  reporting: "Generating the report",
  done: "Finished",
};

export default function RunPage() {
  const params = useParams<{ id: string }>();
  const runId = params.id;

  const [run, setRun] = useState<RunDetail | null>(null);
  const [error, setError] = useState("");
  const [liveEvents, setLiveEvents] = useState<TimelineEvent[]>([]);
  const [logText, setLogText] = useState("");
  const [busy, setBusy] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  const logRef = useRef<HTMLDivElement | null>(null);

  const refresh = useCallback(async () => {
    try {
      const detail = await api.getRun(runId);
      setRun(detail);
      setError("");
      return detail;
    } catch (e) {
      setError((e as Error).message);
      return null;
    }
  }, [runId]);

  // Initial load. Run state lives in the backend, so a refresh re-reads it.
  useEffect(() => {
    refresh();
  }, [refresh]);

  // Elapsed-time ticker.
  useEffect(() => {
    if (run?.is_terminal) return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [run?.is_terminal]);

  const terminal = run?.is_terminal ?? false;
  const isExample = run?.is_example ?? false;
  const phase = run?.phase ?? "";

  // Live progress over SSE. If the stream drops, polling below takes over.
  useEffect(() => {
    if (!runId || terminal || isExample || !phase) return;
    const source = new EventSource(api.streamUrl(runId, 0));
    let closed = false;

    source.addEventListener("timeline", (event) => {
      const payload = JSON.parse((event as MessageEvent).data) as TimelineEvent;
      setLiveEvents((prior) =>
        prior.some((e) => e.id === payload.id) ? prior : [...prior, payload],
      );
    });
    source.addEventListener("log", (event) => {
      const payload = JSON.parse((event as MessageEvent).data) as {
        step_id: string;
        text: string;
      };
      setLogText((prior) => (prior + payload.text).slice(-200_000));
    });
    source.addEventListener("state", (event) => {
      const payload = JSON.parse((event as MessageEvent).data) as {
        status: string;
        phase: string;
        is_terminal: boolean;
      };
      setRun((prior) =>
        prior
          ? {
              ...prior,
              status: payload.status as RunDetail["status"],
              phase: payload.phase,
            }
          : prior,
      );
      if (payload.is_terminal && !closed) {
        closed = true;
        source.close();
        refresh();
      }
    });
    source.addEventListener("done", () => {
      if (!closed) {
        closed = true;
        source.close();
        refresh();
      }
    });
    source.onerror = () => {
      if (!closed) {
        closed = true;
        source.close();
      }
    };
    return () => {
      closed = true;
      source.close();
    };
  }, [runId, terminal, isExample, refresh]);

  // Polling: the authoritative refresh, and the fallback if SSE is blocked.
  useEffect(() => {
    if (terminal || isExample) return;
    const timer = setInterval(refresh, 4000);
    return () => clearInterval(timer);
  }, [terminal, isExample, refresh]);

  useEffect(() => {
    if (logRef.current) {
      logRef.current.scrollTop = logRef.current.scrollHeight;
    }
  }, [logText]);

  const events = useMemo(() => {
    if (!run) return [];
    const byId = new Map<number, TimelineEvent>();
    for (const event of [...run.events, ...liveEvents]) byId.set(event.id, event);
    return [...byId.values()].sort((a, b) => a.id - b.id);
  }, [run, liveEvents]);

  async function act(action: "approve" | "decline" | "cancel") {
    setBusy(true);
    try {
      if (action === "approve") await api.approve(runId);
      else if (action === "decline") await api.decline(runId);
      else await api.cancel(runId);
      setLiveEvents([]);
      await refresh();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  if (error && !run) {
    return (
      <div className="notice bad">
        <strong>Cannot load this run</strong>
        {error}
      </div>
    );
  }
  if (!run) {
    return (
      <p className="muted">
        <span className="spinner" /> Loading run…
      </p>
    );
  }

  const elapsed = run.started_at
    ? (run.finished_at ? new Date(run.finished_at).getTime() : now) -
      new Date(run.started_at).getTime()
    : 0;

  const showPlanReview =
    run.phase === "awaiting_approval" && run.plan !== null && !run.is_example;
  const showReport = run.manifest !== null;
  const canCancel = !run.is_terminal && !run.is_example;

  return (
    <>
      <div className="page-head">
        <h1>{run.repo_name}</h1>
        <p className="small">
          {run.task_type === "setup_check" ? "Setup check" : "Small experiment"} ·{" "}
          <a href={run.paper_url} target="_blank" rel="noreferrer">
            paper
          </a>{" "}
          ·{" "}
          <a href={run.repo_url} target="_blank" rel="noreferrer">
            repository
          </a>
          {run.repo_commit ? (
            <>
              {" "}
              · commit <code>{run.repo_commit.slice(0, 12)}</code>
            </>
          ) : null}
        </p>
      </div>

      {error ? (
        <div className="notice bad">
          <strong>Last action failed</strong>
          {error}
        </div>
      ) : null}

      <div className="card">
        <div
          className="actions"
          style={{ justifyContent: "space-between", alignItems: "flex-start" }}
        >
          <div>
            <div className="actions" style={{ alignItems: "center" }}>
              <StatusBadge status={run.status} animate />
              <span className="small muted">
                {PHASE_LABEL[run.phase] ?? run.phase}
              </span>
            </div>
            <dl className="kv" style={{ marginTop: "0.8rem" }}>
              <dt>Elapsed</dt>
              <dd className="mono">{formatDuration(elapsed)}</dd>
              <dt>Budget</dt>
              <dd className="mono">{run.runtime_budget_seconds} s</dd>
              <dt>Repair attempts</dt>
              <dd className="mono">
                {run.repair_attempts} used
                {run.plan?.limits?.max_repairs
                  ? ` of ${String(run.plan.limits.max_repairs)}`
                  : ""}
              </dd>
              {run.model_used ? (
                <>
                  <dt>Model</dt>
                  <dd className="mono small">{run.model_used}</dd>
                </>
              ) : null}
              {run.goal ? (
                <>
                  <dt>Goal</dt>
                  <dd className="small">{run.goal}</dd>
                </>
              ) : null}
            </dl>
          </div>
          {canCancel ? (
            <button className="danger" onClick={() => act("cancel")} disabled={busy}>
              Cancel run
            </button>
          ) : null}
        </div>
        {run.outcome_detail ? (
          <p className="small" style={{ marginTop: "0.8rem" }}>
            {run.outcome_detail}
          </p>
        ) : null}
      </div>

      {showPlanReview && run.plan ? (
        <PlanReview
          plan={run.plan}
          onApprove={() => act("approve")}
          onDecline={() => act("decline")}
          busy={busy}
        />
      ) : null}

      <div className="grid grid-2">
        <div className="stack">
          <div className="card">
            <h3>Progress timeline</h3>
            <p className="card-note">
              Concise action summaries and tool results. ReproLab does not
              request or display hidden model reasoning.
            </p>
            <Timeline events={events} />
          </div>

          <div className="card">
            <h3>Execution logs</h3>
            <p className="card-note">
              Streamed from the container as it runs, redacted, and capped at
              ReproLab&apos;s log-size limit.
            </p>
            <div className="logbox" ref={logRef}>
              {logText}
            </div>

            {run.steps.length > 0 ? (
              <div style={{ marginTop: "1rem" }}>
                <h3>Recorded steps</h3>
                <div className="table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <th>Evidence</th>
                        <th>Kind</th>
                        <th>Command</th>
                        <th>Exit</th>
                        <th>Duration</th>
                      </tr>
                    </thead>
                    <tbody>
                      {run.steps.map((step) => (
                        <tr key={step.evidence_id}>
                          <td className="mono">{step.evidence_id}</td>
                          <td className="small">
                            {step.kind}
                            <br />
                            <span className="muted small">
                              {step.network.startsWith("none")
                                ? "isolated"
                                : "network"}
                            </span>
                          </td>
                          <td>
                            <code>{step.command}</code>
                            {step.purpose ? (
                              <>
                                <br />
                                <span className="muted small">{step.purpose}</span>
                              </>
                            ) : null}
                          </td>
                          <td>
                            <span
                              className={`badge ${
                                step.status === "succeeded"
                                  ? "ok"
                                  : step.status === "running"
                                    ? "active"
                                    : "bad"
                              }`}
                            >
                              {step.exit_code === null
                                ? step.status
                                : step.exit_code}
                            </span>
                            {step.timed_out ? (
                              <>
                                <br />
                                <span className="badge warn">timeout</span>
                              </>
                            ) : null}
                            {step.truncated ? (
                              <>
                                <br />
                                <span className="badge warn">log capped</span>
                              </>
                            ) : null}
                          </td>
                          <td className="mono small">
                            {formatDuration(step.duration_ms)}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            ) : null}
          </div>

          {run.patches.length > 0 ? (
            <div className="card">
              <h3>Repair attempts and code diffs</h3>
              <p className="card-note">
                A patch means the run used a modified implementation, not the
                repository as published.
              </p>
              {run.patches.map((patch, index) => (
                <div key={index} style={{ marginBottom: "0.9rem" }}>
                  <p className="small">
                    <strong>
                      Attempt {patch.attempt} — <code>{patch.path}</code>
                    </strong>
                    <br />
                    {patch.rationale}
                  </p>
                  <pre>
                    <code>{patch.diff.slice(0, 8000)}</code>
                  </pre>
                </div>
              ))}
            </div>
          ) : null}
        </div>

        <div className="stack">
          <SourcePanel sources={run.sources} />

          {run.agent_actions.length > 0 ? (
            <div className="card">
              <h3>Agent actions ({run.agent_actions.length})</h3>
              <p className="card-note">
                Every tool call, with its inputs and a summary of its output.
              </p>
              <div className="stack">
                {run.agent_actions.map((action, index) => (
                  <details key={index} className="evidence">
                    <summary>
                      <span className={`badge ${action.ok ? "ok" : "bad"}`}>
                        {action.ok ? "ok" : "error"}
                      </span>{" "}
                      <code>{action.tool}</code>
                    </summary>
                    <div className="body">
                      <p className="small" style={{ margin: "0 0 0.4rem" }}>
                        {action.summary}
                      </p>
                      <p className="muted small" style={{ margin: 0 }}>
                        {formatDuration(action.duration_ms)}
                      </p>
                      {action.input ? (
                        <pre style={{ marginTop: "0.5rem" }}>
                          <code>{JSON.stringify(action.input, null, 2)}</code>
                        </pre>
                      ) : null}
                    </div>
                  </details>
                ))}
              </div>
            </div>
          ) : null}
        </div>
      </div>

      {showReport && run.manifest ? (
        <div style={{ marginTop: "1.5rem" }}>
          <FinalReport
            runId={runId}
            manifest={run.manifest}
            isExample={run.is_example}
          />
        </div>
      ) : null}

      <p className="small muted" style={{ marginTop: "1.5rem" }}>
        <Link href="/runs">← All runs</Link>
      </p>
    </>
  );
}
