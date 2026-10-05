/**
 * Wizard UI — клиентский стейт-машина пошагам и polling-цикл (Стадия 2).
 *
 * Архитектурно отделено от `core/state.js` (там живёт «бизнес»-стейт сессии
 * и тарифов). Здесь — представленческий слой Wizard'а: видимая стадия,
 * локальная карта галочек и видимость секций в DOM.
 *
 * Контракт:
 *   wizardState.getStage()           -> 'STAGE_1' | 'STAGE_2' | 'STAGE_3'
 *   wizardState.setStage(stage)      — мгновенно переключает DOM
 *   wizardState.setChecklist(obj)    — полный снимок из сервера
 *   wizardState.patchChecklistKey(k,v) — локальный апдейт одной галочки
 *   wizardState.getChecklistState()  -> {key: bool}
 *   wizardState.subscribe(fn)        — observer, snapshot, dt {{stage,checklist}}
 *
 *   pollWizardUntilReady({ onProgress, intervalMs=2000 })
 *     — цикл опроса /api/wizard/state; на has_active_task=false переводит
 *       Wizard на СТАРЫЙ STAGE_2 и применяет снимок чек-листа с сервера.
 *
 * Соглашения:
 *   * Никаких таймаутов дольше 30 с: при «зависшей» задаче бэкенд сам
 *     переведёт её в failed, и следующий poll-ответ даст has_active_task=false.
 *   * visibility/display идёт через атрибут `hidden`, а не display:none,
 *     чтобы не конфликтовать с CSS-сеткой `[hidden]{display:none!important}`
 *     (см. style.css — глобальное).
 *   * При смене стадии скроллим в начало секции: пользователь видит
 *     «новый экран», а не середину предыдущего.
 */

import { api } from '../core/net.js';

/** Упорядоченный список стадий — для расчёта is-done у пунктов степпера. */
export const STAGE_ORDER = ['STAGE_1', 'STAGE_2', 'STAGE_3'];

/** Человекочитаемые названия стадий (для счётчика «Шаг N из 3: …»). */
const STAGE_LABELS = {
  STAGE_1: 'Ситуация',
  STAGE_2: 'Стратегия',
  STAGE_3: 'Документ',
};

/**
 * Класс-обсервер: единая точка правды для UI-представления Wizard'а.
 * Иммутабельный снаружи (ЭФФ), и его мутации идут только через сеттеры — чтобы
 * тесты могли проверять state.isolated().
 */
export class WizardState {
  constructor() {
    /** @type {'STAGE_1' | 'STAGE_2' | 'STAGE_3'} */
    this._stage = 'STAGE_1';
    /** @type {Map<string, boolean>} */
    this._checklist = new Map();
    /** @type {Set<(snapshot) => void>} */
    this._listeners = new Set();
    /** Подтверждение, что DOM-секции уже отрендерены (для безопасного setStage). */
    this._mounted = false;
  }

  // --- Getters --------------------------------------------------------------
  getStage() {
    return this._stage;
  }

  /** Вернуть чек-лист плоским объектом (для POST на сервер). */
  getChecklistState() {
    return Object.fromEntries(this._checklist);
  }

  /** Снимок всего состояния — для логов и отладки. */
  snapshot() {
    return {
      stage: this._stage,
      checklist: this.getChecklistState(),
      mounted: this._mounted,
    };
  }

  // --- Setters --------------------------------------------------------------
  /**
   * Переключить Wizard на активную (видимую) стадию.
   * Idempotent: повторный вызов с той же стадией — no-op (без notify).
   * @param {'STAGE_1' | 'STAGE_2' | 'STAGE_3'} stage
   */
  setStage(stage) {
    if (!STAGE_ORDER.includes(stage)) {
      console.warn('[wizard] неизвестная стадия:', stage);
      return;
    }
    if (this._stage === stage) return;
    this._stage = stage;
    if (this._mounted) {
      this._applyStageVisibility();
      this._scrollToCurrentSection();
    }
    this._notify();
  }

  /** Полная замена чек-листа (после ответа сервера). */
  setChecklist(state) {
    this._checklist = new Map(Object.entries(state || {}));
    this._notify();
  }

  /** Локальный апдейт одной галочки (используется чекбоксом при клике). */
  patchChecklistKey(key, value) {
    if (!key) return;
    this._checklist.set(key, Boolean(value));
    this._notify();
  }

  /** Сбросить всё в начальное состояние (для «Начать новую консультацию»). */
  reset() {
    this._stage = 'STAGE_1';
    this._checklist.clear();
    if (this._mounted) this._applyStageVisibility();
    this._notify();
  }

  // --- Observer API ---------------------------------------------------------
  /**
   * Подписаться на изменения. Возвращает функцию отписки.
   * @param {(snapshot: {stage: string, checklist: object}) => void} fn
   */
  subscribe(fn) {
    this._listeners.add(fn);
    // Сразу отдать текущий снимок — чтобы UI не показывал пустоту до первого set.
    try {
      fn({ stage: this._stage, checklist: this.getChecklistState() });
    } catch (err) {
      console.error('[wizard] subscriber error on subscribe:', err);
    }
    return () => this._listeners.delete(fn);
  }

  _notify() {
    const snap = { stage: this._stage, checklist: this.getChecklistState() };
    this._listeners.forEach((fn) => {
      try {
        fn(snap);
      } catch (err) {
        console.error('[wizard] subscriber error:', err);
      }
    });
  }

  // --- DOM-интеграция -------------------------------------------------------
  /**
   * Привязаться к DOM. После этого setStage() реально переключает секции.
   * Вызывается один раз из entry-point (script.js).
   */
  mount() {
    if (this._mounted) return;
    this._applyStageVisibility();
    this._mounted = true;
  }

  /**
   * Применить текущую стадию к DOM:
   *   * всем <section data-stage=…> — атрибут hidden;
   *   * всем <li data-wizard-stage=…> — классы is-current / is-done;
   *   * счётчику «Шаг N из 3: …» — текст.
   */
  _applyStageVisibility() {
    document.querySelectorAll('[data-stage]').forEach((el) => {
      el.hidden = el.dataset.stage !== this._stage;
    });
    const currentIdx = STAGE_ORDER.indexOf(this._stage);
    document.querySelectorAll('[data-wizard-stage]').forEach((el) => {
      const itemIdx = STAGE_ORDER.indexOf(el.dataset.wizardStage);
      el.classList.toggle('is-current', itemIdx === currentIdx);
      el.classList.toggle('is-done', itemIdx >= 0 && itemIdx < currentIdx);
    });
    const counter = document.querySelector('[data-wizard-counter]');
    if (counter) {
      const idx = currentIdx + 1;
      counter.textContent = `Шаг ${idx} из 3: ${STAGE_LABELS[this._stage]}`;
    }
  }

  /**
   * Скролл к активной секции — вызывается после смены стадии,
   * чтобы пользователь видел «новый экран», а не хвост предыдущего.
   */
  _scrollToCurrentSection() {
    const target = document.querySelector(`[data-stage="${this._stage}"]`);
    if (target && typeof target.scrollIntoView === 'function') {
      target.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
  }
}

/** Синглтон — общий для всего приложения (аналогично `state` в core/state.js). */
export const wizardState = new WizardState();

// ---------------------------------------------------------------------------
// Polling /api/wizard/state
// ---------------------------------------------------------------------------

/**
 * Активные polling-циклы: ключ — session_id, значение — AbortController.
 * Хранилище позволяет отменить поллинг при unmount/route-change, не утекая
 * в setInterval. Используется в :func:`stopWizardPoll`.
 */
const _activePolls = new Map();

/**
 * Опросить /api/wizard/state; если в ответе has_active_task=true — продолжить
 * через `intervalMs` (дефолт 2000 мс); если false — перевести Wizard на
 * STAGE_2, применить серверный снимок чек-листа и вернуть результат.
 *
 * ВАЖНО:
 *   * polling «снимается» при AbortController.abort() — никаких висящих
 *     setTimeout после перехода на следующий шаг;
 *   * при сетевом сбое делаем ещё одну попытку через тот же интервал,
 *     но не больше MAX_ATTEMPTS подряд (страховка от зависшего бэкенда).
 *   * скролл к экрану ожидания делается ОДИН раз — в самом первом тике,
 *     чтобы не дёргать вьюпорт каждые 2 секунды.
 *
 * @param {object} [opts]
 * @param {number} [opts.intervalMs=2000]
 * @param {(progress:number)=>void} [opts.onProgress]
 * @param {(state:object)=>void} [opts.onReady]
 * @param {string} [opts.sessionId] — ключ для stopWizardPoll (по умолчанию 'default')
 * @returns {{ abort: () => void, promise: Promise<object|null> }}
 */
export function pollWizardUntilReady(opts = {}) {
  const {
    intervalMs = 2000,
    onProgress,
    onReady,
    sessionId = 'default',
  } = opts;

  // Если уже идёт polling для этой сессии — отменим предыдущий, чтобы не было
  // гонки двух циклов, пишущих в один и тот же DOM.
  stopWizardPoll(sessionId);

  const controller = new AbortController();
  const promise = (async () => {
    let attempts = 0;
    const MAX_ATTEMPTS = 90; // 90 × 2с = 3 минуты — дальше считаем зависшим

    // Скроллим к экрану ожидания ОДИН раз.
    const loader = document.querySelector('[data-wizard-loader]');
    if (loader) {
      loader.hidden = false;
      loader.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }

    while (!controller.signal.aborted && attempts < MAX_ATTEMPTS) {
      attempts += 1;
      try {
        const { ok, data } = await api('GET', '/api/wizard/state');
        if (!ok || !data) {
          // сетевая ошибка / 5xx — пробуем ещё раз через intervalMs
          await sleep(intervalMs, controller.signal);
          continue;
        }

        if (data.has_active_task) {
          if (typeof onProgress === 'function') {
            try {
              onProgress(Number(data.task_progress || 0));
            } catch (err) {
              console.error('[wizard] onProgress error:', err);
            }
          }
          await sleep(intervalMs, controller.signal);
          continue;
        }

        // Задача завершилась — применяем снимок.
        wizardState.setChecklist((data.session && data.session.checklist_state) || {});
        wizardState.setStage('STAGE_2');
        if (loader) loader.hidden = true;
        if (typeof onReady === 'function') {
          try {
            onReady(data);
          } catch (err) {
            console.error('[wizard] onReady error:', err);
          }
        }
        return data;
      } catch (err) {
        if (controller.signal.aborted) return null;
        console.warn('[wizard] poll error:', err);
        await sleep(intervalMs, controller.signal);
      }
    }
    // Истёк лимит попыток — НЕ падаем, просто возвращаем null.
    console.warn('[wizard] polling: MAX_ATTEMPTS reached');
    return null;
  })();

  _activePolls.set(sessionId, { controller, promise });
  promise.finally(() => {
    // Чистим реестр, если это та же самая сессия (мог быть заменён новой).
    const current = _activePolls.get(sessionId);
    if (current && current.controller === controller) {
      _activePolls.delete(sessionId);
    }
  });

  return {
    abort: () => controller.abort(),
    promise,
  };
}

/**
 * Остановить активный polling (если он был запущен).
 * @param {string} [sessionId='default']
 */
export function stopWizardPoll(sessionId = 'default') {
  const entry = _activePolls.get(sessionId);
  if (!entry) return;
  entry.controller.abort();
  _activePolls.delete(sessionId);
}

/** Прервать ВСЕ polling-циклы — например, при переходе на маршрут away. */
export function stopAllWizardPolls() {
  _activePolls.forEach((entry) => entry.controller.abort());
  _activePolls.clear();
}

/** Помощник: «поспать», учитывая AbortSignal. */
function sleep(ms, signal) {
  return new Promise((resolve, reject) => {
    if (signal && signal.aborted) {
      reject(new DOMException('aborted', 'AbortError'));
      return;
    }
    const t = setTimeout(() => {
      if (signal) signal.removeEventListener('abort', onAbort);
      resolve();
    }, ms);
    const onAbort = () => {
      clearTimeout(t);
      reject(new DOMException('aborted', 'AbortError'));
    };
    if (signal) signal.addEventListener('abort', onAbort, { once: true });
  });
}