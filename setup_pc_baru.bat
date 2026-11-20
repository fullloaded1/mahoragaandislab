@echo off
chcp 65001 >nul
title Setup Harvey di PC Baru

echo ===================================================
echo     INSTALLASI DAN PERSIAPAN HARVEY AI SALES
echo ===================================================
echo.

:: 1. Cek Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python belum terinstall atau belum masuk ke PATH!
    echo Silakan install Python 3.10, 3.11, atau 3.12 dari python.org.
    echo Jangan lupa centang "Add python.exe to PATH" saat install.
    pause
    exit /b 1
)

echo [1/4] Membuat Virtual Environment (.venv)...
if not exist ".venv" (
    python -m venv .venv
)

echo [2/4] Mengaktifkan Virtual Environment...
call .venv\Scripts\activate.bat

echo [3/4] Menginstall Dependensi Harvey...
python -m pip install --upgrade pip
pip install -e .

echo [4/4] Mempersiapkan Browser Automation Playwright...
playwright install chromium

echo.
echo ===================================================
echo    INSTALLASI SELESAI! HARVEY SIAP DIGUNAKAN
echo ===================================================
echo.
echo Anda sekarang bisa menjalankan: start_harvey.bat
echo.
pause

