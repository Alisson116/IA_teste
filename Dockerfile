FROM python:3.12-slim

WORKDIR /app

# Dependências de sistema necessárias para Playwright / navegadores
RUN apt-get update && apt-get install -y \
    wget curl gnupg ca-certificates libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libxcomposite1 libxdamage1 \
    libxrandr2 libgbm1 libasound2 libpangocairo-1.0-0 libgtk-3-0 && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# instalar navegadores do Playwright
RUN python -m playwright install --with-deps

COPY . .

ENV PORT=8000
EXPOSE 8000

CMD ["sh", "-c", "uvicorn server:app --host 0.0.0.0 --port ${PORT}"]
