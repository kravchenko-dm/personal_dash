import { useEffect, useState, type FormEvent } from "react";
import { del, get, post, put, type BalanceAccount, type ProviderInfo } from "../lib/api";
import { useFetch } from "../lib/useFetch";
import { fmtMoney, relTime } from "../lib/format";
import { Modal } from "../components/Modal";
import { useToast } from "../components/Toast";

function Sparkline({ accountId, version }: { accountId: number; version: string | null }) {
  const [points, setPoints] = useState<number[]>([]);
  // `version` is the account's last check time: a new sample means the line must be reloaded
  useEffect(() => {
    get<{ value: number }[]>(`/api/balances/${accountId}/history?limit=60`)
      .then((rows) => setPoints(rows.map((r) => r.value)))
      .catch(() => setPoints([]));
  }, [accountId, version]);
  if (points.length < 2) return null;
  const min = Math.min(...points);
  const max = Math.max(...points);
  const w = 200;
  const h = 40;
  const d = points
    .map((v, i) => {
      const x = (i / (points.length - 1)) * w;
      const y = max === min ? h / 2 : h - ((v - min) / (max - min)) * (h - 4) - 2;
      return `${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
  return (
    <svg className="sparkline" viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none">
      <path d={d} fill="none" stroke="var(--accent)" strokeWidth={2} />
    </svg>
  );
}

function AccountForm({
  providers,
  account,
  onClose,
  onSaved
}: {
  providers: ProviderInfo[];
  account: BalanceAccount | null;
  onClose: () => void;
  onSaved: (a: BalanceAccount | null) => void;
}) {
  const [provider, setProvider] = useState(account?.provider ?? providers[0]?.key ?? "manual");
  const [name, setName] = useState(account?.name ?? "");
  const [currency, setCurrency] = useState(account?.currency ?? "");
  const [alert, setAlert] = useState(account?.alert_below?.toString() ?? "");
  const [config, setConfig] = useState<Record<string, string>>(account?.config ?? {});
  const [busy, setBusy] = useState(false);
  const toast = useToast();
  const info = providers.find((p) => p.key === provider);

  async function save(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    const body = { name, provider, config, currency, alert_below: alert ? Number(alert) : null, enabled: true };
    try {
      const saved = account ? await put<BalanceAccount>(`/api/balances/${account.id}`, body) : await post<BalanceAccount>("/api/balances", body);
      onSaved(saved);
      onClose();
    } catch (err) {
      toast((err as Error).message, true);
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    if (!account || !confirm("Удалить источник?")) return;
    await del(`/api/balances/${account.id}`);
    onSaved(null);
    onClose();
  }

  return (
    <Modal title={account ? "Источник баланса" : "Новый источник"} onClose={onClose}>
      <form onSubmit={save}>
        <label className="field">
          <span>Название</span>
          <input className="input" value={name} onChange={(e) => setName(e.target.value)} required placeholder="OpenRouter, Тинькофф…" />
        </label>
        <label className="field">
          <span>Провайдер</span>
          <select className="input" value={provider} onChange={(e) => { setProvider(e.target.value); setConfig({}); }} disabled={!!account}>
            {providers.map((p) => (
              <option key={p.key} value={p.key}>
                {p.title}
              </option>
            ))}
          </select>
          {info && <div className="help">{info.description}</div>}
        </label>
        {info?.fields.map((f) => (
          <label className="field" key={f.key}>
            <span>
              {f.label}
              {f.required ? " *" : ""}
            </span>
            {f.key === "headers" ? (
              <textarea className="input" value={config[f.key] ?? ""} placeholder={f.placeholder} onChange={(e) => setConfig({ ...config, [f.key]: e.target.value })} />
            ) : f.kind === "select" ? (
              <select className="input" value={config[f.key] ?? f.options?.[0]?.[0] ?? ""} onChange={(e) => setConfig({ ...config, [f.key]: e.target.value })}>
                {f.options?.map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </select>
            ) : (
              <input
                className="input"
                type={f.kind === "secret" ? "password" : f.kind === "number" ? "number" : "text"}
                step="any"
                value={config[f.key] ?? ""}
                placeholder={f.placeholder}
                onChange={(e) => setConfig({ ...config, [f.key]: e.target.value })}
                required={f.required}
              />
            )}
            {f.help && <div className="help">{f.help}</div>}
          </label>
        ))}
        <div className="row">
          <label className="field" style={{ flex: 1 }}>
            <span>Валюта</span>
            <input className="input" value={currency} onChange={(e) => setCurrency(e.target.value)} placeholder="USD / ₽" />
          </label>
          <label className="field" style={{ flex: 1 }}>
            <span>Пуш, если меньше</span>
            <input className="input" type="number" step="any" value={alert} onChange={(e) => setAlert(e.target.value)} placeholder="например 5" />
          </label>
        </div>
        <div className="row between">
          {account ? (
            <button type="button" className="btn danger" onClick={remove}>
              Удалить
            </button>
          ) : (
            <span />
          )}
          <button className="btn primary" disabled={busy || !name.trim()}>
            {busy ? "Проверяю…" : "Сохранить"}
          </button>
        </div>
      </form>
    </Modal>
  );
}

export default function Balances() {
  const providers = useFetch<ProviderInfo[]>("/api/balances/providers");
  const accounts = useFetch<BalanceAccount[]>("/api/balances", [], { pollMs: 60_000 });
  const [editing, setEditing] = useState<BalanceAccount | null | "new">(null);
  const [refreshing, setRefreshing] = useState(false);
  const toast = useToast();

  async function refreshAll() {
    setRefreshing(true);
    try {
      accounts.setData(await post<BalanceAccount[]>("/api/balances/refresh"));
      toast("Балансы обновлены");
    } catch (e) {
      toast((e as Error).message, true);
    } finally {
      setRefreshing(false);
    }
  }

  function onSaved(id: number | null, a: BalanceAccount | null) {
    accounts.setData((prev) => {
      const list = prev ?? [];
      if (!a) return list.filter((x) => x.id !== id);
      return list.some((x) => x.id === a.id) ? list.map((x) => (x.id === a.id ? a : x)) : [...list, a];
    });
  }

  const total = (accounts.data ?? []).reduce<Record<string, number>>((acc, a) => {
    if (a.last_value !== null) acc[a.currency || "?"] = (acc[a.currency || "?"] ?? 0) + a.last_value;
    return acc;
  }, {});

  return (
    <div className="page">
      <div className="page-head">
        <h1>Балансы</h1>
        <div className="row">
          <button className="btn" onClick={refreshAll} disabled={refreshing}>
            {refreshing ? "…" : "↻"}
          </button>
          <button className="btn primary" onClick={() => setEditing("new")}>
            + Источник
          </button>
        </div>
      </div>
      {Object.keys(total).length > 0 && (
        <div className="card row wrap">
          {Object.entries(total).map(([cur, v]) => (
            <div key={cur}>
              <div className="small muted">Итого {cur}</div>
              <div className="big">{fmtMoney(v, "")}</div>
            </div>
          ))}
        </div>
      )}
      {accounts.error && <div className="card" style={{ color: "var(--danger)" }}>{accounts.error}</div>}
      <div className="grid">
        {(accounts.data ?? []).map((a) => (
          <div key={a.id} className="card clickable" onClick={() => setEditing(a)}>
            <div className="row between">
              <strong>{a.name}</strong>
              <span className="chip">{providers.data?.find((p) => p.key === a.provider)?.title ?? a.provider}</span>
            </div>
            <div className="big" style={{ color: a.alert_below !== null && a.last_value !== null && a.last_value < a.alert_below ? "var(--danger)" : undefined }}>
              {fmtMoney(a.last_value, a.currency)}
            </div>
            <Sparkline accountId={a.id} version={a.last_checked_at} />
            {a.last_error ? (
              <div className="small" style={{ color: "var(--danger)" }}>{a.last_error}</div>
            ) : (
              <div className="small muted">обновлено {relTime(a.last_checked_at) || "—"}</div>
            )}
          </div>
        ))}
        {accounts.data?.length === 0 && (
          <div className="card muted">Пока нет источников. Добавьте API-ключ сервиса или ручной счёт.</div>
        )}
      </div>
      {editing && providers.data && (
        <AccountForm
          providers={providers.data}
          account={editing === "new" ? null : editing}
          onClose={() => setEditing(null)}
          onSaved={(a) => onSaved(editing === "new" ? null : editing.id, a)}
        />
      )}
    </div>
  );
}
