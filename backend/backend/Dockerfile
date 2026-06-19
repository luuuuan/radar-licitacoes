FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Dependências do sistema (psycopg2 etc.)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000
# Shell form (sem colchetes) para que ${PORT} seja expandido.
# O Render injeta PORT (ex.: 10000); local cai no 8000.
CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
