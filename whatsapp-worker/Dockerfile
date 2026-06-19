FROM python:3.11-slim

# Install system dependencies needed for Playwright/Chromium
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    gnupg \
    libgconf-2-4 \
    libnss3 \
    libxss1 \
    libasound2 \
    libatk-bridge2.0-0 \
    libgtk-3-0 \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browsers (specifically chromium) and their system dependencies
RUN playwright install chromium
RUN playwright install-deps chromium

COPY . .

# Environment variable default overrides can go here
ENV PYTHONUNBUFFERED=1

CMD ["python", "worker.py"]
