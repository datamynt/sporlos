FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

# Avhengigheter først (cache-vennlig)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Appkode + tracker + schema + brand-assets (self-hostet font)
COPY app/ ./app/
COPY tracker/ ./tracker/
COPY assist/ ./assist/
COPY db/ ./db/
COPY static/ ./static/
# integrasjoner: appen leser integrations/shopify/sporlos-pixel.js ved oppstart
COPY integrations/ ./integrations/
# one-off ops script, run with `docker compose exec app python scripts/purge_bot_signups.py`;
# only this file: the other scripts/ files are asset generators (fonts, PIL) that don't belong in the image
COPY scripts/purge_bot_signups.py ./scripts/purge_bot_signups.py

# Kjør som ikke-root
# /app/backups: where purge_bot_signups.py --apply writes its pre-delete backup. The compose files
# bind-mount ./backups over it, so that host directory must exist and be writable by uid 1000
# (mkdir backups, as the deploy user) before the first `up`; Docker would otherwise create it as root.
RUN useradd -m appuser && mkdir -p /app/backups && chown appuser:appuser /app/backups
USER appuser

EXPOSE 8000
# init = idempotent skjema-migrering (IF NOT EXISTS/ALTER). MÅ kjøre før appen:
# 15.07 deployet en kolonne-endring uten migrering → ingest droppet events
# stille og dashbordet 500-et til `manage init` ble kjørt manuelt på b550.
CMD ["sh", "-c", "python -m app.manage init && exec uvicorn app.main:app --host 0.0.0.0 --port 8000"]
