/**
 * Ряд кнопок быстрого ответа под сообщением бота.
 */
import { el, on, clear } from '../utils/dom.js';

export class QuickReplies {
  /**
   * @param {object} deps
   * @param {HTMLElement} deps.node
   * @param {import('../core/EventBus.js').EventBus} deps.bus
   */
  constructor({ node, bus }) {
    this._node = node;
    this._bus = bus;
  }

  /**
   * Отрисовать набор кнопок.
   * @param {Array<{id: string, label: string}>} replies
   */
  render(replies) {
    clear(this._node);
    if (!Array.isArray(replies) || !replies.length) return;

    const group = el('div', {
      className: 'cj-quick',
      attrs: { role: 'group', 'aria-label': 'Быстрые ответы' },
    });

    replies.forEach((reply) => {
      const button = el('button', {
        className: 'cj-chip',
        text: reply.label,
        attrs: { type: 'button' },
      });
      on(button, 'click', () => {
        this._bus.emit('quick:reply', reply);
      });
      group.appendChild(button);
    });

    this._node.appendChild(group);
    this._node.scrollTop = this._node.scrollHeight;
  }

  /** Убрать кнопки. */
  clear() {
    clear(this._node);
  }
}
