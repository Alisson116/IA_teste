FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# optional: instalar libs do sistema para playwright
RUN apt-get update && apt-get install -y wget gnupg ca-certificates libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libx11-xcb1 libxcomposite1 libxdamage1 libxrandr2 libgbm1 libasound2 libpangocairo-1.0-0 libgtk-3-0 --no-install-recommends && rm -rf /var/lib/apt/lists/*

COPY . .
ENV PORT=8000
EXPOSE 8000

# se usar playwright, instalar binários do browser:
RUN python -m playwright install chromium

CMD ["sh","-c","uvicorn server:app --host 0.0.0.0 --port ${PORT}"]
