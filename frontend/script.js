/* ClickJurist Production - клиентский модуль.
 * Состояние, сетевой слой, UX-обвязка. Никаких внешних зависимостей.
 */
(function () {
  "use strict";

  const API_BASE = window.location.origin;
  const LS_FP = "clickjurist_fp";

  /** Лёгкий стабильный отпечаток браузера (без cookies). */
  function fingerprint() {
    const cached = localStorage.getItem(LS_FP);
    if (cached) return cached;
    const data = [
      navigator.userAgent,
      navigator.language,
      screen.width + "x" + screen.height + "@" + (window.devicePixelRatio || 1),
      new Date().getTimezoneOffset(),
      (navigator.platform || ""),
    ].join("|");
    let hash = 5381;
    for (let i = 0; i < data.length; i++) hash = ((hash << 5) + hash) + data.charCodeAt(i);
    const value = ("fp_" + (hash >>> 0).toString(16));
    try { localStorage.setItem(LS_FP, value); } catch (_) {}
    return value;
  }

  const headers = {
    "Content-Type": "application/json",
    "X-Client-Fingerprint": fingerprint(),
  };

  async function api(method, path, body) {
    const init = { method, headers: { ...headers }, credentials: "include" };
    if (body !== undefined) init.body = JSON.stringify(body);
    const resp = await fetch(API_BASE + path, init);
    let data = null;
    const text = await resp.text();
    try { data = text ? JSON.parse(text) : null; } catch (_) { data = { raw: text }; }
    return { status: resp.status, ok: resp.ok, data, headers: resp.headers };
  }

  /** Шаблоны быстрых пресетов (Шаг 2 ТЗ prompt170926.md: 6 тегов, 50/50 B2C/B2B). */
  const QUICK_TAG_TEMPLATES = {
    // --- B2C: бытовые потребности ---
    refund_goods:
      "Купил смартфон в магазине «[ORG_1]» за 75 000 ₽. Через 10 дней обнаружил заводской брак — " +
      "не работает камера. Продавец отказывается вернуть деньги, предлагает только ремонт. " +
      "Чек и гарантийный талон на руках. Хочу вернуть уплаченную сумму, неустойку и компенсацию морального вреда.",
    labor_rights:
      "Работодатель ООО «[ORG_1]» систематически нарушает трудовое право: задерживает заработную плату " +
      "3 месяца, не предоставляет отпуск, не оформляет трудовой договор в полном объёме. " +
      "Зарплата «в конверте» 80 000 ₽. Хочу взыскать задолженность, компенсацию по ст. 236 ТК РФ " +
      "и привлечь работодателя к административной ответственности.",
    gibdd_fine:
      "Получил постановление ГИБДД от 22.09.2026 о штрафе за превышение скорости. " +
      "Считаю штраф незаконным: в момент фиксации нарушения автомобилем управлял не я, " +
      "а мой коллега по доверенности. Хочу обжаловать постановление в суде и отменить штраф.",
    // --- B2B: корпоративное право и для юристов ---
    debt_recovery:
      "Контрагент ООО «[ORG_1]» (ИНН [INN_1]) не оплатил поставленный товар по договору поставки " +
      "№ [CASE_1] от 01.08.2026 на сумму 1 200 000 ₽. Срок оплаты истёк 30.09.2026. " +
      "Претензию направили, ответа нет. Хочу взыскать задолженность, неустойку по ст. 395 ГК РФ " +
      "и расходы на юридическую помощь через Арбитражный суд.",
    ooo_meeting:
      "Участник ООО «[ORG_1]» с долей 30% уставного капитала. 15.10.2026 проведено внеочередное " +
      "общее собрание участников, на которое я не был надлежащим образом уведомлён. " +
      "Приняты решения об одобрении крупной сделки по отчуждению недвижимого имущества " +
      "общества по заниженной цене. Хочу обжаловать решение собрания по ст. 43 ФЗ «Об ООО» " +
      "и признать сделку недействительной.",
    b2b_audit:
      "Необходим правовой аудит договора поставки № [CASE_1] между ООО «[ORG_1]» (покупатель) " +
      "и ООО «[ORG_2]» (поставщик) на сумму 5 000 000 ₽. Договор содержит спорные условия " +
      "об ограничении ответственности поставщика, одностороннем изменении цены и арбитражной " +
      "оговорке. Хочу получить экспертное заключение о рисках и рекомендации по корректировке " +
      "существенных условий перед подписанием.",
  };

  /** Подставить шаблон пресета в поле ввода. */
  function applyQuickTag(tagKey) {
    const template = QUICK_TAG_TEMPLATES[tagKey];
    if (!template) return;
    const input = $("queryInput");
    if (input) {
      input.value = template;
      input.focus();
      // Прокрутить к форме.
      const card = input.closest(".card");
      if (card) card.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  }

  function $(id) { return document.getElementById(id); }
  function show(el) { if (el) el.hidden = false; }
  function hide(el) { if (el) el.hidden = true; }
  function setStatus(el, text, kind) {
    if (!el) return;
    el.textContent = text || "";
    el.classList.remove("error", "success");
    if (kind === "error") el.classList.add("error");
    if (kind === "success") el.classList.add("success");
  }
  function markdownToHtml(text) {
    if (!text) return "";
    const escape = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

    // Блочную разметку (списки, заголовки, параграфы) разбираем построчно,
    // чтобы маркеры "*"/"•" не «съедались» инлайн-форматированием и не
    // превращались в одну сплошную строку.
    const lines = escape(text).split("\n");
    const bulletRe = /^[\*•]\s+(.+)$/;
    const h3Re = /^###\s+(.+)$/;
    const h2Re = /^##\s+(.+)$/;
    const h1Re = /^#\s+(.+)$/;

    const blocks = [];
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      if (bulletRe.test(line)) {
        const items = [];
        while (i < lines.length && bulletRe.test(lines[i])) {
          items.push("<li>" + lines[i].replace(bulletRe, "$1") + "</li>");
          i++;
        }
        blocks.push("<ul>" + items.join("") + "</ul>");
        continue;
      }
      const h3 = line.match(h3Re);
      if (h3) { blocks.push("<h3>" + h3[1] + "</h3>"); i++; continue; }
      const h2 = line.match(h2Re);
      if (h2) { blocks.push("<h2>" + h2[1] + "</h2>"); i++; continue; }
      const h1 = line.match(h1Re);
      if (h1) { blocks.push("<h1>" + h1[1] + "</h1>"); i++; continue; }
      if (line.trim() === "") { i++; continue; }
      // Обычный текстовый блок: собираем подряд идущие непустые строки,
      // не начинающиеся с маркера списка/заголовка. Одиночные \n → <br>.
      const paraLines = [line];
      i++;
      while (
        i < lines.length &&
        lines[i].trim() !== "" &&
        !bulletRe.test(lines[i]) &&
        !h3Re.test(lines[i]) &&
        !h2Re.test(lines[i]) &&
        !h1Re.test(lines[i])
      ) {
        paraLines.push(lines[i]);
        i++;
      }
      blocks.push("<p>" + paraLines.join("<br>") + "</p>");
    }

    let html = blocks.join("");
    // Инлайн-форматирование — после блочной разметки, чтобы ** внутри <li>
    // корректно превращались в <strong>, а не «ломали» парсер списков.
    html = html.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
    html = html.replace(/`([^`]+)`/g, "<code>$1</code>");
    return html;
  }

  // Начальное состояние: чип сразу показывает «Бесплатно: 1 из 1», пока бэкенд
  // ещё не ответил на /api/session. Никакого триггера paywall здесь нет.
  let sessionState = { prices: {}, is_free: true, paid_access: false, free_requests_left: 1 };
  const state = {
    lastQuery: "",
    lastAnswer: "",
  };

  /**
   * Допустимые сервисы для окна оплаты. Окно открывается СТРОГО из обработчиков
   * 402 для /api/query, /api/checklist, /api/document и /api/pdf (см. runQuery,
   * runChecklist, runDocument, runPdf). Любой иной источник вызова заблокирован.
   */
  const PAYWALL_SERVICES = new Set(["consultation", "checklist", "document", "pdf", "package_basic", "package_premium"]);

  async function loadSession() {
    const { status, data } = await api("GET", "/api/session");
    if (status === 200 && data) {
      sessionState = data;
      renderSessionChip();
      renderPrices();
    }
  }

  async function loadLegal() {
    const { data } = await api("GET", "/api/legal");
    if (!data) return;
    // /api/legal возвращает готовый markdown — отрендерим его тем же
    // markdownToHtml, что и результаты консультаций (заголовки, жирный,
    // маркированные списки).
    if ($("legalPrivacy") && data.privacy) {
      $("legalPrivacy").innerHTML = markdownToHtml(data.privacy);
    }
    if ($("legalDisclaimer") && data.disclaimer) {
      $("legalDisclaimer").textContent = data.disclaimer;
    }
    // Блок «Как мы защищаем ваши персональные данные (152-ФЗ)» (Шаг 5 ТЗ prompt170926.md).
    // Если бэкенд вернул текст маскировки — рендерим его в аккордеон.
    // Если нет — оставляем статический текст из index.html.
    if ($("legalMasking") && data.masking) {
      $("legalMasking").innerHTML = markdownToHtml(data.masking);
    }
  }

  // Состояние выбранного типа документа (см. блок «Шаг 3. Шаблон документа»).
  // Кнопки идут строго слева направо: complaint → claim → lawsuit → court_order_cancellation.
  const DOC_TYPE_DEFAULT = "complaint";
  const stateDocType = { value: DOC_TYPE_DEFAULT };

  function setActiveDocType(value) {
    stateDocType.value = value;
    document.querySelectorAll(".doc-type-btn").forEach((btn) => {
      const active = btn.getAttribute("data-doc-type") === value;
      btn.classList.toggle("primary", active);
      btn.classList.toggle("ghost", !active);
      btn.setAttribute("aria-pressed", active ? "true" : "false");
    });
  }

  function renderSessionChip() {
    const label = $("sessionLabel");
    const chip = $("sessionChip");
    if (!label || !chip) return;
    if (sessionState.paid_access) {
      chip.classList.add("locked");
      label.textContent = "Оплаченный доступ";
    } else if (sessionState.is_free && (sessionState.free_requests_left || 0) > 0) {
      chip.classList.remove("locked");
      label.textContent = "Бесплатно: " + sessionState.free_requests_left + " из 1";
    } else {
      chip.classList.add("locked");
      label.textContent = "Первый запрос бесплатно";
    }
  }

  function renderPrices() {
    const p = sessionState.prices || {};
    if ($("priceConsult")) $("priceConsult").textContent = (p.consultation || 99) + " ₽";
    if ($("priceChecklist")) $("priceChecklist").textContent = (p.checklist || 100) + " ₽";
    if ($("priceDocument")) $("priceDocument").textContent = (p.document || 300) + " ₽";
    // Пакетный тариф «Всё включено» (Шаг 4 ТЗ prompt170926.md).
    if ($("packagePrice")) $("packagePrice").textContent = (p.package_basic || 390) + " ₽";
  }

  function toast(message, kind) {
    const el = $("toast");
    if (!el) return;
    el.textContent = message;
    el.classList.remove("error", "success");
    if (kind) el.classList.add(kind);
    el.classList.add("show");
    clearTimeout(toast._t);
    toast._t = setTimeout(() => el.classList.remove("show"), 3500);
  }

  /**
   * Открыть окно оплаты. Защита от случайных вызовов из других мест: принимаем
   * только валидный код сервиса из PAYWALL_SERVICES и обязательно payment_url.
   */
  function openPaywall(service, amount, paymentUrl) {
    if (!PAYWALL_SERVICES.has(service)) {
      console.warn("[paywall] openPaywall вызван с неизвестным сервисом:", service);
      return;
    }
    if (!paymentUrl) {
      console.warn("[paywall] отсутствует payment_url, окно не открывается");
      return;
    }
    $("paywallService").textContent = "Услуга: " + ({
      consultation: "юридическая консультация",
      checklist: "чек-лист действий",
      document: "шаблон документа",
      pdf: "PDF-версия",
      package_basic: "Всё включено (Консультация + Чек-лист + Документ)",
      package_premium: "Всё включено Премиум",
    }[service] || service);
    $("paywallAmount").textContent = amount + " ₽";
    const link = $("paywallLink");
    link.href = paymentUrl || "#";
    show($("paywall"));
  }
  function closePaywall() { hide($("paywall")); }

  async function runQuery() {
    const query = ($("queryInput").value || "").trim();
    if (query.length < 3) { toast("Опишите ситуацию подробнее", "error"); return; }
    setStatus($("queryStatus"), "Ставим задачу в очередь…");
    const btn = $("runQueryBtn"); btn.disabled = true;
    state.lastQuery = query; state.lastAnswer = "";
    showSkeletonAndTimer();
    try {
      const { status, data } = await api("POST", "/api/query/async", { query });
      if (status === 202 && data && data.task_id) {
        const taskId = data.task_id;
        setStatus($("queryStatus"), "Изучаю законы...");
        await streamConsultation(taskId, query);
      } else if (status === 402 && data && data.payment_url) {
        hideSkeletonAndTimer();
        setStatus($("queryStatus"), "Требуется оплата", "error");
        openPaywall(data.service || "consultation", data.amount, data.payment_url);
      } else {
        hideSkeletonAndTimer();
        setStatus($("queryStatus"), "Ошибка", "error");
        toast((data && (data.error || data.detail)) || "Не удалось поставить задачу", "error");
      }
    } catch (e) {
      hideSkeletonAndTimer();
      setStatus($("queryStatus"), "Сбой сети", "error");
      toast("Сбой сети: " + e.message, "error");
    } finally {
      btn.disabled = false;
    }
  }

  /**
   * SSE-стриминг консультации (Шаг 1 ТЗ prompt170926.md).
   * Подписывается на /api/query/stream/{task_id} и рендерит токены
   * в реальном времени по мере их поступления от бэкенда.
   */
  function streamConsultation(taskId, query) {
    return new Promise((resolve) => {
      let sources = [];
      let stage2Provider = "";
      let stage1Provider = "";
      let warning = "";
      let timerHandle = null;
      const startedAt = Date.now();

      const timerEl = $("queryTimerText");
      if (timerEl) {
        timerEl.textContent = "Идёт правовой анализ… Прошло 0 сек.";
        timerHandle = setInterval(() => {
          const sec = Math.floor((Date.now() - startedAt) / 1000);
          if (timerEl) timerEl.textContent = "Идёт правовой анализ… Прошло " + sec + " сек.";
        }, 1000);
      }
      const stopTimer = () => { if (timerHandle) { clearInterval(timerHandle); timerHandle = null; } };

      /** Прокрутить контейнер результата к самой свежей строке (typewriter-эффект). */
      const scrollToBottom = () => {
        const body = $("queryResultBody");
        if (body && typeof body.scrollHeight === "number") {
          // scrollIntoView на последнем дочернем узле даёт плавную прокрутку
          // именно к свежему тексту, а не к произвольной точке контейнера.
          const last = body.lastChild;
          if (last && typeof last.scrollIntoView === "function") {
            last.scrollIntoView({ block: "end", inline: "nearest" });
          } else {
            body.scrollTop = body.scrollHeight;
          }
        }
      };

      let es;
      try {
        es = new EventSource(API_BASE + "/api/query/stream/" + taskId);
      } catch (e) {
        stopTimer();
        hideSkeletonAndTimer();
        setStatus($("queryStatus"), "Сбой сети", "error");
        toast("Не удалось открыть поток: " + e.message, "error");
        resolve();
        return;
      }

      es.onmessage = (ev) => {
        let payload = null;
        try { payload = JSON.parse(ev.data); } catch (_) { return; }
        if (!payload || !payload.type) return;

        if (payload.type === "started" || payload.type === "progress") return;

        if (payload.type === "token") {
          // Немедленный рендеринг: каждый токен дописывается прямо в DOM
          // без буферизации/накопления — пользователь видит «печатную машинку».
          const chunk = payload.text || "";
          if (!chunk) return;
          const body = $("queryResultBody");
          if (body) {
            body.appendChild(document.createTextNode(chunk));
            scrollToBottom();
          }
          const sk = $("querySkeleton");
          if (sk && !sk.hidden) sk.hidden = true;
          return;
        }
        if (payload.type === "sources") { sources = payload.sources || []; return; }
        if (payload.type === "meta") {
          stage1Provider = payload.stage1_provider || stage1Provider;
          stage2Provider = payload.stage2_provider || stage2Provider;
          warning = payload.warning || warning;
          return;
        }
        if (payload.type === "completed") {
          stopTimer(); es.close();
          const finalText = (payload.result && payload.result.response) || ($("queryResultBody") ? $("queryResultBody").textContent : "");
          state.lastAnswer = finalText;
          const body = $("queryResultBody");
          if (body) body.innerHTML = markdownToHtml(finalText);
          const list = $("querySourcesList");
          if (list) {
            list.innerHTML = "";
            (sources || []).forEach((src) => {
              const li = document.createElement("li");
              const a = document.createElement("a");
              a.href = src.url; a.textContent = src.title; a.target = "_blank"; a.rel = "noopener noreferrer";
              li.appendChild(a); list.appendChild(li);
            });
          }
          show($("querySources")); if (!(sources || []).length) hide($("querySources"));
          hideSkeletonAndTimer();
          show($("queryResult"));
          setStatus($("queryStatus"), "Готово ✓", "success");
          toast("Консультация получена", "success");
          loadSession();
          if (typeof console !== "undefined" && console.debug) {
            console.debug("[clickjurist] consultation", {
              stage1_provider: stage1Provider || "—",
              stage2_provider: stage2Provider || "—",
              warning: warning || "",
            });
          }
          resolve();
          return;
        }
        if (payload.type === "failed") {
          stopTimer(); es.close();
          hideSkeletonAndTimer();
          setStatus($("queryStatus"), "Ошибка генерации", "error");
          toast(payload.error || "Не удалось получить ответ", "error");
          resolve();
          return;
        }
        if (payload.type === "cancelled") {
          stopTimer(); es.close();
          hideSkeletonAndTimer();
          resolve();
          return;
        }
      };

      es.onerror = () => {
        if (typeof console !== "undefined" && console.debug) {
          console.debug("[clickjurist] SSE reconnecting…");
        }
      };
    });
  }

  /** Показать скелетон-заглушку и таймер (Шаг 1 ТЗ prompt170926.md). */
  function showSkeletonAndTimer() {
    const sk = $("querySkeleton"); if (sk) sk.hidden = false;
    const tm = $("queryTimer"); if (tm) tm.hidden = false;
    const body = $("queryResultBody"); if (body) body.textContent = "";
    show($("queryResult"));
  }

  /** Скрыть скелетон и таймер после завершения генерации. */
  function hideSkeletonAndTimer() {
    const sk = $("querySkeleton"); if (sk) sk.hidden = true;
    const tm = $("queryTimer"); if (tm) tm.hidden = true;
  }

  /**
   * Универсальный SSE-стример для Шагов 2/3 (ТЗ prompt170926.md).
   * Подписывается на /api/query/stream/{task_id} и рендерит токены
   * в указанный контейнер по мере их поступления.
   *
   * @param {string} taskId — идентификатор фоновой задачи.
   * @param {object} opts — параметры рендеринга:
   *   - skeletonId, timerId, timerTextId, bodyId, resultId — DOM-id элементов;
   *   - statusId — id элемента статуса;
   *   - initialLabel — текст таймера до старта («Готовлю чек-лист…»);
   *   - onCompleted(finalText) — колбэк финализации (markdown-рендер).
   */
  function streamGeneric(taskId, opts) {
    return new Promise((resolve) => {
      const {
        skeletonId, timerId, timerTextId, bodyId, resultId,
        statusId, initialLabel, onCompleted,
      } = opts;

      let timerHandle = null;
      const startedAt = Date.now();

      const timerEl = $(timerTextId);
      if (timerEl) {
        timerEl.textContent = initialLabel + " Прошло 0 сек.";
        timerHandle = setInterval(() => {
          const sec = Math.floor((Date.now() - startedAt) / 1000);
          if (timerEl) timerEl.textContent = initialLabel + " Прошло " + sec + " сек.";
        }, 1000);
      }
      const stopTimer = () => { if (timerHandle) { clearInterval(timerHandle); timerHandle = null; } };

      const hideSkeleton = () => {
        const sk = $(skeletonId); if (sk) sk.hidden = true;
        const tm = $(timerId); if (tm) tm.hidden = true;
      };

      const scrollToBottom = () => {
        const body = $(bodyId);
        if (body && typeof body.scrollHeight === "number") {
          const last = body.lastChild;
          if (last && typeof last.scrollIntoView === "function") {
            last.scrollIntoView({ block: "end", inline: "nearest" });
          } else {
            body.scrollTop = body.scrollHeight;
          }
        }
      };

      let es;
      try {
        es = new EventSource(API_BASE + "/api/query/stream/" + taskId);
      } catch (e) {
        stopTimer();
        hideSkeleton();
        if (statusId) setStatus($(statusId), "Сбой сети", "error");
        toast("Не удалось открыть поток: " + e.message, "error");
        resolve();
        return;
      }

      es.onmessage = (ev) => {
        let payload = null;
        try { payload = JSON.parse(ev.data); } catch (_) { return; }
        if (!payload || !payload.type) return;

        if (payload.type === "started" || payload.type === "progress") return;

        if (payload.type === "token") {
          const chunk = payload.text || "";
          if (!chunk) return;
          const body = $(bodyId);
          if (body) {
            body.appendChild(document.createTextNode(chunk));
            scrollToBottom();
          }
          const sk = $(skeletonId);
          if (sk && !sk.hidden) sk.hidden = true;
          return;
        }

        if (payload.type === "completed") {
          stopTimer(); es.close();
          const result = payload.result || {};
          const finalText = result.checklist || result.document
            || ($(bodyId) ? $(bodyId).textContent : "");
          hideSkeleton();
          show($(resultId));
          if (typeof onCompleted === "function") {
            try { onCompleted(finalText, result); } catch (_) {}
          }
          if (statusId) setStatus($(statusId), "Готово ✓", "success");
          resolve();
          return;
        }
        if (payload.type === "failed") {
          stopTimer(); es.close();
          hideSkeleton();
          if (statusId) setStatus($(statusId), "Ошибка генерации", "error");
          toast(payload.error || "Не удалось получить ответ", "error");
          resolve();
          return;
        }
        if (payload.type === "cancelled") {
          stopTimer(); es.close();
          hideSkeleton();
          resolve();
          return;
        }
      };

      es.onerror = () => {
        if (typeof console !== "undefined" && console.debug) {
          console.debug("[clickjurist] SSE reconnecting…");
        }
      };
    });
  }

  /** Показать скелетон + таймер для произвольного шага (2 или 3). */
  function showStepSkeletonAndTimer(step) {
    const sk = $(step + "Skeleton"); if (sk) sk.hidden = false;
    const tm = $(step + "Timer"); if (tm) tm.hidden = false;
    const body = $(step + "ResultBody"); if (body) body.textContent = "";
    show($(step + "Result"));
  }

  /** Скрыть скелетон + таймер для произвольного шага (2 или 3). */
  function hideStepSkeletonAndTimer(step) {
    const sk = $(step + "Skeleton"); if (sk) sk.hidden = true;
    const tm = $(step + "Timer"); if (tm) tm.hidden = true;
  }

  /** Асинхронная генерация через polling (раздел 2.1 ТЗ prompt160926.md). */
  async function runQueryAsync() {
    const query = ($("queryInput").value || "").trim();
    if (query.length < 3) { toast("Опишите ситуацию подробнее", "error"); return; }
    setStatus($("queryStatus"), "Ставим задачу в очередь…");
    const btn = $("runQueryBtn"); btn.disabled = true;
    try {
      // 1. Поставить задачу в очередь.
      const { status, data } = await api("POST", "/api/query/async", { query });
      if (status === 202 && data && data.task_id) {
        const taskId = data.task_id;
        setStatus($("queryStatus"), "Генерация в фоне (этап 1/3)…");
        // 2. Polling статуса каждые 2 секунды.
        const poll = async () => {
          const { status: s, data: d } = await api("GET", "/api/query/status/" + taskId);
          if (!d) return false;
          if (d.status === "completed") {
            renderConsultationResult(query, d.result || {});
            setStatus($("queryStatus"), "Готово ✓", "success");
            toast("Консультация получена", "success");
            loadSession();
            return true;
          }
          if (d.status === "failed") {
            setStatus($("queryStatus"), "Ошибка генерации", "error");
            toast(d.error || "Не удалось получить ответ", "error");
            return true;
          }
          // Обновить прогресс.
          const stage = d.stage || "обработка";
          const progress = d.progress || 0;
          setStatus($("queryStatus"), `Генерация: ${stage} (${progress}%)…`);
          return false;
        };
        // Цикл опроса (макс. 5 минут).
        for (let i = 0; i < 150; i++) {
          const done = await poll();
          if (done) break;
          await new Promise((r) => setTimeout(r, 2000));
        }
      } else if (status === 402 && data && data.payment_url) {
        setStatus($("queryStatus"), "Требуется оплата", "error");
        openPaywall(data.service || "consultation", data.amount, data.payment_url);
      } else {
        setStatus($("queryStatus"), "Ошибка", "error");
        toast((data && (data.error || data.detail)) || "Не удалось поставить задачу", "error");
      }
    } catch (e) {
      setStatus($("queryStatus"), "Сбой сети", "error");
      toast("Сбой сети: " + e.message, "error");
    } finally {
      btn.disabled = false;
    }
  }

  /** Отобразить результат консультации (общая функция для sync/async). */
  function renderConsultationResult(query, result) {
    if (!result || !result.response) return;
    state.lastQuery = query;
    state.lastAnswer = result.response;
    $("queryResultBody").innerHTML = markdownToHtml(result.response);
    const list = $("querySourcesList");
    list.innerHTML = "";
    (result.sources || []).forEach((src) => {
      const li = document.createElement("li");
      const a = document.createElement("a");
      a.href = src.url; a.textContent = src.title; a.target = "_blank"; a.rel = "noopener noreferrer";
      li.appendChild(a); list.appendChild(li);
    });
    show($("querySources")); if (!(result.sources || []).length) hide($("querySources"));
    show($("queryResult"));
  }

  async function runChecklist() {
    const queryInput = document.getElementById("queryInput");
    // Scenario A: пустое поле ввода — блокируем запуск и подсказываем, что делать.
    if (!queryInput || !queryInput.value.trim()) {
      toast("Для корректной работы сервиса необходимо сначала заполнить поле описания ситуации или перейти к шагу «Получить консультацию».", "error");
      return;
    }
    // Scenario B: текст есть, но Шаг 1 ещё не выполнен — без lastAnswer чек-лист
    // не имеет смысла, поэтому останавливаем выполнение и просим сначала
    // получить консультацию.
    if (!state.lastAnswer || !state.lastAnswer.trim()) {
      toast("Для корректной работы сервиса необходимо сначала перейти к шагу «Получить консультацию».", "error");
      return;
    }
    const query = queryInput.value.trim();
    setStatus($("checklistStatus"), "Ставим задачу в очередь…");
    const btn = $("runChecklistBtn"); btn.disabled = true;
    showStepSkeletonAndTimer("checklist");
    try {
      const { status, data } = await api("POST", "/api/checklist/async", {
        query: state.lastQuery, final_answer: state.lastAnswer,
      });
      if (status === 202 && data && data.task_id) {
        const taskId = data.task_id;
        setStatus($("checklistStatus"), "Составляю план...");
        await streamGeneric(taskId, {
          skeletonId: "checklistSkeleton",
          timerId: "checklistTimer",
          timerTextId: "checklistTimerText",
          bodyId: "checklistResultBody",
          resultId: "checklistResult",
          statusId: "checklistStatus",
          initialLabel: "Готовлю чек-лист…",
          onCompleted: (finalText) => {
            $("checklistResultBody").innerHTML = markdownToHtml(finalText);
            toast("Чек-лист готов", "success");
            loadSession();
          },
        });
      } else if (status === 402 && data && data.payment_url) {
        hideStepSkeletonAndTimer("checklist");
        setStatus($("checklistStatus"), "Требуется оплата", "error");
        openPaywall(data.service || "checklist", data.amount, data.payment_url);
      } else {
        hideStepSkeletonAndTimer("checklist");
        setStatus($("checklistStatus"), "Ошибка", "error");
        toast((data && (data.error || data.detail)) || "Не удалось построить чек-лист", "error");
      }
    } catch (e) {
      hideStepSkeletonAndTimer("checklist");
      setStatus($("checklistStatus"), "Сбой сети", "error");
      toast("Сбой сети: " + e.message, "error");
    } finally {
      btn.disabled = false;
    }
  }

  async function runDocument() {
    const queryInput = document.getElementById("queryInput");
    // Scenario A: пустое поле ввода — блокируем запуск и подсказываем, что делать.
    if (!queryInput || !queryInput.value.trim()) {
      toast("Для корректной работы сервиса необходимо сначала заполнить поле описания ситуации или перейти к шагу «Получить консультацию».", "error");
      return;
    }
    // Scenario B: текст есть, но Шаг 1 ещё не выполнен — без lastAnswer
    // документ не имеет смысла, поэтому останавливаем выполнение и просим
    // сначала получить консультацию.
    if (!state.lastAnswer || !state.lastAnswer.trim()) {
      toast("Для корректной работы сервиса необходимо сначала перейти к шагу «Получить консультацию».", "error");
      return;
    }
    const query = queryInput.value.trim();
    // doc_type приходит ИЗ АКТИВНОЙ КНОПКИ в блоке «Шаг 3. Шаблон документа».
    // Кнопки идут слева направо: complaint → claim → lawsuit → court_order_cancellation.
    const docType = stateDocType.value;
    const btn = $("runDocumentBtn");
    // 1. Блокируем кнопку и показываем аккуратный анимированный спиннер внутри неё —
    //    это надёжный индикатор загрузки на любых устройствах, в т.ч. на медленном канале.
    btn.disabled = true;
    btn.classList.add("is-loading");
    btn.dataset.originalLabel = btn.dataset.originalLabel || btn.textContent;
    btn.innerHTML = '<span class="spinner" aria-hidden="true"></span>' + btn.dataset.originalLabel;
    // 2. Солидный статус рядом с кнопкой + скелетон + таймер (Шаг 3 ТЗ prompt170926.md).
    setStatus($("documentStatus"), "Ставим задачу в очередь…");
    showStepSkeletonAndTimer("document");
    try {
      const { status, data } = await api("POST", "/api/document/async", { query, doc_type: docType });
      if (status === 202 && data && data.task_id) {
        const taskId = data.task_id;
        setStatus($("documentStatus"), "Пишу документ...");
        await streamGeneric(taskId, {
          skeletonId: "documentSkeleton",
          timerId: "documentTimer",
          timerTextId: "documentTimerText",
          bodyId: "documentResultBody",
          resultId: "documentResult",
          statusId: "documentStatus",
          initialLabel: "Формирую документ…",
          onCompleted: (finalText) => {
            $("documentResultBody").innerHTML = markdownToHtml(finalText);
            toast("Документ сформирован", "success");
            loadSession();
          },
        });
      } else if (status === 402 && data && data.payment_url) {
        hideStepSkeletonAndTimer("document");
        setStatus($("documentStatus"), "Требуется оплата", "error");
        openPaywall(data.service || "document", data.amount, data.payment_url);
      } else {
        hideStepSkeletonAndTimer("document");
        setStatus($("documentStatus"), "Ошибка", "error");
        toast((data && (data.error || data.detail)) || "Не удалось сформировать документ", "error");
      }
    } catch (e) {
      hideStepSkeletonAndTimer("document");
      setStatus($("documentStatus"), "Сбой сети", "error");
      toast("Сбой сети: " + e.message, "error");
    } finally {
      // 3. Снимаем блокировку и спиннер, восстанавливаем оригинальную подпись.
      btn.disabled = false;
      btn.classList.remove("is-loading");
      if (btn.dataset.originalLabel) {
        btn.textContent = btn.dataset.originalLabel;
      }
    }
  }

  async function runPdf() {
    // 1. Сначала показываем статус «Формирую PDF…» ДО отправки запроса.
    setStatus($("documentStatus"), "Формирую PDF…");
    const btn = $("runPdfBtn");
    btn.disabled = true;
    btn.classList.add("is-loading");
    if (!btn.dataset.originalLabel) btn.dataset.originalLabel = btn.textContent;
    btn.textContent = "Формирую PDF…";

    try {
      // 2. Берём текст СТРОГО из Шага 3 (#documentResultBody) — это готовый
      //    шаблон документа. НЕ используем state.lastAnswer / #queryResultBody
      //    (это текст Шага 1 — юридическая консультация, а не документ).
      const docBody = $("documentResultBody");
      const documentText = docBody ? (docBody.innerText || docBody.textContent || "").trim() : "";
      if (!documentText) {
        toast("Сначала сформируйте документ в Шаге 3, чтобы скачать его в PDF.", "error");
        setStatus($("documentStatus"), "Ошибка", "error");
        return;
      }

      // 3. Запрос к /api/pdf напрямую через fetch — ответ приходит как
      //    бинарный поток application/pdf, его НЕЛЬЗЯ парсить как JSON.
      const response = await fetch(API_BASE + "/api/pdf", {
        method: "POST",
        headers: { ...headers },
        credentials: "include",
        body: JSON.stringify({
          query: state.lastQuery,
          final_answer: documentText,
        }),
      });

      // 3. Обработка 402 (paywall) — сервер возвращает JSON с payment_url.
      if (response.status === 402) {
        let paywall = null;
        try {
          paywall = await response.json();
        } catch (_) {
          paywall = null;
        }
        if (paywall && paywall.payment_url) {
          openPaywall(paywall.service || "pdf", paywall.amount, paywall.payment_url);
        } else {
          toast("Не удалось получить ссылку на оплату", "error");
        }
        return;
      }

      // 4. Любой неуспешный статус — пробуем прочитать JSON с ошибкой.
      if (!response.ok) {
        let errPayload = null;
        try {
          errPayload = await response.json();
        } catch (_) {
          errPayload = null;
        }
        toast((errPayload && errPayload.error) || `Ошибка ${response.status}`, "error");
        return;
      }

      // 5. Успешный HTTP 200 — получаем бинарный blob и инициируем скачивание.
      const blob = await response.blob();
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "Документ.pdf";
      document.body.appendChild(a);
      a.click();
      window.URL.revokeObjectURL(url);
      a.remove();

      // 6. ТОЛЬКО после успешного скачивания показываем «PDF готов».
      toast("PDF готов", "success");
      setStatus($("documentStatus"), "PDF готов");
      loadSession();
    } catch (e) {
      toast("Сбой при формировании PDF: " + (e && e.message ? e.message : e), "error");
      setStatus($("documentStatus"), "Ошибка", "error");
    } finally {
      // 7. Снимаем блокировку и спиннер, восстанавливаем оригинальную подпись.
      btn.disabled = false;
      btn.classList.remove("is-loading");
      if (btn.dataset.originalLabel) {
        btn.textContent = btn.dataset.originalLabel;
      }
    }
  }

  /**
   * Пакетная покупка «Всё включено» (Шаг 4 ТЗ prompt170926.md).
   * Создаёт счёт в Робокассе и открывает окно оплаты.
   */
  async function runPackage() {
    const btn = $("runPackageBtn"); if (btn) btn.disabled = true;
    try {
      const { status, data } = await api("POST", "/api/payment/create", { service: "package_basic" });
      if (status === 200 && data && data.payment_url) {
        openPaywall("package_basic", data.amount, data.payment_url);
      } else {
        toast((data && data.error) || "Не удалось создать счёт", "error");
      }
    } catch (e) {
      toast("Сбой при создании счёта: " + e.message, "error");
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  /**
   * Полный сброс состояния приложения: очищает поле ввода, результаты всех шагов,
   * глобальный state, скрывает скелетоны/таймеры/результаты, снимает блокировки
   * с кнопок и закрывает платёжное окно. Вызывается по клику на «Начать новую
   * консультацию» в Шаге 1.
   */
  function resetApp() {
    // 1. Поле ввода.
    const input = $("queryInput");
    if (input) input.value = "";

    // 2. Тела результатов всех шагов.
    ["queryResultBody", "checklistResultBody", "documentResultBody"].forEach((id) => {
      const el = $(id);
      if (el) el.innerHTML = "";
    });

    // 3. Список источников (Шаг 1).
    const sourcesList = $("querySourcesList");
    if (sourcesList) sourcesList.innerHTML = "";

    // 4. Глобальный state.
    state.lastQuery = "";
    state.lastAnswer = "";

    // 5. Скрыть контейнеры результатов и блок источников.
    ["queryResult", "checklistResult", "documentResult", "querySources"].forEach((id) => {
      hide($(id));
    });

    // 6. Скрыть скелетоны и таймеры, вернуть их текст в дефолтное состояние.
    hideSkeletonAndTimer();
    hideStepSkeletonAndTimer("checklist");
    hideStepSkeletonAndTimer("document");
    if ($("queryTimerText")) $("queryTimerText").textContent = "Идёт правовой анализ… Прошло 0 сек.";
    if ($("checklistTimerText")) $("checklistTimerText").textContent = "Готовлю чек-лист… Прошло 0 сек.";
    if ($("documentTimerText")) $("documentTimerText").textContent = "Формирую документ… Прошло 0 сек.";

    // 7. Сбросить статусы рядом с кнопками.
    setStatus($("queryStatus"), "");
    setStatus($("checklistStatus"), "");
    setStatus($("documentStatus"), "");

    // 8. Разблокировать кнопки и вернуть им исходный лейбл (на случай, если
    //    внутри runQuery/runChecklist/runDocument они были переведены в
    //    is-loading и/или disabled).
    ["runQueryBtn", "runChecklistBtn", "runDocumentBtn", "runPdfBtn"].forEach((id) => {
      const btn = $(id);
      if (btn) {
        btn.disabled = false;
        btn.classList.remove("is-loading");
      }
    });
    const runDocBtn = $("runDocumentBtn");
    if (runDocBtn && runDocBtn.dataset && runDocBtn.dataset.originalLabel) {
      runDocBtn.innerHTML = runDocBtn.dataset.originalLabel;
    }

    // 9. Сбросить выбор типа документа к дефолту (Шаг 3).
    setActiveDocType(DOC_TYPE_DEFAULT);

    // 10. Закрыть платёжное окно, если оно открыто.
    closePaywall();

    // 11. Подтверждение для пользователя.
    toast("Готово к новой консультации", "success");
  }

  function bind() {
    document.querySelectorAll("[data-scroll]").forEach((el) => {
      el.addEventListener("click", () => {
        const target = document.getElementById(el.getAttribute("data-scroll"));
        if (target) target.scrollIntoView({ behavior: "smooth", block: "start" });
      });
    });
    if ($("runQueryBtn")) $("runQueryBtn").addEventListener("click", runQuery);
    if ($("runChecklistBtn")) $("runChecklistBtn").addEventListener("click", runChecklist);
    if ($("runDocumentBtn")) $("runDocumentBtn").addEventListener("click", runDocument);
    if ($("runPdfBtn")) $("runPdfBtn").addEventListener("click", runPdf);
    if ($("runPackageBtn")) $("runPackageBtn").addEventListener("click", runPackage);
    if ($("resetAppBtn")) $("resetAppBtn").addEventListener("click", resetApp);
    if ($("toChecklistBtn")) $("toChecklistBtn").addEventListener("click", () => {
      const target = $("checklist"); if (target) target.scrollIntoView({ behavior: "smooth" });
    });
    if ($("paywallClose")) $("paywallClose").addEventListener("click", closePaywall);
    if ($("paywall")) $("paywall").addEventListener("click", (e) => { if (e.target === $("paywall")) closePaywall(); });
    // 4 кнопки выбора типа документа (см. блок «Шаг 3»). Навешиваем обработчик
    // единожды, чтобы не дублировать клики.
    document.querySelectorAll(".doc-type-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        const value = btn.getAttribute("data-doc-type");
        if (value) setActiveDocType(value);
      });
    });
    // Быстрые пресеты (раздел 4.1 ТЗ prompt160926.md).
    document.querySelectorAll(".quick-tag").forEach((btn) => {
      btn.addEventListener("click", () => {
        const key = btn.getAttribute("data-tag");
        if (key) applyQuickTag(key);
      });
    });
    // Активируем дефолтную кнопку при загрузке (complaint — первая слева).
    setActiveDocType(DOC_TYPE_DEFAULT);
  }

  document.addEventListener("DOMContentLoaded", () => {
    // 1. Гарантированно скрыть paywall ДО любых сетевых вызовов и рендера чипа.
    //    Даже если бы на нём случайно оказался класс активности - здесь он
    //    снимается, и пользователь видит чистый экран консультации.
    closePaywall();
    // 2. Затем привязать обработчики и загрузить состояние сессии.
    bind();
    loadSession();
    loadLegal();
  });
})();