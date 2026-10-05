/** Окно оплаты: открытие строго из обработчиков 402. */

import { $, hide, show } from '../core/dom.js';
import { PAYWALL_SERVICES } from '../core/state.js';

/** Окно оплаты: открытие строго из обработчиков 402. */

export function openPaywall(service, amount, paymentUrl) {
  if (!PAYWALL_SERVICES.has(service)) {
    console.warn("[paywall] openPaywall вызван с неизвестным сервисом:", service);
    return;
  }
  if (!paymentUrl) {
    console.warn("[paywall] отсутствует payment_url, окно не открывается");
    return;
  }
  $("paywallService").textContent = "Услуга: " + ({
    consultation: "юридическая консультация",
    checklist: "чек-лист действий",
    document: "шаблон документа",
    pdf: "PDF-версия",
    package_basic: "Всё включено (Консультация + Чек-лист + Документ)",
  }[service] || service);
  $("paywallAmount").textContent = amount + " ₽";
  const link = $("paywallLink");
  link.href = paymentUrl || "#";
  show($("paywall"));
}

export function closePaywall() { hide($("paywall")); }
