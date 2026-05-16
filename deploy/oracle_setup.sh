#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_USER="$(whoami)"
PORT="${PORT:-8000}"

echo "==> Installing system packages"
sudo apt-get update
sudo apt-get install -y python3-pip python3-venv git nginx

echo "==> Creating Python virtual environment"
cd "$APP_DIR"
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip wheel
pip install -r requirements.txt

echo "==> Creating data folders"
mkdir -p "$APP_DIR/data/uploads"

if [ ! -f "$APP_DIR/.env" ]; then
  echo "==> Creating .env"
  read -r -p "Paste your new Gemini API key: " GEMINI_API_KEY
  read -r -p "Secret key, or press Enter to generate one: " SECRET_KEY
  if [ -z "$SECRET_KEY" ]; then
    SECRET_KEY="$(python3 - <<'PY'
import secrets
print(secrets.token_hex(32))
PY
)"
  fi

  cat > "$APP_DIR/.env" <<EOF
GEMINI_API_KEY=$GEMINI_API_KEY
SECRET_KEY=$SECRET_KEY
GEMINI_MODEL=gemini-2.5-flash
DATABASE_PATH=$APP_DIR/data/qslide.db
UPLOAD_FOLDER=$APP_DIR/data/uploads
FLASK_DEBUG=false
EOF
  chmod 600 "$APP_DIR/.env"
else
  echo "==> Keeping existing .env"
fi

echo "==> Installing systemd service"
sudo tee /etc/systemd/system/qslide.service >/dev/null <<EOF
[Unit]
Description=Qslide Flask App
After=network.target

[Service]
User=$APP_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
ExecStart=$APP_DIR/.venv/bin/gunicorn --workers 2 --bind 127.0.0.1:$PORT app:app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now qslide

echo "==> Configuring Nginx"
sudo tee /etc/nginx/sites-available/qslide >/dev/null <<EOF
server {
    listen 80;
    server_name _;

    client_max_body_size 50M;

    location / {
        proxy_pass http://127.0.0.1:$PORT;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
EOF

sudo ln -sfn /etc/nginx/sites-available/qslide /etc/nginx/sites-enabled/qslide
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl restart nginx

echo "==> Checking app service"
sudo systemctl --no-pager --full status qslide || true

echo
echo "Qslide is installed on this VM."
echo "If Oracle port 80 is open, visit: http://YOUR_PUBLIC_IP"
echo "To check logs later: sudo journalctl -u qslide -f"
