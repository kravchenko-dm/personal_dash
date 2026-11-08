import { useEffect, useState, type FormEvent } from "react";
import { useSearchParams } from "react-router-dom";
import { get, post, put, type CalendarEvent, type Status } from "../lib/api";
import { Modal } from "../components/Modal";
import { useFetch } from "../lib/useFetch";
import { useToast } from "../components/Toast";

type EventsResp = { connected: boolean; events: CalendarEvent[]; errors: string[] };
type CalInfo = { id: string; summary: string; color?: string | null; primary: boolean; source: "google" | "icloud" };
type CalsResp = { calendars: CalInfo[]; selected: { google: string[]; icloud: string[] }; errors: string[] };
const SOURCE_LABEL = { google: "Google", icloud: "iCloud" } as const;

function dayKey(iso: string) {
  return iso.slice(0, 10);
}

function dayLabel(key: string) {
  const d = new Date(key + "T00:00:00");
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const diff = Math.round((d.getTime() - today.getTime()) / 86400000);
  const base = d.toLocaleDateString("ru-RU", { weekday: "short", day: "numeric", month: "long" });
  if (diff === 0) return `Сегодня · ${base}`;
  if (diff === 1) return `Завтра · ${base}`;
  return base;
}

export function EventList({ events, compact = false }: { events: CalendarEvent[]; compact?: boolean }) {
  const groups = events.reduce<Record<string, CalendarEvent[]>>((acc, e) => {
    (acc[dayKey(e.start)] ??= []).push(e);
    return acc;
  }, {});
  if (events.length === 0) return <p className="muted">Событий нет.</p>;
  return (
    <>
      {Object.entries(groups).map(([day, list]) => (
        <div key={day}>
          {!compact && <div className="day-head">{dayLabel(day)}</div>}
          {list.map((e) => (
            <div className="event" key={e.id + e.calendar_id}>
              <div className="time">
                {e.all_day ? "весь день" : new Date(e.start).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" })}
              </div>
              <div style={{ flex: 1 }}>
                <div>{e.link ? <a href={e.link} target="_blank" rel="noreferrer">{e.title}</a> : e.title}</div>
                <div className="small muted">
                  {e.calendar && <span>{e.calendar} · </span>}
                  <span>{SOURCE_LABEL[e.source] ?? e.source}</span>
                  {e.location && <span> · 📍 {e.location}</span>}
                </div>
              </div>
            </div>
          ))}
        </div>
      ))}
    </>
  );
}

function pad(n: number) {
  return String(n).padStart(2, "0");
}
function localDate(d: Date) {
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}
function localTime(d: Date) {
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
function withOffset(date: string, time: string) {
  const d = new Date(`${date}T${time}:00`);
  const off = -d.getTimezoneOffset();
  const sign = off >= 0 ? "+" : "-";
  return `${date}T${time}:00${sign}${pad(Math.floor(Math.abs(off) / 60))}:${pad(Math.abs(off) % 60)}`;
}

function NewEventModal({ cals, onClose, onCreated }: { cals: CalsResp; onClose: () => void; onCreated: () => void }) {
  const toast = useToast();
  const writable = cals.calendars.filter((c) => !/reminders|напоминани/i.test(c.summary));
  const preferred =
    writable.find((c) => cals.selected[c.source].includes(c.id)) ?? writable.find((c) => c.primary) ?? writable[0];
  const now = new Date();
  now.setMinutes(now.getMinutes() < 30 ? 30 : 60, 0, 0);
  const [calKey, setCalKey] = useState(preferred ? `${preferred.source}|${preferred.id}` : "");
  const [title, setTitle] = useState("");
  const [allDay, setAllDay] = useState(false);
  const [date, setDate] = useState(localDate(now));
  const [start, setStart] = useState(localTime(now));
  const [end, setEnd] = useState(localTime(new Date(now.getTime() + 3600_000)));
  const [location, setLocation] = useState("");
  const [notes, setNotes] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    const [source, calendar_id] = calKey.split("|") as ["google" | "icloud", string];
    if (!calendar_id || !title.trim()) return;
    setBusy(true);
    try {
      await post("/api/calendar/events", {
        source,
        calendar_id,
        title: title.trim(),
        all_day: allDay,
        start: allDay ? date : withOffset(date, start),
        end: allDay ? null : withOffset(date, end),
        location: location.trim() || null,
        description: notes.trim() || null
      });
      toast("Событие добавлено");
      onCreated();
      onClose();
    } catch (err) {
      toast((err as Error).message, true);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal title="Новое событие" onClose={onClose}>
      <form className="stack" onSubmit={submit}>
        <select className="input" value={calKey} onChange={(e) => setCalKey(e.target.value)}>
          {writable.map((c) => (
            <option key={`${c.source}|${c.id}`} value={`${c.source}|${c.id}`}>
              {SOURCE_LABEL[c.source]} · {c.summary}
            </option>
          ))}
        </select>
        <input className="input" placeholder="Название" value={title} onChange={(e) => setTitle(e.target.value)} autoFocus required />
        <label className="row">
          <input type="checkbox" checked={allDay} onChange={(e) => setAllDay(e.target.checked)} /> Весь день
        </label>
        <div className="row">
          <input className="input" type="date" value={date} onChange={(e) => setDate(e.target.value)} required />
          {!allDay && (
            <>
              <input className="input" type="time" value={start} onChange={(e) => setStart(e.target.value)} required />
              <input className="input" type="time" value={end} onChange={(e) => setEnd(e.target.value)} required />
            </>
          )}
        </div>
        <input className="input" placeholder="Место (необязательно)" value={location} onChange={(e) => setLocation(e.target.value)} />
        <textarea className="input" placeholder="Заметки" rows={2} value={notes} onChange={(e) => setNotes(e.target.value)} />
        <button className="btn primary" type="submit" disabled={busy || !calKey}>
          {busy ? "Сохраняю…" : "Добавить"}
        </button>
      </form>
    </Modal>
  );
}

export default function Calendar() {
  const [days, setDays] = useState(7);
  const status = useFetch<Status>("/api/status");
  const { data, error, loading, reload } = useFetch<EventsResp>(`/api/calendar/events?days=${days}`, [days]);
  const [cals, setCals] = useState<CalsResp | null>(null);
  const [showCals, setShowCals] = useState(false);
  const [showNew, setShowNew] = useState(false);
  const [params] = useSearchParams();
  const toast = useToast();

  useEffect(() => {
    if (params.get("connected")) toast("Google Calendar подключён");
    const err = params.get("error");
    if (err) toast(`Google Calendar: ${err}`, true);
  }, [params, toast]);

  async function connect() {
    try {
      const { url } = await get<{ url: string }>("/api/calendar/oauth/start");
      window.location.href = url;
    } catch (e) {
      toast((e as Error).message, true);
    }
  }

  async function openCals() {
    try {
      setCals(await get<CalsResp>("/api/calendar/calendars"));
      setShowCals(true);
    } catch (e) {
      toast((e as Error).message, true);
    }
  }

  async function openNew() {
    try {
      setCals(await get<CalsResp>("/api/calendar/calendars"));
      setShowNew(true);
    } catch (e) {
      toast((e as Error).message, true);
    }
  }

  async function toggleCal(c: CalInfo) {
    if (!cals) return;
    const current = cals.selected[c.source];
    const ids = current.includes(c.id) ? current.filter((x) => x !== c.id) : [...current, c.id];
    setCals({ ...cals, selected: { ...cals.selected, [c.source]: ids } });
    await put("/api/calendar/calendars/selected", { ids, source: c.source });
    void reload();
  }

  async function disconnect() {
    if (!confirm("Отключить Google Calendar?")) return;
    await post("/api/calendar/disconnect");
    void reload();
    void status.reload();
  }

  return (
    <div className="page">
      <div className="page-head">
        <h1>Календарь</h1>
        {data?.connected && (
          <div className="row">
            <button className="btn small" onClick={openCals}>
              Календари
            </button>
            <button className="btn primary small" onClick={openNew}>
              + Событие
            </button>
          </div>
        )}
      </div>
      {data && !data.connected && (
        <div className="card stack">
          <p>Календари не подключены.</p>
          {status.data && !status.data.google ? (
            <p className="small muted">Google: задайте GOOGLE_CLIENT_ID и GOOGLE_CLIENT_SECRET в .env на сервере.</p>
          ) : (
            <button className="btn primary" onClick={connect}>
              Подключить Google
            </button>
          )}
          <p className="small muted">iCloud (семейный календарь Apple): задайте ICLOUD_USERNAME и ICLOUD_APP_PASSWORD в .env.</p>
        </div>
      )}
      {data?.connected && (
        <>
          {data.errors.length > 0 && (
            <div className="card small" style={{ color: "var(--warn)" }}>
              {data.errors.map((e) => (
                <div key={e}>{e}</div>
              ))}
            </div>
          )}
          <div className="tabs">
            {[1, 3, 7, 14, 30].map((d) => (
              <button key={d} className={days === d ? "active" : ""} onClick={() => setDays(d)}>
                {d === 1 ? "Сегодня" : `${d} дн`}
              </button>
            ))}
          </div>
          <div className="card">
            {loading && !data && <p className="muted">Загрузка…</p>}
            {error && <p style={{ color: "var(--danger)" }}>{error}</p>}
            <EventList events={data.events} />
          </div>
          <div className="row wrap" style={{ marginTop: 8 }}>
            {status.data?.google && !status.data.google_connected && (
              <button className="btn small" onClick={connect}>
                Подключить Google
              </button>
            )}
            {status.data?.google_connected && (
              <button className="btn danger small" onClick={disconnect}>
                Отключить Google
              </button>
            )}
          </div>
        </>
      )}
      {showNew && cals && <NewEventModal cals={cals} onClose={() => setShowNew(false)} onCreated={() => void reload()} />}
      {showCals && cals && (
        <div className="modal-backdrop" onClick={() => setShowCals(false)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <h2>Какие календари показывать</h2>
            <p className="small muted">Google: если ничего не выбрано — основной. iCloud: если ничего не выбрано — все.</p>
            {cals.errors.map((e) => (
              <p key={e} className="small" style={{ color: "var(--warn)" }}>{e}</p>
            ))}
            {(["google", "icloud"] as const).map((src) => {
              const list = cals.calendars.filter((c) => c.source === src);
              if (!list.length) return null;
              return (
                <div key={src}>
                  <h3 style={{ marginTop: 12 }}>{SOURCE_LABEL[src]}</h3>
                  {list.map((c) => (
                    <label key={c.id} className="row" style={{ padding: "8px 0" }}>
                      <input type="checkbox" checked={cals.selected[src].includes(c.id)} onChange={() => toggleCal(c)} />
                      <span style={{ color: c.color ?? undefined }}>●</span> {c.summary} {c.primary && <span className="chip">основной</span>}
                    </label>
                  ))}
                </div>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}
