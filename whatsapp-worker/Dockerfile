FROM python:3.11-slim

# Install system dependencies needed for Playwright/Chromium
# NOTE: libgconf-2-4 was REMOVED from Debian after buster (10). The
# python:3.11-slim base is Debian bookworm (12), where that package does not
# exist, so installing it aborts the whole RUN and the image never builds.
# It is a legacy GTK2 library modern Chromium does not need; the real runtime
# libraries are installed by `playwright install-deps chromium` below.
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    gnupg \
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
