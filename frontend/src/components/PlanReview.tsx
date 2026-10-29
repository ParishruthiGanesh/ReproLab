"use client";

import { useState } from "react";
import type { Plan } from "@/lib/api";

/**
 * The approval gate. Everything that will be executed is shown verbatim,
 * with the limits that will be enforced, before any downloaded code runs.
 */
export function PlanReview({
  plan,
  onApprove,
  onDecline,
  busy,
}: {
  plan: Plan;
  onApprove: () => void;
  onDecline: () => void;
  busy: boolean;
}) {
  const [confirmed, setConfirmed] = useState(false);
  const total =
    plan.setup_commands.length + plan.experiment_commands.length;

  return (
    <div className="card">
      <h2>Plan review</h2>
      <p className="card-note">
        No repository code has been executed yet. Review the commands and
        limits, then approve.
      </p>

      <h3>Objective</h3>
      <p>{plan.objective}</p>

      {plan.paper_claim_summary ? (
        <>
          <h3>What the paper reports</h3>
          <p className="small">{plan.paper_claim_summary}</p>
        </>
      ) : null}

      {plan.what_this_run_can_show ? (
        <div className="notice info">
          <strong>What a successful run would and would not show</strong>
          {plan.what_this_run_can_show}
        </div>
      ) : null}

      <h3>Required resources</h3>
      {plan.required_resources.length === 0 ? (
        <p className="muted small">None identified.</p>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Resource</th>
                <th>Kind</th>
                <th>Available here</th>
                <th>Detail</th>
              </tr>
            </thead>
            <tbody>
              {plan.required_resources.map((resource, index) => (
                <tr key={`${resource.name}-${index}`}>
                  <td>{resource.name}</td>
                  <td className="mono">{resource.kind}</td>
                  <td>
                    <span className={`badge ${resource.available ? "ok" : "warn"}`}>
                      {resource.available ? "yes" : "no"}
                    </span>
                  </td>
                  <td className="small">{resource.detail}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <h3 style={{ marginTop: "1.1rem" }}>Proposed commands ({total})</h3>
      <p className="small muted">
        Each command was parsed into an argument list and checked against
        ReproLab&apos;s program allowlist. No shell is used. Setup commands have
        network access for installing dependencies; experiment commands run with
        no network at all.
      </p>
      {total === 0 ? (
        <p className="muted small">No executable command survived validation.</p>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>#</th>
                <th>Phase</th>
                <th>Command</th>
                <th>Purpose</th>
              </tr>
            </thead>
            <tbody>
              {[...plan.setup_commands, ...plan.experiment_commands].map(
                (command, index) => (
                  <tr key={`${command.command}-${index}`}>
                    <td className="mono">{index + 1}</td>
                    <td>
                      <span
                        className={`badge ${
                          command.phase === "setup" ? "active" : "attention"
                        }`}
                      >
                        {command.phase === "setup" ? "setup · network" : "experiment · no network"}
                      </span>
                    </td>
                    <td>
                      <code>{command.command}</code>
                    </td>
                    <td className="small">{command.purpose || "—"}</td>
                  </tr>
                ),
              )}
            </tbody>
          </table>
        </div>
      )}

      {plan.rejected_commands.length > 0 ? (
        <div className="notice warn" style={{ marginTop: "1rem" }}>
          <strong>
            {plan.rejected_commands.length} proposed command(s) were rejected by
            ReproLab&apos;s validator
          </strong>
          <ul className="plain">
            {plan.rejected_commands.map((item, index) => (
              <li key={index}>
                <code>{item.command}</code> — {item.reason}
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {plan.expected_outputs.length > 0 ? (
        <>
          <h3>Expected outputs</h3>
          <ul className="plain">
            {plan.expected_outputs.map((output, index) => (
              <li key={index}>{output}</li>
            ))}
          </ul>
        </>
      ) : null}

      {plan.blockers.length > 0 ? (
        <div className="notice warn">
          <strong>Known blockers</strong>
          <ul className="plain">
            {plan.blockers.map((blocker, index) => (
              <li key={index}>{blocker}</li>
            ))}
          </ul>
        </div>
      ) : null}

      <h3>Limits that will be enforced</h3>
      <dl className="kv">
        <dt>Estimated runtime</dt>
        <dd>
          {plan.estimated_runtime_seconds} s (budget{" "}
          {plan.runtime_budget_seconds} s)
        </dd>
        <dt>Per-command timeout</dt>
        <dd>{String(plan.limits.step_timeout_seconds ?? "—")} s</dd>
        <dt>CPU / memory</dt>
        <dd>
          {String(plan.limits.container_cpus ?? "—")} cores ·{" "}
          {String(plan.limits.container_memory_mb ?? "—")} MB · no GPU
        </dd>
        <dt>Max log captured</dt>
        <dd>
          {Math.round(Number(plan.limits.max_log_bytes ?? 0) / 1024)} KB per
          command
        </dd>
        <dt>Repair attempts</dt>
        <dd>at most {String(plan.limits.max_repairs ?? 2)}</dd>
        <dt>Planned by</dt>
        <dd className="mono">{plan.model || "—"}</dd>
        <dt>Planner confidence</dt>
        <dd>{plan.confidence}</dd>
      </dl>

      <label
        style={{
          display: "flex",
          gap: "0.5rem",
          alignItems: "flex-start",
          marginTop: "1.1rem",
          fontWeight: 400,
          color: "var(--ink-soft)",
          fontSize: "0.86rem",
        }}
      >
        <input
          type="checkbox"
          checked={confirmed}
          onChange={(e) => setConfirmed(e.target.checked)}
          style={{ width: "auto", marginTop: "0.25rem" }}
        />
        <span>
          I have read the commands above and approve executing this
          repository&apos;s code in ReproLab&apos;s isolated container.
        </span>
      </label>

      <div className="actions" style={{ marginTop: "0.9rem" }}>
        <button
          className="primary"
          onClick={onApprove}
          disabled={busy || !confirmed || total === 0}
        >
          {busy ? "Starting…" : "Approve execution"}
        </button>
        <button className="danger" onClick={onDecline} disabled={busy}>
          Decline
        </button>
      </div>
    </div>
  );
}
