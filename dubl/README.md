# dubl.top — бекенд (ізольований сервіс)

Пошук дублів оголошень нерухомості за фото. Сервіс **повністю ізольований** від
ocinka.pro та інших сайтів на сервері: окремий чекаут у `/opt/dubl.top`, окремий
`venv`, окремий `.env.dubl`, окремий порт `8020`, окремий `systemd`-юніт. Жоден
файл ocinka не змінюється.

## Етапи
- **M1 (готово):** `POST /api/analyze` — Gemini читає 1–3 скриншоти і повертає
  параметри об'єкта (місто, тип, кімнати, площа, поверх, адреса, ціна, власник/
  агентство, телефон, к-сть фото).
- M2: кандидати з сайтів агентств (карта + пошук), фільтр ±10% площа / кімнати строго.
- M3: порівняння фото (pHash/ORB → CLIP) — підтвердження дубля (мін. 2–3 збіги).
- M4: зріз цін DIM.RIA / OLX / rieltor + таблиця видачі.

## Деплой M1 (PuTTY, root)

Окремий чекаут гілки `dubl-top` — ocinka не чіпаємо:
```bash
git clone -b dubl-top https://github.com/kapystenko-cmd/winner.git /opt/dubl.top
cd /opt/dubl.top
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r dubl/requirements.txt
```

Налаштування (ті самі ключі, що в ocinka, але окрема копія):
```bash
cp dubl/env.dubl.example .env.dubl
nano .env.dubl          # впиши GEMINI_API_KEY (мінімум для M1)
mkdir -p /var/lib/dubl/uploads
chown -R www-data:www-data /var/lib/dubl /opt/dubl.top
```

Сервіс:
```bash
cp deploy/dubl-top.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now dubl-top
systemctl status dubl-top --no-pager
curl -s http://127.0.0.1:8020/api/health      # очікуємо {"status":"ok",...}
```

nginx `/api` → сервіс. Відкрий `/etc/nginx/sites-available/dubl.top`, додай усередину
**server-блоку на 443** (який створив certbot) location з `deploy/dubl-top.nginx.conf`,
поряд з наявним `location / { ... }`:
```bash
nano /etc/nginx/sites-available/dubl.top
nginx -t && systemctl reload nginx
curl -s https://dubl.top/api/health
```

## Оновлення (нові етапи)
```bash
cd /opt/dubl.top
git pull
./venv/bin/pip install -r dubl/requirements.txt
systemctl restart dubl-top
```

## Перевірка /api/analyze вручну
```bash
curl -s -X POST https://dubl.top/api/analyze \
  -F "files=@screenshot1.jpg" -F "files=@screenshot2.jpg" | python3 -m json.tool
```
