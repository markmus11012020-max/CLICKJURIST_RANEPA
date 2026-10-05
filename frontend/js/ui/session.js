/** Сессия и тарифы: чип, цены, первичная загрузка состояния. */

import { $ } from '../core/dom.js';
import { api } from '../core/net.js';
import { getSessionState, setSessionState } from '../core/state.js';

/** Сессия и тарифы: чип, цены, первичная загрузка состояния. */

export async function loadSession() {
  const { status, data } = await api("GET", "/api/session");
  if (status === 200 && data) {
    setSessionState(data);
    renderSessionChip();
    renderPrices();
  }
}

export function renderSessionChip() {
  const label = $("sessionLabel");
  const chip = $("sessionChip");
  if (!label || !chip) return;
  const sessionState = getSessionState();
  if (sessionState.paid_access) {
    chip.classList.add("locked");
    label.textContent = "Оплаченный доступ · 30 дней";
  } else {
    // Бесплатный trial снят (октябрь 2026). Все запросы платные с первого.
    // Чип — «витрина тарифа», не «бесплатно». Используем cyan-стиль
    // (``locked``), чтобы подсказать: чтобы получить ответ, нужно оплатить.
    chip.classList.add("locked");
    label.textContent = "Оплата до результата · от 49 ₽";
  }
}

export function renderPrices() {
  const p = getSessionState().prices || {};
  if ($("priceConsult")) $("priceConsult").textContent = (p.consultation || 49) + " ₽";
  if ($("priceChecklist")) $("priceChecklist").textContent = (p.checklist || 50) + " ₽";
  if ($("priceDocument")) $("priceDocument").textContent = (p.document || 150) + " ₽";
  // Пакетный тариф «Всё включено» (Шаг 4 ТЗ prompt170926.md).
  if ($("packagePrice")) $("packagePrice").textContent = (p.package_basic || 195) + " ₽";
  // Зеркалим цены в верхний степпер, чтобы пользователь видел тарифы
  // до скролла к карточкам шагов.
  document.querySelectorAll("[data-step-price]").forEach((node) => {
    const slot = node.getAttribute("data-step-price");
    if (slot === "consult") node.textContent = (p.consultation || 49) + " ₽";
    else if (slot === "checklist") node.textContent = (p.checklist || 50) + " ₽";
    else if (slot === "document") node.textContent = (p.document || 150) + " ₽";
  });
}
