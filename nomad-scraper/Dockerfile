FROM python:3.11-slim

# Playwright/Chromium system deps (ScrapeGraphAI renders JS pages with a headless browser).
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates fonts-liberation libnss3 libnspr4 libatk1.0-0 \
        libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 libxcomposite1 \
        libxdamage1 libxfixes3 libxrandr2 libgbm1 libasound2 libpango-1.0-0 \
        libcairo2 libatspi2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Bake the Chromium browser into the image so the first scrape is fast.
RUN python -m playwright install chromium

COPY server.py .
ENV SCRAPER_MODEL=gpt \
    LITELLM_BASE_URL=http://litellm:4000
EXPOSE 8210
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8210"]
