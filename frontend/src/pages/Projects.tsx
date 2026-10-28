import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { post, put, type Project, type Stage } from "../lib/api";
import { useFetch } from "../lib/useFetch";
import { HEALTH_COLOR, relTime, STAGE_LABEL } from "../lib/format";
import { Modal } from "../components/Modal";
import { useToast } from "../components/Toast";

const COLORS = ["#63a0ff", "#52d273", "#ffb454", "#ff6b6b", "#c084fc", "#2dd4bf"];

export function ProjectForm({ project, onClose, onSaved }: { project: Project | null; onClose: () => void; onSaved: (p: Project) => void }) {
  const [name, setName] = useState(project?.name ?? "");
  const [description, setDescription] = useState(project?.description ?? "");
  const [stage, setStage] = useState<Stage>(project?.stage ?? "dev");
  const [color, setColor] = useState(project?.color ?? COLORS[0]);
  const [repo, setRepo] = useState(project?.repo_url ?? "");
  const [site, setSite] = useState(project?.site_url ?? "");
  const [yougile, setYougile] = useState(project?.yougile_board_id ?? "");
  const [archived, setArchived] = useState(project?.archived ?? false);
  const [busy, setBusy] = useState(false);
  const toast = useToast();

  async function save(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    const body = {
      name,
      description: description || null,
      stage,
      color,
      repo_url: repo || null,
      site_url: site || null,
      yougile_board_id: yougile || null,
      archived
    };
    try {
      const saved = project ? await put<Project>(`/api/projects/${project.id}`, body) : await post<Project>("/api/projects", body);
      onSaved(saved);
      onClose();
    } catch (err) {
      toast((err as Error).message, true);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal title={project ? "Проект" : "Новый проект"} onClose={onClose}>
      <form onSubmit={save}>
        <label className="field">
          <span>Название</span>
          <input className="input" value={name} onChange={(e) => setName(e.target.value)} required autoFocus />
        </label>
        <label className="field">
          <span>Описание</span>
          <textarea className="input" value={description} onChange={(e) => setDescription(e.target.value)} placeholder="Что это за сервис и для кого" />
        </label>
        <div className="row">
          <label className="field" style={{ flex: 1 }}>
            <span>Стадия</span>
            <select className="input" value={stage} onChange={(e) => setStage(e.target.value as Stage)}>
              {Object.entries(STAGE_LABEL).map(([k, v]) => (
                <option key={k} value={k}>
                  {v}
                </option>
              ))}
            </select>
          </label>
          <label className="field" style={{ flex: 1 }}>
            <span>Цвет</span>
            <div className="row" style={{ gap: 6, minHeight: 42 }}>
              {COLORS.map((c) => (
                <button
                  type="button"
                  key={c}
                  onClick={() => setColor(c)}
                  style={{ width: 26, height: 26, borderRadius: "50%", background: c, border: color === c ? "3px solid #fff" : "3px solid transparent", cursor: "pointer" }}
                  aria-label={c}
                />
              ))}
            </div>
          </label>
        </div>
        <label className="field">
          <span>Репозиторий</span>
          <input className="input" value={repo} onChange={(e) => setRepo(e.target.value)} placeholder="https://github.com/…" />
        </label>
        <label className="field">
          <span>Адрес сервиса</span>
          <input className="input" value={site} onChange={(e) => setSite(e.target.value)} placeholder="https://…" />
        </label>
        <label className="field">
          <span>ID доски YouGile</span>
          <input className="input" value={yougile} onChange={(e) => setYougile(e.target.value)} placeholder="необязательно" />
        </label>
        {project && (
          <label className="row" style={{ marginBottom: 12 }}>
            <input type="checkbox" checked={archived} onChange={(e) => setArchived(e.target.checked)} /> В архив
          </label>
        )}
        <div className="row between">
          <span />
          <button className="btn primary" disabled={busy || !name.trim()}>
            Сохранить
          </button>
        </div>
      </form>
    </Modal>
  );
}

export function ProjectCard({ p }: { p: Project }) {
  const open = (p.feature_counts.planned ?? 0) + (p.feature_counts.in_progress ?? 0) + (p.feature_counts.testing ?? 0);
  return (
    <Link to={`/projects/${p.id}`} className="card clickable" style={{ display: "block", color: "inherit", borderLeft: `4px solid ${p.color}` }}>
      <div className="row between">
        <strong>
          <span style={{ color: HEALTH_COLOR[p.health] }}>●</span> {p.name}
        </strong>
        <span className="chip">{STAGE_LABEL[p.stage]}</span>
      </div>
      {p.status_note ? (
        <div className="small" style={{ marginTop: 6 }}>
          {p.status_note} <span className="muted">· {relTime(p.status_note_at)}</span>
        </div>
      ) : (
        p.description && <div className="small muted" style={{ marginTop: 6 }}>{p.description.slice(0, 120)}</div>
      )}
      <div className="row wrap small muted" style={{ marginTop: 8, gap: 12 }}>
        <span>💡 {p.feature_counts.idea ?? 0}</span>
        <span>🔧 {open} в работе</span>
        <span>✅ {p.feature_counts.done ?? 0}</span>
        {p.chat_count > 0 && <span>💬 {p.chat_count}</span>}
        {p.open_incidents > 0 && <span style={{ color: HEALTH_COLOR[p.health] }}>⚠ {p.open_incidents}</span>}
      </div>
    </Link>
  );
}

export default function Projects() {
  const [showArchived, setShowArchived] = useState(false);
  const { data, error, loading, setData } = useFetch<Project[]>(`/api/projects?archived=${showArchived}`, [showArchived]);
  const [creating, setCreating] = useState(false);

  return (
    <div className="page">
      <div className="page-head">
        <h1>Проекты</h1>
        <button className="btn primary" onClick={() => setCreating(true)}>
          + Проект
        </button>
      </div>
      {error && <div className="card" style={{ color: "var(--danger)" }}>{error}</div>}
      {loading && !data && <p className="muted">Загрузка…</p>}
      {data?.length === 0 && !showArchived && (
        <div className="card muted">Пока нет проектов. Заведите по одному на каждый сервис: доска фич, заметки, привязка чатов и, дальше, мониторинг и бэкапы.</div>
      )}
      <div className="grid">{(data ?? []).map((p) => <ProjectCard key={p.id} p={p} />)}</div>
      <button className="btn small" style={{ marginTop: 8 }} onClick={() => setShowArchived((v) => !v)}>
        {showArchived ? "Показать активные" : "Архив"}
      </button>
      {creating && <ProjectForm project={null} onClose={() => setCreating(false)} onSaved={(p) => setData((prev) => [...(prev ?? []), p])} />}
    </div>
  );
}
