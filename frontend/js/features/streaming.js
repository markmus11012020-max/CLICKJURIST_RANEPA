/** SSE-стриминг ответов и скелетоны с таймерами. */

import { $, hide, setStatus, show, toast } from '../core/dom.js';
import { applyConsultationSummary, extractAndStripSummary, markdownToHtml } from '../core/markdown.js';
import { API_BASE } from '../core/net.js';
import { state } from '../core/state.js';
import { applyLegalCategory } from '../ui/legal.js';
import { loadSession } from '../ui/session.js';
import { saveCurrent } from './archive.js';

/** SSE-стриминг ответов и скелетоны с таймерами. */

export function showSkeletonAndTimer() {
  const sk = $("querySkeleton"); if (sk) sk.hidden = false;
  const tm = $("queryTimer"); if (tm) tm.hidden = false;
  const body = $("queryResultBody"); if (body) body.textContent = "";
  show($("queryResult"));
}

/** Скрыть скелетон и таймер после завершения генерации. */

export function hideSkeletonAndTimer() {
  const sk = $("querySkeleton"); if (sk) sk.hidden = true;
  const tm = $("queryTimer"); if (tm) tm.hidden = true;
}

/**
 * Универсальный SSE-стример для Шагов 2/3 (ТЗ prompt170926.md).
 * Подписывается на /api/query/stream/{task_id} и рендерит токены
 * в указанный контейнер по мере их поступления.
 *
 * @param {string} taskId — идентификатор фоновой задачи.
 * @param {object} opts — параметры рендеринга:
 *   - skeletonId, timerId, timerTextId, bodyId, resultId — DOM-id элементов;
 *   - statusId — id элемента статуса;
 *   - initialLabel — текст таймера до старта («Готовлю чек-лист…»);
 *   - onCompleted(finalText) — колбэк финализации (markdown-рендер).
 */

export function streamConsultation(taskId, query) {
  return new Promise((resolve) => {
    let sources = [];
    let stage2Provider = "";
    let stage1Provider = "";
    let warning = "";
    let timerHandle = null;
    const startedAt = Date.now();

    /**
     * Стриминг идёт токен за токеном, и служебный блок-сводка ClickJurist
     * (<!--CJ_SUMMARY_START-->...<!--CJ_SUMMARY_END-->) может появиться в DOM
     * кусками. Эта функция вырезает из тела контейнера любые участки текста,
     * попавшие внутрь маркеров, чтобы пользователь не видел сырой JSON.
     */
    const stripStreamedSummary = (bodyEl) => {
      if (!bodyEl) return;
      const txt = bodyEl.textContent || "";
      if (txt.indexOf("<!--CJ_SUMMARY_START-->") === -1) return;
      const fullRe = /<!--CJ_SUMMARY_START-->[\s\S]*?<!--CJ_SUMMARY_END-->/g;
      const partialStartRe = /<!--CJ_SUMMARY_START-->[\s\S]*$/;
      if (fullRe.test(txt)) {
        bodyEl.textContent = txt.replace(fullRe, "");
        return;
      }
      // Маркер начала есть, но END ещё не пришёл — отрезаем всё от START.
      if (partialStartRe.test(txt)) {
        bodyEl.textContent = txt.replace(partialStartRe, "");
      }
    };

    const timerEl = $("queryTimerText");
    if (timerEl) {
      timerEl.textContent = "Идёт правовой анализ… Прошло 0 сек.";
      timerHandle = setInterval(() => {
        const sec = Math.floor((Date.now() - startedAt) / 1000);
        if (timerEl) timerEl.textContent = "Идёт правовой анализ… Прошло " + sec + " сек.";
      }, 1000);
    }
    const stopTimer = () => { if (timerHandle) { clearInterval(timerHandle); timerHandle = null; } };

    /** Прокрутить контейнер результата к самой свежей строке (typewriter-эффект). */
    const scrollToBottom = () => {
      const body = $("queryResultBody");
      if (body && typeof body.scrollHeight === "number") {
        // scrollIntoView на последнем дочернем узле даёт плавную прокрутку
        // именно к свежему тексту, а не к произвольной точке контейнера.
        const last = body.lastChild;
        if (last && typeof last.scrollIntoView === "function") {
          last.scrollIntoView({ block: "end", inline: "nearest" });
        } else {
          body.scrollTop = body.scrollHeight;
        }
      }
    };

    let es;
    try {
      es = new EventSource(API_BASE + "/api/query/stream/" + taskId);
    } catch (e) {
      stopTimer();
      hideSkeletonAndTimer();
      setStatus($("queryStatus"), "Сбой сети", "error");
      toast("Не удалось открыть поток: " + e.message, "error");
      resolve();
      return;
    }

    es.onmessage = (ev) => {
      let payload = null;
      try { payload = JSON.parse(ev.data); } catch (_) { return; }
      if (!payload || !payload.type) return;

      if (payload.type === "started" || payload.type === "progress") return;

      if (payload.type === "token") {
        // Немедленный рендеринг: каждый токен дописывается прямо в DOM
        // без буферизации/накопления — пользователь видит «печатную машинку».
        const chunk = payload.text || "";
        if (!chunk) return;
        const body = $("queryResultBody");
        if (body) {
          // Если в уже накопленном тексте появился (полностью или частично)
          // служебный JSON-блок сводки — вырезаем его, чтобы пользователь
          // не видел сырой JSON до прихода финального completed.
          stripStreamedSummary(body);
          body.appendChild(document.createTextNode(chunk));
          scrollToBottom();
        }
        const sk = $("querySkeleton");
        if (sk && !sk.hidden) sk.hidden = true;
        return;
      }
      if (payload.type === "sources") { sources = payload.sources || []; return; }
      if (payload.type === "meta") {
        stage1Provider = payload.stage1_provider || stage1Provider;
        stage2Provider = payload.stage2_provider || stage2Provider;
        warning = payload.warning || warning;
        return;
      }
      if (payload.type === "completed") {
        stopTimer(); es.close();
        const rawText = (payload.result && payload.result.response) || ($("queryResultBody") ? $("queryResultBody").textContent : "");
        // Извлечь JSON-блок сводки и заполнить карточку над основным текстом.
        const { summary, stripped } = extractAndStripSummary(rawText);
        applyConsultationSummary(summary, "querySummary");
        state.lastAnswer = stripped || rawText;
        const body = $("queryResultBody");
        if (body) body.innerHTML = markdownToHtml(stripped || rawText);
        const list = $("querySourcesList");
        if (list) {
          list.innerHTML = "";
          (sources || []).forEach((src) => {
            const li = document.createElement("li");
            const a = document.createElement("a");
            a.href = src.url; a.textContent = src.title; a.target = "_blank"; a.rel = "noopener noreferrer";
            li.appendChild(a); list.appendChild(li);
          });
        }
        show($("querySources")); if (!(sources || []).length) hide($("querySources"));
        hideSkeletonAndTimer();
        show($("queryResult"));
        // Правовая категория из Шага 1: блокируем нерелевантные шаблоны Шага 3
        // для B2B-кейсов (Жалоба, Отмена судебного приказа).
        const legalCat = (payload.result && payload.result.legal_category) || null;
        state.legalCategory = legalCat;
        applyLegalCategory(legalCat);
        // Локальный архив консультаций (без серверной записи — Zero-Storage).
        saveCurrent();
        setStatus($("queryStatus"), "Готово ✓", "success");
        toast("Консультация получена", "success");
        loadSession();
        if (typeof console !== "undefined" && console.debug) {
          console.debug("[clickjurist] consultation", {
            stage1_provider: stage1Provider || "—",
            stage2_provider: stage2Provider || "—",
            warning: warning || "",
          });
        }
        resolve();
        return;
      }
      if (payload.type === "failed") {
        stopTimer(); es.close();
        hideSkeletonAndTimer();
        setStatus($("queryStatus"), "Ошибка генерации", "error");
        toast(payload.error || "Не удалось получить ответ", "error");
        resolve();
        return;
      }
      if (payload.type === "cancelled") {
        stopTimer(); es.close();
        hideSkeletonAndTimer();
        resolve();
        return;
      }
    };

    es.onerror = () => {
      if (typeof console !== "undefined" && console.debug) {
        console.debug("[clickjurist] SSE reconnecting…");
      }
    };
  });
}

/** Показать скелетон-заглушку и таймер (Шаг 1 ТЗ prompt170926.md). */

export function streamGeneric(taskId, opts) {
  return new Promise((resolve) => {
    const {
      skeletonId, timerId, timerTextId, bodyId, resultId,
      statusId, initialLabel, onCompleted,
    } = opts;

    let timerHandle = null;
    const startedAt = Date.now();

    const timerEl = $(timerTextId);
    if (timerEl) {
      timerEl.textContent = initialLabel + " Прошло 0 сек.";
      timerHandle = setInterval(() => {
        const sec = Math.floor((Date.now() - startedAt) / 1000);
        if (timerEl) timerEl.textContent = initialLabel + " Прошло " + sec + " сек.";
      }, 1000);
    }
    const stopTimer = () => { if (timerHandle) { clearInterval(timerHandle); timerHandle = null; } };

    const hideSkeleton = () => {
      const sk = $(skeletonId); if (sk) sk.hidden = true;
      const tm = $(timerId); if (tm) tm.hidden = true;
    };

    const scrollToBottom = () => {
      const body = $(bodyId);
      if (body && typeof body.scrollHeight === "number") {
        const last = body.lastChild;
        if (last && typeof last.scrollIntoView === "function") {
          last.scrollIntoView({ block: "end", inline: "nearest" });
        } else {
          body.scrollTop = body.scrollHeight;
        }
      }
    };

    let es;
    try {
      es = new EventSource(API_BASE + "/api/query/stream/" + taskId);
    } catch (e) {
      stopTimer();
      hideSkeleton();
      if (statusId) setStatus($(statusId), "Сбой сети", "error");
      toast("Не удалось открыть поток: " + e.message, "error");
      resolve();
      return;
    }

    es.onmessage = (ev) => {
      let payload = null;
      try { payload = JSON.parse(ev.data); } catch (_) { return; }
      if (!payload || !payload.type) return;

      if (payload.type === "started" || payload.type === "progress") return;

      if (payload.type === "token") {
        const chunk = payload.text || "";
        if (!chunk) return;
        const body = $(bodyId);
        if (body) {
          body.appendChild(document.createTextNode(chunk));
          scrollToBottom();
        }
        const sk = $(skeletonId);
        if (sk && !sk.hidden) sk.hidden = true;
        return;
      }

      if (payload.type === "completed") {
        stopTimer(); es.close();
        const result = payload.result || {};
        const finalText = result.checklist || result.document
          || ($(bodyId) ? $(bodyId).textContent : "");
        hideSkeleton();
        show($(resultId));
        if (typeof onCompleted === "function") {
          try { onCompleted(finalText, result); } catch (_) {}
        }
        if (statusId) setStatus($(statusId), "Готово ✓", "success");
        resolve();
        return;
      }
      if (payload.type === "failed") {
        stopTimer(); es.close();
        hideSkeleton();
        if (statusId) setStatus($(statusId), "Ошибка генерации", "error");
        toast(payload.error || "Не удалось получить ответ", "error");
        resolve();
        return;
      }
      if (payload.type === "cancelled") {
        stopTimer(); es.close();
        hideSkeleton();
        resolve();
        return;
      }
    };

    es.onerror = () => {
      if (typeof console !== "undefined" && console.debug) {
        console.debug("[clickjurist] SSE reconnecting…");
      }
    };
  });
}

/** Показать скелетон + таймер для произвольного шага (2 или 3). */

export function showStepSkeletonAndTimer(step) {
  const sk = $(step + "Skeleton"); if (sk) sk.hidden = false;
  const tm = $(step + "Timer"); if (tm) tm.hidden = false;
  const body = $(step + "ResultBody"); if (body) body.textContent = "";
  show($(step + "Result"));
}

/** Скрыть скелетон + таймер для произвольного шага (2 или 3). */

export function hideStepSkeletonAndTimer(step) {
  const sk = $(step + "Skeleton"); if (sk) sk.hidden = true;
  const tm = $(step + "Timer"); if (tm) tm.hidden = true;
}

/** Асинхронная генерация через polling (раздел 2.1 ТЗ prompt160926.md). */