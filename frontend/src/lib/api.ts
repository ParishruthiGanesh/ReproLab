/**
 * Typed client for the ReproLab backend.
 *
 * Run state lives in the backend's database, so every view reads from it
 * rather than holding progress in React state. A refresh re-reads the same
 * truth.
 */

export const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE ?? "http://127.0.0.1:8000";

export type TaskType = "setup_check" | "small_experiment";

export type RunStatus =
  | "Queued"
  | "Active"
  | "Awaiting approval"
  | "Completed"
  | "Failed"
  | "Blocked"
  | "Cancelled"
  | "Not attempted";

export interface RunSummary {
  id: string;
  created_at: string;
  updated_at: string;
  started_at: string | null;
  finished_at: string | null;
  paper_url: string;
  repo_url: string;
  repo_name: string;
  repo_commit: string | null;
  goal: string;
  task_type: TaskType;
  runtime_budget_seconds: number;
  status: RunStatus;
  phase: string;
  outcome: string | null;
  outcome_detail: string | null;
  model_used: string | null;
  repair_attempts: number;
  cancel_requested: boolean;
  is_example: boolean;
  is_terminal: boolean;
  has_report: boolean;
}

export interface PlannedCommand {
  argv: string[];
  command: string;
  phase: "setup" | "experiment";
  purpose: string;
  workdir: string;
}

export interface RequiredResource {
  name: string;
  kind: string;
  available: boolean;
  detail: string;
}

export interface Plan {
  objective: string;
  paper_claim_summary: string;
  what_this_run_can_show: string;
  required_resources: RequiredResource[];
  setup_commands: PlannedCommand[];
  experiment_commands: PlannedCommand[];
  rejected_commands: { command: string; reason: string }[];
  expected_outputs: string[];
  blockers: string[];
  estimated_runtime_seconds: number;
  runtime_budget_seconds: number;
  is_blocked: boolean;
  blocked_reason: string;
  confidence: string;
  limits: Record<string, unknown>;
  model: string;
}

export interface TimelineEvent {
  id: number;
  ts: string;
  kind: string;
  title: string;
  detail: string;
  data: Record<string, unknown> | null;
}

export interface Step {
  evidence_id: string;
  index: number;
  kind: string;
  purpose: string;
  command: string;
  phase: string;
  network: string;
  workdir: string;
  status: string;
  exit_code: number | null;
  duration_ms: number | null;
  log_bytes: number;
  truncated: boolean;
  timed_out: boolean;
  repair_attempt: number | null;
}

export interface Source {
  kind: string;
  title: string;
  url: string;
  snippet: string;
  retrieved_via: string;
}

export interface Patch {
  attempt: number;
  path: string;
  diff: string;
  rationale: string;
  applied: boolean;
}

export interface Metric {
  name: string;
  value: number | null;
  raw_value: string;
  unit: string;
  parser: string;
  provenance: string;
  evidence_id: string;
}

export interface AgentAction {
  ts: string;
  tool: string;
  ok: boolean;
  duration_ms: number;
  summary: string;
  input: Record<string, unknown> | null;
  output: Record<string, unknown> | null;
}

export interface RunDetail extends RunSummary {
  plan: Plan | null;
  manifest: Manifest | null;
  environment: Record<string, unknown> | null;
  error: string | null;
  events: TimelineEvent[];
  steps: Step[];
  sources: Source[];
  patches: Patch[];
  metrics: Metric[];
  agent_actions: AgentAction[];
  notice?: string;
}

export interface Manifest {
  schema_version: string;
  generated_at: string;
  is_example: boolean;
  run: Record<string, string | number>;
  status: { value: string; meaning: string; detail: string; blocked_category: string };
  outcomes: {
    repository_inspected: boolean;
    environment_installed: boolean;
    smoke_test_passed: boolean;
    experiment_completed: boolean;
    specific_result_reproduced: boolean;
    reproduction_note: string;
  };
  inference: Record<string, string>;
  retrieval: Record<string, string | boolean>;
  execution_environment: {
    interpreter: string;
    runner: Record<string, unknown>;
    limits: Record<string, number | boolean>;
  };
  plan: Partial<Plan>;
  evidence: Record<string, unknown>[];
  measurements: {
    verified: Metric[];
    discarded_claims: { name: string; reason: string; claimed_line?: string }[];
    note: string;
  };
  artifacts: { name: string; bytes: number; collected: boolean; reason: string }[];
  sources: Source[];
  implementation: {
    modified: boolean;
    patches: Patch[];
    diff: string;
    note: string;
  };
  assessment: {
    headline: string;
    what_was_attempted: string;
    what_the_paper_reports: string;
    what_reprolab_measured: string;
    differences_from_paper: string[];
    remains_unverified: string[];
    limitations: string[];
    missing_resources: string[];
  };
  agent_actions: Record<string, unknown>[];
  repair_attempts: number;
}

export interface Health {
  service: string;
  version: string;
  status: string;
  integrations: {
    nebius_token_factory: {
      configured: boolean;
      base_url: string;
      requested_model: string;
      autoselect: boolean;
      role: string;
    };
    tavily: { configured: boolean; role: string };
  };
  runner: Record<string, unknown> & { available?: boolean; error?: string };
  policy: {
    curated_repositories_only: boolean;
    api_token_required: boolean;
    allowed_programs: string[];
  };
  limits: Record<string, number | boolean | number[]>;
}

export interface CuratedRepo {
  slug: string;
  name: string;
  paper_url: string;
  paper_title: string;
  repo_url: string;
  license: string;
  approx_size_mb: number;
  notes: string;
  expected_blockers: string[];
  demo_label: string;
}

export interface RunList {
  runs: RunSummary[];
  statistics: {
    total_real_runs: number;
    completed: number;
    failed: number;
    blocked: number;
    cancelled: number;
    example_runs_excluded: number;
  };
}

class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const token =
    typeof window !== "undefined"
      ? window.localStorage.getItem("reprolab_token")
      : null;
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    cache: "no-store",
    headers: {
      "Content-Type": "application/json",
      ...(token ? { "X-ReproLab-Token": token } : {}),
      ...(init?.headers ?? {}),
    },
  });
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      if (body?.detail) {
        detail =
          typeof body.detail === "string"
            ? body.detail
            : JSON.stringify(body.detail);
      }
    } catch {
      /* keep the status-line message */
    }
    throw new ApiError(detail, response.status);
  }
  const contentType = response.headers.get("content-type") ?? "";
  if (contentType.includes("application/json")) {
    return (await response.json()) as T;
  }
  return (await response.text()) as unknown as T;
}

export const api = {
  health: () => request<Health>("/api/health"),
  verifyIntegrations: () =>
    request<Record<string, Record<string, unknown>>>("/api/integrations/verify"),
  curated: () =>
    request<{ curated_only: boolean; repositories: CuratedRepo[] }>("/api/curated"),
  listRuns: () => request<RunList>("/api/runs"),
  getRun: (id: string) => request<RunDetail>(`/api/runs/${id}`),
  example: () => request<RunDetail>("/api/example"),
  createRun: (body: {
    paper_url: string;
    repo_url: string;
    goal: string;
    task_type: TaskType;
    runtime_budget_seconds: number;
  }) =>
    request<{ id: string; status: string }>("/api/runs", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  approve: (id: string) =>
    request<{ id: string }>(`/api/runs/${id}/approve`, { method: "POST" }),
  decline: (id: string) =>
    request<{ id: string }>(`/api/runs/${id}/decline`, { method: "POST" }),
  cancel: (id: string) =>
    request<{ id: string; terminated_execution: boolean }>(
      `/api/runs/${id}/cancel`,
      { method: "POST" },
    ),
  stepLog: (id: string, evidenceId: string) =>
    request<string>(`/api/runs/${id}/logs/${evidenceId}`),
  reportUrl: (id: string) => `${API_BASE}/api/runs/${id}/report.md`,
  manifestUrl: (id: string) => `${API_BASE}/api/runs/${id}/manifest.json`,
  streamUrl: (id: string, afterEvent = 0) =>
    `${API_BASE}/api/runs/${id}/stream?after=${afterEvent}`,
};

export { ApiError };

export function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "—";
  if (ms < 1000) return `${ms} ms`;
  const seconds = ms / 1000;
  if (seconds < 60) return `${seconds.toFixed(1)} s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes}m ${Math.round(seconds % 60)}s`;
}

export function elapsedSince(iso: string | null): number {
  if (!iso) return 0;
  return Math.max(0, Date.now() - new Date(iso).getTime());
}

export function statusTone(status: string): string {
  switch (status) {
    case "Completed":
      return "ok";
    case "Failed":
      return "bad";
    case "Blocked":
      return "warn";
    case "Cancelled":
    case "Not attempted":
      return "muted";
    case "Awaiting approval":
      return "attention";
    default:
      return "active";
  }
}
