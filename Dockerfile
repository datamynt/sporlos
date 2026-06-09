FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

# Avhengigheter først (cache-vennlig)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Appkode + tracker + schema
COPY app/ ./app/
COPY tracker/ ./tracker/
COPY db/ ./db/

# Kjør som ikke-root
RUN useradd -m appuser
USER appuser

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
