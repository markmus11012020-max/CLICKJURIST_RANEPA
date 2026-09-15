@echo off
REM ============================================================================
REM  ClickJurist Production — автоматический деплой (start.bat)
REM
REM  Оркестрация запуска:
REM    1. Остановка старых процессов
REM    2. Очистка кэша Python (__pycache__, .pytest_cache)
REM    3. Создание / активация виртуального окружения
REM    4. Установка зависимостей
REM    5. Старт сервера FastAPI
REM
REM  Использование:
REM    start.bat           — запустить сервер
REM    start.bat --dry     — сборка окружения без запуска сервера
REM ============================================================================
setlocal EnableDelayedExpansion

cd /d "%~dp0"

echo [1/5] Остановка старых процессов...
for /f "tokens=5" %%a in ('netstat -aon ^| findstr :8000 2^>nul') do (
    if defined PID (call :kill_silent %%a)
)
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
    python -m venv venv
    if errorlevel 1 (
        echo [ERROR] Не удалось создать виртуальное окружение
        exit /b 1
    )
)
echo     -> готово

echo [4/5] Установка зависимостей...
call venv\Scripts\activate.bat
python -m pip install --upgrade pip --quiet
if exist backend\requirements.txt (
    pip install -r backend\requirements.txt --quiet
)
echo     -> готово

if "%~1"=="--dry" (
    echo [5/5] Сборка завершена (режим --dry, сервер не запущен).
    exit /b 0
)

echo [5/5] Запуск сервера...
echo     -> http://localhost:8000  (API: http://localhost:8000/api/docs)
echo     -> Ctrl+C чтобы остановить
python -m backend.main
goto :eof

:kill_silent
taskkill /f /pid %1 >nul 2>&1
goto :eof