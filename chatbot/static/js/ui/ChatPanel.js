/**
 * Окно чат-бота: шапка, лента сообщений, поле ввода.
 *
 * Собирает остальные компоненты и управляет показом/скрытием окна.
 */
import { el, clear, on } from '../utils/dom.js';
import { Composer } from './Composer.js';
import { QuickReplies } from './QuickReplies.js';
import { TypingIndicator } from './TypingIndicator.js';

export class ChatPanel {
  /**
   * @param {object} deps
   * @param {HTMLElement} deps.root
   * @param {import('../core/EventBus.js').EventBus} deps.bus
   * @param {import('../core/StateStore.js').StateStore} deps.state
   * @param {object} [deps.config]
   */
  constructor({ root, bus, state, config = {} }) {
    this._bus = bus;
    this._state = state;
    this._config = config;

    const panel = el('section', {
      className: 'cj-panel',
      attrs: {
        role: 'dialog',
        'aria-modal': 'false',
        'aria-label': 'Чат-бот КликЮрист',
        hidden: true,
      },
    });

    const header = el('header', { className: 'cj-header' });
    const title = el('div', { className: 'cj-header__title' });
    // Эмблема — инлайновый SVG, а не символ-эмодзи весов: тот
    // отрисовывается системным шрифтом по-разному (в Chrome это
    // «два мешочка»), из-за чего аватар выглядел сломанным.
    // SVG выглядит одинаково во всех браузерах.
    title.innerHTML = `
      <span class="cj-avatar" aria-hidden="true">
        <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor"
             stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
          <path d="M12 3v18M7 21h10M12 6 4 8m8-2 8 2"/>
          <path d="M4 8l-2.5 5.5A3.5 3.5 0 0 0 8.5 13.5L6 8M20 8l2.5 5.5A3.5 3.5 0 0 1 15.5 13.5L18 8"/>
        </svg>
      </span>
      <span>
        <strong>${escapeText(config.brand || 'КликЮрист')}</strong>
        <small class="cj-status"><i aria-hidden="true"></i> Онлайн · отвечает по функционалу сайта</small>
      </span>
    `;

    const controls = el('div', { className: 'cj-header__controls' });
    const stopBtn = el('button', {
      className: 'cj-icon-btn cj-stop',
      text: '⏸',
      attrs: {
        type: 'button',
        title: 'Остановить ответ',
        'aria-label': 'Остановить генерацию ответа',
      },
    });
    stopBtn.hidden = true;
    const resetBtn = el('button', {
      className: 'cj-icon-btn',
      text: '⟳',
      attrs: { type: 'button', title: 'Начать заново', 'aria-label': 'Начать диалог заново' },
    });
    const minimizeBtn = el('button', {
      className: 'cj-icon-btn cj-minimize',
      text: '—',
      attrs: { type: 'button', title: 'Свернуть в пузырь', 'aria-label': 'Свернуть чат в пузырь' },
    });
    const closeBtn = el('button', {
      className: 'cj-icon-btn',
      text: '✕',
      attrs: { type: 'button', title: 'Закрыть', 'aria-label': 'Закрыть чат' },
    });
    controls.appendChild(stopBtn);
    controls.appendChild(minimizeBtn);
    controls.appendChild(resetBtn);
    controls.appendChild(closeBtn);

    header.appendChild(title);
    header.appendChild(controls);

    const body = el('div', { className: 'cj-body' });
    const list = el('div', {
      className: 'cj-list',
      attrs: { role: 'list', 'aria-live': 'polite' },
    });
    // Плавающая кнопка «к новым сообщениям» — появляется в правом нижнем
    // углу ленты, когда пользователь проскроллил выше низа (например,
    // читает историю). На мобильных касаться узкого скроллбара неудобно,
    // поэтому крупная кнопка с явным действием и подписью. Внутри —
    // счётчик, в который MessageList пишет количество непрочитанных.
    const scrollToBottomBtn = el('button', {
      className: 'cj-scroll-btn',
      attrs: {
        type: 'button',
        title: 'К новым сообщениям',
        'aria-label': 'Прокрутить к новым сообщениям',
      },
      html: `
        <svg viewBox="0 0 24 24" width="20" height="20" fill="none"
             stroke="currentColor" stroke-width="2.4"
             stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <path d="M6 9l6 6 6-6"/>
        </svg>
        <span class="cj-scroll-btn__label">К концу</span>
        <span class="cj-scroll-btn__count" aria-hidden="true" hidden></span>
      `,
    });
    scrollToBottomBtn.hidden = true;
    const quick = el('div', { className: 'cj-quick-slot' });
    const typingSlot = el('div', { className: 'cj-typing-slot' });
    const footer = el('div', { className: 'cj-footer' });

    body.appendChild(list);
    body.appendChild(scrollToBottomBtn);
    body.appendChild(quick);
    body.appendChild(typingSlot);
    body.appendChild(footer);

    panel.appendChild(header);
    panel.appendChild(body);
    root.appendChild(panel);

    this._node = panel;
    this._list = list;
    this._footer = footer;
    this._scrollToBottomBtn = scrollToBottomBtn;

    this.quickReplies = new QuickReplies({ node: quick, bus });
    this.typing = new TypingIndicator(typingSlot);
    this.composer = new Composer({
      node: footer,
      bus,
      state,
      maxLength: config.maxMessageLength || 1000,
    });

    // Дисклеймер — над полем ввода, чтобы был виден у каждого ответа
    // и не занимал место в ленте сообщений.
    if (config.disclaimer) {
      footer.prepend(
        el('p', { className: 'cj-disclaimer', text: config.disclaimer }),
      );
    }

    on(closeBtn, 'click', () => this._bus.emit('widget:close'));
    on(resetBtn, 'click', () => this._bus.emit('dialog:reset'));
    on(stopBtn, 'click', () => this._bus.emit('stream:stop'));
    on(minimizeBtn, 'click', () => this._bus.emit('widget:minimize'));
    on(document, 'keydown', (event) => {
      if (event.key === 'Escape' && this._state.get('isOpen')) {
        this._bus.emit('widget:close');
      }
    });
  }

  /** @returns {HTMLElement} корневой узел окна */
  get node() {
    return this._node;
  }

  /**
   * Показать окно.
   * @param {boolean} isOpen
   */
  setOpen(isOpen) {
    this._node.hidden = !isOpen;
    this._node.classList.toggle('is-open', isOpen);
    if (isOpen) this.composer.focus();
    else this.composer.blur();
  }

  /** Показать приветственный экран-заглушку до ответа сервера. */
  showPlaceholder(text) {
    clear(this._list);
    this._list.appendChild(
      el('div', { className: 'cj-message cj-message--bot' }, {
        html: `<div class="cj-bubble">${escapeText(text)}</div>`,
      }),
    );
  }
}

/**
 * Экранировать текст для вставки в готовый шаблон.
 * @param {string} text
 * @returns {string}
 */
function escapeText(text) {
  return String(text ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}
