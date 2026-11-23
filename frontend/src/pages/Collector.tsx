import { useEffect, useState } from "react";
import { del, patch, post, put, type MailRule, type MailState, type MaxStatus } from "../lib/api";
import { relTime } from "../lib/format";
import { useFetch } from "../lib/useFetch";
import { useToast } from "../components/Toast";

export default function Collector() {
  return (
    <div className="page">
      <h1>Сборщик запросов</h1>
      <p className="small muted">
        Письма от ваших поставщиков и личные сообщения MAX бот пересылает вам в Telegram с кнопкой «➕ в задачи».
      </p>
      <MailCard />
      <MaxCard />
    </div>
  );
}

// ---------- mail ----------

function MailCard() {
  const state = useFetch<MailState>("/api/collector/mail");
  const acc = state.data?.account ?? null;
  const [host, setHost] = useState("imap.mail.ru");
  const [login, setLogin] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const toast = useToast();

  useEffect(() => {
    if (acc) {
      setHost(acc.host);
      setLogin(acc.login);
    }
  }, [acc?.host, acc?.login]); // eslint-disable-line react-hooks/exhaustive-deps

  async function save(enabled = true) {
    setBusy(true);
    try {
      state.setData(
        await put<MailState>("/api/collector/mail", {
          host: host.trim(),
          port: acc?.port ?? 993,
          login: login.trim(),
          password: password || null,
          folder: acc?.folder ?? "INBOX",
          enabled,
        }),
      );
      setPassword("");
      toast(enabled ? "Почта подключена" : "Сбор писем выключен");
    } catch (e) {
      toast((e as Error).message, true);
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    if (!confirm("Отключить почту? Пароль приложения будет удалён с сервера.")) return;
    try {
      await del("/api/collector/mail");
      setLogin("");
      state.reload();
    } catch (e) {
      toast((e as Error).message, true);
    }
  }

  return (
    <div className="card stack">
      <h3>📧 Почта</h3>
      <p className="small muted">
        Ящик читается только на чтение: письма не помечаются прочитанными. Нужен не обычный пароль, а пароль для
        внешнего приложения: Mail.ru → Настройки → Безопасность → «Пароли для внешних приложений» (доступ к IMAP).
        Пересылаются только новые письма от поставщиков ниже.
      </p>
      {acc && (
        <p className="small">
          {!acc.enabled ? (
            <span className="muted">Выключено</span>
          ) : acc.last_error ? (
            <span style={{ color: "var(--danger)" }}>{acc.last_error}</span>
          ) : (
            <span style={{ color: "var(--ok)" }}>
              Подключено{acc.last_check_at && <span className="muted"> · проверено {relTime(acc.last_check_at)}</span>}
            </span>
          )}
        </p>
      )}
      <div className="row wrap" style={{ gap: 6 }}>
        <input className="input" style={{ flex: "1 1 140px" }} placeholder="IMAP-сервер" value={host} onChange={(e) => setHost(e.target.value)} />
        <input
          className="input"
          style={{ flex: "2 1 180px" }}
          placeholder="адрес почты"
          autoComplete="username"
          value={login}
          onChange={(e) => setLogin(e.target.value)}
        />
        <input
          className="input"
          style={{ flex: "2 1 180px" }}
          type="password"
          autoComplete="new-password"
          placeholder={acc?.has_password ? "пароль приложения сохранён" : "пароль приложения"}
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />
      </div>
      <div className="row wrap">
        <button className="btn primary" onClick={() => save(true)} disabled={busy || !login.trim() || (!password && !acc?.has_password)}>
          {busy ? "Проверяю…" : acc ? "Сохранить" : "Подключить"}
        </button>
        {acc?.enabled && (
          <button className="btn" onClick={() => save(false)} disabled={busy}>
            Выключить
          </button>
        )}
        {acc && (
          <button className="btn danger" onClick={remove} disabled={busy}>
            Удалить
          </button>
        )}
      </div>
      <Suppliers rules={state.data?.rules ?? []} reload={state.reload} />
    </div>
  );
}

function Suppliers({ rules, reload }: { rules: MailRule[]; reload: () => void }) {
  const [name, setName] = useState("");
  const [patterns, setPatterns] = useState("");
  const [editing, setEditing] = useState<number | null>(null);
  const toast = useToast();

  async function submit() {
    try {
      if (editing) await patch(`/api/collector/mail/rules/${editing}`, { name: name.trim(), patterns });
      else await post("/api/collector/mail/rules", { name: name.trim(), patterns });
      setName("");
      setPatterns("");
      setEditing(null);
      reload();
    } catch (e) {
      toast((e as Error).message, true);
    }
  }

  async function toggle(r: MailRule) {
    try {
      await patch(`/api/collector/mail/rules/${r.id}`, { enabled: !r.enabled });
      reload();
    } catch (e) {
      toast((e as Error).message, true);
    }
  }

  async function remove(r: MailRule) {
    if (!confirm(`Удалить поставщика «${r.name}»?`)) return;
    try {
      await del(`/api/collector/mail/rules/${r.id}`);
      reload();
    } catch (e) {
      toast((e as Error).message, true);
    }
  }

  return (
    <div className="stack" style={{ gap: 8 }}>
      <h4 style={{ margin: "8px 0 0" }}>Поставщики</h4>
      {rules.map((r) => (
        <div key={r.id} className="row between" style={{ gap: 8, alignItems: "flex-start", opacity: r.enabled ? 1 : 0.55 }}>
          <span className="small" style={{ flex: 1 }}>
            {r.name}
            <br />
            <span className="muted">{r.patterns.join(", ")}</span>
          </span>
          <button className="btn small" onClick={() => toggle(r)}>
            {r.enabled ? "Пауза" : "Вкл"}
          </button>
          <button
            className="btn small"
            onClick={() => {
              setEditing(r.id);
              setName(r.name);
              setPatterns(r.patterns.join(", "));
            }}
          >
            ✎
          </button>
          <button className="btn small" onClick={() => remove(r)}>
            ✕
          </button>
        </div>
      ))}
      {rules.length === 0 && <p className="small muted">Пока никого. Добавьте 2–3 поставщиков.</p>}
      <div className="row wrap" style={{ gap: 6 }}>
        <input className="input" style={{ flex: "1 1 140px" }} placeholder="название" value={name} onChange={(e) => setName(e.target.value)} />
        <input
          className="input"
          style={{ flex: "3 1 220px" }}
          placeholder="адреса или домены: ivan@x.ru, @supplier.ru"
          value={patterns}
          onChange={(e) => setPatterns(e.target.value)}
        />
        <button className="btn" onClick={submit} disabled={!name.trim() || !patterns.trim()}>
          {editing ? "Сохранить" : "Добавить"}
        </button>
        {editing && (
          <button
            className="btn"
            onClick={() => {
              setEditing(null);
              setName("");
              setPatterns("");
            }}
          >
            Отмена
          </button>
        )}
      </div>
    </div>
  );
}

// ---------- MAX ----------

const MAX_STATUS: Record<MaxStatus["status"], string> = {
  off: "Не подключён",
  connecting: "Подключаюсь…",
  waiting_qr: "Ждёт сканирования QR",
  waiting_password: "Нужен пароль двухфакторной защиты",
  online: "Подключён",
  error: "Ошибка",
  logged_out: "Сессию закрыли в приложении MAX",
};

function MaxCard() {
  const [waiting, setWaiting] = useState(false);
  const st = useFetch<MaxStatus>("/api/collector/max", [], { pollMs: waiting ? 2000 : 0 });
  const s = st.data;
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const toast = useToast();

  useEffect(() => {
    setWaiting(!!s && ["connecting", "waiting_qr", "waiting_password"].includes(s.status));
  }, [s?.status]); // eslint-disable-line react-hooks/exhaustive-deps

  async function act(path: string, body?: unknown) {
    setBusy(true);
    try {
      st.setData(await post<MaxStatus>(path, body));
    } catch (e) {
      toast((e as Error).message, true);
    } finally {
      setBusy(false);
    }
  }

  async function logout() {
    if (!confirm("Отключить MAX? Сессия на сервере будет удалена.")) return;
    await act("/api/collector/max/logout");
  }

  const online = s?.status === "online";
  return (
    <div className="card stack">
      <h3>💬 MAX</h3>
      <p className="small" style={{ color: "var(--warn)" }}>
        Это неофициальный способ (как web.max.ru): MAX может его сломать обновлением, а аккаунт теоретически могут
        ограничить. Начните с запасного номера.
      </p>
      <p className="small muted">
        Пересылаются только личные сообщения, без групп и каналов; вместо фото и файлов — пометка. В MAX ничего не
        отправляется.
      </p>
      {s && (
        <p className="small">
          <span style={{ color: online ? "var(--ok)" : s.status === "error" ? "var(--danger)" : undefined }}>
            {MAX_STATUS[s.status] ?? s.status}
          </span>
          {online && s.me_name && <span className="muted"> · {s.me_name}</span>}
          {s.last_error && <span className="muted"> · {s.last_error}</span>}
        </p>
      )}
      {s?.status === "waiting_qr" && s.qr_svg && (
        <div className="stack" style={{ alignItems: "center", gap: 8 }}>
          <div
            style={{ background: "#fff", padding: 8, borderRadius: 8, width: 240, maxWidth: "100%" }}
            dangerouslySetInnerHTML={{ __html: s.qr_svg }}
          />
          <p className="small muted" style={{ textAlign: "center" }}>
            Откройте MAX на телефоне → Профиль → Устройства → «Подключить устройство» и отсканируйте код. Код
            действует пару минут.
          </p>
        </div>
      )}
      {s?.status === "waiting_password" && (
        <div className="row wrap" style={{ gap: 6 }}>
          <input
            className="input"
            style={{ flex: "2 1 180px" }}
            type="password"
            autoComplete="off"
            placeholder={s.password_hint ? `пароль (подсказка: ${s.password_hint})` : "пароль MAX"}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
          <button
            className="btn primary"
            disabled={busy || !password}
            onClick={async () => {
              await act("/api/collector/max/password", { password });
              setPassword("");
            }}
          >
            Отправить
          </button>
        </div>
      )}
      <div className="row wrap">
        {s && !online && !["connecting", "waiting_qr", "waiting_password"].includes(s.status) && (
          <button className="btn primary" onClick={() => act("/api/collector/max/connect")} disabled={busy}>
            {busy ? "Запускаю…" : s.status === "off" && s.last_error ? "Показать новый QR" : "Подключить"}
          </button>
        )}
        {s && (online || s.enabled || s.running) && (
          <button className="btn danger" onClick={logout} disabled={busy}>
            Отключить
          </button>
        )}
      </div>
    </div>
  );
}
