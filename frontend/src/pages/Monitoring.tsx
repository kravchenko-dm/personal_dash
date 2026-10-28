import { useEffect, useState, type FormEvent } from "react";
import { del, get, post, type AgentCommand, type Incident, type Monitor, type Server, type ServerService } from "../lib/api";
import { useFetch } from "../lib/useFetch";
import { fmtBytes, fmtDate, fmtUptime, INCIDENT_KIND, relTime } from "../lib/format";
import { Modal } from "../components/Modal";
import { Sparkline, UptimeBar } from "../components/Sparkline";
import { useToast } from "../components/Toast";

const SVC_COLOR: Record<string, string> = { running: "var(--ok)", starting: "var(--warn)", unhealthy: "var(--warn)", restarting: "var(--warn)" };

function TokenModal({ server, onClose }: { server: Server; onClose: () => void }) {
  const toast = useToast();
  const copy = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      toast("Скопировано");
    } catch {
      prompt("Скопируйте:", text);
    }
  };
  return (
    <Modal title={`Агент для «${server.name}»`} onClose={onClose}>
      <p className="small">Выполните на сервере от root. Токен показывается один раз, на сервере хранится в /etc/dash-agent/config.json.</p>
      <pre className="input" style={{ whiteSpace: "pre-wrap", wordBreak: "break-all", fontSize: 12 }}>{server.install_command}</pre>
      <div className="row">
        <button className="btn primary" onClick={() => copy(server.install_command ?? "")}>
          Скопировать команду
        </button>
        <button className="btn" onClick={() => copy(server.token ?? "")}>
          Только токен
        </button>
      </div>
      <p className="small muted" style={{ marginTop: 10 }}>
        После установки впишите сервисы в config.json: systemd-юниты (<code>unit</code>), контейнеры (<code>container</code>), процессы (<code>match</code>), путь к коду (<code>path</code>) для версии и локальный URL здоровья (<code>url</code>). Контейнеры Docker находятся автоматически.
      </p>
    </Modal>
  );
}

function CommandOutput({ projectId, server, command, onClose }: { projectId: number; server: Server; command: AgentCommand; onClose: () => void }) {
  const [cmd, setCmd] = useState(command);
  useEffect(() => {
    if (cmd.status === "done" || cmd.status === "failed") return;
    const t = setInterval(async () => {
      try {
        const fresh = await get<AgentCommand>(`/api/projects/${projectId}/servers/${server.id}/commands/${cmd.id}`);
        setCmd(fresh);
      } catch {
        /* keep polling */
      }
    }, 3000);
    return () => clearInterval(t);
  }, [cmd.status, cmd.id, projectId, server.id]);
  return (
    <Modal title={`${cmd.kind} ${String(cmd.args.service ?? "")}`} onClose={onClose}>
      {cmd.status === "queued" && <p className="muted">Ждём, пока агент заберёт команду (до {server.silence_alert_min > 0 ? "30" : "30"} сек)…</p>}
      {cmd.status === "sent" && <p className="muted">Агент выполняет…</p>}
      {(cmd.status === "done" || cmd.status === "failed") && (
        <>
          <p className={cmd.ok ? "small" : "small"} style={{ color: cmd.ok ? "var(--ok)" : "var(--danger)" }}>
            {cmd.ok ? "Выполнено" : "Ошибка"} · {relTime(cmd.finished_at)}
          </p>
          <pre className="input" style={{ whiteSpace: "pre-wrap", fontSize: 12, maxHeight: "50vh", overflow: "auto" }}>{cmd.output || "(пусто)"}</pre>
        </>
      )}
    </Modal>
  );
}

function ServiceRow({ svc, onAction }: { svc: ServerService; onAction: (kind: string) => void }) {
  const color = SVC_COLOR[svc.status] ?? (svc.status === "unknown" ? "var(--muted)" : "var(--danger)");
  return (
    <div className="row between" style={{ padding: "6px 0", borderTop: "1px solid var(--border)" }}>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div>
          <span style={{ color }}>●</span> {svc.name} <span className="chip">{svc.kind}</span>
          {svc.discovered && <span className="chip">авто</span>}
        </div>
        <div className="small muted">
          {svc.status}
          {svc.detail ? ` · ${svc.detail}` : ""}
          {svc.restarts ? ` · рестартов ${svc.restarts}` : ""}
          {svc.version && ` · ${svc.version.kind === "git" ? "git " : svc.version.kind === "file" ? "v" : "обновлён "}${svc.version.value}`}
          {svc.http && (
            <span style={{ color: svc.http.ok ? undefined : "var(--danger)" }}>
              {" "}
              · HTTP {svc.http.status ?? svc.http.error} {svc.http.latency_ms}мс
            </span>
          )}
        </div>
      </div>
      <div className="row" style={{ gap: 4 }}>
        <button className="btn small icon" title="Логи" onClick={() => onAction("logs")}>
          📜
        </button>
        <button className="btn small icon" title="Перезапустить" onClick={() => onAction("restart")}>
          ↻
        </button>
      </div>
    </div>
  );
}

function ServerCard({ projectId, server, onChange }: { projectId: number; server: Server; onChange: () => void }) {
  const [metrics, setMetrics] = useState<{ load1: number; mem_pct: number; disk_pct: number }[]>([]);
  const [cmd, setCmd] = useState<AgentCommand | null>(null);
  const [showToken, setShowToken] = useState<Server | null>(null);
  const toast = useToast();
  const r = server.last_report;

  useEffect(() => {
    get<{ samples: typeof metrics }>(`/api/projects/${projectId}/servers/${server.id}/metrics?hours=24`)
      .then((d) => setMetrics(d.samples))
      .catch(() => setMetrics([]));
  }, [projectId, server.id, server.last_seen_at]);

  async function action(kind: string, service?: string) {
    if (kind === "restart" && !confirm(`Перезапустить ${service} на ${server.name}?`)) return;
    try {
      const c = await post<AgentCommand>(`/api/projects/${projectId}/servers/${server.id}/commands`, { kind, args: service ? { service, lines: 200 } : {} });
      setCmd(c);
    } catch (e) {
      toast((e as Error).message, true);
    }
  }

  async function rotate() {
    if (!confirm("Выпустить новый токен? Старый перестанет работать, агент нужно переустановить.")) return;
    setShowToken(await post<Server>(`/api/projects/${projectId}/servers/${server.id}/rotate-token`));
  }

  async function remove() {
    if (!confirm(`Удалить сервер ${server.name} вместе с историей?`)) return;
    await del(`/api/projects/${projectId}/servers/${server.id}`);
    onChange();
  }

  return (
    <div className="card">
      <div className="row between">
        <div>
          <strong>
            <span style={{ color: server.online ? "var(--ok)" : "var(--danger)" }}>●</span> {server.name}
          </strong>
          <div className="small muted">
            {server.hostname && `${server.hostname} · `}
            {server.os && `${server.os} · `}
            {server.last_seen_at ? `на связи ${relTime(server.last_seen_at)}` : "агент ещё не подключался"}
            {server.agent_version && ` · agent ${server.agent_version}`}
          </div>
        </div>
        <div className="row" style={{ gap: 4 }}>
          <button className="btn small icon" title="Ping" onClick={() => action("ping")}>
            📡
          </button>
          <button className="btn small icon" title="Новый токен" onClick={rotate}>
            🔑
          </button>
          <button className="btn small icon danger" title="Удалить" onClick={remove}>
            ✕
          </button>
        </div>
      </div>

      {r && (
        <div className="grid" style={{ gridTemplateColumns: "repeat(3, 1fr)", gap: 8, marginTop: 10 }}>
          <div>
            <div className="small muted">
              CPU load {r.metrics.load1} / {r.metrics.cpus}
            </div>
            <Sparkline points={metrics.map((m) => m.load1)} />
          </div>
          <div>
            <div className="small muted">
              RAM {r.metrics.mem_pct}% · {fmtBytes(r.metrics.mem_used)} из {fmtBytes(r.metrics.mem_total)}
            </div>
            <Sparkline points={metrics.map((m) => m.mem_pct)} max={100} color="var(--warn)" />
          </div>
          <div>
            <div className="small muted">
              Диск {r.metrics.disks.map((d) => `${d.path} ${d.pct}%`).join(", ")}
            </div>
            <Sparkline points={metrics.map((m) => m.disk_pct)} max={100} color={r.metrics.disks.some((d) => d.pct >= server.disk_alert_pct) ? "var(--danger)" : "var(--ok)"} />
          </div>
        </div>
      )}
      {r && <div className="small muted" style={{ marginTop: 4 }}>аптайм {fmtUptime(r.metrics.uptime)}</div>}

      {r && r.services.length > 0 && (
        <div style={{ marginTop: 10 }}>
          {r.services.map((s) => (
            <ServiceRow key={s.name} svc={s} onAction={(k) => action(k, s.name)} />
          ))}
        </div>
      )}
      {r && r.services.length === 0 && <p className="small muted" style={{ marginTop: 8 }}>Сервисы не описаны. Добавьте их в /etc/dash-agent/config.json на сервере.</p>}
      {r && r.failed_units.length > 0 && (
        <p className="small" style={{ color: "var(--danger)", marginTop: 6 }}>
          failed-юниты: {r.failed_units.join(", ")}
        </p>
      )}
      {!r && (
        <p className="small muted" style={{ marginTop: 8 }}>
          Агент не подключён.{" "}
          <button className="btn small" onClick={rotate}>
            Показать команду установки
          </button>
        </p>
      )}
      {cmd && <CommandOutput projectId={projectId} server={server} command={cmd} onClose={() => setCmd(null)} />}
      {showToken && <TokenModal server={showToken} onClose={() => setShowToken(null)} />}
    </div>
  );
}

function MonitorRow({ projectId, m, onChange }: { projectId: number; m: Monitor; onChange: (m: Monitor | null) => void }) {
  const [uptime, setUptime] = useState<{ uptime_pct: number | null; samples: { ok: boolean; latency_ms: number | null }[] } | null>(null);
  const toast = useToast();
  useEffect(() => {
    get<typeof uptime>(`/api/projects/${projectId}/monitors/${m.id}/uptime`).then(setUptime).catch(() => setUptime(null));
  }, [projectId, m.id, m.last_checked_at]);

  async function check() {
    try {
      onChange(await post<Monitor>(`/api/projects/${projectId}/monitors/${m.id}/check`));
    } catch (e) {
      toast((e as Error).message, true);
    }
  }
  async function remove() {
    if (!confirm(`Удалить проверку ${m.name}?`)) return;
    await del(`/api/projects/${projectId}/monitors/${m.id}`);
    onChange(null);
  }
  const color = m.last_ok === null ? "var(--muted)" : m.last_ok ? "var(--ok)" : "var(--danger)";
  return (
    <div style={{ padding: "8px 0", borderTop: "1px solid var(--border)" }}>
      <div className="row between">
        <div style={{ flex: 1, minWidth: 0 }}>
          <div>
            <span style={{ color }}>●</span> {m.name}{" "}
            <a className="small muted" href={m.url} target="_blank" rel="noreferrer">
              {m.url.replace(/^https?:\/\//, "").slice(0, 40)}
            </a>
          </div>
          <div className="small muted">
            {m.last_ok === null ? "ещё не проверялся" : m.last_ok ? `OK ${m.last_status} · ${m.last_latency_ms} мс` : `${m.last_error ?? "ошибка"}`}
            {m.last_checked_at && ` · ${relTime(m.last_checked_at)}`}
            {uptime?.uptime_pct !== null && uptime?.uptime_pct !== undefined && ` · аптайм 24ч ${uptime.uptime_pct}%`}
          </div>
        </div>
        <div className="row" style={{ gap: 4 }}>
          <button className="btn small icon" onClick={check} title="Проверить сейчас">
            ↻
          </button>
          <button className="btn small icon danger" onClick={remove} title="Удалить">
            ✕
          </button>
        </div>
      </div>
      {uptime && <div style={{ marginTop: 6 }}><UptimeBar samples={uptime.samples} /></div>}
    </div>
  );
}

export default function Monitoring({ projectId }: { projectId: number }) {
  const servers = useFetch<Server[]>(`/api/projects/${projectId}/servers`);
  const monitors = useFetch<Monitor[]>(`/api/projects/${projectId}/monitors`);
  const incidents = useFetch<Incident[]>(`/api/projects/${projectId}/incidents?limit=30`);
  const [newServer, setNewServer] = useState<Server | null>(null);
  const [addServer, setAddServer] = useState(false);
  const [addMonitor, setAddMonitor] = useState(false);
  const [serverName, setServerName] = useState("");
  const [mon, setMon] = useState({ name: "", url: "", expect_status: 200, expect_text: "" });
  const toast = useToast();

  useEffect(() => {
    const t = setInterval(() => {
      void servers.reload();
      void incidents.reload();
    }, 30000);
    return () => clearInterval(t);
  }, [servers.reload, incidents.reload]); // eslint-disable-line react-hooks/exhaustive-deps

  async function createServer(e: FormEvent) {
    e.preventDefault();
    try {
      const s = await post<Server>(`/api/projects/${projectId}/servers`, { name: serverName });
      setAddServer(false);
      setServerName("");
      setNewServer(s);
      void servers.reload();
    } catch (err) {
      toast((err as Error).message, true);
    }
  }

  async function createMonitor(e: FormEvent) {
    e.preventDefault();
    try {
      const m = await post<Monitor>(`/api/projects/${projectId}/monitors`, { ...mon, expect_text: mon.expect_text || null });
      monitors.setData((prev) => [...(prev ?? []), m]);
      setAddMonitor(false);
      setMon({ name: "", url: "", expect_status: 200, expect_text: "" });
      void incidents.reload();
    } catch (err) {
      toast((err as Error).message, true);
    }
  }

  async function resolve(i: Incident) {
    await post(`/api/projects/${projectId}/incidents/${i.id}/resolve`);
    void incidents.reload();
  }

  const open = (incidents.data ?? []).filter((i) => !i.resolved_at);
  const closed = (incidents.data ?? []).filter((i) => i.resolved_at).slice(0, 10);

  return (
    <>
      {open.length > 0 && (
        <div className="card" style={{ borderColor: "var(--danger)" }}>
          <h3 style={{ color: "var(--danger)" }}>Открытые инциденты · {open.length}</h3>
          {open.map((i) => (
            <div key={i.id} className="row between" style={{ padding: "4px 0" }}>
              <span>
                {i.severity === "critical" ? "🔴" : "⚠️"} {i.message} <span className="small muted">· с {fmtDate(i.started_at)}</span>
              </span>
              <button className="btn small" onClick={() => resolve(i)} title="Закрыть вручную">
                закрыть
              </button>
            </div>
          ))}
        </div>
      )}

      <div className="row between" style={{ margin: "4px 0 8px" }}>
        <h3 style={{ margin: 0 }}>Серверы</h3>
        <button className="btn primary small" onClick={() => setAddServer(true)}>
          + Сервер
        </button>
      </div>
      {servers.data?.length === 0 && (
        <div className="card muted small">Добавьте сервер: получите команду установки агента, выполните её на VPS. Docker не нужен, достаточно python3 и systemd.</div>
      )}
      {(servers.data ?? []).map((s) => (
        <ServerCard key={s.id} projectId={projectId} server={s} onChange={() => void servers.reload()} />
      ))}

      <div className="row between" style={{ margin: "12px 0 8px" }}>
        <h3 style={{ margin: 0 }}>Внешние проверки</h3>
        <button className="btn primary small" onClick={() => setAddMonitor(true)}>
          + URL
        </button>
      </div>
      <div className="card">
        {monitors.data?.length === 0 && <p className="small muted">Дашборд будет сам дёргать адреса раз в минуту и ругаться после двух неудач подряд.</p>}
        {(monitors.data ?? []).map((m) => (
          <MonitorRow key={m.id} projectId={projectId} m={m} onChange={(nm) => monitors.setData((prev) => (nm ? (prev ?? []).map((x) => (x.id === nm.id ? nm : x)) : (prev ?? []).filter((x) => x.id !== m.id)))} />
        ))}
      </div>

      {closed.length > 0 && (
        <div className="card">
          <h3>История инцидентов</h3>
          {closed.map((i) => (
            <div key={i.id} className="small" style={{ padding: "3px 0" }}>
              <span className="muted">{fmtDate(i.started_at)}</span> · {INCIDENT_KIND[i.kind] ?? i.kind}: {i.message}{" "}
              <span className="muted">· {Math.max(1, Math.round((new Date(i.resolved_at!).getTime() - new Date(i.started_at).getTime()) / 60000))} мин</span>
            </div>
          ))}
        </div>
      )}

      {addServer && (
        <Modal title="Новый сервер" onClose={() => setAddServer(false)}>
          <form onSubmit={createServer}>
            <label className="field">
              <span>Название</span>
              <input className="input" value={serverName} onChange={(e) => setServerName(e.target.value)} placeholder="prod-1, api-vps…" autoFocus required />
            </label>
            <button className="btn primary" disabled={!serverName.trim()}>
              Создать и показать команду
            </button>
          </form>
        </Modal>
      )}
      {newServer && <TokenModal server={newServer} onClose={() => setNewServer(null)} />}
      {addMonitor && (
        <Modal title="Внешняя проверка" onClose={() => setAddMonitor(false)}>
          <form onSubmit={createMonitor}>
            <label className="field">
              <span>Название</span>
              <input className="input" value={mon.name} onChange={(e) => setMon({ ...mon, name: e.target.value })} required autoFocus />
            </label>
            <label className="field">
              <span>URL</span>
              <input className="input" value={mon.url} onChange={(e) => setMon({ ...mon, url: e.target.value })} placeholder="https://api.example.com/health" required />
            </label>
            <div className="row">
              <label className="field" style={{ flex: 1 }}>
                <span>Ожидаемый код</span>
                <input className="input" type="number" value={mon.expect_status} onChange={(e) => setMon({ ...mon, expect_status: Number(e.target.value) })} />
              </label>
              <label className="field" style={{ flex: 2 }}>
                <span>Текст в ответе (необязательно)</span>
                <input className="input" value={mon.expect_text} onChange={(e) => setMon({ ...mon, expect_text: e.target.value })} placeholder='"ok"' />
              </label>
            </div>
            <button className="btn primary" disabled={!mon.name || !mon.url}>
              Добавить
            </button>
          </form>
        </Modal>
      )}
    </>
  );
}
