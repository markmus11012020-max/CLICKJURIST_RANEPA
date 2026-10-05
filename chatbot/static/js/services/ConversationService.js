/**
 * Сценарии диалога на клиенте: загрузка приветствия, отправка
 * сообщений, восстановление истории.
 *
 * Это единственный класс, который знает и про API, и про состояние.
 * UI-компоненты работают только с событиями.
 */
/** Маркер служебного ответа «генерация остановлена пользователем». */
const STOPPED_MARKER = '_stopped_';

export class ConversationService {
  /**
   * @param {object} deps
   * @param {import('./ApiClient.js').ApiClient} deps.api
   * @param {import('./StorageService.js').StorageService} deps.storage
   * @param {import('../core/StateStore.js').StateStore} deps.state
   * @param {import('../core/EventBus.js').EventBus} deps.bus
   * @param {import('./TypingService.js').TypingService} deps.typing
   * @param {number} [deps.historyLimit]
   */
  constructor({ api, storage, state, bus, typing, stream, historyLimit = 30 }) {
    this._api = api;
    this._storage = storage;
    this._state = state;
    this._bus = bus;
    this._typing = typing;
    this._stream = stream || null;
    this._historyLimit = historyLimit;
    /** @type {Array<object>} */
    this._messages = [];
    /** @type {AbortController|null} */
    this._abort = null;
  }

  /** @returns {string} идентификатор сессии */
  get sessionId() {
    return this._storage.sessionId();
  }

  /** @returns {Array<object>} текущая история сообщений */
  get messages() {
    return [...this._messages];
  }

  /**
   * Загрузить приветствие сервера и показать его в диалоге.
   * @returns {Promise<object|null>}
   */
  async loadGreeting() {
    try {
      const data = await this._api.greeting(this.sessionId);
      this._pushMessage({
        role: 'bot',
        text: data.text,
        quickReplies: data.quick_replies ?? [],
        createdAt: new Date().toISOString(),
      });
      this._state.set({ turnCount: this._state.get('turnCount') || 0 });
      this._persist();
      return data;
    } catch (err) {
      this._handleError(err);
      return null;
    }
  }

  /**
   * Отправить сообщение пользователя.
   *
   * Если подключён потоковый клиент — ответ печатается по мере
   * поступления, иначе используется обычный JSON-запрос.
   * @param {string} text
   * @param {string} [intentHint]
   * @returns {Promise<object|null>}
   */
  async send(text, intentHint) {
    const trimmed = String(text ?? '').trim();
    if (!trimmed) return null;

    this._pushMessage({
      role: 'user',
      text: trimmed,
      createdAt: new Date().toISOString(),
    });
    this._state.set({ isLoading: true, error: null });

    try {
      if (this._stream) return await this._sendStreamed(trimmed, intentHint);
      return await this._sendPlain(trimmed, intentHint);
    } finally {
      this._abort = null;
      this._state.set({ isLoading: false, isTyping: false });
    }
  }

  /**
   * Прервать генерацию ответа на лету.
   * @returns {boolean} было ли что прерывать
   */
  cancel() {
    if (!this._abort) return false;
    this._abort.abort();
    this._abort = null;
    return true;
  }

  /**
   * Обычный запрос без стриминга.
   * @param {string} text
   * @param {string} intentHint
   * @returns {Promise<object|null>}
   */
  async _sendPlain(text, intentHint) {
    await this._typing.wait();
    const data = await this._api.message(this.sessionId, text, intentHint);
    const reply = data?.reply;
    if (reply) this._pushMessage(this._toMessage(reply));
    this._state.set({ turnCount: data?.turn_count ?? this._state.get('turnCount') });
    this._persist();
    return data;
  }

  /**
   * Потоковый запрос: фрагменты приходят событиями `stream:delta`.
   * @param {string} text
   * @param {string} intentHint
   * @returns {Promise<object|null>}
   */
  async _sendStreamed(text, intentHint) {
    this._abort = new AbortController();

    return this._stream
      .send({
        sessionId: this.sessionId,
        message: text,
        intentHint,
        signal: this._abort.signal,
        onMeta: (event) => this._bus.emit('stream:meta', event),
        onDelta: (piece) => this._bus.emit('stream:delta', piece),
        onDone: (reply) => this._finishStream(reply),
      })
      .then(() => null)
      .catch((err) => {
        if (err.name === 'AbortError') {
          this._bus.emit('stream:done', { text: STOPPED_MARKER });
          return null;
        }
        this._pushMessage({
          role: 'bot',
          text: err.message || 'Не получилось получить ответ. Попробуйте ещё раз.',
          isError: true,
          createdAt: new Date().toISOString(),
        });
        this._handleError(err);
        return null;
      });
  }

  /**
   * Финализация потокового ответа.
   *
   * Сообщение только записывается в историю: в ленте его уже показал
   * «живой» пузырь, и второе сообщение продублировало бы ответ.
   * @param {object} reply
   */
  _finishStream(reply) {
    if (reply && reply.text !== STOPPED_MARKER) {
      this._messages.push(this._toMessage(reply));
      this._persist();
    }
    this._bus.emit('stream:done', reply);
  }

  /**
   * Привести DTO ответа к внутреннему виду сообщения.
   * @param {object} reply
   * @returns {object}
   */
  _toMessage(reply) {
    return {
      role: 'bot',
      text: reply.text ?? '',
      quickReplies: reply.quick_replies ?? [],
      actions: reply.actions ?? [],
      source: reply.source,
      isError: Boolean(reply.isError),
      createdAt: reply.created_at || new Date().toISOString(),
    };
  }

  /**
   * Очистить диалог и начать заново.
   */
  async reset() {
    this._messages = [];
    this._storage.remove('messages');
    this._bus.emit('messages:reset');
    try {
      await this._api.reset(this.sessionId);
    } catch (err) {
      console.warn('[chatbot] сброс диалога не дошёл до сервера', err);
    }
    await this.loadGreeting();
  }

  /**
   * Восстановить историю из локального хранилища (после перезагрузки).
   * @returns {Array<object>}
   */
  restore() {
    const saved = this._storage.get('messages', []);
    if (Array.isArray(saved) && saved.length) {
      this._messages = saved.slice(-this._historyLimit);
      this._bus.emit('messages:restored', this.messages);
    }
    return this.messages;
  }

  /**
   * Добавить сообщение в историю и сообщить UI.
   * @param {object} message
   */
  _pushMessage(message) {
    this._messages.push(message);
    this._bus.emit('message:new', message);
  }

  /** Сохранить историю локально. */
  _persist() {
    this._storage.set('messages', this._messages.slice(-this._historyLimit));
  }

  /**
   * Единая обработка ошибок: состояние + событие.
   * @param {Error} err
   */
  _handleError(err) {
    const message = err?.message || 'Что-то пошло не так';
    this._state.set({ error: message });
    this._bus.emit('error', message);
  }
}
