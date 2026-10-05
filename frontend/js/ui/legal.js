/** Юридический блок и правовая категория B2B/B2C для шаблонов. */

import { $ } from '../core/dom.js';
import { markdownToHtml } from '../core/markdown.js';
import { B2B_BLOCKED_DOC_TYPES, stateDocType } from '../core/state.js';
import { api } from '../core/net.js';

/** Юридический блок и правовая категория B2B/B2C для шаблонов. */

export async function loadLegal() {
  const { data } = await api("GET", "/api/legal");
  if (!data) return;
  // /api/legal возвращает готовый markdown — отрендерим его тем же
  // markdownToHtml, что и результаты консультаций (заголовки, жирный,
  // маркированные списки).
  if ($("legalOperator") && data.operator) {
    $("legalOperator").innerHTML = markdownToHtml(data.operator);
  }
  if ($("legalPrivacy") && data.privacy) {
    $("legalPrivacy").innerHTML = markdownToHtml(data.privacy);
  }
  if ($("legalDisclaimer") && data.disclaimer) {
    $("legalDisclaimer").textContent = data.disclaimer;
  }
  // Блок «Как мы защищаем ваши персональные данные (152-ФЗ)» (Шаг 5 ТЗ prompt170926.md).
  // Если бэкенд вернул текст маскировки — рендерим его в аккордеон.
  // Если нет — оставляем статический текст из index.html.
  if ($("legalMasking") && data.masking) {
    $("legalMasking").innerHTML = markdownToHtml(data.masking);
  }
}

// Состояние выбранного типа документа (см. блок «Шаг 3. Шаблон документа»).
// Кнопки идут строго слева направо: complaint → claim → lawsuit → court_order_cancellation.

export function applyLegalCategory(category) {
  const isB2B = category === "b2b";
  const buttons = document.querySelectorAll(".doc-type-btn");
  let firstAvailable = null;
  buttons.forEach((btn) => {
    const type = btn.getAttribute("data-doc-type") || "";
    const blocked = isB2B && B2B_BLOCKED_DOC_TYPES.has(type);
    if (blocked) {
      btn.disabled = true;
      btn.setAttribute("aria-disabled", "true");
      btn.style.opacity = "0.4";
      btn.style.pointerEvents = "none";
      btn.title = "Этот шаблон не подходит для вашего случая — выберите другой";
      // Снимаем визуальный «выбранный» стиль, если он был активен.
      btn.classList.remove("primary");
      btn.classList.add("ghost");
      btn.setAttribute("aria-pressed", "false");
    } else {
      btn.disabled = false;
      btn.removeAttribute("aria-disabled");
      btn.style.opacity = "";
      btn.style.pointerEvents = "";
      btn.removeAttribute("title");
      if (!firstAvailable) firstAvailable = type;
    }
  });
  // Если текущий выбор оказался заблокирован — переключаемся на
  // первый доступный шаблон, чтобы состояние stateDocType не
  // указывало на отключённую кнопку.
  if (
    isB2B &&
    stateDocType.value &&
    B2B_BLOCKED_DOC_TYPES.has(stateDocType.value) &&
    firstAvailable
  ) {
    setActiveDocType(firstAvailable);
  }
}

export function setActiveDocType(value) {
  stateDocType.value = value;
  document.querySelectorAll(".doc-type-btn").forEach((btn) => {
    const active = btn.getAttribute("data-doc-type") === value;
    btn.classList.toggle("primary", active);
    btn.classList.toggle("ghost", !active);
    btn.setAttribute("aria-pressed", active ? "true" : "false");
  });
}