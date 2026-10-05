/**
 * Работа с `localStorage`.
 *
 * Хранилище может быть недоступно (приватный режим Safari, отключённые
 * куки), поэтому все методы безопасны: при ошибке виджет работает без
 * сохранения состояния, но не падает.
 */

const PREFIX = 'clickjurist.chatbot.';

export class StorageService {
  /**
   * @param {Storage|null} [storage]
   */
  constructor(storage = safeLocalStorage()) {
    this._storage = storage;
  }

  /**
   * Прочитать значение.
   * @param {string} key
   * @param {*} [fallback]
   * @returns {*}
   */
  get(key, fallback = null) {
    if (!this._storage) return fallback;
    try {
      const raw = this._storage.getItem(PREFIX + key);
      return raw === null ? fallback : JSON.parse(raw);
    } catch {
      return fallback;
    }
  }

  /**
   * Записать значение.
   * @param {string} key
   * @param {*} value
   */
  set(key, value) {
    if (!this._storage) return;
    try {
      this._storage.setItem(PREFIX + key, JSON.stringify(value));
    } catch {
      /* приватный режим — молча игнорируем */
    }
  }

  /**
   * Удалить значение.
   * @param {string} key
   */
  remove(key) {
    if (!this._storage) return;
    try {
      this._storage.removeItem(PREFIX + key);
    } catch {
      /* игнорируем */
    }
  }

  /**
   * Идентификатор сессии: генерируется один раз на браузер.
   * @returns {string}
   */
  sessionId() {
    const existing = this.get('sessionId');
    if (typeof existing === 'string' && existing.length >= 8) return existing;
    const created = generateSessionId();
    this.set('sessionId', created);
    return created;
  }
}

/**
 * Сгенерировать идентификатор сессии.
 * @returns {string}
 */
function generateSessionId() {
  if (window.crypto?.randomUUID) {
    return `w${window.crypto.randomUUID().replace(/-/g, '')}`;
  }
  const bytes = new Uint8Array(16);
  window.crypto.getRandomValues(bytes);
  return `w${Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('')}`;
}

/**
 * Безопасно получить `localStorage`.
 * @returns {Storage|null}
 */
function safeLocalStorage() {
  try {
    const probe = '__cj_probe__';
    window.localStorage.setItem(probe, '1');
    window.localStorage.removeItem(probe);
    return window.localStorage;
  } catch {
    return null;
  }
}
