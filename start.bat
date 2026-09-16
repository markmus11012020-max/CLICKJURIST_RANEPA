@chcp 65001 > nul
@echo off
REM ============================================================================
REM  ClickJurist Production — Линейный запуск без багов с пробелами
REM ============================================================================
setlocal EnableDelayedExpansion

cd /d "%~dp0"

echo [1/5] Освобождение портов...
for /f "tokens=5" %%a in ('netstat -aon ^| findstr :8001') do taskkill /f /pid %%a >nul 2>&1
echo     -> готово

echo [2/5] Очистка кэша Python...
for /d /r %%d in (__pycache__) do if exist "%%d" rmdir /s /q "%%d" 2>nul
if exist ".pytest_cache" rmdir /s /q ".pytest_cache" 2>nul
echo     -> готово

echo [3/5] Проверка виртуального окружения...
if exist "venv\Scripts\python.exe" goto :activate_env
echo Создание нового виртуального окружения venv...
"python" -m venv venv

:activate_env
echo     -> готово

echo [4/5] Установка зависимостей (PyJWT, FastAPI)...
call "%~dp0venv\Scripts\activate.bat"

echo Обновление pip...
python -m pip install --upgrade pip --quiet

echo Синхронизация пакетов...
python -m pip install -r "%~dp0backend\requirements.txt"
if errorlevel 1 (
    echo [ERROR] Ошибка при установке библиотек!
    pause
    exit /b 1
)
echo     -> готово

echo [5/5] Запуск сервера ClickJurist...
echo     -> Локальный адрес: http://127.0.0.1:8001
echo ----------------------------------------------------------------------

python -m uvicorn backend.main:app --host 127.0.0.1 --port 8001
if errorlevel 1 (
    echo [ERROR] Uvicorn завершил работу со сбоем.
    pause
)
goto :eof
