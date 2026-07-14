"""Логирование использования LLM-токенов и оценка стоимости запросов.

Идея:
- Каждый вызов AITUNNEL (LLM-1, LLM-2) пишет одну строку в `usage.jsonl` (JSON Lines):
    {"ts": "...", "endpoint": "generate", "model": "minimax-m3",
     "prompt_tokens": 123, "completion_tokens": 456, "total_tokens": 579,
     "cost_rub": 0.0123, "latency_s": 4.2, "ok": true}
- Цены берутся из `PRICE_TABLE` (рубли за 1M токенов, input / output).
- Модуль потокобезопасен (Lock), работает при многопоточном uvicorn.
- Не использует БД — только файл, чтобы не плодить зависимости.

Где обновлять цены:
- Зайди в ЛК AITUNNEL → https://aitunnel.ru/ → Тарифы.
- Скопируй цены за 1M токенов (input / output) и пересчитай в рубли (если даны в $).
- Обнови PRICE_TABLE ниже.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# =====================================================================================
# ТАБЛИЦА ЦЕН (рубли за 1 000 000 токенов).
# Эти значения — ДЕФОЛТНЫЕ, нужно уточнить под свой тариф в ЛК провайдера AITUNNEL
# (см. https://aitunnel.ru/). Если модели нет в таблице — запись всё равно пишется,
# просто с cost_rub = None.
# =====================================================================================
PRICE_TABLE: dict[str, dict[str, float]] = {
    # model_id: {"input": ₽/1M, "output": ₽/1M}
    # Цены ориентировочные (на момент 2026 г.). Подставьте актуальные из ЛК.
    "minimax-m3":      {"input": 30.0, "output": 90.0},
    "deepseek-v4-flash": {"input": 8.0, "output": 24.0},
    "gemini-2.5-flash":  {"input": 18.0, "output": 54.0},
    "claude-sonnet-4.5": {"input": 270.0, "output": 1350.0},
    "gpt-4o":            {"input": 225.0, "output": 900.0},
}


def get_price(model: str) -> dict[str, float] | None:
    """Возвращает {'input': X, 'output': Y} для модели или None, если модель не в таблице."""
    return PRICE_TABLE.get(model)


def calc_cost_rub(model: str, prompt_tokens: int, completion_tokens: int) -> float | None:
    """Считает стоимость запроса в рублях. None — если цены для модели не заданы."""
    price = get_price(model)
    if price is None:
        return None
    cost = (prompt_tokens / 1_000_000.0) * price["input"] \
         + (completion_tokens / 1_000_000.0) * price["output"]
    return round(cost, 6)


# =====================================================================================
# ХРАНИЛИЩЕ: usage.jsonl рядом с config.py
# =====================================================================================
def _resolve_log_path() -> Path:
    """Путь к usage.jsonl — в clerk_craft/backend/usage.jsonl."""
    # usage.py лежит в <repo>/clerk_craft/backend/services/usage.py
    return Path(__file__).resolve().parent.parent / "usage.jsonl"


LOG_PATH: Path = _resolve_log_path()
_LOCK = threading.Lock()


def log_usage(
    *,
    endpoint: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    latency_s: float,
    ok: bool = True,
    error: str | None = None,
) -> dict[str, Any]:
    """Записать одну строку в usage.jsonl. Возвращает записанный dict (для отладки).

    Безопасно вызывать из нескольких потоков одновременно (Lock).
    Не падает, если файл не открылся — только пишет в stderr.
    """
    total_tokens = (prompt_tokens or 0) + (completion_tokens or 0)
    cost_rub = calc_cost_rub(model, prompt_tokens or 0, completion_tokens or 0)

    record: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "endpoint": endpoint,
        "model": model,
        "prompt_tokens": int(prompt_tokens or 0),
        "completion_tokens": int(completion_tokens or 0),
        "total_tokens": int(total_tokens),
        "cost_rub": cost_rub,
        "latency_s": round(float(latency_s), 3),
        "ok": bool(ok),
        "error": error,
    }

    line = json.dumps(record, ensure_ascii=False) + "\n"
    try:
        with _LOCK:
            with LOG_PATH.open("a", encoding="utf-8") as f:
                f.write(line)
    except Exception as e:  # noqa: BLE001
        # Не валим основной запрос из-за проблем с логом
        print(f"[usage] WARN: не удалось записать usage-строку: {e}", flush=True)
    return record


def aggregate(window: str = "day") -> dict[str, Any]:
    """Агрегирует данные из usage.jsonl за окно:
    - "day"   — за последние 24 ч
    - "week"  — за последние 7 дней
    - "month" — за последние 30 дней
    - "all"   — за всё время

    Возвращает:
    {
      "window": "day",
      "requests": <int>,
      "errors": <int>,
      "total_tokens": <int>,
      "total_cost_rub": <float | None>,
      "by_model": { "<model>": { "requests", "tokens", "cost_rub" } },
      "by_endpoint": { "<endpoint>": { "requests", "tokens", "cost_rub" } }
    }
    """
    if not LOG_PATH.exists():
        return {
            "window": window, "requests": 0, "errors": 0, "total_tokens": 0,
            "total_cost_rub": 0.0, "by_model": {}, "by_endpoint": {},
            "log_path": str(LOG_PATH), "exists": False,
        }

    # Окно
    now = time.time()
    window_seconds = {"day": 86400, "week": 86400 * 7, "month": 86400 * 30, "all": None}
    seconds = window_seconds.get(window, 86400)

    total_requests = 0
    total_errors = 0
    total_tokens = 0
    total_cost = 0.0
    by_model: dict[str, dict[str, Any]] = {}
    by_endpoint: dict[str, dict[str, Any]] = {}

    with LOG_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if seconds is not None:
                # ts формат ISO с timezone (UTC); конвертим в epoch
                try:
                    ts_epoch = datetime.fromisoformat(rec["ts"]).timestamp()
                except Exception:  # noqa: BLE001
                    continue
                if (now - ts_epoch) > seconds:
                    continue
            total_requests += 1
            if not rec.get("ok", True):
                total_errors += 1
            total_tokens += int(rec.get("total_tokens", 0))
            if rec.get("cost_rub") is not None:
                total_cost += float(rec["cost_rub"])

            m = rec.get("model", "?")
            by_model.setdefault(m, {"requests": 0, "tokens": 0, "cost_rub": 0.0})
            by_model[m]["requests"] += 1
            by_model[m]["tokens"] += int(rec.get("total_tokens", 0))
            if rec.get("cost_rub") is not None:
                by_model[m]["cost_rub"] += float(rec["cost_rub"])

            e = rec.get("endpoint", "?")
            by_endpoint.setdefault(e, {"requests": 0, "tokens": 0, "cost_rub": 0.0})
            by_endpoint[e]["requests"] += 1
            by_endpoint[e]["tokens"] += int(rec.get("total_tokens", 0))
            if rec.get("cost_rub") is not None:
                by_endpoint[e]["cost_rub"] += float(rec["cost_rub"])

    return {
        "window": window,
        "requests": total_requests,
        "errors": total_errors,
        "total_tokens": total_tokens,
        "total_cost_rub": round(total_cost, 4),
        "by_model": by_model,
        "by_endpoint": by_endpoint,
        "log_path": str(LOG_PATH),
        "exists": True,
    }
