/**
 * Клиент потокового ответа (SSE поверх fetch).
 *
 * `EventSource` не подходит: он умеет только GET, а тело запроса
 * (текст сообщения) нужно передать POST-ом. Поэтому разбираем поток
 * вручную — парсер держит неполный кадр между вызовами.
 */

/** Разделитель кадров SSE. */
const FRAME_DELIMITER = '\n\n';

export class StreamClient {
  /**
   * @param {object} [options]
   * @param {string} [options.baseUrl]
   * @param {typeof fetch} [options.fetchImpl]
   */
  constructor({ baseUrl = '/api/chatbot', fetchImpl } = {}) {
    this._baseUrl = baseUrl.replace(/\/$/, '');
    this._fetch = fetchImpl || ((...args) => fetch(...args));
  }

  /**
   * Отправить сообщение и вызвать обработчики по мере поступления ответа.
   *
   * @param {object} params
   * @param {string} params.sessionId
   * @param {string} params.message
   * @param {string} [params.intentHint]
   * @param {(text: string) => void} params.onDelta — фрагмент текста
   * @param {(event: object) => void} [params.onMeta] — начало ответа
   * @param {(reply: object) => void} [params.onDone] — финальный ответ
   * @param {AbortSignal} [params.signal] — отмена по требованию пользователя
   * @returns {Promise<void>}
   */
  async send({ sessionId, message, intentHint, onDelta, onMeta, onDone, signal }) {
    const response = await this._fetch(`${this._baseUrl}/stream`, {
      method: 'POST',
      signal,
      headers: {
        'Content-Type': 'application/json',
        Accept: 'text/event-stream',
      },
      body: JSON.stringify({ session_id: sessionId, message, intent_hint: intentHint }),
    });

    if (!response.ok) {
      const payload = await safeJson(response);
      throw new Error(payload?.detail || `Ошибка ${response.status}`);
    }
    if (!response.body) {
      throw new Error('Браузер не поддерживает потоковое чтение ответа.');
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder('utf-8');
    let buffer = '';

    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const frames = buffer.split(FRAME_DELIMITER);
      // Посний фрагмент может быть неполным — возвращаем его в буфер.
      buffer = frames.pop() ?? '';

      frames.forEach((frame) => this._handleFrame(frame, { onDelta, onMeta, onDone }));
    }

    if (buffer.trim()) {
      this._handleFrame(buffer, { onDelta, onMeta, onDone });
    }
  }

  /**
   * Разобрать один кадр и вызвать нужный обработчик.
   * @param {string} frame
   * @param {{onDelta: Function, onMeta?: Function, onDone?: Function}} handlers
   */
  _handleFrame(frame, { onDelta, onMeta, onDone }) {
    const trimmed = frame.trim();
    if (!trimmed.startsWith('data:')) return;

    let event;
    try {
      event = JSON.parse(trimmed.slice(5).trim());
    } catch {
      return; // повреждённый кадр пропускаем, поток не прерываем
    }

    switch (event.type) {
      case 'meta':
        onMeta?.(event);
        break;
      case 'delta':
        onDelta(event.text ?? '');
        break;
      case 'done':
        onDone?.(event.reply);
        break;
      case 'error':
        onDone?.({ text: event.message || 'Ошибка генерации', isError: true });
        break;
      default:
        break;
    }
  }
}

/**
 * Разобрать ответ, не падая на не-JSON.
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
