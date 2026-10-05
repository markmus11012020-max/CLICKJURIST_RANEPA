/**
 * Индикатор «бот печатает…».
 */
import { el, clear } from '../utils/dom.js';

export class TypingIndicator {
  /**
   * @param {HTMLElement} node — контейнер, в котором живёт индикатор
   */
  constructor(node) {
    this._node = node;
    this._visible = false;
  }

  /** Показать индикатор. */
  show() {
    if (!this._node || this._visible) return;
    clear(this._node);
    const wrap = el('div', {
      className: 'cj-message cj-message--bot',
      attrs: { 'aria-live': 'polite', 'aria-label': 'Бот печатает' },
    });
    wrap.innerHTML = `
      <div class="cj-bubble cj-bubble--typing">
        <span class="cj-dot"></span>
        <span class="cj-dot"></span>
        <span class="cj-dot"></span>
      </div>
    `;
    this._node.appendChild(wrap);
    this._visible = true;
    this._node.scrollTop = this._node.scrollHeight;
  }

  /** Скрыть индикатор. */
  hide() {
    if (!this._node || !this._visible) return;
    clear(this._node);
    this._visible = false;
  }
}
