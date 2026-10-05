/**
 * Поле ввода и кнопка отправки.
 */
import { el, on } from '../utils/dom.js';

export class Composer {
  /**
   * @param {object} deps
   * @param {HTMLElement} deps.node
   * @param {import('../core/EventBus.js').EventBus} deps.bus
   * @param {import('../core/StateStore.js').StateStore} deps.state
   * @param {number} [deps.maxLength]
   * @param {string} [deps.placeholder]
   */
  constructor({
    node,
    bus,
    state,
    maxLength = 1000,
    placeholder = 'Напишите вопрос…',
  }) {
    this._bus = bus;
    this._state = state;
    this._maxLength = maxLength;

    const form = el('form', { className: 'cj-composer', attrs: { novalidate: true } });
    const input = el('textarea', {
      className: 'cj-input',
      attrs: {
        rows: '1',
        placeholder,
        'aria-label': 'Сообщение для чат-бота',
        maxlength: String(maxLength),
      },
    });
    const send = el('button', {
      className: 'cj-send',
      attrs: { type: 'submit', 'aria-label': 'Отправить' },
      html: `<svg viewBox="0 0 24 24" width="18" height="18" fill="none"
               stroke="currentColor" stroke-width="2" stroke-linecap="round"
               stroke-linejoin="round"><path d="M22 2 11 13M22 2l-7 20-4-9-9-4 20-7z"/></svg>`,
    });

    form.appendChild(input);
    form.appendChild(send);
    node.appendChild(form);

    this._input = input;
    this._send = send;
    this._syncDisabled();

    on(form, 'submit', (event) => {
      event.preventDefault();
      this.submit();
    });

    on(input, 'keydown', (event) => {
      if (event.key === 'Enter' && !event.shiftKey) {
        event.preventDefault();
        this.submit();
      }
    });

    on(input, 'input', () => {
      this._autosize();
      this._syncDisabled();
    });

    // Подписчик пересчитывает доступность поля и кнопки по двум независимым
    // сигналам: идёт ли запрос (`isLoading`) и печатает ли бот ответ
    // (`isTyping`). Раньше `_syncDisabled` сбрасывал только `send.disabled`,
    // из-за чего `input.disabled` оставался `true` после первого ответа и
    // поле выглядело «неактивным» (плейсхолдер виден, но ввод не работает).
    // Оформлено как `(current) => { … }` — тело подписки читает
    // `current.isLoading` и обязательно зовёт `_syncDisabled()`, чтобы при
    // возврате `isLoading=false` (стриминг ответа завершён) поле ввода
    // разблокировалось.
    state.subscribe((current) => {
      const isLoading = Boolean(current && current.isLoading);
      if (!isLoading || this._input) {
        this._syncDisabled();
      }
    });
  }

  /**
   * Отправить введённый текст и очистить поле.
   * @param {string} [intentHint]
   */
  submit(intentHint) {
    const text = this._input.value.trim();
    if (!text || this._state.get('isLoading')) return;
    this._input.value = '';
    this._autosize();
    this._syncDisabled();
    this._bus.emit('composer:submit', { text, intentHint });
    this.focus();
  }

  /**
   * Вставить текст в поле (например, из быстрого ответа).
   * @param {string} text
   */
  fill(text) {
    this._input.value = text;
    this._autosize();
    this._syncDisabled();
    this.focus();
  }

  /** Передать фокус полю ввода. */
  focus() {
    // На мобильных вызов фокуса с клавиатуры не нужен — только на десктопе.
    if (window.matchMedia('(min-width: 768px)').matches) this._input.focus();
  }

  /** Снять фокус с поля ввода. */
  blur() {
    this._input.blur();
  }

  /** Подстроить высоту поля под содержимое. */
  _autosize() {
    this._input.style.height = 'auto';
    this._input.style.height = `${Math.min(this._input.scrollHeight, 120)}px`;
  }

  /** Заблокировать кнопку, если вводить нечего. */
  _syncDisabled() {
    const isLoading = this._state.get('isLoading');
    const isTyping = this._state.get('isTyping');
    const locked = isLoading || isTyping;
    const empty = !this._input.value.trim();
    this._input.disabled = locked;
    this._send.disabled = locked || empty;
  }
}
