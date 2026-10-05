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
import { api } from './js/core/net.js';
import { bindWizard } from './js/features/wizard.js';
import { pollWizardUntilReady } from './js/ui/state.js';

/**
 * Восстановить процесс визарда после перезагрузки страницы (F5).
 *
 * Сценарий: пользователь отправил запрос на STAGE_1 и ушёл со страницы
 * (или у него случилась перезагрузка), а фоновая задача на бэкенде ещё
 * крутится. На свежей загрузке мы спрашиваем /api/wizard/state и, если
 * бэкенд вернул `has_active_task: true`, переключаем UI на экран
 * лоадера и подхватываем polling — чтобы клиент сам дошёл до STAGE_2,
 * когда бэкенд закончит работу. Никаких висящих setTimeout'ов: polling
 * подписан на AbortController и сам отменяется при переходе дальше.
 */
async function _recoverActiveWizardTask() {
  try {
    const { ok, data } = await api('GET', '/api/wizard/state');
    if (!ok || !data) return;
    if (data.has_active_task !== true) return;

    // Переключаем UI на экран лоадера сразу — пользователь не должен
    // видеть пустую форму STAGE_1, пока задача ещё крутится.
    const loader = document.querySelector('[data-wizard-loader]');
    if (loader) {
      loader.hidden = false;
      if (typeof loader.scrollIntoView === 'function') {
        loader.scrollIntoView({ behavior: 'smooth', block: 'center' });
      }
    }

    // Обновим прогресс-бар из текущего снимка, чтобы он не висел на 0%.
    const fill = document.querySelector('[data-wizard-progress-fill]');
    const lbl = document.querySelector('[data-wizard-progress-label]');
    const startProgress = Number(data.task_progress || 0);
    if (fill) fill.style.width = startProgress + '%';
    if (lbl) lbl.textContent = startProgress + '%';

    // Подхватываем polling. По готовности бэкенд сам переведёт Wizard
    // на STAGE_2 (см. pollWizardUntilReady → wizardState.setStage).
    pollWizardUntilReady({
      onProgress: (p) => {
        const f = document.querySelector('[data-wizard-progress-fill]');
        const t = document.querySelector('[data-wizard-progress-label]');
        if (f) f.style.width = p + '%';
        if (t) t.textContent = p + '%';
      },
    });
  } catch (err) {
    // Не блокируем запуск приложения сетевой ошибкой.
    console.warn('[entry] failed to recover active wizard task:', err);
  }
}

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
  // 3. Привязать визард (bindWizard() внутри сам монтирует wizardState,
  //    навешивает обработчики форм/кнопок и регистрирует cleanup при
  //    pagehide/beforeunload).
  bindWizard();
  // 4. Восстановить процесс визарда, если бэкенд уже работает над задачей
  //    (F5 во время STAGE_1 — пользователь вернётся к лоадеру, а не к форме).
  _recoverActiveWizardTask();
}

// Модульные скрипты выполняются с задержкой, поэтому DOMContentLoaded
// к этому моменту мог уже сработать — тогда стартуем сразу.
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', start, { once: true });
} else {
  start();
}