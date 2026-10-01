#!/bin/bash
# ocinka.pro — повна установка бекенду на Contabo VPS (Ubuntu)
# Запускати під root або через sudo
set -e

echo "=========================================="
echo "  ocinka.pro — встановлення бекенду"
echo "=========================================="

# 1. Системні пакети
echo "[1/8] Оновлення системи та встановлення пакетів..."
apt update
apt install -y python3.11 python3.11-venv python3-pip \
    postgresql postgresql-contrib redis-server \
    nginx certbot python3-certbot-nginx \
    libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf2.0-0 \
    build-essential libffi-dev libpq-dev \
    supervisor

# 2. PostgreSQL
echo "[2/8] Налаштування PostgreSQL..."
sudo -u postgres psql -c "CREATE USER ocinka WITH PASSWORD 'CHANGE_THIS_PASSWORD';" 2>/dev/null || true
sudo -u postgres psql -c "CREATE DATABASE ocinka OWNER ocinka;" 2>/dev/null || true
sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE ocinka TO ocinka;"

# 3. Redis
echo "[3/8] Запуск Redis..."
systemctl enable redis-server --now

# 4. Директорії для файлів
echo "[4/8] Створення директорій..."
mkdir -p /var/lib/ocinka/{uploads,generated,screenshots}
mkdir -p /opt/ocinka
mkdir -p /var/backups/ocinka
mkdir -p /var/log/ocinka

# 5. Копіювання бекенду
echo "[5/8] Встановлення Python-оточення..."
cp -r /root/backend/* /opt/ocinka/ 2>/dev/null || echo "  → Скопіюйте файли бекенду в /opt/ocinka/ через WinSCP"
cd /opt/ocinka

python3.11 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -e . 2>/dev/null || pip install fastapi uvicorn sqlalchemy asyncpg alembic python-jose passlib python-multipart python-dotenv httpx google-generativeai anthropic python-docx reportlab pillow cryptography pydantic pydantic-settings

# 6. Конфігурація
echo "[6/8] Конфігурація..."
if [ ! -f /opt/ocinka/.env ]; then
    cp /opt/ocinka/.env.example /opt/ocinka/.env
    # Генеруємо випадковий SECRET_KEY
    SECRET=$(python3 -c "import secrets; print(secrets.token_urlsafe(48))")
    sed -i "s/your-secret-key-change-this-to-random-64-chars/$SECRET/" /opt/ocinka/.env
    echo "  → УВАГА: Відредагуйте /opt/ocinka/.env — вставте API-ключі!"
fi

# 7. Systemd юніт
echo "[7/8] Налаштування systemd..."
cat > /etc/systemd/system/ocinka-api.service << 'EOF'
[Unit]
Description=ocinka.pro API
After=network.target postgresql.service redis-server.service

[Service]
Type=simple
User=root
WorkingDirectory=/opt/ocinka
ExecStart=/opt/ocinka/venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 2
Restart=always
RestartSec=5
Environment=PATH=/opt/ocinka/venv/bin

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable ocinka-api --now

# 8. Nginx — додаємо /api/ прокси
echo "[8/8] Оновлення nginx..."
NGINX_CONF="/etc/nginx/sites-available/ocinka.pro"
if [ -f "$NGINX_CONF" ]; then
    # Додаємо location /api/ якщо ще немає
    if ! grep -q "location /api/" "$NGINX_CONF"; then
        sed -i '/location \/ {/i\    location /api/ {\n        proxy_pass http://127.0.0.1:8000;\n        proxy_set_header Host $host;\n        proxy_set_header X-Real-IP $remote_addr;\n        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n        proxy_set_header X-Forwarded-Proto $scheme;\n        client_max_body_size 50M;\n    }\n' "$NGINX_CONF"
        nginx -t && systemctl reload nginx
    fi
fi

echo ""
echo "=========================================="
echo "  ВСТАНОВЛЕННЯ ЗАВЕРШЕНО"
echo "=========================================="
echo ""
echo "Наступні кроки:"
echo "1. Відредагуйте /opt/ocinka/.env — вставте API-ключі (Gemini, DIM.RIA)"
echo "2. Перезапустіть API: systemctl restart ocinka-api"
echo "3. Перевірте: curl http://localhost:8000/api/health"
echo "4. Перевірте: curl https://ocinka.pro/api/health"
echo ""
echo "Логи: journalctl -u ocinka-api -f"
echo "БД:   sudo -u postgres psql ocinka"
echo ""
