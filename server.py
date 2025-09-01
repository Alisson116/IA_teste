# server.py — FastAPI com /server-info, /generate_stream (POST) e /generate_stream_sse (SSE)
import os
import time
import requests
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse

HF_TOKEN = os.getenv("HF_TOKEN", "")      # configure no Railway
SERP_API_KEY = os.getenv("SERP_API_KEY", "")  # configure no Railway
HF_MODEL = os.getenv("HF_MODEL", "gpt2")  # opcional

app = FastAPI(title="IA Chat API")

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # em produção restrinja
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/server-info")
def server_info():
    # retorna ok e (opcional) a URL pública (o front usa a URL fixa do Railway)
    return {"status": "ok", "msg": "Servidor rodando no Railway 🚀"}

def search_google(query: str):
    if not SERP_API_KEY:
        return ""
    try:
        url = f"https://serpapi.com/search.json?q={query}&hl=pt&api_key={SERP_API_KEY}"
        res = requests.get(url, timeout=8).json()
        results = []
        for r in res.get("organic_results", [])[:5]:
            results.append(r.get("snippet",""))
        return " ".join(results)
    except Exception:
        return ""

@app.post("/ask")
async def ask(req: Request):
    data = await req.json()
    question = data.get("question","")
    context = search_google(question)
    if not HF_TOKEN:
        return JSONResponse({"answer":"HuggingFace token não configurado.","context":context}, status_code=500)
    headers = {"Authorization": f"Bearer {HF_TOKEN}"}
    payload = {"inputs": f"P: {question}\nC: {context}\nR:"}
    try:
        resp = requests.post(f"https://api-inference.huggingface.co/models/{HF_MODEL}", headers=headers, json=payload, timeout=30)
        if resp.status_code == 200:
            answer = resp.json()
            if isinstance(answer, list) and len(answer)>0:
                text = answer[0].get("generated_text","")
            else:
                text = str(answer)
            return {"answer": text, "context": context}
        else:
            return JSONResponse({"error":"Erro HuggingFace","status":resp.status_code, "body": resp.text}, status_code=502)
    except Exception as e:
        return JSONResponse({"error":"Erro interno ao chamar HF","detail": str(e)}, status_code=500)

@app.post("/generate_stream")
async def generate_stream(req: Request):
    # apenas confirma que recebeu o prompt (o front faz a SSE depois)
    try:
        data = await req.json()
    except Exception:
        data = {}
    return {"status":"ok", "received": data}

@app.get("/generate_stream_sse")
async def generate_stream_sse(prompt: str = ""):
    # Exemplo de generator que simula streaming. Substitua comportamento por seu stream real.
    def event_generator():
        try:
            for i in range(5):
                yield f"data: {i+1}. resposta parcial para: {prompt}\n\n"
                time.sleep(1)
            yield "data: [DONE]\n\n"
        except GeneratorExit:
            # cliente desconectou
            return
    return StreamingResponse(event_generator(), media_type="text/event-stream")

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    print(f"🚀 Rodando FastAPI na porta {port}")
    uvicorn.run(app, host="0.0.0.0", port=port)
