@echo off
title Harvey - Login WhatsApp Web (Scan QR Sekali Saja)
cd /d "%~dp0"
".venv\Scripts\harvey.exe" whatsapp login
pause

