/** Шаг 1: юридическая консультация. */

import { $, hide, setStatus, show, toast } from '../core/dom.js';
import { applyConsultationSummary, extractAndStripSummary, markdownToHtml } from '../core/markdown.js';
import { state } from '../core/state.js';
import { hideSkeletonAndTimer, showSkeletonAndTimer, streamConsultation } from '../features/streaming.js';
import { applyLegalCategory } from '../ui/legal.js';
import { openPaywall } from '../ui/paywall.js';
import { api } from '../core/net.js';
import { loadSession } from '../ui/session.js';
import { saveCurrent } from './archive.js';

/** Шаг 1: юридическая консультация. */

export async function runQuery() {
  const query = ($("queryInput").value || "").trim();
  if (query.length < 3) { toast("Опишите ситуацию подробнее", "error"); return; }
  setStatus($("queryStatus"), "Ставим задачу в очередь…");
  const btn = $("runQueryBtn"); btn.disabled = true;
  state.lastQuery = query; state.lastAnswer = "";
  showSkeletonAndTimer();
  try {
    const { status, data } = await api("POST", "/api/query/async", { query });
    if (status === 202 && data && data.task_id) {
      const taskId = data.task_id;
      setStatus($("queryStatus"), "Изучаю законы...");
      await streamConsultation(taskId, query);
    } else if (status === 402 && data && data.payment_url) {
      hideSkeletonAndTimer();
      setStatus($("queryStatus"), "Требуется оплата", "error");
      openPaywall(data.service || "consultation", data.amount, data.payment_url);
    } else {
      hideSkeletonAndTimer();
      setStatus($("queryStatus"), "Ошибка", "error");
      toast((data && (data.error || data.detail)) || "Не удалось поставить задачу", "error");
    }
  } catch (e) {
    hideSkeletonAndTimer();
    setStatus($("queryStatus"), "Сбой сети", "error");
    toast("Сбой сети: " + e.message, "error");
  } finally {
    btn.disabled = false;
  }
}

/**
 * SSE-стриминг консультации (Шаг 1 ТЗ prompt170926.md).
 * Подписывается на /api/query/stream/{task_id} и рендерит токены
 * в реальном времени по мере их поступления от бэкенда.
 */

export async function runQueryAsync() {
  const query = ($("queryInput").value || "").trim();
  if (query.length < 3) { toast("Опишите ситуацию подробнее", "error"); return; }
  setStatus($("queryStatus"), "Ставим задачу в очередь…");
  const btn = $("runQueryBtn"); btn.disabled = true;
  try {
    // 1. Поставить задачу в очередь.
    const { status, data } = await api("POST", "/api/query/async", { query });
    if (status === 202 && data && data.task_id) {
      const taskId = data.task_id;
      setStatus($("queryStatus"), "Генерация в фоне (этап 1/3)…");
      // 2. Polling статуса каждые 2 секунды.
      const poll = async () => {
        const { status: s, data: d } = await api("GET", "/api/query/status/" + taskId);
        if (!d) return false;
        if (d.status === "completed") {
          renderConsultationResult(query, d.result || {});
          setStatus($("queryStatus"), "Готово ✓", "success");
          toast("Консультация получена", "success");
          loadSession();
          return true;
        }
        if (d.status === "failed") {
          setStatus($("queryStatus"), "Ошибка генерации", "error");
          toast(d.error || "Не удалось получить ответ", "error");
          return true;
        }
        // Обновить прогресс.
        const stage = d.stage || "обработка";
        const progress = d.progress || 0;
        setStatus($("queryStatus"), `Генерация: ${stage} (${progress}%)…`);
        return false;
      };
      // Цикл опроса (макс. 5 минут).
      for (let i = 0; i < 150; i++) {
        const done = await poll();
        if (done) break;
        await new Promise((r) => setTimeout(r, 2000));
      }
    } else if (status === 402 && data && data.payment_url) {
      setStatus($("queryStatus"), "Требуется оплата", "error");
      openPaywall(data.service || "consultation", data.amount, data.payment_url);
    } else {
      setStatus($("queryStatus"), "Ошибка", "error");
      toast((data && (data.error || data.detail)) || "Не удалось поставить задачу", "error");
    }
  } catch (e) {
    setStatus($("queryStatus"), "Сбой сети", "error");
    toast("Сбой сети: " + e.message, "error");
  } finally {
    btn.disabled = false;
  }
}

/** Отобразить результат консультации (общая функция для sync/async). */

export function renderConsultationResult(query, result) {
  if (!result || !result.response) return;
  state.lastQuery = query;
  state.lastAnswer = result.response;
  // Извлечь JSON-блок сводки (если бэкенд его вывел) и отрендерить
  // карточку-сводку над основным текстом. Сам блок из ответа вырезаем.
  const { summary, stripped } = extractAndStripSummary(result.response);
  applyConsultationSummary(summary, "querySummary");
  $("queryResultBody").innerHTML = markdownToHtml(stripped || result.response);
  const list = $("querySourcesList");
  list.innerHTML = "";
  (result.sources || []).forEach((src) => {
    const li = document.createElement("li");
    const a = document.createElement("a");
    a.href = src.url; a.textContent = src.title; a.target = "_blank"; a.rel = "noopener noreferrer";
    li.appendChild(a); list.appendChild(li);
  });
  show($("querySources")); if (!(result.sources || []).length) hide($("querySources"));
  show($("queryResult"));
  // Правовая категория из Шага 1: блокируем нерелевантные шаблоны Шага 3
  // (для B2B — «Жалоба» и «Отмена судебного приказа»).
  const legalCat = result.legal_category || null;
  state.legalCategory = legalCat;
  applyLegalCategory(legalCat);
  // Локальный архив консультаций (без серверной записи — Zero-Storage).
  saveCurrent();
}