import requests

# ВСТАВЬТЕ СЮДА ВАШ НОВЫЙ ТОКЕН
API_KEY = "sk-aitunnel-I48aGgM5GpMR5YIgvwAwRG44W815ykhc" 

# ОФИЦИАЛЬНЫЙ АДРЕС AITUNNEL RESPONSES API
URL = "https://aitunnel.ru"

HEADERS = {
    "Authorization": f"Bearer {API_KEY.strip()}",
    "Content-Type": "application/json"
}

# В Responses API используется 'input' вместо 'messages'
PAYLOAD = {
    "model": "deepseek/deepseek-v4-flash",
    "input": "Привет! Ответь строго одним словом: РАБОТАЕТ"
}

print("Отправка запроса в AITUNNEL...")
try:
    response = requests.post(URL, json=PAYLOAD, headers=HEADERS, timeout=15)
    print(f"Статус ответа сервера: {response.status_code}")
    print("Полный ответ от провайдера:")
    print(response.text)
except Exception as e:
    print(f"Ошибка сети: {e}")