import { useEffect, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { del, post, put, type BackupDestination, type BackupItem, type BackupPlan, type BackupRun, type Server } from "../lib/api";
import { useFetch } from "../lib/useFetch";
import { fmtBytes, fmtDate, relTime } from "../lib/format";
import { Modal } from "../components/Modal";
import { useToast } from "../components/Toast";

const ITEM_LABEL: Record<BackupItem["type"], string> = { path: "Каталог", pg_dump: "PostgreSQL дамп", mysql_dump: "MySQL дамп", docker_volume: "Том Docker", clickhouse: "ClickHouse таблица" };
const SCHEDULES: [string, string][] = [
  ["0 3 * * *", "каждый день в 03:00"],
  ["0 */6 * * *", "каждые 6 часов"],
  ["0 * * * *", "каждый час"],
  ["0 4 * * 0", "по воскресеньям в 04:00"]
];

function describeItem(it: BackupItem): string {
  if (it.type === "path") return `${it.path}${it.exclude?.length ? ` (кроме ${it.exclude.join(", ")})` : ""}`;
  if (it.type === "docker_volume") return `том ${it.volume}`;
  if (it.type === "clickhouse") return `${it.database ?? "default"}.${it.table ?? "?"} · контейнер ${it.container}`;
  const where = it.mode === "dsn" ? "по DSN" : it.mode === "local" ? "локально" : `контейнер ${it.container}`;
  return `${it.database ?? "все базы"} · ${where}`;
}

function ItemEditor({ item, onChange, onRemove }: { item: BackupItem; onChange: (i: BackupItem) => void; onRemove: () => void }) {
  const set = (patch: Partial<BackupItem>) => onChange({ ...item, ...patch });
  return (
    <div className="card" style={{ padding: 10, background: "var(--panel-2)" }}>
      <div className="row between" style={{ marginBottom: 6 }}>
        <select className="input" style={{ flex: 1 }} value={item.type} onChange={(e) => onChange({ type: e.target.value as BackupItem["type"], name: item.name })}>
          {Object.entries(ITEM_LABEL).map(([k, v]) => (
            <option key={k} value={k}>
              {v}
            </option>
          ))}
        </select>
        <input className="input" style={{ flex: 1 }} placeholder="имя (необязательно)" value={item.name ?? ""} onChange={(e) => set({ name: e.target.value })} />
        <button type="button" className="btn small icon danger" onClick={onRemove}>
          ✕
        </button>
      </div>
      {item.type === "path" && (
        <>
          <input className="input" placeholder="/opt/myapp" value={item.path ?? ""} onChange={(e) => set({ path: e.target.value })} />
          <input className="input" style={{ marginTop: 6 }} placeholder="исключения через запятую: node_modules, .venv, *.log" value={(item.exclude ?? []).join(", ")} onChange={(e) => set({ exclude: e.target.value.split(",").map((s) => s.trim()).filter(Boolean) })} />
        </>
      )}
      {item.type === "docker_volume" && <input className="input" placeholder="имя тома (docker volume ls)" value={item.volume ?? ""} onChange={(e) => set({ volume: e.target.value })} />}
      {item.type === "clickhouse" && (
        <div className="row">
          <input className="input" placeholder="контейнер clickhouse" value={item.container ?? ""} onChange={(e) => set({ container: e.target.value })} />
          <input className="input" placeholder="база" value={item.database ?? ""} onChange={(e) => set({ database: e.target.value })} />
          <input className="input" placeholder="таблица" value={item.table ?? ""} onChange={(e) => set({ table: e.target.value })} />
        </div>
      )}
      {(item.type === "pg_dump" || item.type === "mysql_dump") && (
        <>
          <div className="row" style={{ marginBottom: 6 }}>
            <select className="input" value={item.mode ?? "docker"} onChange={(e) => set({ mode: e.target.value as BackupItem["mode"] })}>
              <option value="docker">в контейнере</option>
              <option value="local">локальный сервер БД</option>
              {item.type === "pg_dump" && <option value="dsn">строка подключения</option>}
            </select>
            {(item.mode ?? "docker") === "docker" && <input className="input" placeholder="имя контейнера" value={item.container ?? ""} onChange={(e) => set({ container: e.target.value })} />}
          </div>
          {item.mode === "dsn" ? (
            <input className="input" placeholder="postgresql://user:pass@127.0.0.1:5432/db" value={item.dsn ?? ""} onChange={(e) => set({ dsn: e.target.value })} />
          ) : (
            <div className="row">
              <input className="input" placeholder="база" value={item.database ?? ""} onChange={(e) => set({ database: e.target.value })} />
              <input className="input" placeholder="пользователь" value={item.user ?? ""} onChange={(e) => set({ user: e.target.value })} />
              {item.type === "mysql_dump" && <input className="input" type="password" placeholder="пароль" value={item.password ?? ""} onChange={(e) => set({ password: e.target.value })} />}
            </div>
          )}
        </>
      )}
    </div>
  );
}

function PlanEditor({ projectId, servers, plan, onClose, onSaved }: { projectId: number; servers: Server[]; plan: BackupPlan | null; onClose: () => void; onSaved: (p: BackupPlan) => void }) {
  const [name, setName] = useState(plan?.name ?? "");
  const [serverId, setServerId] = useState<number>(plan?.server_id ?? servers[0]?.id ?? 0);
  const [items, setItems] = useState<BackupItem[]>(plan?.items ?? [{ type: "path", path: "" }]);
  const [schedule, setSchedule] = useState(plan?.schedule ?? "0 3 * * *");
  const [custom, setCustom] = useState(!SCHEDULES.some(([c]) => c === (plan?.schedule ?? "0 3 * * *")));
  const [ret, setRet] = useState<Record<string, number>>(plan?.retention ?? { daily: 7, weekly: 4, monthly: 6 });
  const [maxAge, setMaxAge] = useState(plan?.max_age_hours ?? 36);
  const [subpath, setSubpath] = useState(plan?.repo_subpath ?? "");
  const [enabled, setEnabled] = useState(plan?.enabled ?? true);
  const [busy, setBusy] = useState(false);
  const toast = useToast();

  async function save(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    const body = { name, server_id: serverId, enabled, items, schedule, retention: ret, max_age_hours: maxAge, repo_subpath: subpath };
    try {
      const saved = plan ? await put<BackupPlan>(`/api/projects/${projectId}/backups/${plan.id}`, body) : await post<BackupPlan>(`/api/projects/${projectId}/backups`, body);
      onSaved(saved);
      onClose();
    } catch (err) {
      toast((err as Error).message, true);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal title={plan ? "План бэкапа" : "Новый план бэкапа"} onClose={onClose}>
      <form onSubmit={save}>
        <div className="row">
          <label className="field" style={{ flex: 2 }}>
            <span>Название</span>
            <input className="input" value={name} onChange={(e) => setName(e.target.value)} required autoFocus placeholder="база + код" />
          </label>
          <label className="field" style={{ flex: 1 }}>
            <span>Сервер</span>
            <select className="input" value={serverId} onChange={(e) => setServerId(Number(e.target.value))}>
              {servers.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name}
                </option>
              ))}
            </select>
          </label>
        </div>
        <div className="field">
          <span style={{ display: "block", fontSize: 13, color: "var(--muted)", marginBottom: 4 }}>Что бэкапить</span>
          {items.map((it, i) => (
            <ItemEditor key={i} item={it} onChange={(ni) => setItems(items.map((x, j) => (j === i ? ni : x)))} onRemove={() => setItems(items.filter((_, j) => j !== i))} />
          ))}
          <button type="button" className="btn small" onClick={() => setItems([...items, { type: "pg_dump", mode: "docker" }])}>
            + элемент
          </button>
        </div>
        <label className="field">
          <span>Расписание (время сервера дашборда)</span>
          <select className="input" value={custom ? "custom" : schedule} onChange={(e) => (e.target.value === "custom" ? setCustom(true) : (setCustom(false), setSchedule(e.target.value)))}>
            {SCHEDULES.map(([c, l]) => (
              <option key={c} value={c}>
                {l}
              </option>
            ))}
            <option value="custom">cron вручную…</option>
          </select>
          {custom && <input className="input" style={{ marginTop: 6 }} value={schedule} onChange={(e) => setSchedule(e.target.value)} placeholder="мин час день месяц день_недели" />}
        </label>
        <div className="row">
          {(["daily", "weekly", "monthly"] as const).map((k) => (
            <label key={k} className="field" style={{ flex: 1 }}>
              <span>{{ daily: "дневных", weekly: "недельных", monthly: "месячных" }[k]}</span>
              <input className="input" type="number" min={0} value={ret[k] ?? 0} onChange={(e) => setRet({ ...ret, [k]: Number(e.target.value) })} />
            </label>
          ))}
          <label className="field" style={{ flex: 1 }}>
            <span>Тревога, если нет свежего (ч)</span>
            <input className="input" type="number" min={1} value={maxAge} onChange={(e) => setMaxAge(Number(e.target.value))} />
          </label>
        </div>
        <label className="field">
          <span>Подпапка в хранилище (пусто = project-ID)</span>
          <input className="input" value={subpath} onChange={(e) => setSubpath(e.target.value)} placeholder="shop" />
        </label>
        <label className="row" style={{ marginBottom: 12 }}>
          <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} /> Включён
        </label>
        <div className="row between">
          <span />
          <button className="btn primary" disabled={busy || !name.trim() || !serverId || items.length === 0}>
            Сохранить
          </button>
        </div>
      </form>
    </Modal>
  );
}

function RunsModal({ projectId, plan, onClose }: { projectId: number; plan: BackupPlan; onClose: () => void }) {
  const runs = useFetch<BackupRun[]>(`/api/projects/${projectId}/backups/${plan.id}/runs`);
  const [open, setOpen] = useState<BackupRun | null>(null);
  useEffect(() => {
    const t = setInterval(() => void runs.reload(), 10000);
    return () => clearInterval(t);
  }, [runs.reload]); // eslint-disable-line react-hooks/exhaustive-deps
  const color = (s: string) => (s === "ok" ? "var(--ok)" : s === "failed" ? "var(--danger)" : "var(--warn)");
  return (
    <Modal title={`Запуски · ${plan.name}`} onClose={onClose}>
      {(runs.data ?? []).map((r) => (
        <div key={r.id} className="row between" style={{ padding: "6px 0", borderTop: "1px solid var(--border)", cursor: "pointer" }} onClick={() => setOpen(r)}>
          <span>
            <span style={{ color: color(r.status) }}>●</span> {r.kind} · {fmtDate(r.started_at)} <span className="small muted">{r.trigger}</span>
          </span>
          <span className="small muted">
            {r.status === "running" ? "выполняется…" : `${r.duration_s ?? 0} c${r.added_bytes ? ` · +${fmtBytes(r.added_bytes)}` : ""}`}
          </span>
        </div>
      ))}
      {runs.data?.length === 0 && <p className="muted">Запусков ещё не было.</p>}
      {open && (
        <Modal title={`${open.kind} #${open.id}`} onClose={() => setOpen(null)}>
          <pre className="input" style={{ whiteSpace: "pre-wrap", fontSize: 12, maxHeight: "55vh", overflow: "auto" }}>{open.output || "(пусто)"}</pre>
        </Modal>
      )}
    </Modal>
  );
}

function SnapshotsModal({ projectId, plan, onClose, onQueued }: { projectId: number; plan: BackupPlan; onClose: () => void; onQueued: () => void }) {
  const toast = useToast();
  const snaps = [...(plan.snapshots ?? [])].reverse();
  async function restore(id: string) {
    if (!confirm(`Восстановить снимок ${id} в /var/backups/dash-restore/${plan.id}/${id} на сервере ${plan.server_name}? Рабочие файлы не затрагиваются.`)) return;
    try {
      await post(`/api/projects/${projectId}/backups/${plan.id}/restore`, { snapshot_id: id });
      toast("Восстановление поставлено в очередь, смотрите «Запуски»");
      onQueued();
    } catch (e) {
      toast((e as Error).message, true);
    }
  }
  return (
    <Modal title={`Снимки · ${plan.name}`} onClose={onClose}>
      <p className="small muted">Список обновляется кнопкой ↻ на карточке плана. Восстановление пишет файлы в отдельный каталог на сервере, дальше вы применяете их сами.</p>
      {snaps.map((s) => (
        <div key={s.id} className="row between" style={{ padding: "6px 0", borderTop: "1px solid var(--border)" }}>
          <span>
            <code>{s.id}</code> · {fmtDate(s.time)} <span className="small muted">{s.tags.filter((t) => t.startsWith("item:")).map((t) => t.slice(5)).join(", ")}</span>
          </span>
          <button className="btn small" onClick={() => restore(s.id)}>
            восстановить
          </button>
        </div>
      ))}
      {snaps.length === 0 && <p className="muted">Снимков пока нет или список не обновлялся.</p>}
    </Modal>
  );
}

export default function Backups({ projectId }: { projectId: number }) {
  const plans = useFetch<BackupPlan[]>(`/api/projects/${projectId}/backups`);
  const servers = useFetch<Server[]>(`/api/projects/${projectId}/servers`);
  const dest = useFetch<BackupDestination>("/api/backups/destination");
  const [editing, setEditing] = useState<BackupPlan | null | "new">(null);
  const [runsOf, setRunsOf] = useState<BackupPlan | null>(null);
  const [snapsOf, setSnapsOf] = useState<BackupPlan | null>(null);
  const toast = useToast();

  useEffect(() => {
    const t = setInterval(() => void plans.reload(), 30000);
    return () => clearInterval(t);
  }, [plans.reload]); // eslint-disable-line react-hooks/exhaustive-deps

  async function act(plan: BackupPlan, action: "run" | "snapshots" | "check") {
    if (action === "run" && !confirm(`Запустить бэкап «${plan.name}» сейчас?`)) return;
    try {
      await post(`/api/projects/${projectId}/backups/${plan.id}/${action}`);
      toast(action === "run" ? "Бэкап запущен" : action === "check" ? "Проверка запущена" : "Запрошен список снимков");
      setTimeout(() => void plans.reload(), 1500);
    } catch (e) {
      toast((e as Error).message, true);
    }
  }

  async function remove(plan: BackupPlan) {
    if (!confirm(`Удалить план «${plan.name}»? Данные в хранилище останутся.`)) return;
    await del(`/api/projects/${projectId}/backups/${plan.id}`);
    plans.setData((prev) => (prev ?? []).filter((p) => p.id !== plan.id));
  }

  const statusColor = (p: BackupPlan) => (p.last_status === "ok" ? "var(--ok)" : p.last_status === "failed" ? "var(--danger)" : p.last_status === "running" ? "var(--warn)" : "var(--muted)");

  return (
    <>
      {dest.data && !dest.data.configured && (
        <div className="card" style={{ borderColor: "var(--warn)" }}>
          Хранилище бэкапов не настроено. <Link to="/settings/backups">Настроить →</Link>
        </div>
      )}
      {servers.data?.length === 0 && <div className="card muted small">Сначала добавьте сервер с агентом на вкладке «Мониторинг»: бэкапы выполняет агент.</div>}
      <div className="row between" style={{ margin: "4px 0 8px" }}>
        <h3 style={{ margin: 0 }}>Планы</h3>
        <button className="btn primary small" onClick={() => setEditing("new")} disabled={!servers.data?.length}>
          + План
        </button>
      </div>
      {(plans.data ?? []).map((p) => (
        <div key={p.id} className="card">
          <div className="row between">
            <div>
              <strong>
                <span style={{ color: statusColor(p) }}>●</span> {p.name}
              </strong>{" "}
              {!p.enabled && <span className="chip">выключен</span>}
              <div className="small muted">
                {p.server_name} · {SCHEDULES.find(([c]) => c === p.schedule)?.[1] ?? p.schedule}
                {p.repo_size !== null && ` · в хранилище ${fmtBytes(p.repo_size)}`}
              </div>
            </div>
            <div className="row" style={{ gap: 4 }}>
              <button className="btn small icon" title="Бэкап сейчас" onClick={() => act(p, "run")}>
                ▶
              </button>
              <button className="btn small icon" title="Обновить список снимков" onClick={() => act(p, "snapshots")}>
                ↻
              </button>
              <button className="btn small icon" title="Проверить репозиторий" onClick={() => act(p, "check")}>
                ✔
              </button>
              <button className="btn small icon" title="Изменить" onClick={() => setEditing(p)}>
                ✎
              </button>
              <button className="btn small icon danger" title="Удалить план" onClick={() => remove(p)}>
                ✕
              </button>
            </div>
          </div>
          <div className="small" style={{ marginTop: 8 }}>
            {p.items.map((it, i) => (
              <div key={i}>
                • {ITEM_LABEL[it.type]}: {describeItem(it)}
              </div>
            ))}
          </div>
          <div className="row wrap small muted" style={{ marginTop: 8, gap: 12 }}>
            <span>последний: {p.last_run_at ? `${relTime(p.last_run_at)} (${p.last_status})` : "ещё не было"}</span>
            <span>успешный: {p.last_ok_at ? relTime(p.last_ok_at) : "—"}</span>
            <span>следующий: {p.next_run_at ? fmtDate(p.next_run_at) : "—"}</span>
            <a onClick={() => setRunsOf(p)} style={{ cursor: "pointer" }}>
              запуски
            </a>
            <a onClick={() => setSnapsOf(p)} style={{ cursor: "pointer" }}>
              снимки{p.snapshots ? ` (${p.snapshots.length})` : ""}
            </a>
          </div>
        </div>
      ))}
      {plans.data?.length === 0 && servers.data && servers.data.length > 0 && <div className="card muted small">Планов нет. Обычно один план на сервер: дамп базы плюс каталог с кодом и конфигами.</div>}
      {editing && servers.data && <PlanEditor projectId={projectId} servers={servers.data} plan={editing === "new" ? null : editing} onClose={() => setEditing(null)} onSaved={(p) => plans.setData((prev) => (prev?.some((x) => x.id === p.id) ? prev.map((x) => (x.id === p.id ? p : x)) : [...(prev ?? []), p]))} />}
      {runsOf && <RunsModal projectId={projectId} plan={runsOf} onClose={() => setRunsOf(null)} />}
      {snapsOf && <SnapshotsModal projectId={projectId} plan={snapsOf} onClose={() => setSnapsOf(null)} onQueued={() => setSnapsOf(null)} />}
    </>
  );
}
