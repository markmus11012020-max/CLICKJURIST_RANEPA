/**
 * Одно сообщение в ленте чата.
 */
import { el } from '../utils/dom.js';
import { renderMarkdown, formatTime } from '../utils/markdown.js';

export class MessageBubble {
  /**
   * @param {object} message
   * @param {string} message.role — 'bot' | 'user'
   * @param {string} message.text
   * @param {string} [message.createdAt]
   * @param {boolean} [message.isError]
   */
  constructor(message) {
    this._message = message;
  }

  /**
   * Построить DOM-узел сообщения.
   * @returns {HTMLElement}
   */
  render() {
    const { role, text, createdAt, isError } = this._message;
    const isBot = role !== 'user';

    const row = el('div', {
      className: `cj-message cj-message--${isBot ? 'bot' : 'user'}${isError ? ' is-error' : ''}`,
      attrs: { role: 'listitem' },
    });

    const bubble = el('div', { className: 'cj-bubble' });
    if (isBot) {
      // innerHTML допустим: renderMarkdown предварительно экранирует текст.
      bubble.innerHTML = renderMarkdown(text);
    } else {
      bubble.textContent = text;
    }

    const meta = el('div', {
      className: 'cj-message__meta',
      text: formatTime(createdAt || Date.now()),
    });

    row.appendChild(bubble);
    row.appendChild(meta);
    return row;
  }
}
