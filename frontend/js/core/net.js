/** Сетевой слой: отпечаток браузера и единая обёртка над fetch. */

export const API_BASE = window.location.origin;

export const LS_FP = "clickjurist_fp";

/** Лёгкий стабильный отпечаток браузера (без cookies). */

export function fingerprint() {
  const cached = localStorage.getItem(LS_FP);
  if (cached) return cached;
  const data = [
    navigator.userAgent,
    navigator.language,
    screen.width + "x" + screen.height + "@" + (window.devicePixelRatio || 1),
    new Date().getTimezoneOffset(),
    (navigator.platform || ""),
  ].join("|");
  let hash = 5381;
  for (let i = 0; i < data.length; i++) hash = ((hash << 5) + hash) + data.charCodeAt(i);
  const value = ("fp_" + (hash >>> 0).toString(16));
  try { localStorage.setItem(LS_FP, value); } catch (_) {}
  return value;
}

export const headers = {
  "Content-Type": "application/json",
  "X-Client-Fingerprint": fingerprint(),
};

export async function api(method, path, body) {
  const init = { method, headers: { ...headers }, credentials: "include" };
  if (body !== undefined) init.body = JSON.stringify(body);
  const resp = await fetch(API_BASE + path, init);
  let data = null;
  const text = await resp.text();
  try { data = text ? JSON.parse(text) : null; } catch (_) { data = { raw: text }; }
  return { status: resp.status, ok: resp.ok, data, headers: resp.headers };
}

/** Шаблоны быстрых пресетов (Шаг 2 ТЗ prompt170926.md: 6 тегов, 50/50 B2C/B2B). */
