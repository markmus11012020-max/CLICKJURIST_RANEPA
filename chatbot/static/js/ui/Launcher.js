/**
 * Плавающая кнопка запуска чат-бота.
 */
import { el, on } from '../utils/dom.js';

export class Launcher {
  /**
   * @param {object} deps
   * @param {import('../core/EventBus.js').EventBus} deps.bus
   * @param {string} [deps.label]
   */
  constructor({ bus, label = 'Задать вопрос' }) {
    this._bus = bus;
    this._label = label;
    this._node = null;
    this._unsubscribe = [];
  }

  /**
   * Встроить кнопку в контейнер.
   * @param {HTMLElement} root
   */
  mount(root) {
    const button = el('button', {
      className: 'cj-launcher',
      attrs: {
        type: 'button',
        'aria-label': this._label,
        title: this._label,
        'aria-expanded': 'false',
      },
    });
    button.innerHTML = `
      <span class="cj-launcher__icon" aria-hidden="true">
        <svg viewBox="0 0 24 24" width="26" height="26" fill="none"
             stroke="currentColor" stroke-width="2"
             stroke-linecap="round" stroke-linejoin="round">
          <path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/>
        </svg>
      </span>
      <span class="cj-launcher__text">${this._label}</span>
      <span class="cj-launcher__badge" hidden aria-hidden="true"></span>
    `;

    this._node = button;
    root.appendChild(button);

    this._unsubscribe.push(
      on(button, 'click', () => this._bus.emit('widget:toggle')),
      this._bus.on('widget:open', () => this.setOpen(true)),
      this._bus.on('widget:close', () => this.setOpen(false)),
      this._bus.on('widget:minimize', () => this.setOpen(false)),
      this._bus.on('notification:show', () => this.showBadge()),
    );
    return this;
  }

  /**
   * Переключить состояние «окно открыто».
   * @param {boolean} isOpen
   */
  setOpen(isOpen) {
    if (!this._node) return;
    this._node.classList.toggle('is-hidden', isOpen);
    this._node.setAttribute('aria-expanded', String(isOpen));
  }

  /** Показать метку непрочитанного сообщения. */
  showBadge() {
    if (!this._node) return;
    this._node.classList.add('has-badge');
    const badge = this._node.querySelector('.cj-launcher__badge');
    if (badge) badge.hidden = false;
  }

  /** Снять метку непрочитанного сообщения. */
  hideBadge() {
    if (!this._node) return;
    this._node.classList.remove('has-badge');
    const badge = this._node.querySelector('.cj-launcher__badge');
    if (badge) badge.hidden = true;
  }
}
