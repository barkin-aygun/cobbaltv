#!/bin/bash
# Run once on the Pi to install deps and register the systemd service.
# Usage: bash install.sh

set -e

INSTALL_DIR="$(cd "$(dirname "$0")" && pwd)"
CURRENT_USER="$(whoami)"
PYTHON="$(which python3)"
SERVICE_FILE="/etc/systemd/system/bloodcam.service"

# Create .env from example if it doesn't exist yet
if [ ! -f "$INSTALL_DIR/.env" ]; then
    cp "$INSTALL_DIR/env.example" "$INSTALL_DIR/.env"
    echo ""
    echo "  Created $INSTALL_DIR/.env"
    echo "  Fill in BOT_ENDPOINT and UPLOAD_SECRET before starting the service."
fi

echo ""
echo "==> Writing systemd service to $SERVICE_FILE..."
sudo tee "$SERVICE_FILE" > /dev/null << EOF
[Unit]
Description=bloodcam — camera capture and upload
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=$PYTHON $INSTALL_DIR/camera.py
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$INSTALL_DIR/.env
User=$CURRENT_USER
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

echo "==> Enabling service (starts on every boot)..."
sudo systemctl daemon-reload
sudo systemctl enable bloodcam

echo ""
echo "All done. Useful commands:"
echo "  sudo systemctl start bloodcam      — start now"
echo "  sudo systemctl stop bloodcam       — stop"
echo "  sudo systemctl restart bloodcam    — restart after code changes"
echo "  journalctl -u bloodcam -f          — live logs"
