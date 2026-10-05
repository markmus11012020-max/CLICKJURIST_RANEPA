/** Локальный архив консультаций (152-ФЗ friendly).
 *
 * Хранит историю в браузерном localStorage — без отправки на сервер.
 * Это ответ на частый запрос пользователей: «где посмотреть прошлые
 * ответы?». Сервер намеренно ничего не сохраняет (Zero-Storage по ТЗ),
 * поэтому архив живёт ТОЛЬКО на устройстве пользователя.
 *
 * Структура одной записи:
 *   {
 *     id: string,           // UUID
 *     ts: number,           // unix timestamp в мс
 *     query: string,        // обезличенный текст вопроса
 *     answer: string,       // markdown-текст ответа
 *     sources: [{title,url}],// ссылки на НПА
 *     legalCategory: string,// "b2b" | "b2c" | null
 *     favorite: boolean,    // флаг избранного
 *   }
 *
 * Лимит — 50 записей (защита от переполнения localStorage). При
 * превышении самая старая запись удаляется (FIFO), если только она
 * не помечена как favorite.
 */

import { $ } from '../core/dom.js';
import { markdownToHtml } from '../core/markdown.js';
import { state } from '../core/state.js';
import { applyLegalCategory } from '../ui/legal.js';

const STORAGE_KEY = 'cj_archive_v1';
const MAX_ENTRIES = 50;
const FAVORITES_HEAD = 8; // кол-во избранных, которые бережём от FIFO

/** Простой UUID для id записи (без crypto.randomUUID для старых браузеров). */
function makeId() {
  return 'cj-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 8);
}

/** Прочитать архив из localStorage. Возвращает массив (может быть пустым). */
export function loadAll() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed : [];
  } catch (e) {
    // Битый JSON или недоступный localStorage (приватный режим в некоторых
    // браузерах) — возвращаем пустой массив, UI покажет empty state.
    return [];
  }
}

/** Сохранить архив в localStorage. Молча проглатывает quota-ошибки. */
function persist(entries) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(entries));
  } catch (e) {
    // QuotaExceededError или SecurityError (приватный режим). Архив не
    // критичен, поэтому просто пишем в консоль и продолжаем работу.
    if (typeof console !== 'undefined' && console.debug) {
      console.debug('[archive] persist failed:', e && e.message);
    }
  }
}

/** Сокращённый текст запроса для превью в карточке архива. */
function snippet(text, limit = 160) {
  if (!text) return '';
  const t = text.replace(/\s+/g, ' ').trim();
  return t.length > limit ? t.slice(0, limit).replace(/[\s.,;:!?]+$/, '') + '…' : t;
}

/** Сохранить новую запись в архив. Вызывается после завершения консультации.
 *
 * Параметры берём из глобального state и DOM — на момент вызова они уже
 * заполнены кодом streamConsultation / renderConsultationResult.
 */
export function saveCurrent() {
  const query = (state.lastQuery || '').trim();
  const answer = (state.lastAnswer || '').trim();
  if (!query || !answer) return; // нечего сохранять

  // Считываем текущие источники прямо из DOM, чтобы не плодить state.
  const sourcesList = $('querySourcesList');
  const sources = [];
  if (sourcesList) {
    sourcesList.querySelectorAll('a').forEach((a) => {
      sources.push({ title: a.textContent || '', url: a.getAttribute('href') || '' });
    });
  }

  const entry = {
    id: makeId(),
    ts: Date.now(),
    query,
    answer,
    sources,
    legalCategory: state.legalCategory || null,
    favorite: false,
  };

  const entries = loadAll();
  entries.unshift(entry); // новые сверху

  // FIFO с защитой favorites.
  if (entries.length > MAX_ENTRIES) {
    const favs = entries.filter(e => e.favorite).slice(0, FAVORITES_HEAD);
    const rest = entries.filter(e => !e.favorite);
    // отрезаем хвост, пока не влезем
    while (rest.length + favs.length > MAX_ENTRIES) rest.pop();
    entries.length = 0;
    entries.push(...favs, ...rest);
  }

  persist(entries);
  renderList();
}

/** Удалить запись по id. */
export function remove(id) {
  const entries = loadAll().filter(e => e.id !== id);
  persist(entries);
  renderList();
}

/** Очистить архив целиком (с подтверждением через confirm()). */
export function clearAll() {
  const entries = loadAll();
  if (!entries.length) return;
  if (typeof window !== 'undefined' && !window.confirm(
    'Удалить все сохранённые консультации (' + entries.length + ' шт.)? Избранные тоже будут удалены.'
  )) {
    return;
  }
  persist([]);
  renderList();
}

/** Переключить флаг favorite у записи. */
export function toggleFavorite(id) {
  const entries = loadAll();
  const target = entries.find(e => e.id === id);
  if (!target) return;
  target.favorite = !target.favorite;
  persist(entries);
  renderList();
}

/** Открыть запись: восстановить state и отрендерить результат в Шаге 1. */
export function openEntry(id) {
  const entry = loadAll().find(e => e.id === id);
  if (!entry) return;
  // 1. Поля ввода и состояние.
  state.lastQuery = entry.query;
  state.lastAnswer = entry.answer;
  state.legalCategory = entry.legalCategory || null;
  const input = $('queryInput');
  if (input) input.value = entry.query;
  // 2. Тело результата + источники.
  const body = $('queryResultBody');
  if (body) body.innerHTML = markdownToHtml(entry.answer);
  const sourcesList = $('querySourcesList');
  if (sourcesList) {
    sourcesList.innerHTML = '';
    (entry.sources || []).forEach((src) => {
      const li = document.createElement('li');
      const a = document.createElement('a');
      a.href = src.url;
      a.textContent = src.title || src.url;
      a.target = '_blank';
      a.rel = 'noopener noreferrer';
      li.appendChild(a);
      sourcesList.appendChild(li);
    });
  }
  const sourcesBlock = $('querySources');
  if (sourcesBlock) {
    if ((entry.sources || []).length) sourcesBlock.hidden = false;
    else sourcesBlock.hidden = true;
  }
  // 3. Контейнер результата видим.
  const resultBlock = $('queryResult');
  if (resultBlock) resultBlock.hidden = false;
  // 4. Правовая категория → блокировка doc-type кнопок Шага 3.
  applyLegalCategory(entry.legalCategory || null);
  // 5. Прокрутка к Шагу 1.
  const consultSection = $('consult');
  if (consultSection && typeof consultSection.scrollIntoView === 'function') {
    consultSection.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
}

/** Отформатировать дату для карточки: «3 окт, 14:23». */
function formatTime(ts) {
  try {
    const d = new Date(ts);
    const months = ['янв', 'фев', 'мар', 'апр', 'мая', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек'];
    return d.getDate() + ' ' + months[d.getMonth()] + ', ' +
      String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
  } catch (_) {
    return '';
  }
}

/** Отрисовать список архива в DOM. */
export function renderList() {
  const list = $('archiveList');
  if (!list) return;
  const entries = loadAll();
  list.innerHTML = '';

  if (!entries.length) {
    const empty = document.createElement('div');
    empty.className = 'archive__empty';
    empty.textContent = 'Здесь пока пусто. После первой консультации появится запись — её можно открыть, скачать в избранное или удалить.';
    list.appendChild(empty);
    const counter = $('archiveCount');
    if (counter) counter.textContent = '0';
    return;
  }

  // Сначала избранные, потом остальные (уже отсортированы: новые сверху).
  const sorted = entries.slice().sort((a, b) => {
    if (a.favorite !== b.favorite) return a.favorite ? -1 : 1;
    return b.ts - a.ts;
  });

  const counter = $('archiveCount');
  if (counter) counter.textContent = String(sorted.length);

  sorted.forEach((entry) => {
    const card = document.createElement('article');
    card.className = 'archive-item' + (entry.favorite ? ' is-favorite' : '');
    card.setAttribute('data-id', entry.id);

    // Заголовок: дата + звёздочка.
    const head = document.createElement('div');
    head.className = 'archive-item__head';
    const time = document.createElement('time');
    time.className = 'archive-item__time';
    time.dateTime = new Date(entry.ts).toISOString();
    time.textContent = formatTime(entry.ts);
    head.appendChild(time);
    const favBtn = document.createElement('button');
    favBtn.type = 'button';
    favBtn.className = 'archive-item__fav';
    favBtn.setAttribute('aria-pressed', entry.favorite ? 'true' : 'false');
    favBtn.setAttribute('aria-label', entry.favorite ? 'Убрать из избранного' : 'В избранное');
    favBtn.title = entry.favorite ? 'Убрать из избранного' : 'В избранное';
    favBtn.innerHTML = entry.favorite ? '★' : '☆';
    favBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      toggleFavorite(entry.id);
    });
    head.appendChild(favBtn);
    card.appendChild(head);

    // Превью запроса.
    const q = document.createElement('p');
    q.className = 'archive-item__query';
    q.textContent = snippet(entry.query, 200);
    card.appendChild(q);

    // Действия.
    const actions = document.createElement('div');
    actions.className = 'archive-item__actions';
    const open = document.createElement('button');
    open.type = 'button';
    open.className = 'btn btn-secondary archive-item__open';
    open.textContent = 'Открыть';
    open.addEventListener('click', () => openEntry(entry.id));
    actions.appendChild(open);
    const del = document.createElement('button');
    del.type = 'button';
    del.className = 'archive-item__del';
    del.setAttribute('aria-label', 'Удалить запись');
    del.title = 'Удалить';
    del.textContent = '×';
    del.addEventListener('click', (e) => {
      e.stopPropagation();
      remove(entry.id);
    });
    actions.appendChild(del);
    card.appendChild(actions);

    list.appendChild(card);
  });
}

/** Привязать обработчик «Очистить архив» (вызывается из bind()). */
export function bindArchive() {
  const clearBtn = $('archiveClearBtn');
  if (clearBtn) clearBtn.addEventListener('click', clearAll);
  // Первичный рендер при загрузке страницы.
  renderList();
}