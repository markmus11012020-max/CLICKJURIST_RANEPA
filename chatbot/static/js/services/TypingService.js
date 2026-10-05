/**
 * Имитация «бот печатает…».
 *
 * Отдельный класс, чтобы задержка перед появлением текста была в одном
 * месте: её легко подкрутить и легко отключить в тестах.
 */
export class TypingService {
  /**
   * @param {object} [options]
   * @param {number} [options.minDelayMs]
   * @param {number} [options.maxDelayMs]
   * @param {typeof setTimeout} [options.timer]
   */
  constructor({ minDelayMs = 400, maxDelayMs = 900, timer } = {}) {
    this._min = minDelayMs;
    this._max = maxDelayMs;
    this._timer = timer || ((fn, ms) => setTimeout(fn, ms));
  }

  /**
   * Выдержать правдоподобную паузу.
   * @returns {Promise<void>}
   */
  wait() {
    const delay = this._min + Math.random() * Math.max(0, this._max - this._min);
    return new Promise((resolve) => this._timer(resolve, Math.round(delay)));
  }
}
