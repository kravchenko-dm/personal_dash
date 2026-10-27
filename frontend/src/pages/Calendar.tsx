import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { get, post, put, type CalendarEvent, type Status } from "../lib/api";
import { useFetch } from "../lib/useFetch";
import { useToast } from "../components/Toast";

type EventsResp = { connected: boolean; events: CalendarEvent[] };
type CalsResp = { calendars: { id: string; summary: string; color?: string; primary: boolean }[]; selected: string[] };

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
                {e.location && <div className="small muted">📍 {e.location}</div>}
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

  async function toggleCal(id: string) {
    if (!cals) return;
    const selected = cals.selected.includes(id) ? cals.selected.filter((x) => x !== id) : [...cals.selected, id];
    setCals({ ...cals, selected });
    await put("/api/calendar/calendars/selected", { ids: selected });
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
          <p>Google Calendar не подключён.</p>
          {status.data && !status.data.google ? (
            <p className="small muted">Задайте GOOGLE_CLIENT_ID и GOOGLE_CLIENT_SECRET в .env на сервере.</p>
          ) : (
            <button className="btn primary" onClick={connect}>
              Подключить Google
            </button>
          )}
        </div>
      )}
      {data?.connected && (
        <>
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
          <button className="btn danger small" onClick={disconnect}>
            Отключить Google
          </button>
        </>
      )}
      {showCals && cals && (
        <div className="modal-backdrop" onClick={() => setShowCals(false)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <h2>Какие календари показывать</h2>
            <p className="small muted">Если ничего не выбрано — основной календарь.</p>
            {cals.calendars.map((c) => (
              <label key={c.id} className="row" style={{ padding: "8px 0" }}>
                <input type="checkbox" checked={cals.selected.includes(c.id)} onChange={() => toggleCal(c.id)} />
                <span style={{ color: c.color }}>●</span> {c.summary} {c.primary && <span className="chip">основной</span>}
              </label>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
