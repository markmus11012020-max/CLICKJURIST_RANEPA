/** Выгрузка результата в PDF. */

import { $, setStatus, toast } from '../core/dom.js';
import { API_BASE, headers } from '../core/net.js';
import { state } from '../core/state.js';
import { openPaywall } from '../ui/paywall.js';
import { loadSession } from '../ui/session.js';

/** Выгрузка результата в PDF. */

export async function runPdf() {
  // 1. Сначала показываем статус «Формирую PDF…» ДО отправки запроса.
  setStatus($("documentStatus"), "Формирую PDF…");
  const btn = $("runPdfBtn");
  btn.disabled = true;
  btn.classList.add("is-loading");
  if (!btn.dataset.originalLabel) btn.dataset.originalLabel = btn.textContent;
  btn.textContent = "Формирую PDF…";

  try {
    // 2. Берём текст СТРОГО из Шага 3 (#documentResultBody) — это готовый
    //    шаблон документа. НЕ используем state.lastAnswer / #queryResultBody
    //    (это текст Шага 1 — юридическая консультация, а не документ).
    const docBody = $("documentResultBody");
    const documentText = docBody ? (docBody.innerText || docBody.textContent || "").trim() : "";
    if (!documentText) {
      toast("Сначала сформируйте документ в Шаге 3, чтобы скачать его в PDF.", "error");
      setStatus($("documentStatus"), "Ошибка", "error");
      return;
    }

    // 3. Запрос к /api/pdf напрямую через fetch — ответ приходит как
    //    бинарный поток application/pdf, его НЕЛЬЗЯ парсить как JSON.
    const response = await fetch(API_BASE + "/api/pdf", {
      method: "POST",
      headers: { ...headers },
      credentials: "include",
      body: JSON.stringify({
        query: state.lastQuery,
        final_answer: documentText,
      }),
    });

    // 3. Обработка 402 (paywall) — сервер возвращает JSON с payment_url.
    if (response.status === 402) {
      let paywall = null;
      try {
        paywall = await response.json();
      } catch (_) {
        paywall = null;
      }
      if (paywall && paywall.payment_url) {
        openPaywall(paywall.service || "pdf", paywall.amount, paywall.payment_url);
      } else {
        toast("Не удалось получить ссылку на оплату", "error");
      }
      return;
    }

    // 4. Любой неуспешный статус — пробуем прочитать JSON с ошибкой.
    if (!response.ok) {
      let errPayload = null;
      try {
        errPayload = await response.json();
      } catch (_) {
        errPayload = null;
      }
      toast((errPayload && errPayload.error) || `Ошибка ${response.status}`, "error");
      return;
    }

    // 5. Успешный HTTP 200 — получаем бинарный blob и инициируем скачивание.
    const blob = await response.blob();
    const url = window.URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "Документ.pdf";
    document.body.appendChild(a);
    a.click();
    window.URL.revokeObjectURL(url);
    a.remove();

    // 6. ТОЛЬКО после успешного скачивания показываем «PDF готов».
    toast("PDF готов", "success");
    setStatus($("documentStatus"), "PDF готов");
    loadSession();
  } catch (e) {
    toast("Сбой при формировании PDF: " + (e && e.message ? e.message : e), "error");
    setStatus($("documentStatus"), "Ошибка", "error");
  } finally {
    // 7. Снимаем блокировку и спиннер, восстанавливаем оригинальную подпись.
    btn.disabled = false;
    btn.classList.remove("is-loading");
    if (btn.dataset.originalLabel) {
      btn.textContent = btn.dataset.originalLabel;
    }
  }
}

/**
 * Пакетный запуск «Всё включено» (Шаг 4 ТЗ prompt170926.md).
 *
 * При нажатии на кнопку `#runPackageBtn` запускает ПОЛНУЮ последовательную
 * цепочку генерации прямо с фронтенда — БЕЗ предварительного ручного
 * прохождения Шага 1:
 *
 *   1) runQuery()     — юридическая консультация (Шаг 1)
 *   2) runChecklist() — чек-лист действий      (Шаг 2)
 *   3) runDocument()  — шаблон документа       (Шаг 3)
 *
 * Каждый шаг самостоятельно включает свой skeleton-loader, таймер
 * «Прошло N сек.» и typewriter-стриминг токенов — пользователь видит
 * плавный «All-In-One» сценарий: одна кнопка → три результата.
 *
 * Требования:
 *   - поле #queryInput должно содержать текст (≥ 3 символов);
 *   - НЕ требуется предварительная генерация Шага 1;
 *   - если какой-либо шаг завершился неуспешно (paywall, ошибка сети,
 *     пустой результат) — цепочка останавливается и пользователю
 *     показывается информативный тост;
 *   - кнопка `#runPackageBtn` блокируется на всём протяжении цепочки.
 */