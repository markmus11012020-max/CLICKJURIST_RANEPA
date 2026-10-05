/**
 * Минимальный Markdown-рендерер для ответов чат-бота.
 *
 * Намеренно без сторонних зависимостей и без `innerHTML` для
 * пользовательского текста: сначала экранируем всё, затем размечаем
 * только заранее известные конструкции. Это защита от XSS, а не
 * «просто на всякий случай».
 */

/** @type {Record<string, string>} */
const ESCAPES = {
  '&': '&amp;',
  '<': '&lt;',
  '>': '&gt;',
  '"': '&quot;',
  "'": '&#39;',
};

/**
 * Экранировать HTML-спецсимволы.
 * @param {string} text
 * @returns {string}
 */
export function escapeHtml(text) {
  return String(text ?? '').replace(/[&<>"']/g, (ch) => ESCAPES[ch]);
}

/**
 * Отформатировать время сообщения.
 * @param {Date|number|string} value
 * @returns {string} «чч:мм»
 */
export function formatTime(value) {
  const date = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  return date.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
}

/**
 * Превратить текст ответа в безопасный HTML.
 * Поддерживаются: `**жирный**`, `*курсив*`, маркированные списки,
 * пустые строки как абзацы и ссылки вида `[текст](https://…)`.
 * @param {string} text
 * @returns {string} HTML-строка
 */
export function renderMarkdown(text) {
  const safe = escapeHtml(text).replace(/\r\n/g, '\n');

  const withLinks = safe.replace(
    /\[([^\]]+)]\((https?:\/\/[^\s)]+)\)/g,
    '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>',
  );

  const blocks = withLinks.split(/\n{2,}/).map((block) => {
    const lines = block.split('\n');
    const isList = lines.every((line) => /^\s*[•*-]\s+/.test(line));
    if (isList && lines.length > 0) {
      const items = lines
        .map((line) => `<li>${inline(line.replace(/^\s*[•*-]\s+/, ''))}</li>`)
        .join('');
      return `<ul class="cj-list">${items}</ul>`;
    }
    if (lines.length > 1 && lines.every((line) => line.trim() === '')) {
      return '';
    }
    const joined = lines
      .map((line) => (line.trim() === '' ? '' : inline(line)))
      .join('<br>');
    return joined.trim() ? `<p>${joined}</p>` : '';
  });

  return blocks.join('');
}

/**
 * Инлайновая разметка внутри строки.
 * @param {string} line
 * @returns {string}
 */
function inline(line) {
  return line
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>');
}
