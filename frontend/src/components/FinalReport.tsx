import { api, formatDuration, type Manifest, type Patch } from "@/lib/api";
import { StatusBadge } from "./StatusBadge";

function Ladder({ outcomes }: { outcomes: Manifest["outcomes"] }) {
  const rows: { label: string; value: boolean; keystone?: boolean }[] = [
    { label: "Repository inspected", value: outcomes.repository_inspected },
    { label: "Environment installed", value: outcomes.environment_installed },
    { label: "Smoke test passed", value: outcomes.smoke_test_passed },
    { label: "Small experiment completed", value: outcomes.experiment_completed },
    {
      label: "A specific reported result reproduced under matching conditions",
      value: outcomes.specific_result_reproduced,
      keystone: true,
    },
  ];
  return (
    <>
      <ul className="ladder">
        {rows.map((row) => (
          <li key={row.label} className={row.keystone ? "keystone" : ""}>
            <span className={`mark ${row.value ? "yes" : "no"}`}>
              {row.value ? "YES" : "NO"}
            </span>
            <span>{row.label}</span>
          </li>
        ))}
      </ul>
      {outcomes.reproduction_note ? (
        <p className="small muted" style={{ marginTop: "0.7rem" }}>
          {outcomes.reproduction_note}
        </p>
      ) : null}
    </>
  );
}

function List({ items, empty }: { items: string[]; empty: string }) {
  if (!items || items.length === 0) {
    return <p className="muted small">{empty}</p>;
  }
  return (
    <ul className="plain">
      {items.map((item, index) => (
        <li key={index}>{item}</li>
      ))}
    </ul>
  );
}

function Diffs({ patches, diff }: { patches: Patch[]; diff: string }) {
  if (patches.length === 0 && !diff) return null;
  return (
    <>
      {patches.map((patch, index) => (
        <div key={index} style={{ marginBottom: "0.8rem" }}>
          <p className="small">
            <strong>
              Repair attempt {patch.attempt} — <code>{patch.path}</code>
            </strong>
            <br />
            {patch.rationale}
          </p>
        </div>
      ))}
      {diff ? (
        <pre>
          <code>{diff.slice(0, 20000)}</code>
        </pre>
      ) : null}
    </>
  );
}

export function FinalReport({
  runId,
  manifest,
  isExample,
}: {
  runId: string;
  manifest: Manifest;
  isExample: boolean;
}) {
  const { status, outcomes, assessment, measurements, evidence } = manifest;
  const runner = manifest.execution_environment.runner as Record<string, unknown>;
  const artifacts = manifest.artifacts.filter((a) => a.collected);

  return (
    <>
      {isExample ? (
        <div className="notice example">
          <strong>Example report — not a real run</strong>
          This is a worked illustration of ReproLab&apos;s output format. It is
          not evidence of a reproduction, and it is excluded from run history
          statistics.
        </div>
      ) : null}

      <div className="card">
        <h2>Final report</h2>
        <div
          className="actions"
          style={{ marginBottom: "0.9rem", alignItems: "center" }}
        >
          <StatusBadge status={status.value} />
          <span className="small muted">{status.meaning}</span>
        </div>
        {assessment.headline ? (
          <p style={{ fontSize: "1.02rem" }}>{assessment.headline}</p>
        ) : null}
        {status.detail ? <p className="small">{status.detail}</p> : null}

        <div className="actions" style={{ marginTop: "1rem" }}>
          <a href={api.reportUrl(runId)} download={`reprolab-${runId}.md`}>
            <button type="button">Download Markdown report</button>
          </a>
          <a href={api.manifestUrl(runId)} download={`reprolab-${runId}.json`}>
            <button type="button">Download JSON run manifest</button>
          </a>
        </div>
      </div>

      <div className="grid grid-2">
        <div className="stack">
          <div className="card">
            <h3>What was attempted</h3>
            <p className="small">{assessment.what_was_attempted}</p>
          </div>

          <div className="card">
            <h3>What the paper reports</h3>
            <p className="small">
              {assessment.what_the_paper_reports || (
                <span className="muted">Not summarised for this run.</span>
              )}
            </p>
            <h3 style={{ marginTop: "1rem" }}>What ReproLab actually measured</h3>
            <p className="small">
              {assessment.what_reprolab_measured || (
                <span className="muted">No measurement was obtained.</span>
              )}
            </p>
          </div>

          <div className="card">
            <h3>Measured outputs and their evidence</h3>
            {measurements.verified.length === 0 ? (
              <div className="notice warn" style={{ marginBottom: 0 }}>
                <strong>No reliable metric was extracted</strong>
                ReproLab reports only numbers it can locate verbatim in a
                recorded log or output file. It does not infer metrics from
                narrative text.
              </div>
            ) : (
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>Metric</th>
                      <th>Value</th>
                      <th>Evidence</th>
                      <th>Parsed from</th>
                    </tr>
                  </thead>
                  <tbody>
                    {measurements.verified.map((metric, index) => (
                      <tr key={index}>
                        <td>{metric.name}</td>
                        <td className="mono">
                          {metric.raw_value}
                          {metric.unit ? ` ${metric.unit}` : ""}
                        </td>
                        <td className="mono">
                          {metric.evidence_id ??
                            (metric as unknown as { source_file?: string })
                              .source_file ??
                            "—"}
                        </td>
                        <td className="mono small">{metric.provenance}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            {measurements.discarded_claims.length > 0 ? (
              <div className="notice warn" style={{ marginTop: "0.9rem" }}>
                <strong>Discarded measurement claims</strong>
                <ul className="plain">
                  {measurements.discarded_claims.map((claim, index) => (
                    <li key={index}>
                      <code>{claim.name}</code> — {claim.reason}
                      {claim.claimed_line ? (
                        <>
                          <br />
                          <span className="muted small">
                            claimed: {claim.claimed_line}
                          </span>
                        </>
                      ) : null}
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}
          </div>

          <div className="card">
            <h3>Exact commands and exit codes</h3>
            {evidence.length === 0 ? (
              <p className="muted small">No commands were executed.</p>
            ) : (
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>Evidence</th>
                      <th>Network</th>
                      <th>Command</th>
                      <th>Exit</th>
                      <th>Duration</th>
                    </tr>
                  </thead>
                  <tbody>
                    {evidence.map((item) => {
                      const row = item as Record<string, never>;
                      const exit = row["exit_code"] as unknown as number | null;
                      const ok = exit === 0;
                      return (
                        <tr key={String(row["evidence_id"])}>
                          <td className="mono">{String(row["evidence_id"])}</td>
                          <td className="small">
                            {String(row["network"]).startsWith("none") ? (
                              <span className="badge attention">isolated</span>
                            ) : (
                              <span className="badge active">network</span>
                            )}
                          </td>
                          <td>
                            <code>{String(row["command"])}</code>
                          </td>
                          <td>
                            <span className={`badge ${ok ? "ok" : "bad"}`}>
                              {exit === null ? "—" : exit}
                            </span>
                          </td>
                          <td className="mono small">
                            {formatDuration(
                              row["duration_ms"] as unknown as number,
                            )}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </div>

          <div className="card">
            <h3>Original versus modified implementation</h3>
            <p className="small">{manifest.implementation.note}</p>
            <Diffs
              patches={manifest.implementation.patches}
              diff={manifest.implementation.diff}
            />
          </div>
        </div>

        <div className="stack">
          <div className="card">
            <h3>Outcome ladder</h3>
            <p className="card-note">
              Deliberately separate. Reaching one step never implies the next.
            </p>
            <Ladder outcomes={outcomes} />
          </div>

          <div className="card">
            <h3>Differences from the paper&apos;s conditions</h3>
            <List
              items={assessment.differences_from_paper}
              empty="No differences were recorded. Treat this as incomplete rather than as agreement with the paper."
            />
          </div>

          <div className="card">
            <h3>What remains unverified</h3>
            <List
              items={assessment.remains_unverified}
              empty="Not stated. Assume the paper's quantitative claims are unverified."
            />
          </div>

          <div className="card">
            <h3>Missing resources</h3>
            <List items={assessment.missing_resources} empty="None recorded." />
            <h3 style={{ marginTop: "1rem" }}>Limitations</h3>
            <List items={assessment.limitations} empty="None recorded." />
          </div>

          <div className="card">
            <h3>Environment</h3>
            <dl className="kv">
              <dt>Repository commit</dt>
              <dd className="mono small">
                {String(manifest.run.repository_commit)}
              </dd>
              <dt>Interpreter</dt>
              <dd className="mono small">
                {manifest.execution_environment.interpreter}
              </dd>
              <dt>Runner</dt>
              <dd className="small">
                {String(runner.backend ?? "—")}
                {runner.image ? (
                  <>
                    <br />
                    <span className="mono small">{String(runner.image)}</span>
                  </>
                ) : null}
              </dd>
              <dt>Isolation</dt>
              <dd className="small">{String(runner.isolation ?? "—")}</dd>
              <dt>Credentials in container</dt>
              <dd className="small">none</dd>
              <dt>Model</dt>
              <dd className="mono small">{manifest.inference.model || "—"}</dd>
            </dl>
          </div>

          {artifacts.length > 0 ? (
            <div className="card">
              <h3>Collected artifacts</h3>
              <ul className="plain">
                {artifacts.map((artifact) => (
                  <li key={artifact.name} className="mono small">
                    {artifact.name} ({artifact.bytes} bytes)
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
        </div>
      </div>
    </>
  );
}
