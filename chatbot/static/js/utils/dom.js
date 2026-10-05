/**
 * Небольшие помощники для работы с DOM.
 *
 * Вынесены отдельно, чтобы UI-классы не занимались строковыми
 * селекторами и созданием элементов вперемешку с логикой.
 */

/**
 * Создать элемент с классами, атрибутами и текстом.
 * @param {string} tag
 * @param {{className?: string, text?: string, attrs?: Object, html?: string}} [options]
 * @returns {HTMLElement}
 */
export function el(tag, options = {}) {
  const node = document.createElement(tag);
  const { className = '', text = '', attrs = {}, html = null } = options;
  if (className) node.className = className;
  if (html !== null) node.innerHTML = html;
  else if (text) node.textContent = text;
  Object.entries(attrs).forEach(([key, value]) => {
    if (value === false || value === null || value === undefined) return;
    node.setAttribute(key, value === true ? '' : String(value));
  });
  return node;
}

/**
 * Найти элемент по селектору.
 * @param {string} selector
 * @param {ParentNode} [root]
 * @returns {HTMLElement|null}
 */
export function qs(selector, root = document) {
  return root.querySelector(selector);
}

/**
 * Удалить все дочерние узлы.
 * @param {HTMLElement} node
 */
export function clear(node) {
  if (!node) return;
  while (node.firstChild) node.removeChild(node.firstChild);
}

/**
 * Добавить обработчик и вернуть функцию снятия.
 * @param {EventTarget} target
 * @param {string} type
 * @param {EventListener} handler
 * @param {object|boolean} [options]
 * @returns {() => void} функция снятия обработчика
 */
export function on(target, type, handler, options) {
  target.addEventListener(type, handler, options);
  return () => target.removeEventListener(type, handler, options);
}

/** Есть ли клавиатурный фокус внутри узла (нужно для автоскролла). */
export function hasFocusWithin(node) {
  if (!node) return false;
  const active = document.activeElement;
  return !!active && node.contains(active);
}
