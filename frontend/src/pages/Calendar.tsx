import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { get, post, put, type CalendarEvent, type Status } from "../lib/api";
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

export default function Calendar() {
  const [days, setDays] = useState(7);
  const status = useFetch<Status>("/api/status");
  const { data, error, loading, reload } = useFetch<EventsResp>(`/api/calendar/events?days=${days}`, [days]);
  const [cals, setCals] = useState<CalsResp | null>(null);
  const [showCals, setShowCals] = useState(false);
  const [params] = useSearchParams();
  const toast = useToast();

  useEffect(() => {
    if (params.get("connected")) toast("Google Calendar подключён");
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
          <button className="btn small" onClick={openCals}>
            Календари
          </button>
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
