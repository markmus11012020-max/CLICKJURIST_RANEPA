/**
 * Минимальная шина событий.
 *
 * Нужна, чтобы UI-компоненты не знали друг о друге: `ChatBotApp`
 * публикует события, подписанты реагируют. Это же место, где позже
 * подключается аналитика — достаточно добавить один подписчик.
 */
export class EventBus {
  constructor() {
    /** @type {Map<string, Set<Function>>} */
    this._listeners = new Map();
  }

  /**
   * Подписаться на событие.
   * @param {string} event
   * @param {Function} handler
   * @returns {() => void} функция отписки
   */
  on(event, handler) {
    if (!this._listeners.has(event)) this._listeners.set(event, new Set());
    this._listeners.get(event).add(handler);
    return () => this.off(event, handler);
  }

  /**
   * Подписаться на одно срабатывание.
   * @param {string} event
   * @param {Function} handler
   * @returns {() => void}
   */
  once(event, handler) {
    const off = this.on(event, (...args) => {
      off();
      handler(...args);
    });
    return off;
  }

  /**
   * Отписаться от события.
   * @param {string} event
   * @param {Function} handler
   */
  off(event, handler) {
    this._listeners.get(event)?.delete(handler);
  }

  /**
   * Опубликовать событие. Ошибка одного подписчика не должна ломать
   * остальных и тем более интерфейс.
   * @param {string} event
   * @param {...any} args
   */
  emit(event, ...args) {
    this._listeners.get(event)?.forEach((handler) => {
      try {
        handler(...args);
      } catch (err) {
        console.error(`[chatbot] ошибка подписчика "${event}"`, err);
      }
    });
  }

  /** Отписать всех подписчиков (при уничтожении виджета). */
  clear() {
    this._listeners.clear();
  }
}
