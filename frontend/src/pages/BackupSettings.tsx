import { useEffect, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { getToken, put, type BackupDestination, type BackupPlan } from "../lib/api";
import { useFetch } from "../lib/useFetch";
import { fmtBytes, relTime } from "../lib/format";
import { useToast } from "../components/Toast";

const TYPES: [BackupDestination["type"], string][] = [
  ["rclone", "rclone (OneDrive, Google Drive, Яндекс Диск…)"],
  ["sftp", "SFTP (Synology, другой сервер)"],
  ["rest", "restic rest-server"],
  ["s3", "S3-совместимое хранилище"],
  ["local", "локальный путь на сервере"]
];

export default function BackupSettings() {
  const dest = useFetch<BackupDestination>("/api/backups/destination");
  const overview = useFetch<{ plans: BackupPlan[] }>("/api/backups/overview");
  const [type, setType] = useState<BackupDestination["type"]>("rclone");
  const [remote, setRemote] = useState("onedrive");
  const [basePath, setBasePath] = useState("backups");
  const [url, setUrl] = useState("");
  const [rcloneConf, setRcloneConf] = useState("");
  const [envText, setEnvText] = useState("");
  const [busy, setBusy] = useState(false);
  const toast = useToast();

  useEffect(() => {
    if (!dest.data) return;
    setType(dest.data.type ?? "rclone");
    setRemote(dest.data.remote ?? "onedrive");
    setBasePath(dest.data.base_path ?? "backups");
    setUrl(dest.data.url ?? "");
  }, [dest.data]);

  async function save(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    const env: Record<string, string> = {};
    for (const line of envText.split("\n")) {
      const [k, ...v] = line.split("=");
      if (k.trim() && v.length) env[k.trim()] = v.join("=").trim();
    }
    try {
      const saved = await put<BackupDestination>("/api/backups/destination", {
        type,
        remote: remote || null,
        base_path: basePath || "backups",
        url: url || null,
        rclone_conf: rcloneConf || null,
        env: Object.keys(env).length ? env : null
      });
      dest.setData(saved);
      setRcloneConf("");
      setEnvText("");
      toast("Хранилище сохранено");
    } catch (err) {
      toast((err as Error).message, true);
    } finally {
      setBusy(false);
    }
  }

  async function downloadSheet() {
    try {
      const text = await fetch("/api/backups/recovery-sheet", { headers: { Authorization: `Bearer ${getToken()}` } }).then((r) => (r.ok ? r.text() : Promise.reject(new Error("Хранилище не настроено"))));
      const blob = new Blob([text], { type: "text/plain" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = "personal-dash-recovery.txt";
      a.click();
    } catch (e) {
      toast((e as Error).message, true);
    }
  }

  return (
    <div className="page">
      <div className="row" style={{ marginBottom: 6 }}>
        <Link to="/settings" className="small muted">
          ← Ещё
        </Link>
      </div>
      <h1>Хранилище бэкапов</h1>
      <p className="small muted">Одно хранилище на все проекты, у каждого плана своя подпапка. Данные шифруются restic на сервере, пароль репозитория генерируется автоматически и показывается в листе восстановления.</p>

      <form onSubmit={save} className="card">
        <label className="field">
          <span>Тип</span>
          <select className="input" value={type} onChange={(e) => setType(e.target.value as BackupDestination["type"])}>
            {TYPES.map(([k, l]) => (
              <option key={k} value={k}>
                {l}
              </option>
            ))}
          </select>
        </label>
        {type === "rclone" && (
          <>
            <label className="field">
              <span>Имя remote в rclone.conf</span>
              <input className="input" value={remote} onChange={(e) => setRemote(e.target.value)} placeholder="onedrive" />
            </label>
            <label className="field">
              <span>rclone.conf {dest.data?.has_rclone_conf && <span className="chip on">сохранён</span>}</span>
              <textarea className="input" style={{ minHeight: 140, fontFamily: "ui-monospace, monospace", fontSize: 12 }} value={rcloneConf} onChange={(e) => setRcloneConf(e.target.value)} placeholder={"[onedrive]\ntype = onedrive\ntoken = {...}\ndrive_id = ...\ndrive_type = personal"} />
              <div className="help">
                На Mac: <code>brew install rclone && rclone config</code> → New remote → onedrive → по умолчанию → в браузере войти в Microsoft. Затем <code>cat ~/.config/rclone/rclone.conf</code> и вставить секцию сюда. Оставьте пустым, чтобы не менять сохранённый.
              </div>
            </label>
          </>
        )}
        {type !== "rclone" && (
          <label className="field">
            <span>Адрес</span>
            <input className="input" value={url} onChange={(e) => setUrl(e.target.value)} placeholder={type === "sftp" ? "sftp:backup@nas.tailnet:/volume1/backups" : type === "rest" ? "rest:https://user:pass@host:8000" : type === "s3" ? "s3:https://s3.example.com/bucket" : "/mnt/backups"} />
            <div className="help">Полный префикс репозитория restic без подпапок. Подпапка проекта добавится автоматически.</div>
          </label>
        )}
        <label className="field">
          <span>Базовая папка</span>
          <input className="input" value={basePath} onChange={(e) => setBasePath(e.target.value)} />
        </label>
        {type === "s3" && (
          <label className="field">
            <span>Переменные окружения {dest.data?.env_keys.length ? <span className="chip on">{dest.data.env_keys.join(", ")}</span> : null}</span>
            <textarea className="input" value={envText} onChange={(e) => setEnvText(e.target.value)} placeholder={"AWS_ACCESS_KEY_ID=...\nAWS_SECRET_ACCESS_KEY=..."} />
          </label>
        )}
        <div className="row between">
          <span className="small muted">{dest.data?.configured ? "✓ настроено" : "не настроено"}</span>
          <button className="btn primary" disabled={busy}>
            Сохранить
          </button>
        </div>
      </form>

      <div className="card stack">
        <h3>Лист восстановления</h3>
        <p className="small muted">Текстовый файл с паролем restic, конфигом rclone и адресами репозиториев. Сохраните в менеджер паролей: с ним бэкапы восстанавливаются без дашборда.</p>
        <button className="btn" onClick={downloadSheet} disabled={!dest.data?.has_password}>
          Скачать лист восстановления
        </button>
      </div>

      {(overview.data?.plans.length ?? 0) > 0 && (
        <div className="card">
          <h3>Все планы</h3>
          {overview.data!.plans.map((p) => (
            <div key={p.id} className="row between small" style={{ padding: "4px 0" }}>
              <Link to={`/projects/${p.project_id}?tab=backups`}>
                {p.name} <span className="muted">· {p.server_name}</span>
              </Link>
              <span className="muted">
                {p.last_status ?? "—"} {p.last_ok_at && `· ok ${relTime(p.last_ok_at)}`} {p.repo_size ? `· ${fmtBytes(p.repo_size)}` : ""}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
