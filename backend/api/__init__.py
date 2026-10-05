"""HTTP-слой ClickJurist: роутеры, разделённые по доменам.

Раньше всё было в ``backend/main.py`` — файл перерос 1100 строк, и любая
правка требовала прокручивать его целиком. Теперь каждый домен живёт в
своём модуле, а ``backend/main.py`` отвечает только за сборку приложения:
создание ``FastAPI``, middleware, обработчики ошибок и подключение
роутеров.

Модули пакета:

* :mod:`backend.api.deps` — платёжный барьер и общие зависимости;
* :mod:`backend.api.router_query` — консультация и фоновые задачи;
* :mod:`backend.api.router_docs` — чек-лист, документ, PDF, пакетный тариф;
* :mod:`backend.api.router_payment` — оплата Robokassa (общий путь);
* :mod:`backend.api.router_payments` — webhook Робокассы для Wizard (STAGE_3);
* :mod:`backend.api.router_auth` — JWT-сессия;
* :mod:`backend.api.router_meta` — сессия, тарифы, здоровье, статика.
"""

from __future__ import annotations

from backend.api.deps import payment_required, session_gate

__all__ = ["payment_required", "session_gate"]
