@chcp 65001 > nul
@echo off
REM ============================================================================
REM  ClickJurist Production — автоматический деплой (start.bat)
REM
REM  Оркестрация запуска:
REM    1. Остановка старых процессов
REM    2. Очистка кэша Python (__pycache__, .pytest_cache)
REM    3. Создание / активация виртуального окружения
REM    4. Установка зависимостей
REM    5. Старт сервера FastAPI (uvicorn, порт 8001)
REM
REM  Использование:
REM    start.bat           — запустить сервер
REM    start.bat --dry     — сборка окружения без запуска сервера
REM ============================================================================
setlocal EnableDelayedExpansion

cd /d "%~dp0"

echo [1/5] Остановка старых процессов...
for /f "tokens=5" %%a in ('netstat -aon ^| findstr :8001') do taskkill /f /pid %%a 2>nul
REM Kill any uvicorn/python processes from previous run
taskkill /f /im uvicorn.exe >nul 2>&1
echo     -> готово

echo [2/5] Очистка кэша Python...
for /d /r %%d in (__pycache__) do (
    if exist "%%d" rmdir /s /q "%%d" 2>nul
)
if exist ".pytest_cache" rmdir /s /q ".pytest_cache" 2>nul
echo     -> готово

echo [3/5] Создание виртуального окружения...
if not exist "venv\Scripts\python.exe" (
    "python" -m venv "venv"
    if errorlevel 1 (
        echo [ERROR] Не удалось создать виртуальное окружение
        exit /b 1
    )
)
echo     -> готово

echo [4/5] Установка зависимостей...
call "%~dp0venv\Scripts\activate.bat"
"%~dp0venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
if exist "backend\requirements.txt" (
    "%~dp0venv\Scripts\python.exe" -m pip install -r "backend\requirements.txt" --quiet
)
echo     -> готово

if "%~1"=="--dry" (
    echo [5/5] Сборка завершена (режим --dry, сервер не запущен).
    exit /b 0
)

echo [5/5] Запуск сервера...
echo     -> http://localhost:8001  (API: http://localhost:8001/docs)
echo     -> Ctrl+C чтобы остановить
"%~dp0venv\Scripts\python.exe" -m uvicorn backend.main:app --host 0.0.0.0 --port 8001 --reload
goto :eof