import type { TimelineEvent } from "@/lib/api";

const TONE_BY_KIND: Record<string, string> = {
  done: "tone-ok",
  outcome: "tone-ok",
  error: "tone-bad",
  validation: "tone-warn",
  diagnosis: "tone-warn",
  repair: "tone-warn",
  budget: "tone-warn",
  warning: "tone-warn",
  cancel: "tone-bad",
  phase: "tone-accent",
  approval: "tone-accent",
  plan: "tone-accent",
  patch: "tone-warn",
};

function clock(ts: string): string {
  const date = new Date(ts);
  return Number.isNaN(date.getTime())
    ? ts
    : date.toLocaleTimeString([], {
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
      });
}

/**
 * Concise action summaries only. ReproLab records what the agent did and the
 * tool inputs and outputs; it never requests or displays hidden reasoning.
 */
export function Timeline({ events }: { events: TimelineEvent[] }) {
  if (events.length === 0) {
    return <p className="muted small">No activity recorded yet.</p>;
  }
  return (
    <ol className="timeline">
      {events.map((event) => (
        <li key={event.id} className={TONE_BY_KIND[event.kind] ?? ""}>
          <div className="timeline-title">{event.title}</div>
          <div className="timeline-meta">
            {clock(event.ts)} · {event.kind}
          </div>
          {event.detail ? (
            <div className="timeline-detail">{event.detail}</div>
          ) : null}
        </li>
      ))}
    </ol>
  );
}
