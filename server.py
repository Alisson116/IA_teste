# server.py
import os
import time
import json
import shlex
import subprocess
from typing import Optional
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
import requests
from bs4 import BeautifulSoup
import re

HF_TOKEN = os.getenv("HF_TOKEN", "")
HF_MODEL = os.getenv("HF_MODEL", "gpt2")  # ajuste no Railway para o modelo que funcionar
SERP_API_KEY = os.getenv("SERP_API_KEY", "")
ENABLE_PLAYWRIGHT = os.getenv("ENABLE_PLAYWRIGHT", "false").lower() in ("1","true","yes")

HF_HEADERS = {"Authorization": f"Bearer {HF_TOKEN}"} if HF_TOKEN else {}

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- util ---
def call_hf_inference(prompt: str, model: Optional[str] = None, params: dict = None, timeout=60):
    """
    Chama o endpoint de Inference do HF e retorna (ok, text_or_error).
    """
    model = model or HF_MODEL
    url = f"https://api-inference.huggingface.co/models/{model}"
    payload = {"inputs": prompt}
    if params:
        payload["parameters"] = params
    try:
        r = requests.post(url, headers=HF_HEADERS, json=payload, timeout=timeout)
    except Exception as e:
        return False, f"Erro de rede ao chamar HF: {e}"

    if r.status_code == 200:
        try:
            data = r.json()
            # resposta típica é lista com generated_text ou dict dependendo do modelo/provider
            if isinstance(data, list) and data and isinstance(data[0], dict):
                text = data[0].get("generated_text", "")
                return True, text
            elif isinstance(data, dict) and "generated_text" in data:
                return True, data["generated_text"]
            else:
                # tentamos converter o JSON pra string
                return True, json.dumps(data)
        except Exception as e:
            return False, f"Erro ao decodificar resposta HF: {e}"
    else:
        # propaga info de erro (status + corpo)
        try:
            body = r.text
        except:
            body = "<sem body>"
        return False, f"[Erro HF] {r.status_code}: {body}"

# --- endpoints ---
@app.get("/server-info")
def server_info():
    return {"status": "ok", "msg": "Servidor pronto", "hf_model": HF_MODEL}

@app.post("/ask")
async def ask(req: Request):
    data = await req.json()
    question = data.get("question", "")
    # context via serpapi (se disponível)
    context = ""
    if SERP_API_KEY and question:
        try:
            qurl = f"https://serpapi.com/search.json?q={requests.utils.quote(question)}&hl=pt&api_key={SERP_API_KEY}"
            r = requests.get(qurl, timeout=8).json()
            snippets = []
            for rj in r.get("organic_results", [])[:5]:
                snippets.append(rj.get("snippet",""))
            context = " ".join(snippets)
        except Exception:
            context = ""
    prompt = f"P: {question}\nC: {context}\nR:"
    ok, resp = call_hf_inference(prompt)
    if ok:
        return {"answer": resp, "context": context}
    else:
        return JSONResponse({"error": resp}, status_code=502)

@app.post("/extract")
async def extract(req: Request):
    """
    Extrai possíveis links mp4/m3u8 etc. Tenta:
     1) requests + regex + bs4
     2) yt-dlp (se instalado)
     3) Playwright (se habilitado)
    """
    data = await req.json()
    url = data.get("url")
    if not url:
        return {"error":"sem url"}

    # método 1: requests + regex + bs4
    try:
        html = requests.get(url, headers={"User-Agent":"Mozilla/5.0"}, timeout=12).text
        found = list(set(re.findall(r'(https?://[^\s"\']+\.(?:mp4|m3u8|mkv|webm))', html)))
        if found:
            return {"method":"requests", "links": found}
        # try to find video tags
        soup = BeautifulSoup(html, "lxml")
        tags = []
        for v in soup.find_all(["video","source"]):
            src = v.get("src") or v.get("data-src")
            if src and src.startswith("http"):
                tags.append(src)
        if tags:
            return {"method":"bs4", "links": list(set(tags))}
    except Exception as e:
        # continue to fallbacks
        pass

    # método 2: tentar yt-dlp (se estiver instalado no sistema)
    try:
        cmd = f"yt-dlp -J {shlex.quote(url)}"
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=40)
        if p.returncode == 0 and p.stdout:
            info = json.loads(p.stdout)
            links = []
            # extrair formatos com url direta
            for f in info.get("formats",[]):
                fu = f.get("url")
                if fu:
                    links.append(fu)
            if links:
                return {"method":"yt-dlp", "links": list(dict.fromkeys(links))}
    except Exception:
        pass

    # método 3: Playwright (se habilitado)
    if ENABLE_PLAYWRIGHT:
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()
                page.goto(url, timeout=30000)
                time.sleep(1)
                # busca por elementos de vídeo e botões "baixar" / "download"
                vids = page.query_selector_all("video, source, [data-src], [src]")
                candidates = set()
                for el in vids:
                    try:
                        src = el.get_attribute("src") or el.get_attribute("data-src")
                        if src and src.startswith("http"):
                            candidates.add(src)
                    except:
                        pass
                # procura por botões com texto "download" / "baixar"
                buttons = page.query_selector_all("button, a")
                for b in buttons:
                    try:
                        txt = (b.inner_text() or "").lower()
                        if "baix" in txt or "download" in txt:
                            href = b.get_attribute("href")
                            if href and href.startswith("http"):
                                candidates.add(href)
                    except:
                        pass
                browser.close()
                if candidates:
                    return {"method":"playwright", "links": list(candidates)}
        except Exception as e:
            # Playwright pode não estar instalado corretamente
            pass

    return {"method":"none","links":[]}

@app.post("/generate_stream")
async def generate_stream(req: Request):
    # endpoint compat com frontend: só confirma que foi recebido
    return {"status":"ok"}

@app.get("/generate_stream_sse")
def generate_stream_sse(prompt: str):
    """
    SSE: chama HF synchronously, recebe texto completo e envia em pedaços para o frontend.
    """
    def event_generator():
        # safety: mensagem rápida se token não configurado
        if not HF_TOKEN:
            yield f"data: [Erro HF] Token HF não configurado. Configure HF_TOKEN.\n\n"
            yield "data: [DONE]\n\n"
            return

        ok, out = call_hf_inference(prompt, timeout=60)
        if not ok:
            yield f"data: {out}\n\n"
            yield "data: [DONE]\n\n"
            return

        text = out.strip()
        # envia em chunks para o frontend (tamanho ajustável)
        chunk_size = 80
        for i in range(0, len(text), chunk_size):
            chunk = text[i:i+chunk_size]
            # limpar quebras de linha indesejadas
            chunk = chunk.replace("\r", "")
            yield f"data: {chunk}\n\n"
            time.sleep(0.08)  # pequeno delay para "efeito streaming"
        yield "data: [DONE]\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")
