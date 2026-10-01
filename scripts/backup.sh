#!/bin/bash
# Щоденний бекап БД та файлів
# Додати в cron: 0 3 * * * /opt/ocinka/scripts/backup.sh

BACKUP_DIR="/var/backups/ocinka"
DATE=$(date +%Y%m%d_%H%M)
DB_NAME="ocinka"

# Дамп БД
pg_dump -U ocinka "$DB_NAME" | gzip > "$BACKUP_DIR/db_$DATE.sql.gz"

# Видалення старих бекапів (тримаємо 30 днів)
find "$BACKUP_DIR" -name "db_*.sql.gz" -mtime +30 -delete

# Видалення старих сканів (24 години)
find /var/lib/ocinka/uploads -type f -mmin +1440 -delete
find /var/lib/ocinka/uploads -type d -empty -delete

# Видалення старих звітів (6 місяців)
find /var/lib/ocinka/generated -type f -mtime +180 -delete
find /var/lib/ocinka/generated -type d -empty -delete

echo "Backup done: $BACKUP_DIR/db_$DATE.sql.gz"
