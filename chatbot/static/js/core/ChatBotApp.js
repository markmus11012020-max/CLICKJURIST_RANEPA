/**
 * Ядро виджета: сборка зависимостей, жизненный цикл, обработка
 * событий между UI и сервисами.
 *
 * Здесь нет ни DOM-логики, ни сетевых вызовов — только связывание.
 */
import { EventBus } from './EventBus.js';
import { StateStore } from './StateStore.js';
import { ApiClient } from '../services/ApiClient.js';
import { StreamClient } from '../services/StreamClient.js';
import { StorageService } from '../services/StorageService.js';
import { TypingService } from '../services/TypingService.js';
import { ConversationService } from '../services/ConversationService.js';
import { ChatPanel } from '../ui/ChatPanel.js';
import { Launcher } from '../ui/Launcher.js';
import { MessageList } from '../ui/MessageList.js';
import { el } from '../utils/dom.js';
import { ensureStyles } from '../utils/styles.js';

const DISMISS_KEY = 'dismissed';
const DISMISS_TTL_DAYS = 7;

export class ChatBotApp {
  /**
   * @param {object} [options]
   * @param {HTMLElement} [options.mount] — корневой узел (создаётся, если не задан)
   * @param {string} [options.apiBase]
   * @param {string} [options.launcherLabel]
   */
  constructor(options = {}) {
    this.bus = new EventBus();
    this.state = new StateStore();
    this.storage = new StorageService();

    this.api = new ApiClient({ baseUrl: options.apiBase || '/api/chatbot' });
    this.stream = new StreamClient({ baseUrl: options.apiBase || '/api/chatbot' });
    this.typing = new TypingService();

    this.conversation = new ConversationService({
      api: this.api,
      storage: this.storage,
      state: this.state,
      bus: this.bus,
      typing: this.typing,
      stream: this.stream,
    });

    this._config = { brand: 'КликЮрист', ...options };
    this._mount = options.mount || createRoot();
    this._panel = null;
    this._list = null;
    this._launcher = null;
    this._autoOpenTimer = null;
  }

  /**
   * Запустить виджет: загрузить конфигурацию, разметить интерфейс,
   * показать приветствие.
   * @returns {Promise<ChatBotApp>}
   */
  async start() {
    await this._loadConfig();
    this._render();
    this._wire();

    this.conversation.restore();
    await this._loadGreeting();
    this._scheduleAutoOpen();
    return this;
  }

  /** Загрузить конфигурацию с сервера; при ошибке — значения по умолчанию. */
  async _loadConfig() {
    try {
      const config = await this.api.config();
      this._config = { ...this._config, ...config };
      this.state.set({ enabled: config.enabled !== false });
    } catch (err) {
      console.warn('[chatbot] конфигурация не загружена, работаем по умолчанию', err);
    }
  }

  /** Разметить интерфейс. */
  _render() {
    ensureStyles();

    const root = el('div', { className: 'cj-root' });
    this._mount.appendChild(root);

    this._launcher = new Launcher({
      bus: this.bus,
      label: this._config.launcherLabel || 'Задать вопрос',
    }).mount(root);

    this._panel = new ChatPanel({
      root,
      bus: this.bus,
      state: this.state,
      config: this._config,
    });

    this._list = new MessageList({
      node: this._panel.node.querySelector('.cj-list'),
      bus: this.bus,
      quickReplies: this._panel.quickReplies,
      typing: this._panel.typing,
      scrollToBottomBtn: this._panel._scrollToBottomBtn,
    });
  }

  /** Связать события и действия. */
  _wire() {
    this.state.subscribe((current, changed) => {
      if (changed.includes('isLoading')) this.bus.emit('state:loading', current.isLoading);
    });

    this.bus.on('widget:toggle', () => {
      this.state.get('isOpen') ? this.minimize() : this.open();
    });
    this.bus.on('widget:open', () => this.open());
    this.bus.on('widget:close', () => this.close());
    this.bus.on('widget:minimize', () => this.minimize());

    this.bus.on('composer:submit', async ({ text, intentHint }) => {
      await this.conversation.send(text, intentHint);
    });

    // Кнопка «Остановить» появляется, пока идёт генерация.
    this.bus.on('stream:meta', () => this._setStoppable(true));
    this.bus.on('stream:done', () => this._setStoppable(false));
    this.bus.on('state:loading', (isLoading) => {
      if (!isLoading) this._setStoppable(false);
    });

    this.bus.on('quick:reply', async (reply) => {
      this._panel.quickReplies.clear();
      await this.conversation.send(reply.label, reply.id);
    });

    this.bus.on('dialog:reset', async () => {
      this.conversation.cancel();
      await this.conversation.reset();
    });

    this.bus.on('stream:stop', () => {
      this.conversation.cancel();
    });

    this.bus.on('error', (message) => {
      console.warn('[chatbot]', message);
    });
  }

  /** Показать окно чата. */
  open() {
    if (this._autoOpenTimer) {
      clearTimeout(this._autoOpenTimer);
      this._autoOpenTimer = null;
    }
    this.state.set({ isOpen: true, hasUnread: false });
    this._panel.setOpen(true);
    this._launcher.setOpen(true);
    this._launcher.hideBadge();
    // Принудительно прокручиваем к низу: пользователь открыл чат —
    // значит, он хочет видеть последнее сообщение, даже если перед
    // этим читал историю и ушёл наверх.
    this._list.scrollToBottom({ force: true });
  }

  /** Закрыть окно чата и запомнить выбор. */
  close() {
    this.state.set({ isOpen: false });
    this._panel.setOpen(false);
    this._launcher.setOpen(false);
    if (this._config.rememberClose !== false) {
      this.storage.set(DISMISS_KEY, buildMarker(true));
    }
  }

  /**
   * Свернуть окно чата в пузырь-лаунчер. История и сессия сохраняются,
   * пользователь может вернуться к диалогу в любой момент. В отличие от
   * {@link close}, не помечает чат как «закрытый» в этом визите.
   */
  minimize() {
    this.state.set({ isOpen: false });
    this._panel.setOpen(false);
    this._launcher.setOpen(false);
  }

  /**
   * Показать или спрятать кнопку остановки генерации.
   * @param {boolean} visible
   */
  _setStoppable(visible) {
    const button = this._panel?.node.querySelector('.cj-stop');
    if (!button) return;
    button.hidden = !visible;
    // Имя поля — `state`. Раньше здесь стоял несуществующий вариант с
    // подчёркиванием: подписчик падал на каждом `stream:meta`, из-за чего
    // кнопка остановки не появлялась, а ошибка маскировала остальное.
    this.state.set({ isTyping: visible });
  }

  /** Загрузить приветствие; при недоступности показать запасной текст. */
  async _loadGreeting() {
    if (this.conversation.messages.length) return;
    const data = await this.conversation.loadGreeting();
    if (!data) {
      this._list.add({
        role: 'bot',
        text:
          'Здравствуйте! Сейчас не получается получить приветствие — ' +
          'попробуйте обновить страницу.',
        createdAt: new Date().toISOString(),
      });
    }
  }

  /**
   * Открыть окно автоматически при первом визите.
   * Открытие не повторяется, если пользователь уже закрывал чат.
   */
  _scheduleAutoOpen() {
    if (this._config.auto_open === false) return;
    if (isDismissed(this.storage.get(DISMISS_KEY))) return;

    const delay = Number(this._config.greeting_delay_ms ?? 1200);
    this._autoOpenTimer = setTimeout(() => {
      this._autoOpenTimer = null;
      this.open();
    }, delay);
  }

  /** Снести виджет и освободить ресурсы. */
  destroy() {
    if (this._autoOpenTimer) clearTimeout(this._autoOpenTimer);
    this._list?.destroy();
    this.bus.clear();
    this._mount.innerHTML = '';
  }
}

/**
 * Собрать маркер закрытия для localStorage: ``<ts>|<0|1>``.
 * @param {boolean} dismissed
 * @returns {string}
 */
function buildMarker(dismissed) {
  return `${Math.floor(Date.now() / 1000)}|${dismissed ? '1' : '0'}`;
}

/**
 * Разобрать маркер закрытия: ``<unix-ts>|<0|1>``.
 * Просроченный маркер считается отсутствующим.
 * @param {string|null|undefined} marker
 * @returns {boolean}
 */
function isDismissed(marker) {
  if (typeof marker !== 'string') return false;
  const [rawTs, flag] = marker.split('|');
  const ts = Number(rawTs);
  if (!Number.isFinite(ts) || flag !== '1') return false;
  const ageDays = (Date.now() / 1000 - ts) / 86_400;
  return ageDays < DISMISS_TTL_DAYS;
}

/**
 * Найти контейнер виджета: заданный страницей или созданный по умолчанию.
 *
 * Наличие элемента с id `chatbot-slot` — соглашение для интеграции:
 * страница может разместить виджет в нужном месте разметки.
 * @returns {HTMLElement}
 */
function createRoot() {
  const existing = document.getElementById('chatbot-slot');
  if (existing) return existing;

  const node = document.createElement('div');
  node.id = 'clickjurist-chatbot';
  document.body.appendChild(node);
  return node;
}
