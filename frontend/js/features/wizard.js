/**
 * Wizard — фичи STAGE_2 и STAGE_3: рендеринг чек-листа, мгновенный sync
 * галочек, светлый paywall-блюр.
 *
 * Сценарии:
 *
 *   1. STAGE_2: renderChecklist(items)
 *      — рисует <li> c <input type=checkbox> и saved-индикатором;
 *      — на change: оптимистичный patch в WizardState → POST
 *        /api/wizard/sync-checklist → показать «Сохранено ✓» на 1.5 с.
 *
 *   2. STAGE_3: renderDocument({ markdown, isPaid })
 *      — markdown → HTML (через core/markdown.js — без внешних либ);
 *      — если !isPaid — наложить CSS-класс .is-blurred и показать paywall;
 *      — если isPaid — снять блюр и показать кнопку скачивания.
 *
 *   3. bindStage2Advance() — кнопка «Готово →» на STAGE_2: шлёт финальный
 *      sync-checklist с advance_stage=true → Wizard переходит на STAGE_3.
 *
 *   4. bindStage1Submit() — форма на STAGE_1: запускает пайплайн анализа,
 *      стартует polling и переходит на STAGE_1-loader.
 *
 * Импортируется из script.js (entry-point) после загрузки DOM.
 */

import { api } from '../core/net.js';
import { $, setStatus, toast } from '../core/dom.js';
import { markdownToHtml } from '../core/markdown.js';
import {
  pollWizardUntilReady,
  stopWizardPoll,
  wizardState,
} from '../ui/state.js';

/**
 * Полный список пунктов чек-листа, который приходит с сервера.
 * Формат: [{ id: 'docs', text: 'Собрать документы' }, …]
 */
let _checklistItems = [];

/** Debounce-токен для POST'ов: не спамим сервер при серии кликов. */
let _syncDebounce = null;
const SYNC_DEBOUNCE_MS = 250;

// ---------------------------------------------------------------------------
// STAGE_2: рендеринг чек-листа
// ---------------------------------------------------------------------------

/**
 * Отрисовать интерактивный чек-лист.
 * @param {Array<{id:string, text:string}>} items
 * @param {object} [opts]
 * @param {Record<string,boolean>} [opts.checkedState] — начальное состояние
 */
export function renderChecklist(items, opts = {}) {
  const list = document.querySelector('[data-wizard-checklist]');
  if (!list) return;
  _checklistItems = Array.isArray(items) ? items : [];
  const checked = (opts && opts.checkedState) || wizardState.getChecklistState();
  list.innerHTML = '';

  if (_checklistItems.length === 0) {
    const empty = document.createElement('li');
    empty.className = 'wizard-checklist__empty';
    empty.textContent = 'Пункты появятся после того, как ИИ закончит анализ.';
    list.appendChild(empty);
    return;
  }

  for (const item of _checklistItems) {
    list.appendChild(_makeChecklistItem(item, Boolean(checked[item.id])));
  }
}

/**
 * Создать <li> для одного пункта чек-листа.
 * Использует нативный <input type=checkbox> — самый дешёвый способ получить
 * a11y, клавиатурную навигацию и состояние indeterminate бесплатно.
 */
function _makeChecklistItem(item, isChecked) {
  const li = document.createElement('li');
  li.className = 'wizard-checklist__item';

  const label = document.createElement('label');
  label.className = 'wizard-checklist__label';

  // Сам чекбокс визуально скрыт, но доступен для screen-reader'а и клавиатуры.
  const cb = document.createElement('input');
  cb.type = 'checkbox';
  cb.className = 'wizard-checklist__cb';
  cb.dataset.key = item.id;
  cb.checked = isChecked;
  cb.setAttribute('aria-label', item.text);

  // Кастомная визуальная «коробка» — тот самый 44×44 touch-target.
  const box = document.createElement('span');
  box.className = 'wizard-checklist__box';
  box.setAttribute('aria-hidden', 'true');

  const text = document.createElement('span');
  text.className = 'wizard-checklist__text';
  text.textContent = item.text;

  // Микро-иконка «Сохранено»: 16×16 check, появляется на 1.5 с после sync.
  const saved = document.createElement('span');
  saved.className = 'wizard-checklist__saved';
  saved.setAttribute('aria-hidden', 'true');
  saved.innerHTML =
    '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" ' +
    'stroke="currentColor" stroke-width="2.4" stroke-linecap="round" ' +
    'stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>';

  label.appendChild(cb);
  label.appendChild(box);
  label.appendChild(text);
  label.appendChild(saved);
  li.appendChild(label);
  return li;
}

/**
 * Делегированный change на <ul>: один обработчик на весь список, чтобы не
 * навешивать N штук. Ловим ближайший <input>.
 */
function _bindChecklistDelegation() {
  const list = document.querySelector('[data-wizard-checklist]');
  if (!list || list.dataset.bound === '1') return;
  list.dataset.bound = '1';
  list.addEventListener('change', async (ev) => {
    const cb = ev.target.closest('.wizard-checklist__cb');
    if (!cb) return;
    const key = cb.dataset.key;
    if (!key) return;
    const value = cb.checked;

    // 1) Локальное обновление — мгновенно, до ответа сервера.
    wizardState.patchChecklistKey(key, value);

    // 2) POST с debounce — при серии кликов отправляем только последний снимок.
    if (_syncDebounce) clearTimeout(_syncDebounce);
    _syncDebounce = setTimeout(() => _postChecklist(cb), SYNC_DEBOUNCE_MS);
  });
}

async function _postChecklist(originCheckbox) {
  const full = wizardState.getChecklistState();
  const { ok, data } = await api('POST', '/api/wizard/sync-checklist', {
    checklist_state: full,
    advance_stage: false,
  });
  if (!ok) {
    toast('Не удалось сохранить пункт — попробуйте ещё раз', 'error');
    // Откатываем локальный апдейт, чтобы UI не врал.
    if (originCheckbox) {
      originCheckbox.checked = !originCheckbox.checked;
      wizardState.patchChecklistKey(originCheckbox.dataset.key, originCheckbox.checked);
    }
    return;
  }
  // Кратковременный индикатор «Сохранено».
  const label = originCheckbox.closest('.wizard-checklist__label');
  if (label) {
    const saved = label.querySelector('.wizard-checklist__saved');
    if (saved) {
      saved.classList.add('is-shown');
      setTimeout(() => saved.classList.remove('is-shown'), 1500);
    }
  }
}

// ---------------------------------------------------------------------------
// STAGE_3: документ + paywall blur
// ---------------------------------------------------------------------------

/**
 * Отрисовать документ и (опционально) размыть его.
 * @param {object} opts
 * @param {string} [opts.markdown] — текст документа
 * @param {boolean} opts.isPaid — флаг оплаты
 * @param {string} [opts.docType='complaint'] — тип документа (для заголовка)
 */
export function renderDocument(opts = {}) {
  const doc = document.querySelector('[data-wizard-document]');
  const paywall = document.querySelector('[data-wizard-paywall]');
  const download = document.querySelector('[data-wizard-download]');
  if (!doc) return;

  const { markdown = '', isPaid = false } = opts;
  // Безопасный рендер: markdownToHtml() экранирует исходный текст.
  doc.innerHTML = markdown
    ? markdownToHtml(markdown)
    : '<p class="wizard-paper__empty">Документ будет здесь после генерации.</p>';

  if (isPaid) {
    doc.classList.remove('is-blurred');
    if (paywall) paywall.hidden = true;
    if (download) download.hidden = false;
  } else {
    doc.classList.add('is-blurred');
    if (paywall) paywall.hidden = false;
    if (download) download.hidden = true;
  }
}

// ---------------------------------------------------------------------------
// Биндинги кнопок
// ---------------------------------------------------------------------------

/**
 * Кнопка «Готово →» на STAGE_2: финальный sync-checklist + advance_stage=true.
 * Сервер (см. backend/api/router_wizard.py) возвращает current_stage=STAGE_3.
 */
function bindStage2Advance() {
  const btn = document.querySelector('[data-wizard-advance]');
  if (!btn || btn.dataset.bound === '1') return;
  btn.dataset.bound = '1';
  btn.addEventListener('click', async () => {
    btn.disabled = true;
    setStatus(btn, 'Переходим…');
    const full = wizardState.getChecklistState();
    const { ok: httpOk, data } = await api('POST', '/api/wizard/sync-checklist', {
      checklist_state: full,
      advance_stage: true,
    });
    if (!httpOk) {
      btn.disabled = false;
      toast('Не удалось перейти к документу', 'error');
      return;
    }
    // Сервер уже сменил стадию; применяем к UI.
    if (data && data.current_stage) {
      wizardState.setStage(data.current_stage);
    } else {
      wizardState.setStage('STAGE_3');
    }
  });
}

/**
 * Форма STAGE_1: запуск анализа. POST /api/query/async → polling.
 * После завершения фоновой задачи Wizard перейдёт на STAGE_2 (см. polling).
 */
function bindStage1Submit() {
  const form = document.querySelector('[data-wizard-form]');
  if (!form || form.dataset.bound === '1') return;
  form.dataset.bound = '1';

  form.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const textarea = form.querySelector('#wizardInput');
    const query = textarea ? textarea.value.trim() : '';
    if (query.length < 3) {
      toast('Опишите ситуацию подробнее (минимум 3 символа)', 'error');
      return;
    }
    const submit = form.querySelector('[data-wizard-submit]');
    if (submit) submit.disabled = true;

    // 1. Запускаем пайплайн (async-вариант — он же возвращает task_id).
    const { ok, data, status } = await api('POST', '/api/query/async', { query });
    if (!ok || !data || !data.task_id) {
      if (submit) submit.disabled = false;
      toast(
        status === 402
          ? 'Сервис работает по предоплате. Откройте оплату.'
          : 'Не удалось запустить анализ',
        'error',
      );
      return;
    }

    // 2. Стартуем polling /api/wizard/state.
    const poll = pollWizardUntilReady({
      onProgress: (p) => {
        const fill = document.querySelector('[data-wizard-progress-fill]');
        const lbl = document.querySelector('[data-wizard-progress-label]');
        if (fill) fill.style.width = p + '%';
        if (lbl) lbl.textContent = p + '%';
      },
    });
    // 3. По завершению — разлочим кнопку, развернём чек-лист.
    poll.promise.finally(() => {
      if (submit) submit.disabled = false;
    });
  });
}

/**
 * Paywall-кнопка «Разблокировать» на STAGE_3: создаёт счёт в Робокассе через
 * /api/payment/create и открывает существующее окно оплаты. После фактической
 * оплаты (successURL) — refresh /api/wizard/state и снять блюр.
 */
function bindStage3Paywall() {
  const btn = document.querySelector('[data-wizard-pay]');
  if (!btn || btn.dataset.bound === '1') return;
  btn.dataset.bound = '1';
  btn.addEventListener('click', async () => {
    btn.disabled = true;
    // 1. Создаём счёт.
    const { ok, data } = await api('POST', '/api/payment/create', {
      service: 'document',
    });
    if (!ok || !data || !data.payment_url) {
      btn.disabled = false;
      toast('Не удалось открыть окно оплаты', 'error');
      return;
    }
    // 2. Открываем существующее окно paywall.js — оно само переведёт
    //    пользователя на Робокассу.
    try {
      const { openPaywall } = await import('../ui/paywall.js');
      openPaywall('document', data.amount, data.payment_url);
    } catch (err) {
      console.warn('[wizard] paywall module unavailable, falling back to direct redirect:', err);
      window.location.href = data.payment_url;
    }
    btn.disabled = false;
  });
}

/**
 * Опросить /api/wizard/state и, если is_paid уже true, перерисовать документ
 * без блюра. Вызывается после успешного возврата с Робокассы (callback
 * successURL или событие focus окна после оплаты).
 */
export async function refreshPaymentState() {
  const { data } = await api('GET', '/api/wizard/state');
  if (data && data.session && data.session.is_paid) {
    const doc = document.querySelector('[data-wizard-document]');
    const raw = doc ? doc.dataset.rawMarkdown || '' : '';
    renderDocument({ markdown: raw, isPaid: true });
  }
}

/** Главная функция модуля — bind'ит все этапы. Вызывать из entry-point. */
export function bindWizard() {
  wizardState.mount();
  bindStage1Submit();
  _bindChecklistDelegation();
  bindStage2Advance();
  bindStage3Paywall();
  // Если пользователь ушёл со страницы — отменим висящий polling.
  window.addEventListener('beforeunload', () => stopWizardPoll());
  window.addEventListener('pagehide', () => stopWizardPoll());
}

// ---------------------------------------------------------------------------
// Дополнительный рендер правового вердикта (используется STAGE_2)
// ---------------------------------------------------------------------------

/**
 * Отрисовать блок вердикта на STAGE_2 (правовая квалификация, применимые
 * нормы, ключевые сроки). Формат `ai_qualification` совпадает с моделью
 * WizardSession.ai_qualification на бэкенде.
 * @param {object} qualification
 */
export function renderWizardVerdict(qualification) {
  const target = document.querySelector('[data-wizard-verdict]');
  if (!target) return;
  const q = qualification || {};
  const articles = Array.isArray(q.articles) ? q.articles : [];
  const deadlines = Array.isArray(q.deadlines) ? q.deadlines : [];

  target.innerHTML = '';
  if (q.industry || q.summary) {
    const h = document.createElement('h3');
    h.className = 'wizard-verdict__title';
    h.textContent = q.summary || 'Правовая квалификация';
    target.appendChild(h);
  }
  if (q.industry) {
    const row = _verdictRow('Отрасль', q.industry);
    target.appendChild(row);
  }
  if (articles.length) {
    const row = document.createElement('div');
    row.className = 'wizard-verdict__row';
    row.innerHTML =
      '<span class="wizard-verdict__label">Применимые нормы</span>' +
      '<span class="wizard-verdict__chips">' +
      articles.map((a) => `<span class="wizard-verdict__chip">${escapeHtml(a)}</span>`).join('') +
      '</span>';
    target.appendChild(row);
  }
  if (deadlines.length) {
    const ul = document.createElement('ul');
    ul.className = 'wizard-verdict__deadlines';
    deadlines.forEach((d) => {
      const li = document.createElement('li');
      li.innerHTML = `<strong>${escapeHtml(String(d.days || ''))}</strong> — ${escapeHtml(d.text || '')}`;
      ul.appendChild(li);
    });
    target.appendChild(ul);
  }
}

function _verdictRow(label, value) {
  const row = document.createElement('div');
  row.className = 'wizard-verdict__row';
  row.innerHTML =
    `<span class="wizard-verdict__label">${escapeHtml(label)}</span>` +
    `<span class="wizard-verdict__value">${escapeHtml(String(value))}</span>`;
  return row;
}

/** Мини-html-escape для безопасной подстановки строк в innerHTML. */
function escapeHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}