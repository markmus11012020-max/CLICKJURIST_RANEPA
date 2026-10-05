/** Сборка приложения: привязка обработчиков и старт. */

import { $, hide, setStatus, toast } from './core/dom.js';
import { applyQuickTag } from './core/quickTags.js';
import { B2B_BLOCKED_DOC_TYPES, DOC_TYPE_DEFAULT, state } from './core/state.js';
import { hideSkeletonAndTimer, hideStepSkeletonAndTimer } from './features/streaming.js';
import { applyLegalCategory, setActiveDocType } from './ui/legal.js';
import { closePaywall } from './ui/paywall.js';
import { runChecklist } from './features/checklist.js';
import { runDocument } from './features/document.js';
import { runPackage } from './features/package.js';
import { runPdf } from './features/pdf.js';
import { runQuery } from './features/query.js';
import { bindArchive } from './features/archive.js';

/** Сборка приложения: привязка обработчиков и старт. */

export function resetApp() {
  // 1. Поле ввода.
  const input = $("queryInput");
  if (input) input.value = "";

  // 2. Тела результатов всех шагов.
  ["queryResultBody", "checklistResultBody", "documentResultBody"].forEach((id) => {
    const el = $(id);
    if (el) el.innerHTML = "";
  });

  // 3. Список источников (Шаг 1).
  const sourcesList = $("querySourcesList");
  if (sourcesList) sourcesList.innerHTML = "";

  // 4. Глобальный state.
  state.lastQuery = "";
  state.lastAnswer = "";
  state.legalCategory = null;

  // 5. Скрыть контейнеры результатов и блок источников.
  ["queryResult", "checklistResult", "documentResult", "querySources"].forEach((id) => {
    hide($(id));
  });

  // 6. Скрыть скелетоны и таймеры, вернуть их текст в дефолтное состояние.
  hideSkeletonAndTimer();
  hideStepSkeletonAndTimer("checklist");
  hideStepSkeletonAndTimer("document");
  if ($("queryTimerText")) $("queryTimerText").textContent = "Идёт правовой анализ… Прошло 0 сек.";
  if ($("checklistTimerText")) $("checklistTimerText").textContent = "Готовлю чек-лист… Прошло 0 сек.";
  if ($("documentTimerText")) $("documentTimerText").textContent = "Формирую документ… Прошло 0 сек.";

  // 7. Сбросить статусы рядом с кнопками.
  setStatus($("queryStatus"), "");
  setStatus($("checklistStatus"), "");
  setStatus($("documentStatus"), "");

  // 8. Разблокировать кнопки и вернуть им исходный лейбл (на случай, если
  //    внутри runQuery/runChecklist/runDocument они были переведены в
  //    is-loading и/или disabled).
  ["runQueryBtn", "runChecklistBtn", "runDocumentBtn", "runPdfBtn"].forEach((id) => {
    const btn = $(id);
    if (btn) {
      btn.disabled = false;
      btn.classList.remove("is-loading");
    }
  });
  const runDocBtn = $("runDocumentBtn");
  if (runDocBtn && runDocBtn.dataset && runDocBtn.dataset.originalLabel) {
    runDocBtn.innerHTML = runDocBtn.dataset.originalLabel;
  }

  // 9. Сбросить выбор типа документа к дефолту (Шаг 3).
  setActiveDocType(DOC_TYPE_DEFAULT);
  // 9.1. Снять B2B-блокировку с кнопок Шага 3 (если активна).
  applyLegalCategory(null);

  // 10. Закрыть платёжное окно, если оно открыто.
  closePaywall();

  // 11. Подтверждение для пользователя.
  toast("Готово к новой консультации", "success");
}

export function bind() {
  document.querySelectorAll("[data-scroll]").forEach((el) => {
    el.addEventListener("click", () => {
      const target = document.getElementById(el.getAttribute("data-scroll"));
      if (target) target.scrollIntoView({ behavior: "smooth", block: "start" });
    });
  });
  if ($("runQueryBtn")) $("runQueryBtn").addEventListener("click", runQuery);
  if ($("runChecklistBtn")) $("runChecklistBtn").addEventListener("click", runChecklist);
  if ($("runDocumentBtn")) $("runDocumentBtn").addEventListener("click", runDocument);
  if ($("runPdfBtn")) $("runPdfBtn").addEventListener("click", runPdf);
  if ($("runPackageBtn")) $("runPackageBtn").addEventListener("click", runPackage);
  if ($("resetAppBtn")) $("resetAppBtn").addEventListener("click", resetApp);
  if ($("toChecklistBtn")) $("toChecklistBtn").addEventListener("click", () => {
    const target = $("checklist"); if (target) target.scrollIntoView({ behavior: "smooth" });
  });
  if ($("paywallClose")) $("paywallClose").addEventListener("click", closePaywall);
  if ($("paywall")) $("paywall").addEventListener("click", (e) => { if (e.target === $("paywall")) closePaywall(); });
  // 4 кнопки выбора типа документа (см. блок «Шаг 3»). Навешиваем обработчик
  // единожды, чтобы не дублировать клики.
  document.querySelectorAll(".doc-type-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      // Защита №1: HTML-уровень. Если кнопка disabled — браузер сам
      // не пропустит клик, но дополнительно проверяем явно на случай
      // программной переинициализации.
      if (btn.disabled || btn.getAttribute("aria-disabled") === "true") return;
      const value = btn.getAttribute("data-doc-type");
      if (!value) return;
      // Защита №2: повторно проверяем по списку заблокированных типов,
      // чтобы клик не переключил stateDocType.value на отключённый тип
      // (например, через DevTools, снимающий атрибут disabled).
      if (B2B_BLOCKED_DOC_TYPES.has(value)) return;
      setActiveDocType(value);
    });
  });
  // Быстрые пресеты (раздел 4.1 ТЗ prompt160926.md).
  document.querySelectorAll(".quick-tag").forEach((btn) => {
    btn.addEventListener("click", () => {
      const key = btn.getAttribute("data-tag");
      if (key) applyQuickTag(key);
    });
  });
  // Активируем дефолтную кнопку при загрузке (complaint — первая слева).
  setActiveDocType(DOC_TYPE_DEFAULT);

  // Подсветка текущего шага в верхнем степпере при прокрутке. Используем
  // IntersectionObserver: как только центр секции оказывается в зоне видимости,
  // соответствующий пункт степпера получает .is-current, более ранние — .is-done.
  bindStepperSync();

  // Гамбургер-меню: открыть/закрыть дровер по клику на кнопку.
  bindMobileNav();

  // Fade-in для карточек шагов, trust-блоков и FAQ-пунктов при скролле.
  bindReveal();

  // «Развернуть» Шаг 1 на весь экран — кнопка в шапке карточки.
  bindStepExpand();

  // Локальный архив консультаций (без серверной записи — Zero-Storage).
  bindArchive();
}

/**
 * Связать степпер с фактической позицией пользователя на странице.
 * Без этого степпер декоративен — кликается, но не реагирует на прогресс.
 */
function bindStepperSync() {
  const stepperItems = document.querySelectorAll(".stepper__item[data-step]");
  if (!stepperItems.length) return;

  const sectionByStep = new Map();
  stepperItems.forEach((item) => {
    const step = item.getAttribute("data-step");
    const id = item.getAttribute("data-scroll");
    if (id) sectionByStep.set(step, document.getElementById(id));
  });

  const observer = new IntersectionObserver(
    (entries) => {
      // Берём самую верхнюю секцию, которая сейчас пересекает верхнюю
      // половину вьюпорта — это и есть «текущий» шаг.
      const visible = entries
        .filter((e) => e.isIntersecting)
        .sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top);
      if (!visible.length) return;
      const currentSection = visible[0].target;
      const currentStep = currentSection.getAttribute("data-step")
        || (currentSection.id === "consult" ? "1" : null);
      if (!currentStep) return;

      stepperItems.forEach((item) => {
        const itemStep = item.getAttribute("data-step");
        item.classList.toggle("is-current", itemStep === currentStep);
        // Считаем шаг «пройденным», если его порядок меньше текущего.
        const itemIdx = parseInt(itemStep, 10);
        const currentIdx = parseInt(currentStep, 10);
        item.classList.toggle("is-done", Number.isFinite(itemIdx) && itemIdx < currentIdx);
      });
    },
    { rootMargin: "-30% 0px -50% 0px", threshold: 0 },
  );

  sectionByStep.forEach((section) => {
    if (section) observer.observe(section);
  });
}

/**
 * Гамбургер-меню для мобильной/планшетной навигации.
 *
 * На <760px горизонтальные ссылки прячутся, появляется кнопка-гамбургер.
 * По клику открывается выпадающий дровер с теми же ссылками. После клика
 * по ссылке дровер закрывается — чтобы не перекрывать контент при
 * скролле к якорю. Escape тоже закрывает.
 */
function bindMobileNav() {
  const toggle = $("navToggle");
  const drawer = $("navDrawer");
  if (!toggle || !drawer) return;

  // Снимаем атрибут hidden: дровер виден только когда открыт, но мы
  // управляем видимостью через .is-open, а не hidden, чтобы анимация
  // работала плавно.
  drawer.removeAttribute("hidden");

  const open = () => {
    drawer.classList.add("is-open");
    toggle.setAttribute("aria-expanded", "true");
    toggle.setAttribute("aria-label", "Закрыть меню");
  };
  const close = () => {
    drawer.classList.remove("is-open");
    toggle.setAttribute("aria-expanded", "false");
    toggle.setAttribute("aria-label", "Открыть меню");
  };

  toggle.addEventListener("click", () => {
    if (drawer.classList.contains("is-open")) close();
    else open();
  });

  // Клик по ссылке в дровере — закрыть его, чтобы не перекрывал секцию.
  drawer.querySelectorAll("[data-drawer-link]").forEach((link) => {
    link.addEventListener("click", () => close());
  });

  // Escape закрывает.
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && drawer.classList.contains("is-open")) {
      close();
      toggle.focus();
    }
  });

  // При ресайзе за пределы мобильного — закрыть принудительно.
  const mq = window.matchMedia("(min-width: 761px)");
  mq.addEventListener("change", (e) => {
    if (e.matches) close();
  });
}

/**
 * Fade-in секций при скролле.
 *
 * Карточки шагов, trust-блоки и FAQ-пункты получают класс .reveal и
 * становятся видимыми через IntersectionObserver — плавно «всплывают»
 * снизу. Каскадная задержка (data-reveal-delay) создаёт эффект волны.
 *
 * Уважает prefers-reduced-motion: если анимация отключена, observer
 * сразу помечает все элементы видимыми без CSS-переходов.
 */
function bindReveal() {
  const targets = [
    ...document.querySelectorAll(".step-card"),
    ...document.querySelectorAll(".trust__card"),
    ...document.querySelectorAll(".faq__item"),
    ...document.querySelectorAll(".stepper"),
  ];
  if (!targets.length) return;

  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // Если анимация отключена — сразу показать все элементы, observer не нужен.
  if (reduceMotion) {
    targets.forEach((el) => el.classList.add("is-visible"));
    return;
  }

  // Каскад: каждой карточке ставим data-reveal-delay согласно её индексу
  // среди «своих» (карточки шагов отдельно от trust-карточек и FAQ).
  const groups = new Map();
  targets.forEach((el) => {
    let key = "default";
    if (el.classList.contains("step-card")) key = "step";
    else if (el.classList.contains("trust__card")) key = "trust";
    else if (el.classList.contains("faq__item")) key = "faq";
    const idx = (groups.get(key) || 0) + 1;
    groups.set(key, idx);
    el.classList.add("reveal");
    el.setAttribute("data-reveal-delay", String(Math.min(idx, 6)));
  });

  const observer = new IntersectionObserver(
    (entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting) {
          entry.target.classList.add("is-visible");
          observer.unobserve(entry.target);
        }
      });
    },
    { rootMargin: "0px 0px -10% 0px", threshold: 0.05 },
  );

  targets.forEach((el) => observer.observe(el));
}

/**
 * Переключатель «Развернуть» для Шага 1.
 *
 * По клику на кнопку в шапке карточки Шаг 1 разворачивается на всю
 * ширину сетки (Шаги 2/3 сворачиваются в превью-полосу). Повторный клик —
 * обратно в трёхколоночный режим. Состояние хранится в data-атрибуте
 * `.steps-grid` (для CSS) и `aria-pressed` на кнопке (для скринридеров).
 */
function bindStepExpand() {
  const btn = $("stepExpandBtn");
  const grid = document.querySelector(".steps-grid");
  if (!btn || !grid) return;

  const set = (expanded) => {
    if (expanded) {
      grid.setAttribute("data-expanded", "true");
      btn.setAttribute("aria-pressed", "true");
      btn.setAttribute("aria-label", "Свернуть обратно в три колонки");
      btn.setAttribute("title", "Свернуть — вернуться к трём колонкам");
      btn.querySelector("span").textContent = "Свернуть";
    } else {
      grid.removeAttribute("data-expanded");
      btn.setAttribute("aria-pressed", "false");
      btn.setAttribute("aria-label", "Развернуть на весь экран");
      btn.setAttribute("title", "Развернуть на весь экран — удобнее вводить длинный текст");
      btn.querySelector("span").textContent = "Развернуть";
    }
  };

  btn.addEventListener("click", () => {
    const isExpanded = grid.getAttribute("data-expanded") === "true";
    set(!isExpanded);
    // После раскрытия — фокус в текстарею, чтобы можно было сразу печатать.
    if (!isExpanded) {
      const ta = document.getElementById("queryInput");
      if (ta) {
        // Небольшая задержка, чтобы анимация макета успела начаться.
        setTimeout(() => ta.focus(), 80);
      }
    }
  });
}
