/**
 * ClickJurist - Фронтенд логика
 * Умный юридический помощник на базе ИИ
 */

// Базовый URL API (относительный, чтобы не было конфликтов CORS)
const API_BASE = "/api";

// Элементы DOM
const queryInput = document.getElementById("query-input");
const analyzeBtn = document.getElementById("analyze-btn");
const checklistBtn = document.getElementById("checklist-btn");
const printBtn = document.getElementById("print-btn");
const copyBtn = document.getElementById("copy-btn");
const loaderOverlay = document.getElementById("loader-overlay");
const markdownOutput = document.getElementById("markdown-output");
const checklistSection = document.getElementById("checklist-section");
const checklistContent = document.getElementById("checklist-content");
const documentSection = document.getElementById("document-section");
const documentContent = document.getElementById("document-content");
const docActions = document.getElementById("doc-actions");
const downloadPdfBtn = document.getElementById("download-pdf-btn");
const copyDocBtn = document.getElementById("copy-doc-btn");
const docTypeSelector = document.getElementById("doc-type-selector");
const docTypeButtons = document.querySelectorAll(".doc-type-btn");
const clearBtn = document.getElementById("clear-btn");

// Текущее состояние
let currentResponse = "";        // Финальный ответ LLM-2 (для чек-листа и копирования)
let currentQuery = "";           // Последний вопрос пользователя (для генерации чек-листа и документа)
let currentDocument = "";        // Текст сгенерированного шаблона (для копирования/PDF)
let currentDocType = "";         // Тип документа (для имени файла)
let isAnalyzing = false;         // Флаг: идёт ли сейчас генерация ответа
let isGeneratingChecklist = false;
let isGeneratingDocument = false;

// Подписи типов документов (общие — для лоадера и имени файла)
const DOC_TYPE_LABELS = {
    isk:        { loader: "исковое заявление", file: "iskovoe-zayavlenie" },
    pretension: { loader: "претензию",         file: "pretension" },
    zhaloba:    { loader: "жалобу",            file: "zhaloba" }
};

/**
 * Показать лоадер
 */
function showLoader(text = "Ваш запрос принят — ожидайте результат…") {
    const loaderText = loaderOverlay.querySelector(".loader-text");
    if (loaderText) loaderText.textContent = text;
    loaderOverlay.classList.add("active");
}

/**
 * Скрыть лоадер
 */
function hideLoader() {
    loaderOverlay.classList.remove("active");
}

/**
 * Отправить запрос на анализ
 * Показываем лоадер сразу при клике ("Ваш запрос принят — ожидайте результат…").
 */
async function analyzeQuery() {
    if (isAnalyzing) return;  // Защита от двойного клика

    const query = queryInput.value.trim();

    if (!query) {
        alert("Пожалуйста, введите ваш юридический вопрос");
        return;
    }

    isAnalyzing = true;
    analyzeBtn.disabled = true;
    currentQuery = query;

    // Сбросить предыдущие результаты (пользователь начал новый запрос)
    resetResults();

    showLoader("Ваш запрос принят — ожидайте результат…");

    try {
        const response = await fetch("/api/generate", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ query: query })
        });

        if (!response.ok) {
            const errData = await response.json().catch(() => ({}));
            throw new Error(errData.detail || `Ошибка сервера: ${response.status}`);
        }

        const data = await response.json();

        if (data.error) {
            throw new Error(data.error);
        }

        currentResponse = data.response || "";

        if (currentResponse) {
            markdownOutput.innerHTML = marked.parse(currentResponse);
            copyBtn.style.display = "inline-flex";
        } else {
            markdownOutput.innerHTML = "Не удалось получить ответ от сервера";
            copyBtn.style.display = "none";
        }

    } catch (error) {
        console.error("Ошибка:", error);
        markdownOutput.innerHTML = `<p style="color: var(--accent-cyan);">Ошибка: ${escapeHtml(error.message)}</p>`;
        copyBtn.style.display = "none";
    } finally {
        hideLoader();
        isAnalyzing = false;
        analyzeBtn.disabled = false;
    }
}

/**
 * Экранирование HTML для безопасной вставки текста ошибки
 */
function escapeHtml(s) {
    return String(s)
        .replace(/&/g, "\u0026amp;")
        .replace(/</g, "\u0026lt;")
        .replace(/>/g, "\u0026gt;")
        .replace(/"/g, "\u0026quot;")
        .replace(/'/g, "\u0026#039;");
}

/**
 * Показать чек-лист — отдельный запрос к /api/checklist.
 * Генерируется ТОЛЬКО по клику, не тратит токены заранее.
 */
async function showChecklist() {
    if (isGeneratingChecklist) return;

    if (!currentResponse) {
        alert("Сначала нажмите «Анализировать» — чек-лист строится на основе ответа юриста.");
        return;
    }

    isGeneratingChecklist = true;
    checklistBtn.disabled = true;

    showLoader("Формирую чек-лист…");

    try {
        const response = await fetch("/api/checklist", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                query: currentQuery,
                final_answer: currentResponse
            })
        });

        if (!response.ok) {
            const errData = await response.json().catch(() => ({}));
            throw new Error(errData.detail || `Ошибка сервера: ${response.status}`);
        }

        const data = await response.json();

        if (data.error) {
            throw new Error(data.error);
        }

        // Отображаем чек-лист как отрендеренный Markdown
        const md = data.checklist || "";
        checklistContent.innerHTML = marked.parse(md);
        checklistSection.classList.add("active");

        // Плавный скролл к чек-листу
        checklistSection.scrollIntoView({ behavior: "smooth", block: "start" });

    } catch (error) {
        console.error("Ошибка чек-листа:", error);
        alert(`Не удалось получить чек-лист: ${error.message}`);
    } finally {
        hideLoader();
        isGeneratingChecklist = false;
        checklistBtn.disabled = false;
    }
}

/**
 * Клик по «Шаблон документа» — просто показываем селектор типа.
 * Документ генерируется только после выбора конкретного типа.
 */
function showDocumentSelector() {
    if (!currentResponse) {
        alert("Сначала нажмите «Анализировать» — шаблон документа строится на основе ответа юриста.");
        return;
    }

    documentSection.classList.add("active");
    docTypeSelector.style.display = "block";
    documentContent.innerHTML = "";  // Сброс предыдущего результата
    docActions.style.display = "none"; // Скрыть кнопки действий до новой генерации
    currentDocument = "";
    currentDocType = "";
    documentSection.scrollIntoView({ behavior: "smooth", block: "start" });
}

/**
 * Генерация шаблона документа выбранного типа
 */
async function generateDocumentOfType(docType) {
    if (isGeneratingDocument) return;

    if (!currentQuery) {
        alert("Нет данных для шаблона. Сначала задайте вопрос и нажмите «Анализировать».");
        return;
    }

    isGeneratingDocument = true;
    docTypeButtons.forEach(b => b.disabled = true);

    const docTypeLabel = DOC_TYPE_LABELS[docType]?.loader || docType;
    showLoader(`Составляю ${docTypeLabel}…`);

    try {
        const response = await fetch("/api/document", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                query: currentQuery,
                doc_type: docType
            })
        });

        if (!response.ok) {
            const errData = await response.json().catch(() => ({}));
            throw new Error(errData.detail || `Ошибка сервера: ${response.status}`);
        }

        const data = await response.json();

        if (data.error) {
            throw new Error(data.error);
        }

        // Прячем селектор, показываем контент
        docTypeSelector.style.display = "none";
        const md = data.document || "";
        currentDocument = md;
        currentDocType = docType;
        documentContent.innerHTML = marked.parse(md);

        // Показать кнопки действий с шаблоном
        docActions.style.display = "flex";

    } catch (error) {
        console.error("Ошибка генерации документа:", error);
        documentContent.innerHTML = `<p style="color: var(--accent-cyan);">Ошибка: ${escapeHtml(error.message)}</p>`;
    } finally {
        hideLoader();
        isGeneratingDocument = false;
        docTypeButtons.forEach(b => b.disabled = false);
    }
}

/**
 * Копировать основной ответ (юридическую консультацию) в буфер обмена
 */
function copyToClipboard() {
    if (!currentResponse) return;

    navigator.clipboard.writeText(currentResponse).then(() => {
        const old = copyBtn.textContent;
        copyBtn.textContent = "Скопировано!";
        setTimeout(() => { copyBtn.textContent = old; }, 2000);
    }).catch(err => {
        console.error("Ошибка копирования:", err);
        alert("Не удалось скопировать текст. Выделите и скопируйте вручную.");
    });
}

/**
 * Копировать сгенерированный шаблон документа в буфер обмена
 */
function copyDocumentToClipboard() {
    if (!currentDocument) {
        alert("Сначала сгенерируйте шаблон документа.");
        return;
    }

    navigator.clipboard.writeText(currentDocument).then(() => {
        const old = copyDocBtn.textContent;
        copyDocBtn.textContent = "✓ Скопировано!";
        setTimeout(() => { copyDocBtn.textContent = old; }, 2000);
    }).catch(err => {
        console.error("Ошибка копирования:", err);
        alert("Не удалось скопировать текст. Выделите и скопируйте вручную.");
    });
}

/**
 * Скачать/распечатать шаблон документа как PDF.
 *
 * Используем нативный window.print() + @media print в CSS:
 *   - открывается системный диалог печати
 *   - пользователь выбирает «Сохранить как PDF» (встроено в Chrome / Edge / Firefox / Safari)
 *   - на печать выводится ТОЛЬКО содержимое шаблона, без шапки/футера/кнопок
 *
 * Это работает без бэкенда и без сторонних JS-библиотек.
 */
function downloadDocumentPdf() {
    if (!currentDocument) {
        alert("Сначала сгенерируйте шаблон документа.");
        return;
    }
    // Запускаем печать; пользователь в диалоге выбирает "Save as PDF"
    window.print();
}

/**
 * Сбросить все результаты (используется перед новой генерацией и в обработчике «Очистить форму»)
 */
function resetResults() {
    checklistSection.classList.remove("active");
    documentSection.classList.remove("active");
    checklistContent.innerHTML = "";
    documentContent.innerHTML = "";
    docActions.style.display = "none";
    docTypeSelector.style.display = "block";
    copyBtn.style.display = "none";
    markdownOutput.innerHTML = "Здесь появится юридическая консультация после отправки запроса...";
    currentResponse = "";
    currentDocument = "";
    currentDocType = "";
}

/**
 * Полная очистка формы: сбрасывает ввод, результаты, скроллит наверх
 */
function clearForm() {
    queryInput.value = "";
    resetResults();
    // Прокрутить наверх, к полю ввода
    window.scrollTo({ top: 0, behavior: "smooth" });
    queryInput.focus();
}

// Обработчики событий
analyzeBtn.addEventListener("click", analyzeQuery);
checklistBtn.addEventListener("click", showChecklist);
printBtn.addEventListener("click", showDocumentSelector);
copyBtn.addEventListener("click", copyToClipboard);
copyDocBtn.addEventListener("click", copyDocumentToClipboard);
downloadPdfBtn.addEventListener("click", downloadDocumentPdf);
clearBtn.addEventListener("click", clearForm);
docTypeButtons.forEach(btn => {
    btn.addEventListener("click", () => generateDocumentOfType(btn.dataset.docType));
});

// Инициализация
document.addEventListener("DOMContentLoaded", () => {
    // Скрываем дополнительные секции по умолчанию
    checklistSection.classList.remove("active");
    documentSection.classList.remove("active");
    copyBtn.style.display = "none";
    docActions.style.display = "none";

    // Настройка marked.js
    marked.setOptions({
        breaks: true,
        gfm: true
    });
});
