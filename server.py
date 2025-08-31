# server.py
import os
import time
import requests
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

HF_TOKEN = os.getenv("HF_TOKEN", "hf_xxx")  # configure no Railway
SERP_API_KEY = os.getenv("SERP_API_KEY", "serp_xxx")  # configure no Railway
HF_MODEL = "gpt2"

app = FastAPI()

# --- CORS ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Endpoints ---
@app.get("/server-info")
def server_info():
    return {"status": "ok", "msg": "Servidor rodando no Railway 🚀"}

def search_google(query: str):
    url = f"https://serpapi.com/search.json?q={query}&hl=pt&api_key={SERP_API_KEY}"
    res = requests.get(url).json()
    results = []
    if "organic_results" in res:
        for r in res["organic_results"][:5]:
            results.append(r.get("snippet", ""))
    return " ".join(results) if results else "Nenhum resultado encontrado."

@app.post("/ask")
async def ask(req: Request):
    data = await req.json()
    question = data.get("question", "")
    context = search_google(question)
    headers = {"Authorization": f"Bearer {HF_TOKEN}"}
    payload = {"inputs": f"P: {question}\nC: {context}\nR:"}
    resp = requests.post(
        f"https://api-inference.huggingface.co/models/{HF_MODEL}",
        headers=headers,
        json=payload
    )

    if resp.status_code == 200:
        answer = resp.json()
        text = answer[0].get("generated_text", "").strip() if isinstance(answer, list) else str(answer)
    else:
        text = "Erro ao chamar HuggingFace."

    return {"answer": text, "context": context}

@app.post("/extract")
async def extract(req: Request):
    data = await req.json()
    url = data.get("url")
    try:
        html = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}).text
        import re
        matches = re.findall(r'(https?://[^\s"\']+\.(?:mp4|m3u8))', html)
        return {"links": list(set(matches))}
    except Exception as e:
        return {"error": str(e)}

@app.post("/generate_stream")
async def generate_stream(req: Request):
    return {"status": "ok"}

@app.get("/generate_stream_sse")
async def generate_stream_sse(prompt: str):
    def event_generator():
        for i in range(5):
            yield f"data: Parte {i+1} do texto para: {prompt}\n\n"
            time.sleep(1)
        yield "data: [DONE]\n\n"
    return StreamingResponse(event_generator(), media_type="text/event-stream")

# --- Inicia servidor ---
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))  # Railway define a porta
    print(f"🚀 Rodando FastAPI na porta {port}")
    uvicorn.run(app, host="0.0.0.0", port=port)
