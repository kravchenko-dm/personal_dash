import { useEffect, useState } from "react";
import { NavLink, Navigate, Route, Routes } from "react-router-dom";
import { getToken, setToken } from "./lib/api";
import { ToastProvider } from "./components/Toast";
import Login from "./pages/Login";
import Home from "./pages/Home";
import Tasks from "./pages/Tasks";
import Balances from "./pages/Balances";
import Calendar from "./pages/Calendar";
import YouGile from "./pages/YouGile";
import Summaries from "./pages/Summaries";
import Settings from "./pages/Settings";

const NAV = [
  { to: "/", ic: "🏠", label: "Главная" },
  { to: "/tasks", ic: "✅", label: "Задачи" },
  { to: "/balances", ic: "💳", label: "Балансы" },
  { to: "/calendar", ic: "📅", label: "Календарь" },
  { to: "/yougile", ic: "🗂", label: "YouGile" },
  { to: "/settings", ic: "⚙️", label: "Ещё" }
];

export default function App() {
  const [authed, setAuthed] = useState(!!getToken());

  useEffect(() => {
    const onLogout = () => setAuthed(false);
    window.addEventListener("dash:logout", onLogout);
    return () => window.removeEventListener("dash:logout", onLogout);
  }, []);

  if (!authed) return <Login onLogin={() => setAuthed(true)} />;

  return (
    <ToastProvider>
      <div className="app">
        <Routes>
          <Route path="/" element={<Home />} />
          <Route path="/tasks" element={<Tasks />} />
          <Route path="/balances" element={<Balances />} />
          <Route path="/calendar" element={<Calendar />} />
          <Route path="/yougile" element={<YouGile />} />
          <Route path="/summaries" element={<Summaries />} />
          <Route
            path="/settings"
            element={
              <Settings
                onLogout={() => {
                  setToken(null);
                  setAuthed(false);
                }}
              />
            }
          />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
        <nav className="nav">
          {NAV.map((n) => (
            <NavLink key={n.to} to={n.to} end={n.to === "/"} className={({ isActive }) => (isActive ? "active" : "")}>
              <span className="ic">{n.ic}</span>
              {n.label}
            </NavLink>
          ))}
        </nav>
      </div>
    </ToastProvider>
  );
}
