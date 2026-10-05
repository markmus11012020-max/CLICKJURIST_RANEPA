/**
 * Подключение CSS виджета.
 *
 * Путь вычисляется от `import.meta.url`, поэтому виджет работает из
 * любого префикса раздачи (`/chatbot/static`, CDN, поддомен) без
 * правок в коде. Повторные вызовы не дублируют тег <link>.
 *
 * Версия в query-строке — против агрессивного кэша браузера: при
 * каждом изменении стилей её нужно поднять здесь, иначе пользователь
 * продолжит видеть старую вёрстку после рефреша.
 */

const STYLE_FILES = ['css/tokens.css', 'css/chatbot.css'];
// Версия CSS: инкрементируется при каждом изменении внешнего вида.
// Сейчас: окно 440×728 (+10% ширина, +30% высота), кнопка «↓ К концу»
// с подписью и счётчиком, scroll-snap в ленте, кнопка прокрутки увеличена
// до 58px высота (+30%) с более толстой иконкой — v10.
const STYLE_VERSION = '10';
let loaded = false;

/**
 * Вставить стили виджета в <head>.
 * @param {string} [baseUrl] — база с директорией `js/`
 */
export function ensureStyles(baseUrl) {
  if (loaded || typeof document === 'undefined') return;
  // Модуль лежит в `<static>/js/utils/`, стили — в `<static>/css/`.
  // Отсюда до каталога `css/` — два уровня вверх: '../..'.
  const base = baseUrl || new URL('../../', import.meta.url).href;
  loaded = true;

  STYLE_FILES.forEach((file) => {
    const link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = new URL(`${file}?v=${STYLE_VERSION}`, base).href;
    document.head.appendChild(link);
  });
}
