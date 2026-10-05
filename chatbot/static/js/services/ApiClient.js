/**
 * HTTP-клиент чат-бота.
 *
 * Единственное место, где виджет знает про сеть: и адреса, и таймауты,
 * и обработку ошибок собраны здесь, а не размазаны по UI-компонентам.
 */
export class ApiClient {
  /**
   * @param {object} options
   * @param {string} options.baseUrl
   * @param {number} [options.timeoutMs]
   * @param {typeof fetch} [options.fetchImpl]
   */
  constructor({ baseUrl = '/api/chatbot', timeoutMs = 30000, fetchImpl } = {}) {
    this._baseUrl = baseUrl.replace(/\/$/, '');
    this._timeoutMs = timeoutMs;
    this._fetch = fetchImpl || ((...args) => fetch(...args));
  }

  /**
   * Загрузить конфигурацию виджета.
   * @returns {Promise<object>}
   */
  config() {
    return this._request('GET', '/config');
  }

  /**
   * Получить приветствие.
   * @param {string} sessionId
   * @returns {Promise<object>}
   */
  greeting(sessionId) {
    return this._request('GET', `/greeting?session_id=${encodeURIComponent(sessionId)}`);
  }

  /**
   * Отправить сообщение.
   * @param {string} sessionId
   * @param {string} message
   * @param {string} [intentHint]
   * @returns {Promise<object>}
   */
  message(sessionId, message, intentHint) {
    const query = intentHint
      ? `?intent_hint=${encodeURIComponent(intentHint)}`
      : '';
    return this._request('POST', `/message${query}`, {
      session_id: sessionId,
      message,
    });
  }

  /**
   * Очистить диалог.
   * @param {string} sessionId
   * @returns {Promise<object>}
   */
  reset(sessionId) {
    return this._request('POST', `/reset?session_id=${encodeURIComponent(sessionId)}`);
  }

  /**
   * Базовый запрос с таймаутом и разбором ошибок.
   * @param {'GET'|'POST'} method
   * @param {string} path
   * @param {object} [body]
   * @returns {Promise<object>}
   */
  async _request(method, path, body) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this._timeoutMs);
    try {
      const response = await this._fetch(`${this._baseUrl}${path}`, {
        method,
        signal: controller.signal,
        headers: {
          Accept: 'application/json',
          ...(body ? { 'Content-Type': 'application/json' } : {}),
        },
        ...(body ? { body: JSON.stringify(body) } : {}),
      });

      const payload = await safeJson(response);
      if (!response.ok) {
        throw new ChatApiError(
          payload?.detail || payload?.error || `Ошибка ${response.status}`,
          response.status,
        );
      }
      return payload;
    } catch (err) {
      if (err.name === 'AbortError') {
        throw new ChatApiError('Сервер не ответил вовремя. Попробуйте ещё раз.', 408);
      }
      if (err instanceof ChatApiError) throw err;
      throw new ChatApiError('Нет связи с сервером. Проверьте подключение.', 0);
    } finally {
      clearTimeout(timer);
    }
  }
}

/** Ошибка обращения к API чат-бота. */
export class ChatApiError extends Error {
  /**
   * @param {string} message
   * @param {number} status
   */
  constructor(message, status) {
    super(message);
    this.name = 'ChatApiError';
    this.status = status;
  }
}

/**
 * Разобрать ответ, не падая на не-JSON (например, HTML-страница 502).
 * @param {Response} response
 * @returns {Promise<object|null>}
 */
async function safeJson(response) {
  try {
    return await response.json();
  } catch {
    return null;
  }
}
