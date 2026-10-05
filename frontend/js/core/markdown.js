/** Рендерер Markdown в HTML (экранирование + блочная разметка + ссылки). */

import { $ } from '../core/dom.js';

/** Рендерер Markdown в HTML (экранирование + блочная разметка + ссылки). */

/**
 * Разрешённые схемы URL для ссылок. Всё, что не попало в этот список,
 * молча выкидывается вместе со скобками — опасные `javascript:`, `data:`
 * (кроме картинок) и `vbscript:` не должны проникнуть в DOM.
 * @param {string} url
 * @returns {string|null} нормализованный URL или null, если ссылка отброшена
 */
function sanitizeUrl(url) {
  if (!url) return null;
  const trimmed = url.trim();
  if (!trimmed) return null;
  // Относительные пути и якоря — безопасны по построению.
  if (
    trimmed.startsWith("/") ||
    trimmed.startsWith("#") ||
    trimmed.startsWith("./") ||
    trimmed.startsWith("../")
  ) {
    return trimmed;
  }
  // Полные http(s), mailto, tel.
  if (/^(https?|mailto|tel):/i.test(trimmed)) return trimmed;
  // Всё остальное (javascript:, data:text/html, vbscript:, ftp:, file:, …)
  // считаем небезопасным и не встраиваем.
  return null;
}

/** Нулевой байт не встречается в пользовательском вводе и не экранируется
 *  нашими escape-функциями — безопасно использовать как маркер плейсхолдера. */
const LINK_TOKEN = "\u0000CJL";

export function markdownToHtml(text) {
  if (!text) return "";

  // ---------- 1. Извлечь [text](url) в плейсхолдеры -----------------------
  // Текст ссылки экранируется СРАЗУ, до любого инлайн-форматирования: иначе
  // содержимое вида `<script>` внутри `[…]` проникло бы в DOM как HTML
  // (XSS). URL проходит через sanitizeUrl — javascript:/data: отбрасываются.
  // Инлайн-разметка (`**bold**`, `code`) внутри ссылки применяется к
  // уже экранированному тексту — `<strong>`/`code` вставляются как есть.
  const escape = (s) =>
    s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const links = [];
  const linkRe = /\[([^\]]+)\]\(([^)]+)\)/g;
  const workingText = text.replace(linkRe, (match, linkText, url) => {
    const safeUrl = sanitizeUrl(url);
    if (!safeUrl) return linkText; // опасная/пустая ссылка → просто текст
    const idx = links.length;
    const safeText = escape(linkText)
      .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
      .replace(/`([^`]+)`/g, "<code>$1</code>");
    links.push({ text: safeText, url: safeUrl });
    return LINK_TOKEN + idx + LINK_TOKEN;
  });

  // ---------- 2. Блочная разметка ----------------------------------------
  const lines = escape(workingText).split("\n");
  const bulletRe = /^[\*•]\s+(.+)$/;
  const h3Re = /^###\s+(.+)$/;
  const h2Re = /^##\s+(.+)$/;
  const h1Re = /^#\s+(.+)$/;

  const blocks = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (bulletRe.test(line)) {
      const items = [];
      while (i < lines.length && bulletRe.test(lines[i])) {
        items.push("<li>" + lines[i].replace(bulletRe, "$1") + "</li>");
        i++;
      }
      blocks.push("<ul>" + items.join("") + "</ul>");
      continue;
    }
    const h3 = line.match(h3Re);
    if (h3) { blocks.push("<h3>" + h3[1] + "</h3>"); i++; continue; }
    const h2 = line.match(h2Re);
    if (h2) { blocks.push("<h2>" + h2[1] + "</h2>"); i++; continue; }
    const h1 = line.match(h1Re);
    if (h1) { blocks.push("<h1>" + h1[1] + "</h1>"); i++; continue; }
    if (line.trim() === "") { i++; continue; }
    // Обычный текстовый блок: собираем подряд идущие непустые строки,
    // не начинающиеся с маркера списка/заголовка. Одиночные \n → <br>.
    const paraLines = [line];
    i++;
    while (
      i < lines.length &&
      lines[i].trim() !== "" &&
      !bulletRe.test(lines[i]) &&
      !h3Re.test(lines[i]) &&
      !h2Re.test(lines[i]) &&
      !h1Re.test(lines[i])
    ) {
      paraLines.push(lines[i]);
      i++;
    }
    blocks.push("<p>" + paraLines.join("<br>") + "</p>");
  }

  let html = blocks.join("");
  // ---------- 3. Инлайн-форматирование -----------------------------------
  // Применяется к блочному HTML; плейсхолдеры ссылок (LINK_TOKEN) не
  // содержат `**`/«`», поэтому форматирование в них не заглядывает.
  html = html.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  html = html.replace(/`([^`]+)`/g, "<code>$1</code>");

  // ---------- 4. Подставить ссылки ---------------------------------------
  // Только после escape и инлайн-разметки: текст и атрибут href уже
  // экранированы один раз, дальнейшая обработка им не нужна.
  const linkReFinal = new RegExp(LINK_TOKEN + "(\\d+)" + LINK_TOKEN, "g");
  html = html.replace(linkReFinal, (_, idxStr) => {
    const idx = Number(idxStr);
    const link = links[idx];
    if (!link) return "";
    const isExternal = /^https?:\/\//i.test(link.url);
    const target = isExternal ? ' target="_blank" rel="noopener noreferrer"' : "";
    return (
      '<a href="' + escape(link.url) + '"' + target + ">" + link.text + "</a>"
    );
  });

  return html;
}

/**
 * Извлечь блок-сводку ClickJurist из ответа LLM.
 * Бэкенд в самом начале ответа выводит:
 *   <!--CJ_SUMMARY_START-->{"category":...}<!--CJ_SUMMARY_END-->
 * Возвращает { summary, stripped } — summary=null если блок не найден.
 * `stripped` — текст без JSON-блока, готов для markdownToHtml.
 */
export function extractAndStripSummary(text) {
  if (!text) return { summary: null, stripped: "" };
  const re = /<!--CJ_SUMMARY_START-->([\s\S]*?)<!--CJ_SUMMARY_END-->/;
  const match = text.match(re);
  if (!match) return { summary: null, stripped: text };
  let summary = null;
  const raw = (match[1] || "").trim();
  if (raw) {
    try {
      summary = JSON.parse(raw);
      if (typeof summary !== "object" || summary === null) summary = null;
    } catch (_) {
      summary = null;
    }
  }
  const stripped = text.replace(re, "").trimStart();
  return { summary, stripped };
}

/**
 * Заполнить карточку-сводку по id контейнера данными из JSON-блока.
 * Контейнер должен содержать элементы с data-summary="category|norms|deadline|risk".
 * Если summary=null или контейнер не найден — скрываем карточку.
 */
export function applyConsultationSummary(summary, containerId) {
  const box = document.getElementById(containerId);
  if (!box) return;
  if (!summary || typeof summary !== "object") { box.hidden = true; return; }

  const setVal = (key, text) => {
    const el = box.querySelector(`[data-summary="${key}"]`);
    if (!el) return;
    el.textContent = text;
    el.classList.remove("is-empty");
  };

  setVal("category", summary.category || "—");
  let normsText = "—";
  if (Array.isArray(summary.norms) && summary.norms.length) {
    normsText = summary.norms.join(", ");
  }
  setVal("norms", normsText);

  let deadlineText = "—";
  if (summary.deadline_days && Number(summary.deadline_days) > 0) {
    const days = Math.round(Number(summary.deadline_days));
    const label = summary.deadline_label ? ` — ${summary.deadline_label}` : "";
    deadlineText = `${days} дн.${label}`;
  } else if (summary.deadline_label && summary.deadline_label !== "нет") {
    deadlineText = summary.deadline_label;
  }
  setVal("deadline", deadlineText);

  const riskEl = box.querySelector('[data-summary="risk"]');
  if (riskEl) {
    const risk = (summary.risk_level || "").toString().toLowerCase();
    riskEl.classList.remove("risk-low", "risk-medium", "risk-high");
    let riskLabel = "—";
    if (risk === "низкий") { riskLabel = "Низкий"; riskEl.classList.add("risk-low"); }
    else if (risk === "средний") { riskLabel = "Средний"; riskEl.classList.add("risk-medium"); }
    else if (risk === "высокий") { riskLabel = "Высокий"; riskEl.classList.add("risk-high"); }
    riskEl.textContent = riskLabel;
    riskEl.classList.remove("is-empty");
  }
  box.hidden = false;
}

// Начальное состояние: чип сразу показывает «Бесплатно: 1 из 1», пока бэкенд
// ещё не ответил на /api/session. Никакого триггера paywall здесь нет.
