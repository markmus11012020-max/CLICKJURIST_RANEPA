/** Шаг 3: шаблон документа. */

import { $, setStatus, toast } from '../core/dom.js';
import { markdownToHtml } from '../core/markdown.js';
import { state, stateDocType } from '../core/state.js';
import { hideStepSkeletonAndTimer, showStepSkeletonAndTimer, streamGeneric } from '../features/streaming.js';
import { openPaywall } from '../ui/paywall.js';
import { api } from '../core/net.js';
import { loadSession } from '../ui/session.js';
import { runQuery } from './query.js';

/** Шаг 3: шаблон документа. */

export async function runDocument() {
  const queryInput = document.getElementById("queryInput");
  // Scenario A: пустое поле ввода — блокируем запуск и подсказываем, что делать.
  if (!queryInput || !queryInput.value.trim()) {
    toast("Для корректной работы сервиса необходимо сначала заполнить поле описания ситуации или перейти к шагу «Получить консультацию».", "error");
    return;
  }
  // Scenario B: текст есть, но Шаг 1 ещё не выполнен — без lastAnswer
  // документ не имеет смысла. Вместо блокировки автоматически запускаем
  // консультацию (она нужна для документа в любом случае) и продолжаем
  // генерацию документа сразу после её завершения. Пользователь получает
  // обе услуги одним кликом, без необходимости отдельно жать
  // «Получить консультацию» (это требовало от него понимать неявную
  // зависимость Шага 3 от Шага 1 — что приводило к багам вроде
  // «кнопка отмены судебного приказа не работает»).
  if (!state.lastAnswer || !state.lastAnswer.trim()) {
    // Показываем понятный статус рядом с кнопкой Шага 3 — пользователь
    // видит, почему генерация пока не началась.
    setStatus($("documentStatus"), "Сначала получаю консультацию…");
    await runQuery();
    if (!state.lastAnswer || !state.lastAnswer.trim()) {
      // runQuery() вернулся без lastAnswer: либо открылось окно оплаты
      // (402), либо ошибка сети/генерации — причина уже показана
      // пользователю через toast внутри runQuery(). Здесь лишь
      // аккуратно закрываем скелетон/таймер Шага 3 и переключаем
      // статус, чтобы карточка не висела в «Ставим задачу в очередь…».
      hideStepSkeletonAndTimer("document");
      setStatus($("documentStatus"), "Требуется консультация", "error");
      return;
    }
    // Консультация успешно получена — прокручиваем к Шагу 1, чтобы
    // пользователь увидел результат перед тем, как продолжится генерация.
    const consultSection = $("consult");
    if (consultSection && typeof consultSection.scrollIntoView === "function") {
      consultSection.scrollIntoView({ behavior: "smooth", block: "start" });
    }
    // Небольшая пауза, чтобы тип-эффект консультации успел зафиксироваться
    // визуально перед запуском генерации документа.
    await new Promise((r) => setTimeout(r, 250));
    // Продолжаем как обычная генерация документа (выходим из if и идём
    // к основному потоку ниже).
  }
  const query = queryInput.value.trim();
  // doc_type приходит ИЗ АКТИВНОЙ КНОПКИ в блоке «Шаг 3. Шаблон документа».
  // Кнопки идут слева направо: complaint → claim → lawsuit → court_order_cancellation.
  // Если на предыдущем шаге Шаг 1 определил категорию B2B, applyLegalCategory
  // уже мог переключить stateDocType.value на первый доступный шаблон —
  // мы автоматически подхватываем этот переключённый тип.
  const docType = stateDocType.value;
  const btn = $("runDocumentBtn");
  // 1. Блокируем кнопку и показываем аккуратный анимированный спиннер внутри неё —
  //    это надёжный индикатор загрузки на любых устройствах, в т.ч. на медленном канале.
  btn.disabled = true;
  btn.classList.add("is-loading");
  btn.dataset.originalLabel = btn.dataset.originalLabel || btn.textContent;
  btn.innerHTML = '<span class="spinner" aria-hidden="true"></span>' + btn.dataset.originalLabel;
  // 2. Солидный статус рядом с кнопкой + скелетон + таймер (Шаг 3 ТЗ prompt170926.md).
  setStatus($("documentStatus"), "Ставим задачу в очередь…");
  showStepSkeletonAndTimer("document");
  try {
    const { status, data } = await api("POST", "/api/document/async", { query, doc_type: docType });
    if (status === 202 && data && data.task_id) {
      const taskId = data.task_id;
      setStatus($("documentStatus"), "Пишу документ...");
      await streamGeneric(taskId, {
        skeletonId: "documentSkeleton",
        timerId: "documentTimer",
        timerTextId: "documentTimerText",
        bodyId: "documentResultBody",
        resultId: "documentResult",
        statusId: "documentStatus",
        initialLabel: "Формирую документ…",
        onCompleted: (finalText) => {
          $("documentResultBody").innerHTML = markdownToHtml(finalText);
          toast("Документ сформирован", "success");
          loadSession();
        },
      });
    } else if (status === 402 && data && data.payment_url) {
      hideStepSkeletonAndTimer("document");
      setStatus($("documentStatus"), "Требуется оплата", "error");
      openPaywall(data.service || "document", data.amount, data.payment_url);
    } else {
      hideStepSkeletonAndTimer("document");
      setStatus($("documentStatus"), "Ошибка", "error");
      toast((data && (data.error || data.detail)) || "Не удалось сформировать документ", "error");
    }
  } catch (e) {
    hideStepSkeletonAndTimer("document");
    setStatus($("documentStatus"), "Сбой сети", "error");
    toast("Сбой сети: " + e.message, "error");
  } finally {
    // 3. Снимаем блокировку и спиннер, восстанавливаем оригинальную подпись.
    btn.disabled = false;
    btn.classList.remove("is-loading");
    if (btn.dataset.originalLabel) {
      btn.textContent = btn.dataset.originalLabel;
    }
  }
}