import { useMemo, useState } from "react";
import { patch, post, type YgOverview, type YgTask } from "../lib/api";
import { useFetch } from "../lib/useFetch";
import { useToast } from "../components/Toast";

type View = "kanban" | "list" | "deadlines" | "mine";

function TaskCard({ t, onToggle }: { t: YgTask; onToggle: (t: YgTask) => void }) {
  return (
    <div className={"yg-task" + (t.completed ? " done" : "")}>
      <div className="row" style={{ alignItems: "flex-start" }}>
        <input type="checkbox" checked={t.completed} onChange={() => onToggle(t)} style={{ marginTop: 3 }} />
        <div style={{ flex: 1 }}>{t.title}</div>
      </div>
      <div className="meta">
        {t.deadline && <span>⏰ {new Date(t.deadline).toLocaleDateString("ru-RU")} </span>}
        {t.assigned.length > 0 && <span>👤 {t.assigned.join(", ")} </span>}
        {t.board && <span>· {t.board}</span>}
      </div>
    </div>
  );
}

export default function YouGile() {
  const { data, error, loading, reload, setData } = useFetch<YgOverview>("/api/yougile/overview");
  const [view, setView] = useState<View>("kanban");
  const [boardId, setBoardId] = useState<string | null>(null);
  const [userId, setUserId] = useState<string>("");
  const [hideDone, setHideDone] = useState(true);
  const toast = useToast();

  const board = data?.boards.find((b) => b.id === boardId) ?? data?.boards[0];
  const columns = useMemo(() => (data && board ? data.columns.filter((c) => c.board_id === board.id) : []), [data, board]);
  const visible = useMemo(() => (data ? data.tasks.filter((t) => !hideDone || !t.completed) : []), [data, hideDone]);

  async function toggle(t: YgTask) {
    try {
      await patch(`/api/yougile/tasks/${t.id}`, { completed: !t.completed });
      setData((prev) => (prev ? { ...prev, tasks: prev.tasks.map((x) => (x.id === t.id ? { ...x, completed: !t.completed } : x)) } : prev));
    } catch (e) {
      toast((e as Error).message, true);
    }
  }

  async function refresh() {
    await post("/api/yougile/refresh");
    void reload();
  }

  async function addTask(columnId: string) {
    const title = prompt("Название задачи");
    if (!title) return;
    try {
      await post("/api/yougile/tasks", { title, column_id: columnId });
      await refresh();
    } catch (e) {
      toast((e as Error).message, true);
    }
  }

  if (error) {
    return (
      <div className="page">
        <h1>YouGile</h1>
        <div className="card" style={{ color: "var(--danger)" }}>{error}</div>
        <p className="small muted">Укажите YOUGILE_API_KEY в .env (Компания → Настройки → API).</p>
      </div>
    );
  }

  return (
    <div className="page">
      <div className="page-head">
        <h1>YouGile</h1>
        <div className="row">
          <label className="small muted row" style={{ gap: 4 }}>
            <input type="checkbox" checked={hideDone} onChange={(e) => setHideDone(e.target.checked)} /> скрыть готовые
          </label>
          <button className="btn small" onClick={refresh}>
            ↻
          </button>
        </div>
      </div>
      <div className="tabs">
        {(
          [
            ["kanban", "Доска"],
            ["list", "Список"],
            ["deadlines", "Дедлайны"],
            ["mine", "По людям"]
          ] as const
        ).map(([k, l]) => (
          <button key={k} className={view === k ? "active" : ""} onClick={() => setView(k)}>
            {l}
          </button>
        ))}
      </div>
      {loading && !data && <p className="muted">Загрузка…</p>}
      {data && view === "kanban" && (
        <>
          <select className="input" value={board?.id ?? ""} onChange={(e) => setBoardId(e.target.value)} style={{ marginBottom: 10 }}>
            {data.boards.map((b) => (
              <option key={b.id} value={b.id}>
                {data.projects.find((p) => p.id === b.project_id)?.title ?? ""} / {b.title}
              </option>
            ))}
          </select>
          <div className="kanban">
            {columns.map((c) => {
              const list = visible.filter((t) => t.column_id === c.id);
              return (
                <div className="col" key={c.id}>
                  <h4>
                    <span>{c.title}</span>
                    <span>
                      {list.length}{" "}
                      <button className="btn small icon" onClick={() => addTask(c.id)} title="Добавить">
                        +
                      </button>
                    </span>
                  </h4>
                  {list.map((t) => (
                    <TaskCard key={t.id} t={t} onToggle={toggle} />
                  ))}
                </div>
              );
            })}
          </div>
        </>
      )}
      {data && view === "list" && (
        <div className="card">
          {data.boards.map((b) => {
            const list = visible.filter((t) => t.board_id === b.id);
            if (!list.length) return null;
            return (
              <div key={b.id}>
                <div className="day-head">{b.title}</div>
                {list.map((t) => (
                  <TaskCard key={t.id} t={t} onToggle={toggle} />
                ))}
              </div>
            );
          })}
        </div>
      )}
      {data && view === "deadlines" && (
        <div className="card">
          {visible
            .filter((t) => t.deadline)
            .sort((a, b) => (a.deadline ?? 0) - (b.deadline ?? 0))
            .map((t) => (
              <TaskCard key={t.id} t={t} onToggle={toggle} />
            ))}
          {!visible.some((t) => t.deadline) && <p className="muted">Задач с дедлайнами нет.</p>}
        </div>
      )}
      {data && view === "mine" && (
        <>
          <select className="input" value={userId} onChange={(e) => setUserId(e.target.value)} style={{ marginBottom: 10 }}>
            <option value="">— выберите человека —</option>
            {data.users.map((u) => (
              <option key={u.id} value={u.id}>
                {u.name}
              </option>
            ))}
          </select>
          <div className="card">
            {visible
              .filter((t) => userId && t.assigned_ids.includes(userId))
              .map((t) => (
                <TaskCard key={t.id} t={t} onToggle={toggle} />
              ))}
          </div>
        </>
      )}
    </div>
  );
}
