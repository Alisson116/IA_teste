# Dockerfile (usar quando for fazer deploy via Docker/Railway)
FROM mcr.microsoft.com/playwright/python:latest


WORKDIR /app


# Copia só requirements e instala
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt


# Copia o código
COPY server.py /app/server.py


ENV PORT=8000
EXPOS