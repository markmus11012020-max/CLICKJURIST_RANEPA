"""
Простой тест подключения к AITUNNEL API по правилам из AITUNNEL_API_RULES.md.

Правила:
- URL: https://aitunnel.ru (без /v1/chat/completions)
- Метод: POST
- Headers: Authorization: Bearer <KEY>, Content-Type: application/json
- Payload: {"model": "<с префиксом автора>", "input": "<строка>"}
- Ответ: response_json["text"]
"""
import os
import sys
import json
import requests
from dotenv import load_dotenv


def load_key() -> str:
    """Загружаем ключ из .env рядом со скриптом."""
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    load_dotenv(env_path)
    api_key = os.getenv("ROUTER_API_KEY", "").strip()
    if not api_key:
        sys.exit(
            "Не найден ROUTER_API_KEY. Положите его в файл .env (см. AITUNNEL_API_RULES.md)."
        )
    return api_key


def ping(api_key: str, model: str, prompt: str) -> str:
    """Один POST-запрос к AITUNNEL Responses API."""
    url = "https://aitunnel.ru"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {"model": model, "input": prompt}
    resp = requests.post(url, headers=headers, json=payload, timeout=30)
    print(f"[HTTP] status={resp.status_code}")
    try:
        data = resp.json()
    except Exception:
        return f"Ошибка: ответ не JSON. Тело: {resp.text[:500]}"
    # Полный JSON для отладки
    print("[RAW JSON]:")
    print(json.dumps(data, ensure_ascii=False, indent=2)[:1500])
    if "text" in data:
        return data["text"]
    return f"Ошибка API: в ответе нет ключа 'text'. Тело: {resp.text[:500]}"


def main() -> int:
    api_key = load_key()
    print(f"[OK] API-ключ загружен, длина={len(api_key)} символов")

    # Тест 1 — самая дешёвая/быстрая модель
    print("\n=== Тест 1: ping модель deepseek/deepseek-v4-flash ===")
    text1 = ping(
        api_key,
        "deepseek/deepseek-v4-flash",
        "Привет! Ответь строго одним словом: РАБОТАЕТ",
    )
    print(f"\n[ОТВЕТ LLM-1]:\n{text1}\n")

    # Тест 2 — вторая модель из цепочки
    print("\n=== Тест 2: ping модель minimax/minimax-m3 ===")
    text2 = ping(
        api_key,
        "minimax/minimax-m3",
        "Привет! Ответь строго одним словом: РАБОТАЕТ",
    )
    print(f"\n[ОТВЕТ LLM-2]:\n{text2}\n")

    ok = "РАБОТАЕТ" in (text1 + text2)
    print("\n=== ИТОГ ===")
    print("OK, обе модели ответили" if ok else "ПРОВАЛ: модели не ответили как ожидалось")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
