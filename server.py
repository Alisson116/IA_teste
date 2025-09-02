# server.py
import os
import time
import json
import requests
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from bs4 import BeautifulSoup

# IMPORT PLAYWRIGHT (síncrono) se disponível
try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_OK = True
except Exception:
    PLAYWRIGHT_OK = False

# IMPORT yt-dlp
try:
    import yt_dlp
    YTDLP_OK = True
except Exception:
    YTDLP_OK = False

HF_TOKEN = os.getenv("HF_TOKEN")  # configure no Railway
SERP_API_KEY = os.getenv("SERP_API_KEY")
HF_MODEL = os.getenv("HF_MODEL", "HuggingFaceH4/zephyr-7b-beta")  # modelo HF (mude se quiser)

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/server-info")
def server_info():
    return {"status":"ok","msg":"Servidor pronto","hf_model": HF_MODEL}

# ---------------- HuggingFace Inference (via API) ----------------
def hf_generate_text(prompt: str, max_length=512):
    if not HF_TOKEN:
        raise RuntimeError("HF_TOKEN não configurado")
    url = f"https://api-inference.huggingface.co/models/{HF_MODEL}"
    headers = {"Authorization": f"Bearer {HF_TOKEN}"}
    payload = {"inputs": prompt, "parameters": {"max_new_tokens": 256, "temperature": 0.9}}
    resp = requests.post(url, headers=headers, json=payload, timeout=60)
    if resp.status_code == 200:
        # resposta pode ser lista com 'generated_text' ou texto simples dependendo do modelo
        try:
            j = resp.json()
            if isinstance(j, list) and len(j) > 0 and "generated_text" in j[0]:
                return j[0]["generated_text"]
            # outros modelos retornam dict com 'generated_text'
            if isinstance(j, dict) and "generated_text" in j:
                return j["generated_text"]
            # fallback: texto cru
            return str(j)
        except Exception:
            return resp.text
    else:
        raise RuntimeError(f"Erro HF {resp.status_code}: {resp.text}")

# --------------- SSE generator para texto ---------------
@app.post("/generate_stream")
async def generate_stream(req: Request):
    # Endpoint de "kick" (mantive compatibilidade com frontend)
    return {"status":"ok"}

@app.get("/generate_stream_sse")
async def generate_stream_sse(prompt: str):
    def event_generator():
        try:
            full = hf_generate_text(prompt)
        except Exception as e:
            yield f"data: [Erro HF] {str(e)}\n\n"
            yield "data: [DONE]\n\n"
            return

        # stream por pedaços (ex.: by sentences ou por 120 chars)
        # tente dividir por sentenças para resposta mais natural
        import re
        parts = re.split(r'(?<=[.!?])\s+', full)
        if len(parts) == 1:
            # fallback -> split por size
            chunk_size = 120
            parts = [full[i:i+chunk_size] for i in range(0, len(full), chunk_size)]

        for p in parts:
            yield f"data: {p}\n\n"
            time.sleep(0.5)
        yield "data: [DONE]\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")

# ---------------- Extraction (várias táticas) ----------------
def yt_dlp_try(url: str):
    if not YTDLP_OK:
        return None
    ydl_opts = {"skip_download": True, "quiet": True, "no_warnings": True}
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(url, download=False)
            # retorna lista de candidate urls (formats etc)
            links = []
            if isinstance(info, dict):
                if "formats" in info:
                    for f in info["formats"]:
                        link = f.get("url")
                        if link:
                            links.append(link)
                # fallback: url key
                if info.get("url"):
                    links.append(info.get("url"))
            return list(dict.fromkeys(links))
        except Exception:
            return None

def requests_regex_try(url: str):
    try:
        html = requests.get(url, headers={"User-Agent":"Mozilla/5.0"}, timeout=15).text
        import re
        matches = re.findall(r'(https?://[^\s"\']+\.(?:mp4|m3u8|m4v|webm))', html)
        return list(dict.fromkeys(matches))
    except Exception:
        return None

def playwright_try(url: str):
    if not PLAYWRIGHT_OK:
        return None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(user_agent="Mozilla/5.0")
            page.goto(url, timeout=30000)
            # espera JS carregar
            page.wait_for_timeout(1500)
            # procura video tags
            candidates = set()
            # 1) <video src=...>
            vid_srcs = page.eval_on_selector_all("video", "els => els.map(e => e.currentSrc || e.src || '')")
            for s in vid_srcs:
                if s:
                    candidates.add(s)
            # 2) procura por links .m3u8 e .mp4 no DOM
            html = page.content()
            import re
            for m in re.findall(r'https?://[^\s"\']+\.(?:m3u8|mp4|webm|m4v)', html):
                candidates.add(m)
            browser.close()
            return list(candidates)
    except Exception:
        return None

@app.post("/extract")
async def extract(req: Request):
    data = await req.json()
    url = data.get("url")
    if not url:
        return JSONResponse({"error":"missing url"}, status_code=400)

    # 1) try yt-dlp
    res = yt_dlp_try(url)
    if res:
        return {"method":"yt-dlp","links":res}

    # 2) try requests regex
    res = requests_regex_try(url)
    if res:
        return {"method":"requests_regex","links":res}

    # 3) try playwright (render JS)
    res = playwright_try(url)
    if res:
        return {"method":"playwright","links":res}

    return {"method":"none","links":[]}
