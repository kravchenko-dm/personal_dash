const TOKEN_KEY = "dash.token";

export function getToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function setToken(token: string | null) {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token);
    else localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* private mode */
  }
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers: Record<string, string> = { ...(init.headers as Record<string, string>) };
  if (init.body && !(init.body instanceof FormData)) headers["Content-Type"] = "application/json";
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  const res = await fetch(path, { ...init, headers });
  if (res.status === 401) {
    setToken(null);
    window.dispatchEvent(new Event("dash:logout"));
    throw new ApiError(401, "Нужно войти");
  }
  if (!res.ok) {
    let msg = res.statusText;
    try {
      const body = await res.json();
      msg = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail ?? body);
    } catch {
      /* keep statusText */
    }
    throw new ApiError(res.status, msg);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export const get = <T>(p: string) => api<T>(p);
export const post = <T>(p: string, body?: unknown) =>
  api<T>(p, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });
export const put = <T>(p: string, body: unknown) => api<T>(p, { method: "PUT", body: JSON.stringify(body) });
export const patch = <T>(p: string, body: unknown) => api<T>(p, { method: "PATCH", body: JSON.stringify(body) });
export const del = (p: string) => api<void>(p, { method: "DELETE" });

// ---------- types ----------

export type TaskStatus = "todo" | "doing" | "done";

export interface Task {
  id: number;
  title: string;
  notes: string | null;
  status: TaskStatus;
  priority: 1 | 2 | 3;
  due_at: string | null;
  source: string;
  source_ref: string | null;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
}

export interface ProviderField {
  key: string;
  label: string;
  kind: "text" | "secret" | "number" | "url";
  required: boolean;
  placeholder: string;
  help: string;
}

export interface ProviderInfo {
  key: string;
  title: string;
  description: string;
  fields: ProviderField[];
}

export interface BalanceAccount {
  id: number;
  name: string;
  provider: string;
  config: Record<string, string>;
  currency: string;
  alert_below: number | null;
  enabled: boolean;
  last_value: number | null;
  last_extra: Record<string, unknown> | null;
  last_error: string | null;
  last_checked_at: string | null;
}

export interface Summary {
  id: number;
  chat_id: number | null;
  project_id: number | null;
  period_start: string;
  period_end: string;
  text: string;
  proposed_tasks: { title: string; reason: string; priority: number; chat: string }[];
  created_at: string;
}

export interface TelegramChat {
  id: number;
  chat_id: number;
  title: string;
  kind: string;
  collect: boolean;
  project_id: number | null;
}

export interface CalendarEvent {
  id: string;
  calendar_id: string;
  calendar?: string;
  source: "google" | "icloud";
  title: string;
  start: string;
  end: string;
  all_day: boolean;
  location: string | null;
  link: string | null;
  description: string;
}

export interface Status {
  push: boolean;
  telegram: boolean;
  claude: boolean;
  llm_provider: string;
  llm_model: string;
  google: boolean;
  google_connected: boolean;
  yougile: boolean;
  icloud: boolean;
  vapid_public_key: string;
  bot_username: string | null;
}

export interface YgOverview {
  projects: { id: string; title: string }[];
  boards: { id: string; title: string; project_id: string }[];
  columns: { id: string; title: string; board_id: string; color?: number }[];
  tasks: YgTask[];
  users: { id: string; name: string }[];
}

export interface YgTask {
  id: string;
  title: string;
  description: string;
  completed: boolean;
  column_id: string | null;
  column: string | null;
  board_id: string | null;
  board: string | null;
  project_id: string | null;
  deadline: number | null;
  assigned: string[];
  assigned_ids: string[];
  updated: number | null;
}

export interface AppleReminder {
  id: number;
  title: string;
  notes: string | null;
  list_name: string;
  due_at: string | null;
  priority: number;
  flagged: boolean;
  url: string | null;
  source: "shortcuts" | "caldav";
  imported_task_id: number | null;
  synced_at: string;
}

export interface AppleRemindersResp {
  items: AppleReminder[];
  last_sync: { at: string; count: number; source: string } | null;
}

export type Stage = "idea" | "dev" | "beta" | "prod" | "paused";
export type FeatureStatus = "idea" | "planned" | "in_progress" | "testing" | "done";

export interface Project {
  id: number;
  name: string;
  slug: string;
  description: string | null;
  stage: Stage;
  color: string;
  repo_url: string | null;
  site_url: string | null;
  yougile_board_id: string | null;
  status_note: string | null;
  status_note_at: string | null;
  archived: boolean;
  created_at: string;
  updated_at: string;
  feature_counts: Record<FeatureStatus, number>;
  chat_count: number;
  health: "ok" | "warning" | "critical";
  open_incidents: number;
}

export interface Feature {
  id: number;
  project_id: number;
  title: string;
  description: string | null;
  status: FeatureStatus;
  priority: 1 | 2 | 3;
  eta: string | null;
  position: number;
  link_url: string | null;
  source: string;
  created_at: string;
  updated_at: string;
  done_at: string | null;
}

export interface ProjectNote {
  id: number;
  project_id: number;
  title: string;
  body: string;
  pinned: boolean;
  position: number;
  created_at: string;
  updated_at: string;
}

export interface ProjectDetail {
  project: Project;
  features: Feature[];
  notes: ProjectNote[];
  chats: TelegramChat[];
  summaries: Summary[];
}

export interface ServerService {
  name: string;
  kind: "systemd" | "docker" | "process";
  status: string;
  detail?: string;
  restarts?: number;
  started_at?: string | null;
  discovered?: boolean;
  scheduled?: boolean;
  last_run?: string | null;
  next_run?: string | null;
  result?: string;
  version?: { kind: string; value: string; branch?: string; date?: string };
  http?: { ok: boolean; status: number | null; latency_ms: number; error?: string };
  image?: string;
}

export interface ServerReport {
  metrics: {
    load1: number;
    load5: number;
    cpus: number;
    mem_total: number;
    mem_used: number;
    mem_pct: number;
    uptime: number;
    disks: { path: string; total: number; used: number; pct: number }[];
  };
  services: ServerService[];
  failed_units: string[];
}

export interface Server {
  id: number;
  project_id: number;
  name: string;
  hostname: string | null;
  os: string | null;
  agent_version: string | null;
  last_seen_at: string | null;
  last_report: ServerReport | null;
  disk_alert_pct: number;
  silence_alert_min: number;
  created_at: string;
  online: boolean;
  token?: string;
  install_command?: string;
}

export interface AgentCommand {
  id: number;
  server_id: number;
  kind: string;
  args: Record<string, unknown>;
  status: "queued" | "sent" | "done" | "failed";
  ok: boolean | null;
  output: string | null;
  created_at: string;
  finished_at: string | null;
}

export interface Monitor {
  id: number;
  project_id: number;
  name: string;
  url: string;
  method: string;
  expect_status: number;
  expect_text: string | null;
  timeout_s: number;
  interval_s: number;
  enabled: boolean;
  last_ok: boolean | null;
  last_status: number | null;
  last_latency_ms: number | null;
  last_error: string | null;
  last_checked_at: string | null;
  consecutive_failures: number;
}

export interface Incident {
  id: number;
  project_id: number;
  server_id: number | null;
  monitor_id: number | null;
  kind: string;
  key: string;
  severity: "warning" | "critical";
  message: string;
  started_at: string;
  resolved_at: string | null;
}
