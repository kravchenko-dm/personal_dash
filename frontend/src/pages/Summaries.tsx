import { useState } from "react";
import { del, patch, post, put, type Project, type Summary, type TelegramChat } from "../lib/api";
import { useFetch } from "../lib/useFetch";
import { fmtDate } from "../lib/format";
import { useToast } from "../components/Toast";

export default function Summaries() {
  const summaries = useFetch<Summary[]>("/api/summaries");
  const chats = useFetch<TelegramChat[]>("/api/telegram/chats");
  const projects = useFetch<Project[]>("/api/projects");
  const [chatId, setChatId] = useState<string>("");
  const [hours, setHours] = useState(24);
  const [busy, setBusy] = useState(false);
  const [accepted, setAccepted] = useState<Set<string>>(new Set());
  const toast = useToast();

  async function make() {
    setBusy(true);
    try {
      const q = new URLSearchParams({ hours: String(hours) });
      if (chatId) q.set("chat_id", chatId);
      const s = await post<Summary | null>(`/api/summaries?${q}`);
      if (!s) toast("За этот период сообщений нет");
      else summaries.setData((prev) => [s, ...(prev ?? [])]);
    } catch (e) {
      toast((e as Error).message, true);
    } finally {
      setBusy(false);
    }
  }

  async function accept(s: Summary, i: number) {
    try {
      await post(`/api/summaries/${s.id}/accept/${i}`);
      setAccepted((prev) => new Set(prev).add(`${s.id}:${i}`));
      toast("Задача добавлена");
    } catch (e) {
      toast((e as Error).message, true);
    }
  }

  async function acceptFeature(s: Summary, i: number) {
    try {
      await post(`/api/summaries/${s.id}/accept-feature/${i}`);
      setAccepted((prev) => new Set(prev).add(`${s.id}:${i}`));
      toast("Добавлено на доску фич");
    } catch (e) {
      toast((e as Error).message, true);
    }
  }

  async function bindChat(c: TelegramChat, projectId: string) {
    const updated = projectId
      ? await put<TelegramChat>(`/api/projects/${projectId}/chats/${c.id}`, {})
      : c.project_id
        ? await (async () => {
            await del(`/api/projects/${c.project_id}/chats/${c.id}`);
            return { ...c, project_id: null };
          })()
        : c;
    chats.setData((prev) => (prev ?? []).map((x) => (x.id === c.id ? updated : x)));
  }

  async function toggleChat(c: TelegramChat) {
    const updated = await patch<TelegramChat>(`/api/telegram/chats/${c.id}?collect=${!c.collect}`, {});
    chats.setData((prev) => (prev ?? []).map((x) => (x.id === c.id ? updated : x)));
  }

  return (
    <div className="page">
      <div className="page-head">
        <h1>Саммари</h1>
      </div>
      <div className="card">
        <div className="row wrap">
          <select className="input" style={{ flex: 2, minWidth: 160 }} value={chatId} onChange={(e) => setChatId(e.target.value)}>
            <option value="">Все чаты</option>
            {(chats.data ?? []).map((c) => (
              <option key={c.id} value={c.chat_id}>
                {c.title}
              </option>
            ))}
          </select>
          <select className="input" style={{ flex: 1, minWidth: 100 }} value={hours} onChange={(e) => setHours(Number(e.target.value))}>
            {[6, 12, 24, 48, 168].map((h) => (
              <option key={h} value={h}>
                {h} ч
              </option>
            ))}
          </select>
          <button className="btn primary" onClick={make} disabled={busy}>
            {busy ? "Думаю…" : "Сделать саммари"}
          </button>
        </div>
      </div>
      {chats.data && chats.data.length > 0 && (
        <div className="card">
          <h3>Чаты бота</h3>
          {chats.data.map((c) => (
            <div key={c.id} className="row" style={{ padding: "6px 0" }}>
              <input type="checkbox" checked={c.collect} onChange={() => toggleChat(c)} />
              <span style={{ flex: 1 }}>
                {c.title} <span className="chip">{c.kind}</span>
              </span>
              <select className="input" style={{ width: 150, minHeight: 32, padding: "4px 8px" }} value={c.project_id ?? ""} onChange={(e) => bindChat(c, e.target.value)}>
                <option value="">— проект —</option>
                {(projects.data ?? []).map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name}
                  </option>
                ))}
              </select>
            </div>
          ))}
        </div>
      )}
      {chats.data && chats.data.length === 0 && (
        <div className="card muted small">Бот ещё не видел ни одного чата. Напишите ему /start или добавьте в группу.</div>
      )}
      {(summaries.data ?? []).map((s) => (
        <div key={s.id} className="card">
          <div className="small muted">
            {fmtDate(s.period_start)} — {fmtDate(s.period_end)}
            {s.chat_id && ` · ${chats.data?.find((c) => c.chat_id === s.chat_id)?.title ?? s.chat_id}`}
            {s.project_id && ` · 🚀 ${projects.data?.find((p) => p.id === s.project_id)?.name ?? ""}`}
          </div>
          <div className="summary-text" style={{ marginTop: 6 }}>{s.text}</div>
          {s.proposed_tasks.length > 0 && (
            <div style={{ marginTop: 10 }}>
              <h3>Предложенные задачи</h3>
              {s.proposed_tasks.map((p, i) => (
                <div className="proposal" key={i}>
                  <div>
                    <div>{p.title}</div>
                    <div className="small muted">
                      {p.reason} {p.chat && `· ${p.chat}`}
                    </div>
                  </div>
                  <div className="row" style={{ gap: 6 }}>
                    <button className="btn small" disabled={accepted.has(`${s.id}:${i}`)} onClick={() => accept(s, i)}>
                      {accepted.has(`${s.id}:${i}`) ? "✓" : "+ задача"}
                    </button>
                    {s.project_id && (
                      <button className="btn small" disabled={accepted.has(`${s.id}:${i}`)} onClick={() => acceptFeature(s, i)}>
                        📋 на доску
                      </button>
                    )}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}
