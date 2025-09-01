# Use uma imagem Python leve
FROM python:3.12-slim

# Diretório da aplicação
WORKDIR /app

# Copia e instala dependências
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copia o resto do código
COPY . .

# Porta padrão (Railway define $PORT em runtime)
ENV PORT=8000

# Expõe a porta (instrução correta: EXPOSE)
EXPOSE 8000

# Comando de inicialização (assume que seu arquivo principal é server.py exportando `app`)
CMD ["sh", "-c", "uvicorn server:app --host 0.0.0.0 --port ${PORT}"]
