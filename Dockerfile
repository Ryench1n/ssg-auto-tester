# SSG Smoke Tester — Playwright (Chromium) + LibreOffice (тайлангийн тулгалт)
FROM mcr.microsoft.com/playwright/python:v1.56.0-noble

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Ulaanbaatar

RUN apt-get update \
 && apt-get install -y --no-install-recommends libreoffice-calc libreoffice-writer poppler-utils fonts-dejavu \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN python scripts/make_report_template.py

EXPOSE 8000
CMD ["python", "-m", "app", "--host", "0.0.0.0", "--port", "8000"]
