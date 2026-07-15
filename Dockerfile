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

# Kjør som ikke-root
RUN useradd -m appuser
USER appuser

EXPOSE 8000
# init = idempotent skjema-migrering (IF NOT EXISTS/ALTER). MÅ kjøre før appen:
# 15.07 deployet en kolonne-endring uten migrering → ingest droppet events
# stille og dashbordet 500-et til `manage init` ble kjørt manuelt på b550.
CMD ["sh", "-c", "python -m app.manage init && exec uvicorn app.main:app --host 0.0.0.0 --port 8000"]
