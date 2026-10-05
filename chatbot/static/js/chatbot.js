/**
 * Точка входа виджета чат-бота ClickJurist.
 *
 * Подключение к странице сайта — одна строка в `index.html`:
 *
 *   <script type="module" src="/chatbot/static/js/chatbot.js" defer></script>
 *
 * Либо программно:
 *
 *   import { createChatBot } from '/chatbot/static/js/chatbot.js';
 *   createChatBot({ mount: document.getElementById('chatbot-slot') });
 */
import { ChatBotApp } from './core/ChatBotApp.js';

export { ChatBotApp };

/** Экземпляр, созданный автозапуском, — чтобы не поднять второй. */
let instance = null;
/** Промис запуска: повторные вызовы обязаны дождаться готовности. */
let ready = null;

/**
 * Создать и запустить виджет.
 *
 * Повторный вызов не создаёт второй виджет, а возвращает тот же промис
 * запуска. Это важно для страниц, где модуль подключён тегом <script>
 * (автостарт) и одновременно вызывается вручную: иначе второй вызов
 * получит ещё не отрендеренный экземпляр.
 *
 * @param {object} [options] — см. конструктор {@link ChatBotApp}
 * @returns {Promise<ChatBotApp>}
 */
export function createChatBot(options = {}) {
  if (instance) return ready ?? Promise.resolve(instance);

  instance = new ChatBotApp(options);
  window.ClickJuristChatBot = instance;
  ready = instance.start();
  return ready;
}

/**
 * Автозапуск при подключении тегом <script> без явной инициализации.
 * Виджет не поднимется на служебных путях (/api, /docs) и при
 * `CHATBOT_ENABLED=false` — тот вернёт `enabled: false` в конфигурации.
 */
function autoStart() {
  const path = window.location.pathname;
  if (path.startsWith('/api') || path.startsWith('/docs')) return;
  if (window.ClickJuristChatBot) return;
  createChatBot().catch((err) => {
    console.error('[chatbot] не удалось запустить виджет', err);
  });
}

if (typeof document !== 'undefined') {
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', autoStart, { once: true });
  } else {
    autoStart();
  }
}
