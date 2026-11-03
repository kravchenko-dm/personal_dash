export function fmtDate(iso: string | null | undefined, withTime = true): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const opts: Intl.DateTimeFormatOptions = withTime
    ? { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }
    : { day: "2-digit", month: "2-digit" };
  return d.toLocaleString("ru-RU", opts);
}

export function fmtMoney(value: number | null, currency: string): string {
  if (value === null || value === undefined) return "—";
  const n = Math.abs(value) >= 100 ? value.toFixed(0) : value.toFixed(2);
  return `${Number(n).toLocaleString("ru-RU")} ${currency}`.trim();
}

export function relTime(iso: string | null): string {
  if (!iso) return "";
  const diff = (Date.now() - new Date(iso).getTime()) / 1000;
  if (diff < 60) return "только что";
  if (diff < 3600) return `${Math.floor(diff / 60)} мин назад`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} ч назад`;
  return `${Math.floor(diff / 86400)} дн назад`;
}

export function toLocalInput(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export function isOverdue(iso: string | null): boolean {
  return !!iso && new Date(iso).getTime() < Date.now();
}

export const STAGE_LABEL: Record<string, string> = { idea: "идея", dev: "разработка", beta: "бета", prod: "в проде", paused: "на паузе" };
export const FEATURE_COLUMNS: { key: "idea" | "planned" | "in_progress" | "testing" | "done"; label: string }[] = [
  { key: "idea", label: "Идеи" },
  { key: "planned", label: "Запланировано" },
  { key: "in_progress", label: "В работе" },
  { key: "testing", label: "Тестирование" },
  { key: "done", label: "Выпущено" }
];

export function fmtBytes(n: number): string {
  if (n >= 1e12) return (n / 1e12).toFixed(1) + " TB";
  if (n >= 1e9) return (n / 1e9).toFixed(1) + " GB";
  if (n >= 1e6) return (n / 1e6).toFixed(0) + " MB";
  return (n / 1e3).toFixed(0) + " KB";
}

export function fmtUptime(sec: number): string {
  const d = Math.floor(sec / 86400);
  const h = Math.floor((sec % 86400) / 3600);
  return d > 0 ? `${d} д ${h} ч` : `${h} ч ${Math.floor((sec % 3600) / 60)} м`;
}

export const HEALTH_COLOR = { ok: "var(--ok)", warning: "var(--warn)", critical: "var(--danger)" } as const;
export const INCIDENT_KIND: Record<string, string> = {
  http_down: "сайт не отвечает",
  agent_silent: "агент молчит",
  disk_high: "диск заполнен",
  service_down: "сервис упал",
  unit_failed: "юнит failed",
  restart_loop: "цикл перезапусков",
  backup_failed: "бэкап упал",
  backup_overdue: "бэкап просрочен",
  backup_check: "ошибка проверки бэкапа"
};
