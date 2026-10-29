"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { api, formatDuration, type RunList } from "@/lib/api";
import { StatusBadge } from "@/components/StatusBadge";

export default function RunsPage() {
  const [data, setData] = useState<RunList | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    const load = () =>
      api
        .listRuns()
        .then(setData)
        .catch((e: Error) => setError(e.message));
    load();
    const timer = setInterval(load, 5000);
    return () => clearInterval(timer);
  }, []);

  if (error) {
    return (
      <div className="notice bad">
        <strong>Cannot load run history</strong>
        {error}
      </div>
    );
  }
  if (!data) {
    return (
      <p className="muted">
        <span className="spinner" /> Loading…
      </p>
    );
  }

  const stats = data.statistics;

  return (
    <>
      <div className="page-head">
        <h1>Run history</h1>
        <p>
          Runs are tracked by the backend, not by this page, so closing the
          browser does not stop or lose one.
        </p>
      </div>

      <div className="card">
        <h3>Real runs on this instance</h3>
        <dl className="kv">
          <dt>Total</dt>
          <dd className="mono">{stats.total_real_runs}</dd>
          <dt>Completed</dt>
          <dd className="mono">{stats.completed}</dd>
          <dt>Failed</dt>
          <dd className="mono">{stats.failed}</dd>
          <dt>Blocked</dt>
          <dd className="mono">{stats.blocked}</dd>
          <dt>Cancelled</dt>
          <dd className="mono">{stats.cancelled}</dd>
        </dl>
        {stats.example_runs_excluded > 0 ? (
          <p className="small muted" style={{ marginTop: "0.7rem" }}>
            {stats.example_runs_excluded} labelled example report is excluded
            from these counts.
          </p>
        ) : null}
      </div>

      {data.runs.length === 0 ? (
        <div className="card">
          <p className="muted">
            No runs yet. <Link href="/">Start one</Link>.
          </p>
        </div>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Repository</th>
                <th>Task</th>
                <th>Status</th>
                <th>Started</th>
                <th>Duration</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {data.runs.map((run) => {
                const duration =
                  run.started_at && run.finished_at
                    ? new Date(run.finished_at).getTime() -
                      new Date(run.started_at).getTime()
                    : null;
                return (
                  <tr key={run.id}>
                    <td>
                      <Link href={`/runs/${run.id}`}>{run.repo_name}</Link>
                      {run.is_example ? (
                        <>
                          {" "}
                          <span className="badge attention">example — not a real run</span>
                        </>
                      ) : null}
                      {run.goal ? (
                        <>
                          <br />
                          <span className="muted small">{run.goal}</span>
                        </>
                      ) : null}
                    </td>
                    <td className="small">
                      {run.task_type === "setup_check"
                        ? "Setup check"
                        : "Small experiment"}
                    </td>
                    <td>
                      <StatusBadge status={run.status} animate />
                    </td>
                    <td className="mono small">
                      {run.started_at
                        ? new Date(run.started_at).toLocaleString()
                        : "—"}
                    </td>
                    <td className="mono small">{formatDuration(duration)}</td>
                    <td>
                      <Link href={`/runs/${run.id}`} className="small">
                        open
                      </Link>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
