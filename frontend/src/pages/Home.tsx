import { Link } from "react-router-dom";
import { type AppleRemindersResp, type BalanceAccount, type CalendarEvent, type Project, type Summary, type Task } from "../lib/api";
import { STAGE_LABEL } from "../lib/format";
import { useFetch } from "../lib/useFetch";
import { fmtDate, fmtMoney } from "../lib/format";
import { TaskRow } from "./Tasks";
import { EventList } from "./Calendar";

export default function Home() {
  const tasks = useFetch<Task[]>("/api/tasks");
  const balances = useFetch<BalanceAccount[]>("/api/balances");
  const events = useFetch<{ connected: boolean; events: CalendarEvent[] }>("/api/calendar/events?days=1");
  const summaries = useFetch<Summary[]>("/api/summaries?limit=1");
  const reminders = useFetch<AppleRemindersResp>("/api/apple/reminders");
  const projects = useFetch<Project[]>("/api/projects");

  const open = (tasks.data ?? []).filter((t) => t.status !== "done");
  const today = open.filter((t) => t.due_at && new Date(t.due_at).getTime() < Date.now() + 86400000);
  const top = (today.length ? today : open).slice(0, 5);
  const latest = summaries.data?.[0];

  function applyChange(id: number, t: Task | null) {
    tasks.setData((prev) => (prev ?? []).flatMap((x) => (x.id === id ? (t ? [t] : []) : [x])));
  }

  return (
    <div className="page">
      <div className="page-head">
        <h1>{new Date().toLocaleDateString("ru-RU", { weekday: "long", day: "numeric", month: "long" })}</h1>
      </div>

      <div className="card" style={{ padding: 0 }}>
        <div className="row between" style={{ padding: "12px 14px 4px" }}>
          <h3 style={{ margin: 0 }}>{today.length ? "Срочные задачи" : "Задачи"}</h3>
          <Link to="/tasks" className="small">
            все {open.length} ›
          </Link>
        </div>
        {top.length === 0 && <p className="muted" style={{ padding: "4px 14px 14px" }}>Открытых задач нет 🎉</p>}
        {top.map((t) => (
          <TaskRow key={t.id} task={t} onChange={(nt) => applyChange(t.id, nt)} />
        ))}
      </div>

      {(projects.data?.length ?? 0) > 0 && (
        <div className="card">
          <div className="row between">
            <h3>Проекты</h3>
            <Link to="/projects" className="small">
              ›
            </Link>
          </div>
          {projects.data!.map((p) => {
            const wip = (p.feature_counts.in_progress ?? 0) + (p.feature_counts.testing ?? 0);
            return (
              <Link key={p.id} to={`/projects/${p.id}`} className="row between" style={{ padding: "6px 0", color: "inherit", borderLeft: `3px solid ${p.color}`, paddingLeft: 8 }}>
                <span>
                  {p.name} <span className="chip">{STAGE_LABEL[p.stage]}</span>
                </span>
                <span className="small muted">{p.status_note ? p.status_note.slice(0, 40) : `${wip} в работе`}</span>
              </Link>
            );
          })}
        </div>
      )}

      {(reminders.data?.items.length ?? 0) > 0 && (
        <div className="card">
          <div className="row between">
            <h3> Напоминания Apple</h3>
            <Link to="/tasks" className="small">
              ›
            </Link>
          </div>
          {reminders.data!.items.slice(0, 5).map((r) => (
            <div key={r.id} className="small" style={{ padding: "3px 0" }}>
              {r.title}
              {r.due_at && <span className="muted"> · {fmtDate(r.due_at)}</span>}
            </div>
          ))}
        </div>
      )}

      {(balances.data?.length ?? 0) > 0 && (
        <div className="card">
          <div className="row between">
            <h3>Балансы</h3>
            <Link to="/balances" className="small">
              ›
            </Link>
          </div>
          <div className="row wrap" style={{ gap: 16 }}>
            {balances.data!.map((a) => (
              <div key={a.id}>
                <div className="small muted">{a.name}</div>
                <div style={{ fontSize: 20, fontWeight: 650, color: a.last_error ? "var(--danger)" : a.alert_below !== null && a.last_value !== null && a.last_value < a.alert_below ? "var(--warn)" : undefined }}>
                  {a.last_error ? "ошибка" : fmtMoney(a.last_value, a.currency)}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      <div className="card">
        <div className="row between">
          <h3>Сегодня в календаре</h3>
          <Link to="/calendar" className="small">
            ›
          </Link>
        </div>
        {events.data && !events.data.connected ? (
          <p className="muted small">Google Calendar не подключён.</p>
        ) : (
          <EventList events={events.data?.events ?? []} compact />
        )}
      </div>

      {latest && (
        <div className="card">
          <div className="row between">
            <h3>Последнее саммари</h3>
            <Link to="/summaries" className="small">
              ›
            </Link>
          </div>
          <div className="summary-text small">{latest.text.slice(0, 400)}{latest.text.length > 400 ? "…" : ""}</div>
          {latest.proposed_tasks.length > 0 && (
            <p className="small muted" style={{ marginTop: 6 }}>
              💡 {latest.proposed_tasks.length} предложенных задач
            </p>
          )}
        </div>
      )}
    </div>
  );
}
