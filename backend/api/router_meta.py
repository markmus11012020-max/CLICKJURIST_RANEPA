"""Служебные эндпоинты: сессия, тарифы, здоровье, статистика, SPA."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from backend.config import PROJECT_ROOT, settings
from backend.db import store
from backend.logging_setup import setup_logging
from backend.models import HealthResponse, SessionStateResponse
from backend.security import session_context
from backend.services.prompts import AI_DISCLAIMER

logger = setup_logging()

router = APIRouter(tags=["meta"])

FRONTEND_DIR = PROJECT_ROOT / "frontend"
INDEX_FILE = FRONTEND_DIR / "index.html"


@router.get("/api/session", response_model=SessionStateResponse)
async def api_session(request: Request) -> SessionStateResponse:
    """Текущее состояние сессии без раскрытия персональных данных."""
    session_hash, session_id, _ = session_context(request)
    session = store.ensure_session(session_hash)
    used = int(session.get("free_requests_used", 0))
    return SessionStateResponse(
        session_id=session_id,
        is_free=bool(int(session.get("is_free", 0))),
        free_requests_left=max(0, settings.FREE_TIER_REQUESTS - used),
        paid_access=store.has_paid_access(session_hash),
        paid_until=session.get("paid_until"),
        prices=settings.prices,
    )


@router.get("/api/prices")
async def api_prices() -> dict[str, int]:
    """Актуальные тарифы сервиса (рубли)."""
    return settings.prices


@router.get("/api/health", response_model=HealthResponse)
async def api_health() -> HealthResponse:
    """Проверка работоспособности и состояние защитных контуров."""
    return HealthResponse(
        status="ok",
        environment=settings.APP_ENV,
        masking_contour=settings.MASKING_PROVIDER,
        analysis_provider=settings.PRIMARY_PROVIDER,
        web_search_provider=settings.WEB_SEARCH_PROVIDER,
        kms_enabled=settings.YANDEX_KMS_ENABLED,
        database=store.dialect,
    )


@router.get("/api/stats")
async def api_stats() -> dict[str, int]:
    """Анонимная агрегированная статистика (без каких-либо ПДн)."""
    return store.stats()


@router.get("/api/legal")
async def api_legal() -> dict[str, str]:
    """Юридическая информация: дисклеймер, режим обработки данных (152-ФЗ)
    и идентификация Оператора (самозанятый, ФЗ-422)."""
    return {
        "disclaimer": AI_DISCLAIMER,
        "operator": (
            "### 👤 Оператор сервиса\n"
            "Владелец и администратор Сервиса «КликЮрист» — **Мусина Гульназ "
            "Мягазовна**, физическое лицо, применяющее специальный налоговый "
            "режим «Налог на профессиональный доход» (НПД) в соответствии с "
            "Федеральным законом от 27.11.2018 № 422-ФЗ.\n\n"
            "**ИНН:** 631306493907\n\n"
            "**Адрес регистрации:** 443086, г. Самара, ул. Мичурина, д. 152, кв. 8\n\n"
            "**Email для обращений:** gmusina@list.ru\n\n"
            "Полные реквизиты и порядок обращения — в разделах "
            "[«Пользовательское соглашение»](/terms.html), "
            "[«Политика конфиденциальности»](/privacy.html) и "
            "[«Юридическое уведомление»](/legal.html)."
        ),
        "privacy": (
            "### 🔒 Безопасность и анонимность данных (152-ФЗ)\n"
            "Сервис спроектирован в строгом соответствии с российским "
            "законодательством о защите персональных данных:\n"
            "• **Полная анонимность:** Мы не храним тексты ваших обращений, "
            "фамилии, адреса или телефоны на серверах.\n"
            "• **Анонимность на лету:** Все личные данные (имена, контакты) "
            "автоматически удаляются из текста до того, как запрос будет "
            "отправлен на интеллектуальный анализ.\n"
            "• **Конфиденциальность сессии:** Доступ к вашим бесплатным лимитам "
            "и документам защищен безопасным цифровым идентификатором вашего "
            "устройства."
        ),
        "masking": (
            "### Режим Zero-Storage\n"
            "ClickJurist работает в режиме **Zero-Storage** (No-Data-Retention): "
            "тексты ваших обращений, фамилии, адреса, телефоны и иные персональные "
            "данные **не сохраняются** на серверах. В базе данных хранятся только "
            "псевдонимизированный идентификатор сессии (SHA-256 от IP + отпечатка "
            "браузера + соли) и технические метрики.\n\n"
            "### Локальный контур Stage 1 (маскировка ПДн)\n"
            "До передачи запроса во внешнюю аналитическую модель (Gemini 2.5 Flash) "
            "ваш текст проходит через **изолированный российский контур маскировки**: "
            "имена заменяются на `[NAME_1]`, адреса — на `[ADDRESS_1]`, телефоны — на "
            "`[PHONE_1]`, названия организаций — на `[ORG_1]`. Двухслойная защита: "
            "LLM (YandexGPT/Ollama) + обязательная regex-страховка. Ни один фрагмент "
            "ПДн не покидает пределы РФ.\n\n"
            "### Защита сессий через JWT\n"
            "Идентификация сессии реализована через **JWT-токен в HttpOnly, Secure, "
            "SameSite=Strict cookie**. JavaScript не может прочитать такой токен "
            "(защита от XSS), cookie не отправляется на сторонние сайты (защита от "
            "CSRF). Сессия не привязана к IP-адресу — переключение Wi-Fi ↔ LTE не "
            "разрывает авторизацию. Дополнительно токен привязан к хешу отпечатка "
            "браузера: даже при перехвате cookie злоумышленник не сможет ей "
            "воспользоваться без оригинального браузера."
        ),
    }


@router.get("/", include_in_schema=False)
async def index() -> Response:
    """Отдать SPA-интерфейс ClickJurist."""
    if INDEX_FILE.exists():
        return FileResponse(str(INDEX_FILE))
    return JSONResponse(
        content={
            "service": "ClickJurist Production",
            "docs": "/api/docs",
            "status": "frontend не найден — используйте API",
        }
    )


# ----------------------------------------------------------------------------
# Правовые страницы (consent / privacy / terms / legal).
# Регистрируем по одному маршруту на каждую — это безопасно и читаемо,
# выставляет корректный Content-Type (text/html; charset=utf-8) и код 404,
# если файл случайно отсутствует в сборке.
# ----------------------------------------------------------------------------
def _make_legal_handler(_filename: str):
    async def handler() -> Response:
        target = FRONTEND_DIR / _filename
        if target.exists():
            return FileResponse(str(target), media_type="text/html; charset=utf-8")
        return JSONResponse(
            status_code=404,
            content={"detail": f"Legal page '{_filename}' not found"},
        )

    handler.__name__ = f"legal_{_filename.replace('.', '_')}"
    return handler


for _name in ("consent.html", "privacy.html", "terms.html", "legal.html"):
    router.add_api_route(
        f"/{_name}",
        _make_legal_handler(_name),
        methods=["GET"],
        include_in_schema=False,
    )


@router.get("/favicon.ico", include_in_schema=False)
async def favicon() -> Response:
    """Пустой ответ на запрос иконки (чтобы не засорять логи 404)."""
    return Response(status_code=204)
