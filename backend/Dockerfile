FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Dependências do sistema:
# - gcc, libpq-dev: para o psycopg2
# - tesseract-ocr + tesseract-ocr-por: OCR de PDFs escaneados (em português)
# - poppler-utils: converte PDF em imagem (pdf2image) para o OCR
# - libreoffice-writer: converte editais publicados como Word (.doc/.docx) em
#   texto — achado real: o PNCP permite órgão publicar o edital em .doc, e o
#   sistema só sabia ler PDF (dava "sem_texto", como se fosse imagem
#   escaneada, quando na real era só um formato de arquivo não suportado).
#   Só o pacote "writer" (não o libreoffice completo, que também traz Calc/
#   Impress/Draw) — bem mais leve, e é só texto que precisamos ler aqui.
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev tesseract-ocr tesseract-ocr-por poppler-utils libreoffice-writer \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000
# Shell form (sem colchetes) para que ${PORT} seja expandido.
# O Render injeta PORT (ex.: 10000); local cai no 8000.
CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
