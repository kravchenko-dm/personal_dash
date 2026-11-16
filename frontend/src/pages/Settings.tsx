import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  del,
  getToken,
  patch,
  post,
  put,
  setToken,
  type BotUser,
  type CalendarAlerts,
  type SecurityInfo,
  type Status,
  type YouGileAlerts,
} from "../lib/api";
import { relTime } from "../lib/format";
import { useFetch } from "../lib/useFetch";
import { currentSubscription, isStandalone, pushSupported, subscribePush, unsubscribePush } from "../lib/push";
import { useToast } from "../components/Toast";
import { useIsOwner, useMe, type Me } from "../lib/me";

function Chip({ on, label }: { on: boolean; label: string }) {
  return <span className={"chip " + (on ? "on" : "off")}>{on ? "●" : "○"} {label}</span>;
}

export default function Settings({ onLogout, onMe }: { onLogout: () => void; onMe: (me: Me) => void }) {
  const me = useMe();
  const owner = useIsOwner();
  const only = (path: string) => (owner ? path : null);
  const { data: status } = useFetch<Status>("/api/status");
  const { data: alerts, setData: setAlerts } = useFetch<CalendarAlerts>(only("/api/calendar/alerts"));
  const { data: ygAlerts, setData: setYgAlerts } = useFetch<YouGileAlerts>(only("/api/yougile/alerts"));
  const security = useFetch<SecurityInfo>(only("/api/auth/events"));
  const [subscribed, setSubscribed] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);
  const toast = useToast();

  useEffect(() => {
    currentSubscription().then((s) => setSubscribed(!!s));
  }, []);

  async function togglePush() {
    if (!status) return;
    setBusy(true);
    try {
      if (subscribed) {
        await unsubscribePush();
        setSubscribed(false);
      } else {
        await subscribePush(status.vapid_public_key);
        setSubscribed(true);
        toast("Уведомления включены");
      }
    } catch (e) {
      toast((e as Error).message, true);
    } finally {
      setBusy(false);
    }
  }

  async function testPush() {
    try {
      const { sent } = await post<{ sent: number }>("/api/push/test");
      toast(sent ? `Отправлено на ${sent} устр.` : "Нет подписок");
    } catch (e) {
      toast((e as Error).message, true);
    }
  }

  async function savePrefs(prefs: Partial<Pick<Me, "evening_digest" | "morning_brief">>) {
    try {
      onMe(await patch<Me>("/api/auth/me", prefs));
      toast("Сохранено");
    } catch (e) {
      toast((e as Error).message, true);
    }
  }

  async function logoutAll() {
    const text = owner
      ? "Все входы на других устройствах станут недействительны — и ваши, и всех пользователей. " +
        "Токен в «Командах» на iPhone тоже перестанет работать — скопируйте новый. Продолжить?"
      : "Все ваши входы на других устройствах станут недействительны. Продолжить?";
    if (!confirm(text)) return;
    try {
      const { token } = await post<{ token: string }>("/api/auth/logout-all");
      setToken(token);
      toast("Все остальные сессии завершены");
    } catch (e) {
      toast((e as Error).message, true);
    }
  }

  const iosHint = /iPhone|iPad/.test(navigator.userAgent) && !isStandalone();

  async function saveAlerts(patch: Partial<CalendarAlerts>) {
    if (!alerts) return;
    const next = { ...alerts, ...patch };
    setAlerts(next);
    try {
      setAlerts(await put<CalendarAlerts>("/api/calendar/alerts", next));
      toast("Сохранено");
    } catch (e) {
      setAlerts(alerts);
      toast((e as Error).message, true);
    }
  }

  async function saveYgAlerts(patch: Partial<YouGileAlerts>) {
    if (!ygAlerts) return;
    const next = { ...ygAlerts, ...patch };
    setYgAlerts(next);
    try {
      setYgAlerts(await put<YouGileAlerts>("/api/yougile/alerts", next));
      toast("Сохранено");
    } catch (e) {
      setYgAlerts(ygAlerts);
      toast((e as Error).message, true);
    }
  }

  return (
    <div className="page">
      <h1>Ещё</h1>
      <Link to="/summaries" className="card clickable row between" style={{ display: "flex" }}>
        <span>📝 Саммари и чаты Telegram</span>
        <span className="muted">›</span>
      </Link>
      {owner && (
        <>
      <Link to="/settings/backups" className="card clickable row between" style={{ display: "flex" }}>
        <span>💾 Хранилище бэкапов</span>
        <span className="muted">›</span>
      </Link>
      <Link to="/yougile" className="card clickable row between" style={{ display: "flex" }}>
        <span>🗂 YouGile</span>
        <span className="muted">›</span>
      </Link>
        </>
      )}

      <div className="card stack">
        <h3>Push-уведомления</h3>
        {!pushSupported() && <p className="muted small">Браузер не поддерживает Web Push.</p>}
        {iosHint && (
          <p className="small" style={{ color: "var(--warn)" }}>
            На iPhone пуши работают только после «Поделиться → На экран “Домой”». Откройте приложение с главного экрана.
          </p>
        )}
        {status && !status.push && <p className="muted small">На сервере не заданы VAPID-ключи.</p>}
        <div className="row wrap">
          <button className="btn primary" onClick={togglePush} disabled={busy || !status?.push || !pushSupported()}>
            {subscribed ? "Отключить на этом устройстве" : "Включить на этом устройстве"}
          </button>
          <button className="btn" onClick={testPush} disabled={!status?.push}>
            Тест
          </button>
        </div>
      </div>

      {!owner && (
        <div className="card stack">
          <h3>Сообщения от бота</h3>
          <label className="row" style={{ gap: 8 }}>
            <input type="checkbox" checked={me.evening_digest} onChange={(e) => savePrefs({ evening_digest: e.target.checked })} />
            <span>Вечерний дайджест в 21:00 — выжимка по вашим чатам за день</span>
          </label>
          <label className="row" style={{ gap: 8 }}>
            <input type="checkbox" checked={me.morning_brief} onChange={(e) => savePrefs({ morning_brief: e.target.checked })} />
            <span>Утренний бриф в 08:00 — открытые задачи</span>
          </label>
          <p className="small muted">
            Бот собирает вашу личку с ним и группы, куда вы сами его добавили. В боте: /tasks, /add, /done, /summary,
            /login — ссылка для входа сюда.
          </p>
          <button className="btn danger" onClick={logoutAll}>
            Выйти на всех устройствах
          </button>
        </div>
      )}

      {owner && (
        <>
      <div className="card stack">
        <h3>Интеграции</h3>
        {status ? (
          <div className="row wrap">
            <Chip on={status.telegram} label={status.bot_username ? `Telegram @${status.bot_username}` : "Telegram"} />
            <Chip on={status.claude} label={`LLM: ${status.llm_provider} / ${status.llm_model}`} />
            <Chip on={status.google_connected} label="Google Calendar" />
            <Chip on={status.icloud} label="iCloud / Семья" />
            <Chip on={status.yougile} label="YouGile" />
            <Chip on={status.push} label="Web Push" />
          </div>
        ) : (
          <p className="muted">…</p>
        )}
        <p className="small muted">Ключи задаются в .env на сервере, см. README.</p>
      </div>

      <div className="card stack">
        <h3>Уведомления календаря</h3>
        {!status?.google_connected && !status?.icloud && (
          <p className="muted small">Подключите Google или iCloud в разделе «Календарь».</p>
        )}
        <label className="field">
          <span>Напоминать перед событием</span>
          <select
            className="input"
            value={alerts?.remind_minutes ?? 15}
            disabled={!alerts}
            onChange={(e) => saveAlerts({ remind_minutes: Number(e.target.value) })}
          >
            {(alerts?.choices ?? [0, 5, 10, 15, 30, 60, 120]).map((m) => (
              <option key={m} value={m}>
                {m === 0 ? "Не напоминать" : m < 60 ? `За ${m} мин` : `За ${m / 60} ч`}
              </option>
            ))}
          </select>
        </label>
        <label className="row" style={{ gap: 8, alignItems: "center" }}>
          <input
            type="checkbox"
            checked={alerts?.watch_changes ?? true}
            disabled={!alerts}
            onChange={(e) => saveAlerts({ watch_changes: e.target.checked })}
          />
          <span>Сообщать о новых, перенесённых и отменённых событиях</span>
        </label>
        {alerts?.watch_changes && (
          <label className="field">
            <span>Следить за событиями на</span>
            <select
              className="input"
              value={alerts.watch_days}
              onChange={(e) => saveAlerts({ watch_days: Number(e.target.value) })}
            >
              {[1, 3, 7, 14, 30].map((d) => (
                <option key={d} value={d}>
                  {d === 1 ? "1 день" : d < 5 ? `${d} дня` : `${d} дней`}
                </option>
              ))}
            </select>
          </label>
        )}
        <p className="small muted">Проверка каждые 5 минут, уведомления приходят пушем и в Telegram.</p>
      </div>

      <div className="card stack">
        <h3>Уведомления YouGile</h3>
        {status && !status.yougile && <p className="muted small">Задайте YOUGILE_API_KEY в .env на сервере.</p>}
        <label className="field">
          <span>Какие задачи</span>
          <select
            className="input"
            value={ygAlerts?.scope ?? "all"}
            disabled={!ygAlerts}
            onChange={(e) => saveYgAlerts({ scope: e.target.value as YouGileAlerts["scope"] })}
          >
            <option value="all">Все задачи компании</option>
            <option value="mine">Только мои (исполнитель или автор)</option>
          </select>
        </label>
        <label className="field">
          <span>Напоминать перед дедлайном со временем</span>
          <select
            className="input"
            value={ygAlerts?.remind_minutes ?? 60}
            disabled={!ygAlerts}
            onChange={(e) => saveYgAlerts({ remind_minutes: Number(e.target.value) })}
          >
            {(ygAlerts?.choices ?? [0, 15, 30, 60, 120, 180]).map((m) => (
              <option key={m} value={m}>
                {m === 0 ? "Не напоминать" : m < 60 ? `За ${m} мин` : `За ${m / 60} ч`}
              </option>
            ))}
          </select>
        </label>
        <label className="row" style={{ gap: 8, alignItems: "center" }}>
          <input
            type="checkbox"
            checked={ygAlerts?.watch_changes ?? true}
            disabled={!ygAlerts}
            onChange={(e) => saveYgAlerts({ watch_changes: e.target.checked })}
          />
          <span>Сообщать о новых задачах, назначениях на меня, выполнении и переносе сроков</span>
        </label>
        <label className="row" style={{ gap: 8, alignItems: "center" }}>
          <input
            type="checkbox"
            checked={ygAlerts?.brief ?? true}
            disabled={!ygAlerts}
            onChange={(e) => saveYgAlerts({ brief: e.target.checked })}
          />
          <span>Просроченные и ближайшие дедлайны в утреннем брифе</span>
        </label>
        <p className="small muted">
          Проверка каждые 5 минут, пушем и в Telegram. Свои действия не присылаются. Дедлайны без времени — только в брифе.
        </p>
      </div>

      <div className="card stack">
        <h3>Безопасность</h3>
        {security.data ? (
          <>
            <p className="small muted">
              Неверных паролей за сутки: {security.data.failed_24h}. После {security.data.max_fails} ошибок с одного
              адреса вход с него закрывается на {security.data.window_minutes} мин, приходит алерт. Вход с нового адреса —
              тоже алерт.
            </p>
            {security.data.locked_ips.length > 0 && (
              <p className="small" style={{ color: "var(--danger)" }}>
                Сейчас заблокированы: {security.data.locked_ips.join(", ")}
              </p>
            )}
            <div className="stack" style={{ gap: 4 }}>
              {security.data.events.slice(0, 12).map((e, i) => (
                <div key={i} className="small row between" style={{ gap: 8 }}>
                  <span>
                    {e.result === "ok" ? "✅" : e.result === "blocked" ? "⛔" : "❌"} {e.ip}
                    {e.method === "telegram" && " · через бота"}
                    {e.user && ` · ${e.user}`}
                  </span>
                  <span className="muted" title={e.user_agent ?? ""} style={{ textAlign: "right" }}>
                    {relTime(e.ts)}
                  </span>
                </div>
              ))}
              {security.data.events.length === 0 && <p className="small muted">Попыток входа пока не было.</p>}
            </div>
          </>
        ) : (
          <p className="muted">…</p>
        )}
        <button className="btn danger" onClick={logoutAll}>
          Выйти на всех устройствах
        </button>
      </div>
        </>
      )}

      {owner && <UsersCard />}

      {owner && (
      <div className="card stack">
        <h3>Напоминания Apple (Команды на iPhone)</h3>
        <p className="small muted">
          Токен подставляется в адрес команды: <code>/api/apple/reminders/sync?token=…</code>. Инструкция в README → «Напоминания Apple».
        </p>
        <button
          className="btn"
          onClick={async () => {
            try {
              await navigator.clipboard.writeText(getToken() ?? "");
              toast("Токен скопирован");
            } catch {
              prompt("Скопируйте токен:", getToken() ?? "");
            }
          }}
        >
          Токен для Команд
        </button>
      </div>
      )}

      <button className="btn danger" onClick={onLogout}>
        Выйти
      </button>
      <p className="small muted" style={{ marginTop: 12 }}>
        Сборка {new Date(__BUILD_TIME__).toLocaleString("ru-RU")} ·{" "}
        <a
          onClick={async () => {
            const regs = await navigator.serviceWorker?.getRegistrations?.();
            await Promise.all((regs ?? []).map((r) => r.update()));
            window.location.reload();
          }}
          style={{ cursor: "pointer" }}
        >
          проверить обновление
        </a>
      </p>
    </div>
  );
}


const LEVELS: Record<string, string> = { workspace: "свой кабинет", media: "только расшифровка" };

function UsersCard() {
  const users = useFetch<{ users: BotUser[] }>("/api/telegram/users");
  const [tgId, setTgId] = useState("");
  const [name, setName] = useState("");
  const [level, setLevel] = useState<"workspace" | "media">("workspace");
  const toast = useToast();

  async function add() {
    const id = Number(tgId.trim());
    if (!Number.isInteger(id) || id <= 0) {
      toast("Нужен числовой Telegram id", true);
      return;
    }
    try {
      await post("/api/telegram/users", { telegram_id: id, name: name.trim(), level });
      setTgId("");
      setName("");
      users.reload();
      toast("Доступ выдан");
    } catch (e) {
      toast((e as Error).message, true);
    }
  }

  async function changeLevel(u: BotUser, next: string) {
    try {
      await patch(`/api/telegram/users/${u.id}`, { level: next });
      users.reload();
    } catch (e) {
      toast((e as Error).message, true);
    }
  }

  async function remove(u: BotUser) {
    if (!confirm(`Удалить ${u.name || u.telegram_id}? Вместе с ним удалятся его задачи, чаты и саммари.`)) return;
    try {
      await del(`/api/telegram/users/${u.id}`);
      users.reload();
    } catch (e) {
      toast((e as Error).message, true);
    }
  }

  return (
    <div className="card stack">
      <h3>Пользователи</h3>
      <p className="small muted">
        «Свой кабинет» — свои задачи, сбор своих чатов (личка с ботом и группы, куда человек сам добавил бота),
        саммари, дайджесты и вход сюда через бота (/login). «Только расшифровка» — расшифровка своих записей.
        Нейросеть и распознавание — за ваш счёт. Их задачи и переписку вы не видите — только счётчики. Новый
        человек пишет боту /start — вам приходит запрос с кнопками.
      </p>
      <div className="stack" style={{ gap: 8 }}>
        {(users.data?.users ?? []).map((u) => (
          <div key={u.id} className="row between" style={{ gap: 8, alignItems: "flex-start" }}>
            <span className="small" style={{ flex: 1 }}>
              {u.name || "без имени"}
              {u.username && <span className="muted"> @{u.username}</span>}
              <span className="muted"> · id {u.telegram_id}</span>
              <br />
              <span className="muted">
                с {new Date(u.created_at).toLocaleDateString("ru-RU")} · записей: {u.jobs}
                {u.cost_rub > 0 && ` (${u.cost_rub.toFixed(2).replace(".", ",")} ₽)`}
                {u.level === "workspace" && ` · чатов: ${u.chats} · саммари: ${u.summaries} · задач: ${u.tasks}`}
              </span>
            </span>
            <select
              className="input"
              style={{ width: 150, minHeight: 32, padding: "4px 8px" }}
              value={u.level}
              onChange={(e) => changeLevel(u, e.target.value)}
            >
              {Object.entries(LEVELS).map(([k, label]) => (
                <option key={k} value={k}>
                  {label}
                </option>
              ))}
            </select>
            <button className="btn small" onClick={() => remove(u)}>
              ✕
            </button>
          </div>
        ))}
        {users.data && users.data.users.length === 0 && <p className="small muted">Пользователей пока нет.</p>}
      </div>
      <div className="row wrap" style={{ gap: 6 }}>
        <input
          className="input"
          style={{ flex: "1 1 120px" }}
          inputMode="numeric"
          placeholder="Telegram id"
          value={tgId}
          onChange={(e) => setTgId(e.target.value)}
        />
        <input
          className="input"
          style={{ flex: "2 1 140px" }}
          placeholder="имя (для себя)"
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
        <select className="input" style={{ flex: "1 1 140px" }} value={level} onChange={(e) => setLevel(e.target.value as "workspace" | "media")}>
          {Object.entries(LEVELS).map(([k, label]) => (
            <option key={k} value={k}>
              {label}
            </option>
          ))}
        </select>
        <button className="btn" onClick={add} disabled={!tgId.trim()}>
          Добавить
        </button>
      </div>
    </div>
  );
}
