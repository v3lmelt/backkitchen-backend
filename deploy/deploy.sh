#!/usr/bin/env bash
# Run as www-data; the signed GitHub push hook supplies the commit SHA.
set -Eeuo pipefail
umask 077

cd /opt/backkitchen/backend
exec 9>/opt/backkitchen/deploy.lock
flock -w 600 9 || { echo 'Another deployment did not finish in time.' >&2; exit 1; }

expected=${1:-}
if [[ -n "$expected" && ! "$expected" =~ ^[0-9a-f]{40}$ ]]; then
    echo 'Invalid deployment revision.' >&2
    exit 1
fi
[[ $(git branch --show-current) == master ]] || { echo 'Expected master checkout.' >&2; exit 1; }
git diff --quiet && git diff --cached --quiet || { echo 'Tracked local changes require attention.' >&2; exit 1; }
command -v ffmpeg >/dev/null
command -v ffprobe >/dev/null
git fetch origin master
target=$(git rev-parse origin/master)
if [[ -n "$expected" && "$expected" != "$target" ]]; then
    echo "Push $expected has been superseded by $target; skipping."
    exit 0
fi
marker=/opt/backkitchen/deployed-revision
if [[ $(git rev-parse HEAD) == "$target" && -f "$marker" && $(cat "$marker") == "$target" ]]; then
    curl --fail --silent --show-error --max-time 10 http://127.0.0.1:8000/api/health
    echo ' Already deployed.'
    exit 0
fi
git merge --ff-only "$target"
.venv/bin/python -m pip install --quiet -r requirements.txt

# Stop writes while taking the recovery snapshot and applying migrations.
# Failures remain visible in the webhook journal; never silently reset user data.
trap 'echo "Deployment failed at line $LINENO. Inspect journalctl -u webhook -u backkitchen; database backups are in /opt/backkitchen/backups." >&2' ERR
sudo -n /usr/bin/systemctl stop backkitchen
.venv/bin/python - <<'PY'
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from sqlalchemy.engine import make_url
from app.config import settings

url = make_url(settings.DATABASE_URL)
if url.get_backend_name() != 'sqlite':
    raise RuntimeError('Configure a database backup procedure before deploying a non-SQLite database.')
database = Path(url.database).resolve()
if not database.is_file():
    raise RuntimeError('Expected an existing production SQLite database.')
directory = Path('/opt/backkitchen/backups')
directory.mkdir(exist_ok=True)
backup = directory / ('before-deploy-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '.db')
with sqlite3.connect(database) as source, sqlite3.connect(backup) as destination:
    source.backup(destination)
print('Database backup:', backup)
PY
.venv/bin/python -m alembic upgrade head
sudo -n /usr/bin/systemctl start backkitchen
healthy=false
for attempt in $(seq 1 60); do
    if curl --fail --silent --max-time 2 http://127.0.0.1:8000/api/health; then
        healthy=true
        break
    fi
    sleep 1
done
[[ "$healthy" == true ]] || { echo 'Backend health check failed.' >&2; exit 1; }
systemctl is-active --quiet backkitchen
printf '%s\n' "$target" > "$marker"
echo " Deployed $target at $(date --iso-8601=seconds)"
