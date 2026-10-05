/** Пакетный тариф «решение под ключ». */

import { $, toast } from '../core/dom.js';
import { state } from '../core/state.js';
import { runChecklist } from './checklist.js';
import { runDocument } from './document.js';
import { runQuery } from './query.js';

/** Пакетный тариф «решение под ключ». */

export async function runPackage() {
  const queryInput = $("queryInput");
  const query = ((queryInput && queryInput.value) || "").trim();
  if (query.length < 3) {
    toast("Опишите ситуацию подробнее", "error");
    return;
  }

  const btn = $("runPackageBtn"); if (btn) btn.disabled = true;
  state.packageChain = true;
  try {
    // ── Шаг 1: юридическая консультация ──────────────────────────────
    // runQuery() сам поднимет skeleton + timer + typewriter-стриминг
    // и по завершении запишет результат в state.lastAnswer.
    await runQuery();
    if (!state.lastAnswer || !state.lastAnswer.trim()) {
      toast("\"Всё включено\": консультация не получена — цепочка остановлена", "error");
      return;
    }

    // Прокрутить страницу к блоку Шага 2, чтобы пользователь видел
    // анимацию генерации чек-листа.
    const checklistSection = $("checklist");
    if (checklistSection && typeof checklistSection.scrollIntoView === "function") {
      checklistSection.scrollIntoView({ behavior: "smooth", block: "start" });
    }

    // ── Шаг 2: чек-лист действий ────────────────────────────────────
    // runChecklist() поднимет свой skeleton + timer + typewriter-стриминг.
    await runChecklist();
    const checklistBody = $("checklistResultBody");
    if (!checklistBody || !checklistBody.textContent.trim()) {
      toast("\"Всё включено\": чек-лист не сформирован — цепочка остановлена", "error");
      return;
    }

    // Прокрутить страницу к блоку Шага 3, чтобы пользователь видел
    // анимацию генерации документа.
    const documentSection = $("document");
    if (documentSection && typeof documentSection.scrollIntoView === "function") {
      documentSection.scrollIntoView({ behavior: "smooth", block: "start" });
    }

    // ── Шаг 3: шаблон документа ─────────────────────────────────────
    // runDocument() использует активный doc_type (по умолчанию «complaint»),
    // поднимает свой skeleton + timer + typewriter-стриминг.
    await runDocument();
    const documentBody = $("documentResultBody");
    if (!documentBody || !documentBody.textContent.trim()) {
      toast("\"Всё включено\": документ не сформирован — цепочка остановлена", "error");
      return;
    }

    toast("\"Всё включено\" готово: консультация + чек-лист + документ ✓", "success");
  } catch (e) {
    toast("Сбой в цепочке \"Всё включено\": " + (e && e.message ? e.message : e), "error");
  } finally {
    state.packageChain = false;
    if (btn) btn.disabled = false;
  }
}

/**
 * Полный сброс состояния приложения: очищает поле ввода, результаты всех шагов,
 * глобальный state, скрывает скелетоны/таймеры/результаты, снимает блокировки
 * с кнопок и закрывает платёжное окно. Вызывается по клику на «Начать новую
 * консультацию» в Шаге 1.
 */