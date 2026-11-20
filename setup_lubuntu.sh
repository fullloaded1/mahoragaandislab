#!/usr/bin/env bash
set -e

echo "=========================================================="
echo "🚀 Memulai Instalasi Otomatis Harvey di Lubuntu..."
echo "=========================================================="

USER_NAME=$(whoami)
HARVEY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "📍 Folder Harvey: $HARVEY_DIR"
echo "👤 User: $USER_NAME"

echo "📦 1. Memperbarui paket sistem dan menginstall dependensi dasar..."
sudo apt update
sudo apt install -y python3 python3-venv python3-pip curl

echo "🐍 2. Menyiapkan Python Virtual Environment..."
cd "$HARVEY_DIR"
if [ ! -d ".venv" ]; then
    python3 -m venv .venv
fi
source .venv/bin/activate
pip install --upgrade pip setuptools wheel
pip install -e .

echo "🌐 3. Menginstall Playwright & Browser Chromium Linux..."
playwright install-deps chromium || true
playwright install chromium

echo "🔒 4. Menyesuaikan izin folder data..."
chmod -R 755 "$HARVEY_DIR/data" || true

echo "⚙️ 5. Menyiapkan Systemd Service agar Harvey jalan otomatis 24/7..."
SERVICE_FILE="/etc/systemd/system/harvey.service"
sudo bash -c "cat <<EOF > $SERVICE_FILE
[Unit]
Description=Harvey Autonomous AI Sales Agent (PT Andis Sentral Laboratorium)
After=network.target network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER_NAME
WorkingDirectory=$HARVEY_DIR
Environment=\"PATH=$HARVEY_DIR/.venv/bin:/usr/local/bin:/usr/bin:/bin\"
Environment=\"PYTHONUNBUFFERED=1\"
ExecStart=$HARVEY_DIR/.venv/bin/harvey run
Restart=always
RestartSec=15

[Install]
WantedBy=multi-user.target
EOF"

echo "🔄 6. Mengaktifkan dan memulai Harvey Service..."
sudo systemctl daemon-reload
sudo systemctl enable harvey
sudo systemctl restart harvey

echo "=========================================================="
echo "✅ SUKSES! Harvey berhasil diinstall dan aktif di latar belakang."
echo "=========================================================="
echo "Perintah penting:"
echo "  - Cek log live       : journalctl -u harvey -f"
echo "  - Cek status         : sudo systemctl status harvey"
echo "  - Stop sementara     : sudo systemctl stop harvey"
echo "  - Start kembali      : sudo systemctl start harvey"
echo "  - Buka Web Dashboard : harvey dashboard"
echo "  - Login E-Katalog v6 : harvey inaproc login"
echo "  - Cek Status INAPROC : harvey inaproc status"
echo "  - Mini Kompetisi Bid : harvey inaproc bids"
echo "=========================================================="

