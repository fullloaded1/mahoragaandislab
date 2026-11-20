@echo off
title OpenWA WhatsApp Gateway
echo ===================================================
echo   Menjalankan OpenWA Gateway untuk Harvey AI
echo ===================================================

docker info >nul 2>&1
if %errorlevel% neq 0 (
    echo.
    echo [!] PERHATIAN: Docker Desktop belum berjalan.
    echo     Silakan buka aplikasi "Docker Desktop" dari Start Menu Windows Anda.
    echo     Setelah ikon Docker di taskbar berwarna hijau (Engine Running),
    echo     tekan tombol apa saja di sini untuk melanjutkan...
    echo.
    pause
)

echo.
echo Mengunduh dan menjalankan container OpenWA...
docker run -d --name openwa --restart unless-stopped -p 2785:2785 -e AUTO_START_SESSIONS=true -e DATABASE_TYPE=sqlite -e DATABASE_NAME=./data/openwa.sqlite -e SESSION_DATA_PATH=./data/sessions -e WEBHOOK_SSRF_PROTECT=false -e SSRF_ALLOWED_HOSTS=host.docker.internal,localhost -v openwa_data:/app/data ghcr.io/rmyndharis/openwa:latest

echo.
echo ===================================================
echo [OK] OpenWA berhasil dijalankan di http://localhost:2785
echo      Buka http://localhost:2785 di browser untuk melihat sesi/scan QR.
echo      Harvey akan otomatis terhubung ke OpenWA!
echo ===================================================
echo.
pause

