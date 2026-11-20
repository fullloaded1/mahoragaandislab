@echo off
cd /d "%~dp0"
title MAHORAGA Adaptive Sales Engine (AndisLab)

echo ===================================================
echo      MEMULAI MAHORAGA AI SALES ENGINE (ANDISLAB)
echo ===================================================
echo.

if not exist ".venv\Scripts\activate.bat" (
    echo [ERROR] Virtual environment belum dibuat!
    echo Silakan jalankan setup_pc_baru.bat terlebih dahulu.
    pause
    exit /b 1
)

call .venv\Scripts\activate.bat

echo [1/4] Memeriksa OpenWA Gateway (Port 2785)...
powershell -Command "try { $r = Invoke-WebRequest -Uri 'http://localhost:2785/health' -TimeoutSec 2 -UseBasicParsing; exit 0 } catch { exit 1 }"
if %errorlevel% neq 0 (
    echo [!] OpenWA belum aktif, mencoba menyalakan container Docker...
    docker start openwa >nul 2>&1
    if %errorlevel% neq 0 (
        docker run -d --name openwa --restart unless-stopped -p 2785:2785 -e AUTO_START_SESSIONS=true -e DATABASE_TYPE=sqlite -e DATABASE_NAME=./data/openwa.sqlite -e SESSION_DATA_PATH=./data/sessions -e WEBHOOK_SSRF_PROTECT=false -e SSRF_ALLOWED_HOSTS=host.docker.internal,localhost -v openwa_data:/app/data ghcr.io/rmyndharis/openwa:latest >nul 2>&1
    )
    timeout /t 3 /nobreak >nul
) else (
    echo [OK] OpenWA Gateway sudah aktif di http://localhost:2785
)

echo [2/4] Membersihkan sesi Dashboard / Tunnel lama (Port 5555)...
python -c "import subprocess; out=subprocess.check_output(['netstat','-ano'],text=True); [subprocess.run(['taskkill','/F','/PID',l.split()[-1]],capture_output=True) for l in out.splitlines() if ':5555' in l and 'LISTENING' in l and l.split()[-1].isdigit()]" >nul 2>&1
taskkill /F /IM cloudflared.exe >nul 2>&1

echo [3/4] Mengecek konektivitas Gemini dan Pipeline...
python -m harvey status --check

echo.
echo [4/4] Memulai Server Dashboard MAHORAGA (Tunnel + Kirim Link WA)...
start "MAHORAGA Dashboard" .venv\Scripts\python.exe -m harvey dashboard --allow-remote --tunnel --send-wa

timeout /t 3 /nobreak >nul

echo.
echo ===================================================
echo   Menjalankan MAHORAGA Heartbeat Loop (Draft Mode)
echo   Tekan Ctrl+C untuk berhenti dengan aman.
echo ===================================================
echo.
.venv\Scripts\python.exe -m harvey run

pause
