FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Chromium system dependencies (Playwright's browsers need these)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libxkbcommon0 libatspi2.0-0 libxcomposite1 libxdamage1 libxfixes3 \
    libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2 libx11-6 \
    libxcb1 libxext6 libxi6 libxtst6 fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

COPY film_scraper/requirements.txt .
RUN pip install -r requirements.txt \
    && python -m playwright install chromium

COPY film_scraper/ .

EXPOSE 8000

CMD ["python", "-m", "middleware"]