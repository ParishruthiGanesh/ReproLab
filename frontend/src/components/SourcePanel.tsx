import type { Source } from "@/lib/api";

const LABELS: Record<string, string> = {
  paper: "Paper",
  repository: "Repository",
  repository_file: "Repository file",
  documentation: "Documentation",
  research: "Research search",
  troubleshooting: "Error troubleshooting",
};

export function SourcePanel({ sources }: { sources: Source[] }) {
  if (sources.length === 0) {
    return (
      <div className="card">
        <h3>Sources</h3>
        <p className="muted small">No sources recorded yet.</p>
      </div>
    );
  }
  return (
    <div className="card">
      <h3>Sources ({sources.length})</h3>
      <p className="card-note">
        Everything retrieved is treated as untrusted data and recorded here, so
        each claim in the report can be traced to where it came from.
      </p>
      <div className="stack">
        {sources.map((source, index) => (
          <details key={`${source.url}-${index}`} className="evidence">
            <summary>
              <span className="badge muted">
                {LABELS[source.kind] ?? source.kind}
              </span>{" "}
              {source.title || source.url || "(untitled)"}
            </summary>
            <div className="body">
              {source.url ? (
                <p className="small" style={{ margin: "0 0 0.4rem" }}>
                  <a href={source.url} target="_blank" rel="noreferrer">
                    {source.url}
                  </a>
                </p>
              ) : null}
              {source.retrieved_via ? (
                <p className="small muted" style={{ margin: "0 0 0.4rem" }}>
                  retrieved via <code>{source.retrieved_via}</code>
                </p>
              ) : null}
              {source.snippet ? (
                <p className="small" style={{ margin: 0, whiteSpace: "pre-wrap" }}>
                  {source.snippet}
                </p>
              ) : null}
            </div>
          </details>
        ))}
      </div>
    </div>
  );
}
