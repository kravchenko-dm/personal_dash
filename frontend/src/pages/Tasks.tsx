import { useMemo, useState, type FormEvent } from "react";
import { del, patch, post, type AppleRemindersResp, type Task, type TaskStatus } from "../lib/api";
import { useFetch } from "../lib/useFetch";
import { fmtDate, isOverdue, relTime, toLocalInput } from "../lib/format";
import { Modal } from "../components/Modal";
import { useToast } from "../components/Toast";

const PRIO = { 1: "🔴 Высокий", 2: "🟡 Обычный", 3: "🟢 Низкий" } as const;
const SOURCE: Record<string, string> = { manual: "", telegram: "из Telegram", ai: "предложено ИИ", apple: "из Напоминаний Apple" };

export function TaskRow({ task, onChange }: { task: Task; onChange: (t: Task | null) => void }) {
  const [editing, setEditing] = useState(false);
  const toast = useToast();

  async function toggle() {
    const status: TaskStatus = task.status === "done" ? "todo" : "done";
    try {
      onChange(await patch<Task>(`/api/tasks/${task.id}`, { status }));
    } catch (e) {
      toast((e as Error).message, true);
    }
  }

  return (
    <>
      <div className={"task" + (task.status === "done" ? " done" : "")}>
        <button className={"check" + (task.status === "done" ? " done" : "")} onClick={toggle} aria-label="Выполнено" />
        <div className="title" onClick={() => setEditing(true)}>
          <div className={task.priority !== 2 ? `prio-${task.priority}` : ""}>{task.title}</div>
          <div className="meta">
            {task.due_at && (
              <span className={isOverdue(task.due_at) && task.status !== "done" ? "overdue" : ""}>⏰ {fmtDate(task.due_at)}</span>
            )}
            {task.status === "doing" && <span>▶ в работе</span>}
            {SOURCE[task.source] && <span>{SOURCE[task.source]}</span>}
            {task.notes && <span>📝</span>}
          </div>
        </div>
      </div>
      {editing && <TaskEditor task={task} onClose={() => setEditing(false)} onChange={onChange} />}
    </>
  );
}

function TaskEditor({ task, onClose, onChange }: { task: Task | null; onClose: () => void; onChange: (t: Task | null) => void }) {
  const [title, setTitle] = useState(task?.title ?? "");
  const [notes, setNotes] = useState(task?.notes ?? "");
  const [priority, setPriority] = useState<number>(task?.priority ?? 2);
  const [status, setStatus] = useState<TaskStatus>(task?.status ?? "todo");
  const [due, setDue] = useState(toLocalInput(task?.due_at ?? null));
  const [busy, setBusy] = useState(false);
  const toast = useToast();

  async function save(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    const body = {
      title,
      notes: notes || null,
      priority,
      status,
      due_at: due ? new Date(due).toISOString() : null,
      clear_due: !due
    };
    try {
      const saved = task ? await patch<Task>(`/api/tasks/${task.id}`, body) : await post<Task>("/api/tasks", body);
      onChange(saved);
      onClose();
    } catch (err) {
      toast((err as Error).message, true);
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    if (!task || !confirm("Удалить задачу?")) return;
    await del(`/api/tasks/${task.id}`);
    onChange(null);
    onClose();
  }

  return (
    <Modal title={task ? "Задача" : "Новая задача"} onClose={onClose}>
      <form onSubmit={save}>
        <label className="field">
          <span>Название</span>
          <input className="input" value={title} onChange={(e) => setTitle(e.target.value)} autoFocus required />
        </label>
        <label className="field">
          <span>Заметки</span>
          <textarea className="input" value={notes} onChange={(e) => setNotes(e.target.value)} />
        </label>
        <div className="row">
          <label className="field" style={{ flex: 1 }}>
            <span>Приоритет</span>
            <select className="input" value={priority} onChange={(e) => setPriority(Number(e.target.value))}>
              {Object.entries(PRIO).map(([k, v]) => (
                <option key={k} value={k}>
                  {v}
                </option>
              ))}
            </select>
          </label>
          <label className="field" style={{ flex: 1 }}>
            <span>Статус</span>
            <select className="input" value={status} onChange={(e) => setStatus(e.target.value as TaskStatus)}>
              <option value="todo">К выполнению</option>
              <option value="doing">В работе</option>
              <option value="done">Сделано</option>
            </select>
          </label>
        </div>
        <label className="field">
          <span>Срок (придёт напоминание)</span>
          <input className="input" type="datetime-local" value={due} onChange={(e) => setDue(e.target.value)} />
        </label>
        <div className="row between">
          {task ? (
            <button type="button" className="btn danger" onClick={remove}>
              Удалить
            </button>
          ) : (
            <span />
          )}
          <button className="btn primary" disabled={busy || !title.trim()}>
            Сохранить
          </button>
        </div>
      </form>
    </Modal>
  );
}

export function AppleReminders({ onImported }: { onImported: (t: Task) => void }) {
  const { data, error, loading, setData } = useFetch<AppleRemindersResp>("/api/apple/reminders");
  const toast = useToast();

  async function toTask(id: number) {
    try {
      const t = await post<Task>(`/api/apple/reminders/${id}/to-task`);
      onImported(t);
      setData((prev) => (prev ? { ...prev, items: prev.items.map((r) => (r.id === id ? { ...r, imported_task_id: t.id } : r)) } : prev));
      toast("Перенесено в задачи");
    } catch (e) {
      toast((e as Error).message, true);
    }
  }

  if (loading && !data) return <p className="muted" style={{ padding: 14 }}>Загрузка…</p>;
  if (error) return <p style={{ padding: 14, color: "var(--danger)" }}>{error}</p>;
  if (!data || data.items.length === 0)
    return (
      <div style={{ padding: 14 }} className="muted small">
        Напоминаний нет. Apple не отдаёт «Напоминания» по сети, поэтому их присылает Команда (Shortcuts) с iPhone, см. README →
        «Напоминания Apple». {data?.last_sync && `Последняя синхронизация ${relTime(data.last_sync.at)}.`}
      </div>
    );
  return (
    <>
      <div className="small muted" style={{ padding: "10px 14px 0" }}>
        Синхронизировано {relTime(data.last_sync?.at ?? null)} · {data.items.length} шт.
      </div>
      {data.items.map((r) => (
        <div key={r.id} className="task">
          <div style={{ flex: 1 }}>
            <div className={r.priority >= 1 && r.priority <= 3 ? "prio-1" : ""}>{r.title}</div>
            <div className="meta">
              {r.list_name && <span>📋 {r.list_name}</span>}
              {r.due_at && <span className={isOverdue(r.due_at) ? "overdue" : ""}>⏰ {fmtDate(r.due_at)}</span>}
              {r.notes && <span title={r.notes}>📝</span>}
            </div>
          </div>
          <button className="btn small" disabled={!!r.imported_task_id} onClick={() => toTask(r.id)}>
            {r.imported_task_id ? "✓ в задачах" : "→ в задачи"}
          </button>
        </div>
      ))}
    </>
  );
}

export default function Tasks() {
  const { data, error, loading, setData, reload } = useFetch<Task[]>("/api/tasks");
  const [filter, setFilter] = useState<"open" | "doing" | "done" | "all" | "apple">("open");
  const [creating, setCreating] = useState(false);
  const [quick, setQuick] = useState("");
  const toast = useToast();

  const tasks = useMemo(() => {
    const all = data ?? [];
    if (filter === "open") return all.filter((t) => t.status !== "done");
    if (filter === "all" || filter === "apple") return all;
    return all.filter((t) => t.status === filter);
  }, [data, filter]);

  function applyChange(id: number | null, t: Task | null) {
    setData((prev) => {
      const list = prev ?? [];
      if (!t) return list.filter((x) => x.id !== id);
      return list.some((x) => x.id === t.id) ? list.map((x) => (x.id === t.id ? t : x)) : [t, ...list];
    });
  }

  async function quickAdd(e: FormEvent) {
    e.preventDefault();
    if (!quick.trim()) return;
    try {
      const t = await post<Task>("/api/tasks", { title: quick.trim() });
      applyChange(null, t);
      setQuick("");
    } catch (err) {
      toast((err as Error).message, true);
    }
  }

  return (
    <div className="page">
      <div className="page-head">
        <h1>Задачи</h1>
        <button className="btn primary" onClick={() => setCreating(true)}>
          + Новая
        </button>
      </div>
      <form onSubmit={quickAdd} className="row" style={{ marginBottom: 12 }}>
        <input className="input" placeholder="Быстро добавить задачу…" value={quick} onChange={(e) => setQuick(e.target.value)} />
        <button className="btn" disabled={!quick.trim()}>
          ↵
        </button>
      </form>
      <div className="tabs">
        {(
          [
            ["open", "Открытые"],
            ["doing", "В работе"],
            ["done", "Сделанные"],
            ["all", "Все"],
            ["apple", " Напоминания"]
          ] as const
        ).map(([k, label]) => (
          <button key={k} className={filter === k ? "active" : ""} onClick={() => setFilter(k)}>
            {label}
          </button>
        ))}
      </div>
      {error && (
        <div className="card">
          <span style={{ color: "var(--danger)" }}>{error}</span>{" "}
          <button className="btn small" onClick={reload}>
            Повторить
          </button>
        </div>
      )}
      <div className="card" style={{ padding: 0 }}>
        {filter === "apple" ? (
          <AppleReminders onImported={(t) => applyChange(null, t)} />
        ) : (
          <>
            {loading && !data && <p className="muted" style={{ padding: 14 }}>Загрузка…</p>}
            {!loading && tasks.length === 0 && <p className="muted" style={{ padding: 14 }}>Пусто 🎉</p>}
            {tasks.map((t) => (
              <TaskRow key={t.id} task={t} onChange={(nt) => applyChange(t.id, nt)} />
            ))}
          </>
        )}
      </div>
      {creating && <TaskEditor task={null} onClose={() => setCreating(false)} onChange={(t) => applyChange(null, t)} />}
    </div>
  );
}
