import { useEffect, useState } from "react";
import { NavLink, Navigate, Route, Routes } from "react-router-dom";
import { clearApiCache, get, getToken, setToken } from "./lib/api";
import { MeContext, type Me } from "./lib/me";
import { ToastProvider } from "./components/Toast";
import Login from "./pages/Login";
import Home from "./pages/Home";
import Tasks from "./pages/Tasks";
import Balances from "./pages/Balances";
import Calendar from "./pages/Calendar";
import YouGile from "./pages/YouGile";
import Summaries from "./pages/Summaries";
import Settings from "./pages/Settings";
import Projects from "./pages/Projects";
import ProjectPage from "./pages/ProjectPage";
import BackupSettings from "./pages/BackupSettings";
import Collector from "./pages/Collector";

const MEMBER_NAV = [
  { to: "/", ic: "🏠", label: "Главная" },
  { to: "/tasks", ic: "✅", label: "Задачи" },
  { to: "/summaries", ic: "📝", label: "Саммари" },
  { to: "/settings", ic: "⚙️", label: "Ещё" }
];

const NAV = [
  { to: "/", ic: "🏠", label: "Главная" },
  { to: "/tasks", ic: "✅", label: "Задачи" },
  { to: "/balances", ic: "💳", label: "Балансы" },
  { to: "/projects", ic: "🚀", label: "Проекты" },
  { to: "/calendar", ic: "📅", label: "Календарь" },
  { to: "/settings", ic: "⚙️", label: "Ещё" }
];

export default function App() {
  const [authed, setAuthed] = useState(!!getToken() && !new URLSearchParams(window.location.search).get("code"));
  const [me, setMe] = useState<Me | null>(null);

  useEffect(() => {
    const onLogout = () => {
      setAuthed(false);
      setMe(null);
      void clearApiCache();
    };
    window.addEventListener("dash:logout", onLogout);
    return () => window.removeEventListener("dash:logout", onLogout);
  }, []);

  useEffect(() => {
    if (!authed) return;
    get<Me>("/api/auth/me")
      .then(setMe)
      .catch(() => undefined); // a 401 fires dash:logout
  }, [authed]);

  if (!authed) return <Login onLogin={() => setAuthed(true)} />;
  if (!me) return <div className="login"><p className="muted">…</p></div>;

  const owner = me.role === "owner";
  const nav = owner ? NAV : MEMBER_NAV;

  return (
    <MeContext.Provider value={me}>
    <ToastProvider>
      <div className="app">
        <Routes>
          <Route path="/" element={<Home />} />
          <Route path="/tasks" element={<Tasks />} />
          {owner && (
            <>
              <Route path="/balances" element={<Balances />} />
              <Route path="/calendar" element={<Calendar />} />
              <Route path="/yougile" element={<YouGile />} />
              <Route path="/projects" element={<Projects />} />
              <Route path="/projects/:id" element={<ProjectPage />} />
              <Route path="/settings/backups" element={<BackupSettings />} />
            </>
          )}
          <Route path="/summaries" element={<Summaries />} />
          <Route path="/collector" element={<Collector />} />
          <Route
            path="/settings"
            element={
              <Settings
                onLogout={() => {
                  setToken(null);
                  setAuthed(false);
                  setMe(null);
                  void clearApiCache();
                }}
                onMe={setMe}
              />
            }
          />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
        <nav className="nav">
          {nav.map((n) => (
            <NavLink key={n.to} to={n.to} end={n.to === "/"} className={({ isActive }) => (isActive ? "active" : "")}>
              <span className="ic">{n.ic}</span>
              {n.label}
            </NavLink>
          ))}
        </nav>
      </div>
    </ToastProvider>
    </MeContext.Provider>
  );
}
