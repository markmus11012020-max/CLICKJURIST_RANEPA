/** Базовые помощники по DOM: поиск элементов, видимость, статусы, тосты. */

export function $(id) { return document.getElementById(id); }

export function show(el) { if (el) el.hidden = false; }

export function hide(el) { if (el) el.hidden = true; }

export function setStatus(el, text, kind) {
  if (!el) return;
  el.textContent = text || "";
  el.classList.remove("error", "success");
  if (kind === "error") el.classList.add("error");
  if (kind === "success") el.classList.add("success");
}

export function toast(message, kind) {
  const el = $("toast");
  if (!el) return;
  el.textContent = message;
  el.classList.remove("error", "success");
  if (kind) el.classList.add(kind);
  el.classList.add("show");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.remove("show"), 3500);
}

/**
 * Открыть окно оплаты. Защита от случайных вызовов из других мест: принимаем
 * только валидный код сервиса из PAYWALL_SERVICES и обязательно payment_url.
 */
