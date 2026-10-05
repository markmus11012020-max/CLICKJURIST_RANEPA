/* ClickJurist Production - клиентский модуль (точка входа).
 *
 * Логика разнесена по ES-модулям в frontend/js/ — состояние, сетевой
 * слой и шаги сервиса больше не лежат в одном файле.
 *
 * Модуль чат-бота подключается отдельно: /chatbot/static/js/chatbot.js
 */
import { bind } from './js/app.js';
import { loadLegal } from './js/ui/legal.js';
import { closePaywall } from './js/ui/paywall.js';
import { loadSession } from './js/ui/session.js';

/** Запуск после готовности DOM. */
function start() {
  // 1. Гарантированно скрыть paywall ДО любых сетевых вызовов и рендера чипа.
  //    Даже если бы на нём случайно оказался класс активности - здесь он
  //    снимается, и пользователь видит чистый экран консультации.
  closePaywall();
  // 2. Затем привязать обработчики и загрузить состояние сессии.
  bind();
  loadSession();
  loadLegal();
}

// Модульные скрипты выполняются с задержкой, поэтому DOMContentLoaded
// к этому моменту мог уже сработать — тогда стартуем сразу.
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', start, { once: true });
} else {
  start();
}
