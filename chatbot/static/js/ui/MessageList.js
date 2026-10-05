/**
 * Лента сообщений: добавление, очистка, автоскролл.
 *
 * Стратегия видимости:
 *  - Во время стрима автоскролл «приклеен» к низу (`_shouldStick = true`),
 *    каждый фрагмент прокручивает ленту к хвосту, чтобы пользователь
 *    видел «живую печать».
 *  - По завершении стрима / при добавлении нового сообщения — конец
 *    всегда на виду: чек-лист, итоги и ссылки не уходят «в подвал».
 *  - Если пользователь сам проскроллил выше низа (читает историю),
 *    автоскролл «отклеивается» и появляется кнопка «↓ К концу»,
 *    чтобы вернуться к последней реплике одним тапом.
 */
import { clear, hasFocusWithin, on } from '../utils/dom.js';
import { MessageBubble } from './MessageBubble.js';
import { StreamingBubble } from './StreamingBubble.js';

export class MessageList {
  /**
   * @param {object} deps
   * @param {HTMLElement} deps.node
   * @param {import('../core/EventBus.js').EventBus} deps.bus
   * @param {import('./QuickReplies.js').QuickReplies} deps.quickReplies
   * @param {import('./TypingIndicator.js').TypingIndicator} deps.typing
   * @param {HTMLElement} [deps.scrollToBottomBtn] — кнопка «к новым сообщениям»
   */
  constructor({ node, bus, quickReplies, typing, scrollToBottomBtn }) {
    this._node = node;
    this._bus = bus;
    this._quick = quickReplies;
    this._typing = typing;
    this._scrollToBottomBtn = scrollToBottomBtn;
    this._shouldStick = true;
    /** @type {StreamingBubble|null} пузырь текущей генерации */
    this._live = null;
    /** Сколько сообщений пришло, пока пользователь был выше низа. */
    this._unreadCount = 0;

    node.addEventListener('scroll', () => {
      // Пока идёт стрим, scroll-события — это scrollToBottom из
      // appendDelta. Не позволяем им «отклеивать» автоскролл: иначе
      // пользователь не увидит «живую печать» (gap длинного ответа
      // быстро становится >80, _isNearBottom → false, _shouldStick →
      // false, scrollToBottom перестаёт работать).
      if (this._live) return;
      // Не трогаем флаг «у низа», пока фокус вне ленты: иначе любой
      // программный scroll (например, после setOpen) перезапишет его
      // в true, и пользователь снова оказывается у конца против воли.
      if (!hasFocusWithin(this._node)) return;
      this._shouldStick = this._isNearBottom();
      // Если пользователь вернулся к низу — сбрасываем счётчик
      // непрочитанных: он уже видит всё, что пришло.
      if (this._shouldStick) this._unreadCount = 0;
      this._refreshScrollButton();
    });

    if (this._scrollToBottomBtn) {
      on(this._scrollToBottomBtn, 'click', () => {
        this.scrollToBottom({ force: true });
      });
    }

    this._offMessage = bus.on('message:new', (message) => this.add(message));
    this._offMeta = bus.on('stream:meta', (meta) => this.startStream(meta));
    this._offDelta = bus.on('stream:delta', (piece) => this.appendDelta(piece));
    this._offDone = bus.on('stream:done', (reply) => this.finishStream(reply));
    this._offTyping = bus.on('state:loading', (isLoading) => {
      if (isLoading && !this._live) this._typing.show();
      else this._typing.hide();
    });
    this._offReset = bus.on('messages:reset', () => this.clear());
    this._offRestored = bus.on('messages:restored', (messages) => {
      this.clear();
      messages.forEach((message) => this.add(message, { scroll: false }));
      this.scrollToBottom({ force: true });
    });
  }

  /**
   * Добавить сообщение в ленту.
   * @param {object} message
   * @param {{scroll?: boolean}} [options]
   */
  add(message, { scroll = true } = {}) {
    const node = new MessageBubble(message).render();
    this._node.appendChild(node);

    const isBot = message.role !== 'user';
    if (isBot) {
      // Во время генерации кнопки появляются только в конце потока,
      // иначе они мигали бы на каждом фрагменте.
      if (!this._live) this._quick.render(message.quickReplies || []);
    } else {
      this._quick.clear();
    }
    if (!scroll) return;

    // И для своего сообщения, и для ответа бота — прокручиваем к низу:
    // пользователь должен видеть, что пришло. Конец ответа (чек-лист,
    // ссылки, итоги) остаётся на виду — это решает проблему «ответ
    // ушёл в подвал».
    this._stickToEnd();
  }

  /** Начать показ «живой печати»: создать пустой пузырь бота. */
  startStream(meta) {
    this._typing.hide();
    if (this._live) return;
    this._live = new StreamingBubble();
    this._live.mount(this._node);
    this._quick.clear();
    // Свежий стрим = пользователь точно ждёт у нижнего края.
    this._shouldStick = true;
  }

  /**
   * Дописать фрагмент в текущий пузырь.
   * @param {string} piece
   */
  appendDelta(piece) {
    if (!this._live) this.startStream();
    this._live.append(piece);
    // Автоскролл к концу стрима нужен, иначе он не виден сразу: новые
    // фрагменты появляются ниже viewport, и пользователь не заметит
    // «живую печать».
    this.scrollToBottom();
  }

  /**
   * Завершить печать.
   * @param {object} [reply] — финальный ответ с быстрыми ответами
   */
  finishStream(reply) {
    if (!this._live) return;
    const stopped = reply?.text === '_stopped_';
    this._live.finish(stopped ? undefined : reply?.text);
    if (reply?.isError) this._live.markError();
    this._live = null;
    // Сервер отдаёт snake_case: быстрые ответы лежат в quick_replies.
    this._quick.render(reply?.quick_replies || reply?.quickReplies || []);
    // Конец ответа — на виду: чек-лист, итоги, ссылки.
    this._stickToEnd();
  }

  /**
   * Прокрутить ленту к самому последнему сообщению с принудительной
   * «приклейкой» (`_shouldStick = true`), чтобы дальнейшие фрагменты
   * стрима не «отлипали». Используется при добавлении готового
   * сообщения — пользователь ждал его у нижнего края.
   */
  _stickToEnd() {
    this._shouldStick = true;
    this._unreadCount = 0;
    // Используем rAF: если лента ещё не зафиксировала новую высоту
    // после appendChild, scrollHeight вернёт значение «до вставки»,
    // и пользователь увидит старый низ вместо нового сообщения.
    requestAnimationFrame(() => {
      this._node.scrollTop = this._node.scrollHeight;
      if (this._scrollToBottomBtn) {
        this._scrollToBottomBtn.hidden = true;
        this._refreshCounter(0);
      }
    });
  }

  /** Очистить ленту. */
  clear() {
    this._live = null;
    this._unreadCount = 0;
    clear(this._node);
    this._quick.clear();
    this._typing.hide();
    if (this._scrollToBottomBtn) {
      this._scrollToBottomBtn.hidden = true;
      this._refreshCounter(0);
    }
  }

  /**
   * Показать/скрыть кнопку «к новым сообщениям» в правом нижнем углу
   * ленты. Появляется, когда пользователь проскроллил выше низа и
   * хочет быстро вернуться к последнему ответу.
   */
  _refreshScrollButton() {
    if (!this._scrollToBottomBtn) return;
    if (this._live) {
      this._scrollToBottomBtn.hidden = true;
      return;
    }
    const nearBottom = this._isNearBottom();
    this._scrollToBottomBtn.hidden = nearBottom;
  }

  /**
   * Прокрутить к последнему сообщению.
   * @param {{force?: boolean}} [opts] — `force: true` игнорирует
   *   `_shouldStick` (для открытия чата / программной прокрутки).
   */
  scrollToBottom({ force = false } = {}) {
    if (!force && !this._shouldStick) return;
    this._shouldStick = true;
    this._unreadCount = 0;
    requestAnimationFrame(() => {
      this._node.scrollTop = this._node.scrollHeight;
      if (this._scrollToBottomBtn) {
        this._scrollToBottomBtn.hidden = true;
        this._refreshCounter(0);
      }
    });
  }

  /** Находится ли лента у нижнего края. */
  _isNearBottom() {
    if (!hasFocusWithin(this._node)) return true;
    const gap = this._node.scrollHeight - this._node.scrollTop - this._node.clientHeight;
    return gap < 80;
  }

  /**
   * Положение ленты ДО вставки нового сообщения.
   *
   * Отличается от {@link _isNearBottom}: та учитывает фокус внутри ленты
   * (чтобы печать в поле ввода не «отклеивала» автоскролл). Здесь фокус
   * нерелевантен — нам важна только реальная позиция скролла.
   * @returns {boolean}
   */
  _wasNearBottom() {
    const list = this._node;
    const gap = list.scrollHeight - list.scrollTop - list.clientHeight;
    return gap < 80;
  }

  /**
   * Обновить счётчик «новых сообщений» на кнопке прокрутки.
   * Если есть элемент `.cj-scroll-btn__count` внутри кнопки — пишем
   * число. Если нет (старая разметка) — не падаем.
   * @param {number} count
   */
  _refreshCounter(count) {
    if (!this._scrollToBottomBtn) return;
    const counter = this._scrollToBottomBtn.querySelector('.cj-scroll-btn__count');
    if (!counter) return;
    if (count > 0) {
      counter.textContent = String(count);
      counter.hidden = false;
    } else {
      counter.textContent = '';
      counter.hidden = true;
    }
  }

  /** Снять все подписки. */
  destroy() {
    this._offMessage?.();
    this._offMeta?.();
    this._offDelta?.();
    this._offDone?.();
    this._offTyping?.();
    this._offReset?.();
    this._offRestored?.();
  }
}