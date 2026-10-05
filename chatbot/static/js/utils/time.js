/**
 * Форматирование времени и текста для интерфейса чат-бота.
 */

import { formatTime } from './markdown.js';

export { formatTime };

/**
 * Короткая подпись «сейчас / 5 мин назад».
 * @param {Date|number|string} value
 * @returns {string}
 */
export function relativeTime(value) {
  const date = value instanceof Date ? value : new Date(value);
  const diffSec = Math.floor((Date.now() - date.getTime()) / 1000);
  if (Number.isNaN(diffSec) || diffSec < 60) return 'сейчас';
  if (diffSec < 3600) return `${Math.floor(diffSec / 60)} мин назад`;
  if (diffSec < 86400) return `${Math.floor(diffSec / 3600)} ч назад`;
  return date.toLocaleDateString('ru-RU');
}

/**
 * Привести текст к одной строке и обрезать.
 * @param {string} text
 * @param {number} [limit]
 * @returns {string}
 */
export function truncate(text, limit = 60) {
  const flat = String(text ?? '').replace(/\s+/g, ' ').trim();
  if (flat.length <= limit) return flat;
  return `${flat.slice(0, limit - 1)}…`;
}

/**
 * Склонение существительного: 1 вопрос, 2 вопроса, 5 вопросов.
 * @param {number} count
 * @param {string} one
 * @param {string} few
 * @param {string} many
 * @returns {string}
 */
export function plural(count, one, few, many) {
  const mod10 = count % 10;
  const mod100 = count % 100;
  if (mod10 === 1 && mod100 !== 11) return `${count} ${one}`;
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 10 || mod100 >= 20)) {
    return `${count} ${few}`;
  }
  return `${count} ${many}`;
}
