import { useCallback, useEffect, useRef, useState } from "react";
import { get } from "./api";

// a PWA resumed from the background keeps the old page mounted: refetch when it comes back
const RESUME_MIN_GAP_MS = 10_000;

export function useFetch<T>(path: string | null, deps: unknown[] = [], opts: { pollMs?: number } = {}) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(!!path);
  const lastLoad = useRef(0);

  const reload = useCallback(async () => {
    if (!path) return;
    lastLoad.current = Date.now();
    setLoading(true);
    try {
      setData(await get<T>(path));
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [path]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    void reload();
  }, [reload, ...deps]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!path) return;
    const onResume = () => {
      if (document.visibilityState === "visible" && Date.now() - lastLoad.current > RESUME_MIN_GAP_MS) void reload();
    };
    document.addEventListener("visibilitychange", onResume);
    window.addEventListener("focus", onResume);
    window.addEventListener("online", onResume);
    const timer = opts.pollMs
      ? window.setInterval(() => document.visibilityState === "visible" && void reload(), opts.pollMs)
      : undefined;
    return () => {
      document.removeEventListener("visibilitychange", onResume);
      window.removeEventListener("focus", onResume);
      window.removeEventListener("online", onResume);
      if (timer) window.clearInterval(timer);
    };
  }, [path, reload, opts.pollMs]);

  return { data, error, loading, reload, setData };
}
