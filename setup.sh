#!/bin/bash
set -e

APP_DIR="/home/hirota/press"
SERVICE_NAME="myapp"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== Press Counter Setup ==="

# 1. パッケージインストール
echo "[1/4] Installing packages..."
sudo apt update -qq
sudo apt install -y python3-lgpio python3-requests

# 2. アプリディレクトリ作成 & ファイルコピー
echo "[2/4] Copying files..."
mkdir -p "$APP_DIR"
cp "$SCRIPT_DIR/SendToPortal.py" "$APP_DIR/"

# 3. raspi_no 設定
if [ ! -f "$APP_DIR/config.txt" ]; then
    read -p "Enter raspi_no (e.g. raspi_01): " RASPI_NO
    if [ -z "$RASPI_NO" ]; then
        RASPI_NO="unknown"
    fi
    echo "$RASPI_NO" > "$APP_DIR/config.txt"
    echo "  -> Set raspi_no = $RASPI_NO"
else
    echo "  -> config.txt already exists: $(cat "$APP_DIR/config.txt")"
fi

# 4. systemd サービス登録
echo "[3/4] Setting up systemd service..."
sudo tee /etc/systemd/system/${SERVICE_NAME}.service > /dev/null << EOF
[Unit]
Description=Press Counter - SendToPortal
After=network.target

[Service]
ExecStart=/usr/bin/python3 ${APP_DIR}/SendToPortal.py
WorkingDirectory=${APP_DIR}
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1
User=root

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable ${SERVICE_NAME}.service
sudo systemctl restart ${SERVICE_NAME}.service

# 5. 確認
echo "[4/4] Verifying..."
sleep 2
sudo systemctl status ${SERVICE_NAME}.service --no-pager

echo ""
echo "=== Setup Complete ==="
echo "  App dir:  $APP_DIR"
echo "  raspi_no: $(cat "$APP_DIR/config.txt")"
echo "  Log:      sudo journalctl -u ${SERVICE_NAME} -f"
