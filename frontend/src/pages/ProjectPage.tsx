import { useMemo, useState, type FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { del, patch, post, put, type Feature, type FeatureStatus, type Project, type ProjectDetail, type ProjectNote, type Summary, type TelegramChat } from "../lib/api";
import { useFetch } from "../lib/useFetch";
import { FEATURE_COLUMNS, fmtDate, relTime, STAGE_LABEL, toLocalInput } from "../lib/format";
import { Modal } from "../components/Modal";
import { useToast } from "../components/Toast";
import { ProjectForm } from "./Projects";

type Tab = "board" | "notes" | "chats" | "info";
const PRIO_MARK = { 1: "🔴", 2: "", 3: "🟢" } as const;

function FeatureEditor({
  projectId,
  feature,
  defaultStatus,
  onClose,
  onSaved,
  onDeleted
}: {
  projectId: number;
  feature: Feature | null;
  defaultStatus: FeatureStatus;
  onClose: () => void;
  onSaved: (f: Feature) => void;
  onDeleted: (id: number) => void;
}) {
  const [title, setTitle] = useState(feature?.title ?? "");
  const [description, setDescription] = useState(feature?.description ?? "");
  const [status, setStatus] = useState<FeatureStatus>(feature?.status ?? defaultStatus);
  const [priority, setPriority] = useState<number>(feature?.priority ?? 2);
  const [eta, setEta] = useState(toLocalInput(feature?.eta ?? null));
  const [link, setLink] = useState(feature?.link_url ?? "");
  const [busy, setBusy] = useState(false);
  const toast = useToast();

  async function save(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      let saved: Feature;
      if (feature) {
        saved = await patch<Feature>(`/api/projects/${projectId}/features/${feature.id}`, {
          title,
          description: description || null,
          priority,
          eta: eta ? new Date(eta).toISOString() : null,
          clear_eta: !eta,
          link_url: link || null
        });
        if (status !== feature.status) saved = await post<Feature>(`/api/projects/${projectId}/features/${feature.id}/move`, { status });
      } else {
        saved = await post<Feature>(`/api/projects/${projectId}/features`, {
          title,
          description: description || null,
          status,
          priority,
          eta: eta ? new Date(eta).toISOString() : null,
          link_url: link || null
        });
      }
      onSaved(saved);
      onClose();
    } catch (err) {
      toast((err as Error).message, true);
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    if (!feature || !confirm("Удалить фичу?")) return;
    await del(`/api/projects/${projectId}/features/${feature.id}`);
    onDeleted(feature.id);
    onClose();
  }

  return (
    <Modal title={feature ? "Фича" : "Новая фича"} onClose={onClose}>
      <form onSubmit={save}>
        <label className="field">
          <span>Название</span>
          <input className="input" value={title} onChange={(e) => setTitle(e.target.value)} required autoFocus />
        </label>
        <label className="field">
          <span>Описание</span>
          <textarea className="input" value={description} onChange={(e) => setDescription(e.target.value)} />
        </label>
        <div className="row">
          <label className="field" style={{ flex: 1 }}>
            <span>Колонка</span>
            <select className="input" value={status} onChange={(e) => setStatus(e.target.value as FeatureStatus)}>
              {FEATURE_COLUMNS.map((c) => (
                <option key={c.key} value={c.key}>
                  {c.label}
                </option>
              ))}
            </select>
          </label>
          <label className="field" style={{ flex: 1 }}>
            <span>Приоритет</span>
            <select className="input" value={priority} onChange={(e) => setPriority(Number(e.target.value))}>
              <option value={1}>🔴 Высокий</option>
              <option value={2}>Обычный</option>
              <option value={3}>🟢 Низкий</option>
            </select>
          </label>
        </div>
        <label className="field">
          <span>Ориентир по сроку</span>
          <input className="input" type="datetime-local" value={eta} onChange={(e) => setEta(e.target.value)} />
        </label>
        <label className="field">
          <span>Ссылка (YouGile, PR, документ)</span>
          <input className="input" value={link} onChange={(e) => setLink(e.target.value)} placeholder="https://…" />
        </label>
        <div className="row between">
          {feature ? (
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

function Board({ projectId, features, setFeatures }: { projectId: number; features: Feature[]; setFeatures: (fn: (f: Feature[]) => Feature[]) => void }) {
  const [editing, setEditing] = useState<Feature | null | { status: FeatureStatus }>(null);
  const [mode, setMode] = useState<"kanban" | "list">(window.innerWidth < 700 ? "list" : "kanban");
  const toast = useToast();

  const byStatus = useMemo(() => {
    const m: Record<FeatureStatus, Feature[]> = { idea: [], planned: [], in_progress: [], testing: [], done: [] };
    for (const f of features) m[f.status].push(f);
    for (const k of Object.keys(m) as FeatureStatus[]) m[k].sort((a, b) => a.position - b.position);
    return m;
  }, [features]);

  function upsert(f: Feature) {
    setFeatures((prev) => (prev.some((x) => x.id === f.id) ? prev.map((x) => (x.id === f.id ? f : x)) : [...prev, f]));
  }

  async function move(f: Feature, status: FeatureStatus, index?: number) {
    try {
      const moved = await post<Feature>(`/api/projects/${projectId}/features/${f.id}/move`, { status, index });
      // server renumbers the column; refetch positions lazily by applying the move locally
      setFeatures((prev) => {
        const others = prev.filter((x) => x.id !== f.id);
        const col = others.filter((x) => x.status === status).sort((a, b) => a.position - b.position);
        const at = index === undefined || index > col.length ? col.length : Math.max(index, 0);
        col.splice(at, 0, moved);
        col.forEach((x, i) => (x.position = i + 1));
        return [...others.filter((x) => x.status !== status), ...col];
      });
    } catch (e) {
      toast((e as Error).message, true);
    }
  }

  function stepStatus(f: Feature, dir: -1 | 1) {
    const idx = FEATURE_COLUMNS.findIndex((c) => c.key === f.status);
    const next = FEATURE_COLUMNS[idx + dir];
    if (next) void move(f, next.key);
  }

  const card = (f: Feature) => (
    <div key={f.id} className="yg-task" style={{ cursor: "pointer" }} onClick={() => setEditing(f)}>
      <div>
        {PRIO_MARK[f.priority]} {f.title} {f.source === "ai" && <span className="chip">ИИ</span>}
      </div>
      <div className="meta row between">
        <span>
          {f.eta && <span>⏳ {fmtDate(f.eta, false)} </span>}
          {f.link_url && (
            <a href={f.link_url} target="_blank" rel="noreferrer" onClick={(e) => e.stopPropagation()}>
              ссылка
            </a>
          )}
        </span>
        <span onClick={(e) => e.stopPropagation()}>
          <button className="btn small icon" disabled={f.status === "idea"} onClick={() => stepStatus(f, -1)} title="Назад">
            ‹
          </button>{" "}
          <button className="btn small icon" disabled={f.status === "done"} onClick={() => stepStatus(f, 1)} title="Дальше">
            ›
          </button>
        </span>
      </div>
    </div>
  );

  return (
    <>
      <div className="row between" style={{ marginBottom: 10 }}>
        <div className="tabs" style={{ margin: 0 }}>
          <button className={mode === "kanban" ? "active" : ""} onClick={() => setMode("kanban")}>
            Доска
          </button>
          <button className={mode === "list" ? "active" : ""} onClick={() => setMode("list")}>
            Список
          </button>
        </div>
        <button className="btn primary small" onClick={() => setEditing({ status: "idea" })}>
          + Фича
        </button>
      </div>
      {mode === "kanban" ? (
        <div className="kanban">
          {FEATURE_COLUMNS.map((c) => (
            <div className="col" key={c.key}>
              <h4>
                <span>{c.label}</span>
                <span>
                  {byStatus[c.key].length}{" "}
                  <button className="btn small icon" onClick={() => setEditing({ status: c.key })}>
                    +
                  </button>
                </span>
              </h4>
              {byStatus[c.key].map(card)}
            </div>
          ))}
        </div>
      ) : (
        FEATURE_COLUMNS.map((c) =>
          byStatus[c.key].length ? (
            <div key={c.key}>
              <div className="day-head">
                {c.label} · {byStatus[c.key].length}
              </div>
              {byStatus[c.key].map(card)}
            </div>
          ) : null
        )
      )}
      {features.length === 0 && <p className="muted small">Доска пустая. Добавьте первую фичу или примите предложение из саммари.</p>}
      {editing && (
        <FeatureEditor
          projectId={projectId}
          feature={"id" in editing ? editing : null}
          defaultStatus={"id" in editing ? editing.status : editing.status}
          onClose={() => setEditing(null)}
          onSaved={upsert}
          onDeleted={(id) => setFeatures((prev) => prev.filter((x) => x.id !== id))}
        />
      )}
    </>
  );
}

function Notes({ projectId, notes, setNotes }: { projectId: number; notes: ProjectNote[]; setNotes: (fn: (n: ProjectNote[]) => ProjectNote[]) => void }) {
  const [editing, setEditing] = useState<ProjectNote | null | "new">(null);
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [pinned, setPinned] = useState(false);
  const toast = useToast();

  function open(n: ProjectNote | "new") {
    setEditing(n);
    setTitle(n === "new" ? "" : n.title);
    setBody(n === "new" ? "" : n.body);
    setPinned(n === "new" ? false : n.pinned);
  }

  async function save(e: FormEvent) {
    e.preventDefault();
    try {
      const saved =
        editing === "new"
          ? await post<ProjectNote>(`/api/projects/${projectId}/notes`, { title, body, pinned })
          : await put<ProjectNote>(`/api/projects/${projectId}/notes/${(editing as ProjectNote).id}`, { title, body, pinned });
      setNotes((prev) => (prev.some((x) => x.id === saved.id) ? prev.map((x) => (x.id === saved.id ? saved : x)) : [saved, ...prev]));
      setEditing(null);
    } catch (err) {
      toast((err as Error).message, true);
    }
  }

  async function remove() {
    if (editing === "new" || !editing || !confirm("Удалить заметку?")) return;
    await del(`/api/projects/${projectId}/notes/${editing.id}`);
    setNotes((prev) => prev.filter((x) => x.id !== editing.id));
    setEditing(null);
  }

  return (
    <>
      <div className="row between" style={{ marginBottom: 10 }}>
        <span className="muted small">Окружения, адреса, контакты, чеклисты. Не храните тут секреты в открытом виде.</span>
        <button className="btn primary small" onClick={() => open("new")}>
          + Заметка
        </button>
      </div>
      {notes.map((n) => (
        <div key={n.id} className="card clickable" onClick={() => open(n)}>
          <div className="row between">
            <strong>
              {n.pinned && "📌 "}
              {n.title}
            </strong>
            <span className="small muted">{relTime(n.updated_at)}</span>
          </div>
          <div className="summary-text small" style={{ marginTop: 6 }}>{n.body.slice(0, 600)}{n.body.length > 600 ? "…" : ""}</div>
        </div>
      ))}
      {editing && (
        <Modal title={editing === "new" ? "Новая заметка" : "Заметка"} onClose={() => setEditing(null)}>
          <form onSubmit={save}>
            <label className="field">
              <span>Заголовок</span>
              <input className="input" value={title} onChange={(e) => setTitle(e.target.value)} required autoFocus />
            </label>
            <label className="field">
              <span>Текст (markdown)</span>
              <textarea className="input" style={{ minHeight: 180, fontFamily: "ui-monospace, monospace", fontSize: 14 }} value={body} onChange={(e) => setBody(e.target.value)} />
            </label>
            <label className="row" style={{ marginBottom: 12 }}>
              <input type="checkbox" checked={pinned} onChange={(e) => setPinned(e.target.checked)} /> Закрепить сверху
            </label>
            <div className="row between">
              {editing !== "new" ? (
                <button type="button" className="btn danger" onClick={remove}>
                  Удалить
                </button>
              ) : (
                <span />
              )}
              <button className="btn primary" disabled={!title.trim()}>
                Сохранить
              </button>
            </div>
          </form>
        </Modal>
      )}
    </>
  );
}

function Chats({ projectId, chats, summaries, onChange }: { projectId: number; chats: TelegramChat[]; summaries: Summary[]; onChange: () => void }) {
  const all = useFetch<TelegramChat[]>("/api/telegram/chats");
  const [busy, setBusy] = useState(false);
  const toast = useToast();
  const free = (all.data ?? []).filter((c) => !c.project_id);

  async function attach(id: string) {
    if (!id) return;
    await put(`/api/projects/${projectId}/chats/${id}`, {});
    onChange();
    void all.reload();
  }

  async function detach(c: TelegramChat) {
    await del(`/api/projects/${projectId}/chats/${c.id}`);
    onChange();
    void all.reload();
  }

  async function summarize() {
    setBusy(true);
    try {
      const s = await post<Summary | null>(`/api/summaries?project_id=${projectId}&hours=24`);
      toast(s ? "Саммари готово" : "Сообщений за сутки нет");
      onChange();
    } catch (e) {
      toast((e as Error).message, true);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <div className="card">
        <h3>Чаты проекта</h3>
        <p className="small muted">Нейросеть в саммари по этим чатам получит описание проекта и доску, и будет предлагать фичи, а не общие задачи.</p>
        {chats.map((c) => (
          <div key={c.id} className="row between" style={{ padding: "6px 0" }}>
            <span>
              💬 {c.title} <span className="chip">{c.kind}</span>
            </span>
            <button className="btn small" onClick={() => detach(c)}>
              отвязать
            </button>
          </div>
        ))}
        {free.length > 0 && (
          <select className="input" style={{ marginTop: 8 }} value="" onChange={(e) => attach(e.target.value)}>
            <option value="">+ привязать чат…</option>
            {free.map((c) => (
              <option key={c.id} value={c.id}>
                {c.title}
              </option>
            ))}
          </select>
        )}
        {chats.length === 0 && free.length === 0 && <p className="small muted">Бот ещё не видел чатов. Добавьте его в группу проекта.</p>}
        {chats.length > 0 && (
          <button className="btn primary small" style={{ marginTop: 10 }} onClick={summarize} disabled={busy}>
            {busy ? "Думаю…" : "Саммари за сутки"}
          </button>
        )}
      </div>
      {summaries.map((s) => (
        <div key={s.id} className="card">
          <div className="small muted">
            {fmtDate(s.period_start)} — {fmtDate(s.period_end)}
          </div>
          <div className="summary-text" style={{ marginTop: 6 }}>{s.text}</div>
          {s.proposed_tasks.length > 0 && (
            <p className="small muted" style={{ marginTop: 6 }}>
              💡 {s.proposed_tasks.length} предложений, принять можно на странице <Link to="/summaries">Саммари</Link>
            </p>
          )}
        </div>
      ))}
    </>
  );
}

export default function ProjectPage() {
  const { id } = useParams();
  const navigate = useNavigate();
  const { data, error, loading, setData, reload } = useFetch<ProjectDetail>(`/api/projects/${id}`, [id]);
  const [tab, setTab] = useState<Tab>("board");
  const [editing, setEditing] = useState(false);
  const [statusDraft, setStatusDraft] = useState<string | null>(null);
  const toast = useToast();

  if (error) {
    return (
      <div className="page">
        <div className="card" style={{ color: "var(--danger)" }}>{error}</div>
        <Link to="/projects">← Проекты</Link>
      </div>
    );
  }
  if (loading || !data) return <div className="page muted">Загрузка…</div>;
  const p = data.project;

  async function saveStatus() {
    try {
      const updated = await patch<Project>(`/api/projects/${p.id}/status`, { status_note: statusDraft || null });
      setData((prev) => (prev ? { ...prev, project: updated } : prev));
      setStatusDraft(null);
    } catch (e) {
      toast((e as Error).message, true);
    }
  }

  async function removeProject() {
    if (!confirm(`Удалить проект «${p.name}» со всеми фичами и заметками?`)) return;
    await del(`/api/projects/${p.id}`);
    navigate("/projects");
  }

  return (
    <div className="page">
      <div className="row" style={{ marginBottom: 6 }}>
        <Link to="/projects" className="small muted">
          ← Проекты
        </Link>
      </div>
      <div className="page-head">
        <div>
          <h1 style={{ borderLeft: `4px solid ${p.color}`, paddingLeft: 10 }}>{p.name}</h1>
          <div className="row wrap small muted" style={{ gap: 10 }}>
            <span className="chip">{STAGE_LABEL[p.stage]}</span>
            {p.site_url && (
              <a href={p.site_url} target="_blank" rel="noreferrer">
                сайт
              </a>
            )}
            {p.repo_url && (
              <a href={p.repo_url} target="_blank" rel="noreferrer">
                репозиторий
              </a>
            )}
            {p.yougile_board_id && <Link to="/yougile">YouGile</Link>}
          </div>
        </div>
        <button className="btn small" onClick={() => setEditing(true)}>
          ✎
        </button>
      </div>

      <div className="card">
        <h3>Сейчас</h3>
        {statusDraft === null ? (
          <div className="row between" onClick={() => setStatusDraft(p.status_note ?? "")} style={{ cursor: "pointer" }}>
            <span className={p.status_note ? "" : "muted"}>{p.status_note ?? "Нажмите, чтобы записать, что происходит с проектом"}</span>
            {p.status_note_at && <span className="small muted">{relTime(p.status_note_at)}</span>}
          </div>
        ) : (
          <div className="stack">
            <textarea className="input" autoFocus value={statusDraft} onChange={(e) => setStatusDraft(e.target.value)} />
            <div className="row">
              <button className="btn primary small" onClick={saveStatus}>
                Сохранить
              </button>
              <button className="btn small" onClick={() => setStatusDraft(null)}>
                Отмена
              </button>
            </div>
          </div>
        )}
      </div>

      <div className="tabs">
        {(
          [
            ["board", `Доска · ${data.features.filter((f) => f.status !== "done").length}`],
            ["notes", `Заметки · ${data.notes.length}`],
            ["chats", `Чаты · ${data.chats.length}`],
            ["info", "О проекте"]
          ] as const
        ).map(([k, l]) => (
          <button key={k} className={tab === k ? "active" : ""} onClick={() => setTab(k)}>
            {l}
          </button>
        ))}
      </div>

      {tab === "board" && (
        <Board projectId={p.id} features={data.features} setFeatures={(fn) => setData((prev) => (prev ? { ...prev, features: fn(prev.features) } : prev))} />
      )}
      {tab === "notes" && <Notes projectId={p.id} notes={data.notes} setNotes={(fn) => setData((prev) => (prev ? { ...prev, notes: fn(prev.notes) } : prev))} />}
      {tab === "chats" && <Chats projectId={p.id} chats={data.chats} summaries={data.summaries} onChange={reload} />}
      {tab === "info" && (
        <div className="card stack">
          <p>{p.description || <span className="muted">Описания нет.</span>}</p>
          <p className="small muted">Создан {fmtDate(p.created_at)} · обновлён {relTime(p.updated_at)}</p>
          <p className="small muted">Мониторинг, статус серверов и бэкапы появятся на этой странице на следующих этапах.</p>
          <div className="row">
            <button className="btn" onClick={() => setEditing(true)}>
              Редактировать
            </button>
            <button className="btn danger" onClick={removeProject}>
              Удалить проект
            </button>
          </div>
        </div>
      )}

      {editing && (
        <ProjectForm
          project={p}
          onClose={() => setEditing(false)}
          onSaved={(saved) => {
            setData((prev) => (prev ? { ...prev, project: saved } : prev));
            if (saved.archived) navigate("/projects");
          }}
        />
      )}
    </div>
  );
}
