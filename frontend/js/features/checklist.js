/** Шаг 2: чек-лист действий. */

import { $, setStatus, toast } from '../core/dom.js';
import { markdownToHtml } from '../core/markdown.js';
import { state } from '../core/state.js';
import { hideStepSkeletonAndTimer, showStepSkeletonAndTimer, streamGeneric } from '../features/streaming.js';
import { openPaywall } from '../ui/paywall.js';
import { api } from '../core/net.js';
import { loadSession } from '../ui/session.js';

/** Шаг 2: чек-лист действий. */

export async function runChecklist() {
  const queryInput = document.getElementById("queryInput");
  // Scenario A: пустое поле ввода — блокируем запуск и подсказываем, что делать.
  if (!queryInput || !queryInput.value.trim()) {
    toast("Для корректной работы сервиса необходимо сначала заполнить поле описания ситуации или перейти к шагу «Получить консультацию».", "error");
    return;
  }
  // Scenario B: текст есть, но Шаг 1 ещё не выполнен — без lastAnswer чек-лист
  // не имеет смысла, поэтому останавливаем выполнение и просим сначала
  // получить консультацию.
  if (!state.lastAnswer || !state.lastAnswer.trim()) {
    toast("Для корректной работы сервиса необходимо сначала перейти к шагу «Получить консультацию».", "error");
    return;
  }
  const query = queryInput.value.trim();
  setStatus($("checklistStatus"), "Ставим задачу в очередь…");
  const btn = $("runChecklistBtn"); btn.disabled = true;
  showStepSkeletonAndTimer("checklist");
  try {
    const { status, data } = await api("POST", "/api/checklist/async", {
      query: state.lastQuery, final_answer: state.lastAnswer,
    });
    if (status === 202 && data && data.task_id) {
      const taskId = data.task_id;
      setStatus($("checklistStatus"), "Составляю план...");
      await streamGeneric(taskId, {
        skeletonId: "checklistSkeleton",
        timerId: "checklistTimer",
        timerTextId: "checklistTimerText",
        bodyId: "checklistResultBody",
        resultId: "checklistResult",
        statusId: "checklistStatus",
        initialLabel: "Готовлю чек-лист…",
        onCompleted: (finalText) => {
          $("checklistResultBody").innerHTML = markdownToHtml(finalText);
          toast("Чек-лист готов", "success");
          loadSession();
        },
      });
    } else if (status === 402 && data && data.payment_url) {
      hideStepSkeletonAndTimer("checklist");
      setStatus($("checklistStatus"), "Требуется оплата", "error");
      openPaywall(data.service || "checklist", data.amount, data.payment_url);
    } else {
      hideStepSkeletonAndTimer("checklist");
      setStatus($("checklistStatus"), "Ошибка", "error");
      toast((data && (data.error || data.detail)) || "Не удалось построить чек-лист", "error");
    }
  } catch (e) {
    hideStepSkeletonAndTimer("checklist");
    setStatus($("checklistStatus"), "Сбой сети", "error");
    toast("Сбой сети: " + e.message, "error");
  } finally {
    btn.disabled = false;
  }
}