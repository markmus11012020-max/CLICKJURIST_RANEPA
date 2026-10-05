/**
 * Пузырь сообщения, который умеет «печатать» текст по мере поступления.
 *
 * Используется потоковым режимом: текст приходит фрагментами, и метод
 * `append` дописывает его без перерисовки всей ленты.
 */
import { el } from '../utils/dom.js';
import { renderMarkdown, formatTime } from '../utils/markdown.js';

export class StreamingBubble {
  /**
   * @param {object} [message] — сообщение для мгновенного рендера
   */
  constructor(message = null) {
    this._text = message?.text ?? '';
    this._isError = Boolean(message?.isError);
    this._role = message?.role ?? 'bot';

    const isBot = this._role !== 'user';
    this._row = el('div', {
      className: `cj-message cj-message--${isBot ? 'bot' : 'user'}`,
      attrs: { role: 'listitem' },
    });

    this._bubble = el('div', { className: 'cj-bubble is-streaming' });
    this._bubble.innerHTML = renderMarkdown(this._text);

    this._meta = el('div', {
      className: 'cj-message__meta',
      text: formatTime(message?.createdAt || Date.now()),
    });

    this._row.appendChild(this._bubble);
    this._row.appendChild(this._meta);
  }

  /**
   * Вставить в список сообщений.
   * @param {HTMLElement} container
   * @returns {HTMLElement}
   */
  mount(container) {
    container.appendChild(this._row);
    return this._row;
  }

  /**
   * Дописать фрагмент текста.
   * @param {string} piece
   */
  append(piece) {
    if (!piece) return;
    this._text += piece;
    this._bubble.innerHTML = renderMarkdown(this._text);
  }

  /**
   * Завершить печать: убрать курсор и зафиксировать итог.
   * @param {string} [finalText] — итоговый текст, если он известен
   */
  finish(finalText) {
    if (finalText !== undefined && finalText !== null) this._text = finalText;
    this._bubble.innerHTML = renderMarkdown(this._text);
    this._bubble.classList.remove('is-streaming');
  }

  /**
   * Пометить сообщение как ошибочное.
   */
  markError() {
    this._row.classList.add('is-error');
    this._bubble.classList.remove('is-streaming');
  }

  /** @returns {string} накопленный текст */
  get text() {
    return this._text;
  }

  /** @returns {HTMLElement} узел сообщения */
  get node() {
    return this._row;
  }
}
