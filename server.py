# server.py
import os
import time
import re
import requests
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

HF_TOKEN = os.getenv("HF_TOKEN", "hf_xxx")        # coloque no Railway
SERP_API_KEY = os.getenv("SERP_API_KEY", "serp_xxx")
HF_MODEL = os.getenv("HF_MODEL", "gpt2")          # ex: "gpt2" ou outro modelo no HF
CHUNK_DELAY = float(os.getenv("CHUNK_DELAY", "0.25"))  # segundos entre chunks

app = FastAPI()

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Health / info
@app.get("/server-info")
def server_info():
    return {"status": "ok", "msg": "Servidor rodando no Railway 🚀"}

def search_google(query: str):
    url = f"https://serpapi.com/search.json?q={query}&hl=pt&api_key={SERP_API_KEY}"
    try:
        res = requests.get(url, timeout=8).json()
        results = []
        if "organic_results" in res:
            for r in res["organic_results"][:5]:
                results.append(r.get("snippet", ""))
        return " ".join(results) if results else "Nenhum resultado encontrado."
    except Exception:
        return "Nenhum resultado (erro ao consultar SerpAPI)."

@app.post("/ask")
async def ask(req: Request):
    data = await req.json()
    question = data.get("question", "")
    context = search_google(question)
    headers = {"Authorization": f"Bearer {HF_TOKEN}"}
    payload = {"inputs": f"P: {question}\nC: {context}\nR:"}
    try:
        resp = requests.post(
            f"https://api-inference.huggingface.co/models/{HF_MODEL}",
            headers=headers,
            json=payload,
            timeout=30
        )
        if resp.status_code == 200:
            answer = resp.json()
            # tentativa genérica para extrair texto
            if isinstance(answer, list) and len(answer) > 0 and "generated_text" in answer[0]:
                text = answer[0]["generated_text"].strip()
            elif isinstance(answer, dict) and "generated_text" in answer:
                text = answer["generated_text"].strip()
            else:
                # fallback: string representation
                text = str(answer)
        else:
            text = f"Erro HuggingFace: {resp.status_code} - {resp.text}"
    except Exception as e:
        text = f"Erro ao chamar HuggingFace: {e}"
    return {"answer": text, "context": context}

@app.post("/extract")
async def extract(req: Request):
    data = await req.json()
    url = data.get("url")
    try:
        html = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=12).text
        matches = re.findall(r'(https?://[^\s"\']+\.(?:mp4|m3u8))', html)
        return {"links": list(set(matches))}
    except Exception as e:
        return {"error": str(e)}

@app.post("/generate_stream")
async def generate_stream(req: Request):
    # O front já faz esse POST antes de abrir o SSE. Mantemos por compatibilidade.
    return {"status": "ok"}

@app.get("/generate_stream_sse")
async def generate_stream_sse(prompt: str):
    """
    Faz a chamada ao HF (sincrona), pega a resposta completa e envia em 'chunks'
    via SSE (cada chunk = sentença / bloco). Isso evita as 'Parte 1/2' do exemplo.
    """
    def _split_into_chunks(text, max_chars=250):
        # tenta quebrar por sentenças; se sentenças muito longas, quebra por tamanho
        parts = re.split(r'(?<=[\.\?\!]\s)', text)
        out = []
        for p in parts:
            p = p.strip()
            if not p:
                continue
            if len(p) <= max_chars:
                out.append(p)
            else:
                # quebra por palavras mantendo tamanho aproximado
                words = p.split()
                cur = ""
                for w in words:
                    if len(cur) + 1 + len(w) <= max_chars:
                        cur = (cur + " " + w).strip()
                    else:
                        out.append(cur)
                        cur = w
                if cur:
                    out.append(cur)
        return out

    def event_generator():
        # 1) Chama a HF (pode demorar). Timeout razoável aplicado.
        headers = {"Authorization": f"Bearer {HF_TOKEN}"}
        payload = {"inputs": prompt}
        try:
            resp = requests.post(
                f"https://api-inference.huggingface.co/models/{HF_MODEL}",
                headers=headers,
                json=payload,
                timeout=60
            )
        except Exception as e:
            yield f"data: Erro ao chamar HuggingFace: {str(e)}\n\n"
            yield "data: [DONE]\n\n"
            return

        if resp.status_code != 200:
            yield f"data: Erro HuggingFace {resp.status_code}: {resp.text}\n\n"
            yield "data: [DONE]\n\n"
            return

        answer = resp.json()
        if isinstance(answer, list) and len(answer)>0 and "generated_text" in answer[0]:
            text = answer[0]["generated_text"].strip()
        elif isinstance(answer, dict) and "generated_text" in answer:
            text = answer["generated_text"].strip()
        elif isinstance(answer, dict) and "text" in answer:
            text = answer["text"].strip()
        else:
            # fallback: stringify
            text = str(answer)

        # 2) Quebra em chunks e envia
        chunks = _split_into_chunks(text, max_chars=200)
        for c in chunks:
            # envia um chunk como evento SSE
            safe = c.replace("\n", " ").strip()
            yield f"data: {safe}\n\n"
            time.sleep(CHUNK_DELAY)
        # 3) finaliza
        yield "data: [DONE]\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")

# Run (usado somente local/colab; Railway usa comando start configurado)
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    print(f"🚀 Rodando FastAPI na porta {port}")
    uvicorn.run(app, host="0.0.0.0", port=port)

