/**
 * Хранилище состояния виджета.
 *
 * Простой наблюдаемый объект: компоновка и UI читают состояние через
 * подписку, а не через прямые обращения друг к другу.
 */
export class StateStore {
  /**
   * @param {object} [initial]
   */
  constructor(initial = {}) {
    this._state = {
      enabled: true,
      isOpen: false,
      isLoading: false,
      isTyping: false,
      hasUnread: false,
      isAutoOpenUsed: false,
      turnCount: 0,
      error: null,
      ...initial,
    };
    /** @type {Set<Function>} */
    this._subscribers = new Set();
  }

  /**
   * Текущее состояние (только для чтения).
   * @returns {object}
   */
  get state() {
    return { ...this._state };
  }

  /**
   * Прочитать одно поле.
   * @param {string} key
   * @returns {any}
   */
  get(key) {
    return this._state[key];
  }

  /**
   * Обновить состояние и уведомить подписчиков.
   * @param {Partial<object>} patch
   */
  set(patch) {
    const changed = Object.keys(patch).filter((key) => this._state[key] !== patch[key]);
    if (!changed.length) return;
    Object.assign(this._state, patch);
    this._subscribers.forEach((fn) => fn(this.state, changed));
  }

  /**
   * Подписаться на изменения.
   * @param {Function} handler
   * @returns {() => void} отписка
   */
  subscribe(handler) {
    this._subscribers.add(handler);
    handler(this.state, Object.keys(this._state));
    return () => this._subscribers.delete(handler);
  }
}
