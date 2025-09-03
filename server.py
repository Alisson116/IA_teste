# server.py
import os
import time
import json
import subprocess
import re
from typing import List, Dict
from fastapi import FastAPI, Request, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
import requests
from bs4 import BeautifulSoup

# opcional: use playwright sync api
try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except Exception:
    HAS_PLAYWRIGHT = False

# check if yt-dlp available
def has_yt_dlp():
    try:
        subprocess.run(["yt-dlp", "--version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        return True
    except Exception:
        return False

HAS_YTDLP = has_yt_dlp()

app = FastAPI(title="Extractor Service")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- util helpers ---
def simple_http_extract(url: str) -> List[str]:
    """Busca fontes óbvias (video, source, meta og:video, links .mp4/.m3u8)"""
    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible)"}
        r = requests.get(url, headers=headers, timeout=12)
        html = r.text
        links = set(re.findall(r'(https?://[^\s"\'<>]+?\.(?:mp4|m3u8|m3u8\?))', html, flags=re.I))
        soup = BeautifulSoup(html, "lxml")
        # <video> sources
        for v in soup.find_all("video"):
            for s in v.find_all("source"):
                src = s.get("src")
                if src:
                    links.add(requests.compat.urljoin(url, src))
            if v.get("src"):
                links.add(requests.compat.urljoin(url, v.get("src")))
        # og:video
        og = soup.find("meta", property="og:video")
        if og and og.get("content"):
            links.add(og["content"])
        return list(links)
    except Exception:
        return []

def yt_dlp_extract(url: str, timeout=30) -> List[str]:
    """Tenta usar yt-dlp -g (retorna URLs diretas)."""
    if not HAS_YTDLP:
        return []
    try:
        # -g -> print direct URLs for video and audio
        proc = subprocess.run(["yt-dlp", "-g", url], capture_output=True, text=True, timeout=timeout)
        if proc.returncode == 0 and proc.stdout.strip():
            out = [l.strip() for l in proc.stdout.splitlines() if l.strip()]
            # remove duplicates & relative
            return out
        # fallback: dump json to inspect formats
        proc2 = subprocess.run(["yt-dlp", "-j", url], capture_output=True, text=True, timeout=timeout)
        if proc2.returncode == 0 and proc2.stdout.strip():
            try:
                j = json.loads(proc2.stdout.splitlines()[0])
                links = []
                if "url" in j and j["url"]:
                    links.append(j["url"])
                if "formats" in j:
                    for f in j["formats"]:
                        if "url" in f:
                            links.append(f["url"])
                return list(dict.fromkeys([l for l in links if l]))
            except Exception:
                return []
        return []
    except Exception:
        return []

def playwright_extract(url: str, timeout=30) -> List[str]:
    """Abre a página com Playwright, intercepta responses contendo mp4/m3u8 e tenta extrair de JS."""
    if not HAS_PLAYWRIGHT:
        return []
    collected = set()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
            context = browser.new_context()
            page = context.new_page()
            # intercept responses
            def handle_response(resp):
                try:
                    rurl = resp.url
                    if re.search(r'\.(mp4|m3u8)(?:\?|$)', rurl, flags=re.I):
                        collected.add(rurl)
                except Exception:
                    pass
            page.on("response", handle_response)
            page.goto(url, wait_until="networkidle", timeout=timeout*1000)
            # try to evaluate common JS players
            try:
                # try to get sources from JS players
                sources = page.eval_on_selector_all("video, source", "els => els.map(e => e.src || e.getAttribute('src')).filter(Boolean)")
                for s in sources:
                    if s:
                        collected.add(s)
            except Exception:
                pass
            # wait a short bit for network events
            time.sleep(1.2)
            browser.close()
    except Exception:
        pass
    return list(collected)

def fallback_regex(html: str, base_url: str) -> List[str]:
    matches = re.findall(r'(https?://[^\s"\'<>]+?\.(?:mp4|m3u8))', html, flags=re.I)
    return list(dict.fromkeys(matches))

# --- Endpoints ---
@app.get("/server-info")
def server_info():
    return {
        "status": "ok",
        "msg": "Service ready",
        "features": {"yt_dlp": HAS_YTDLP, "playwright": HAS_PLAYWRIGHT},
    }

@app.post("/extract")
async def extract(req: Request):
    data = await req.json()
    url = data.get("url")
    if not url:
        return JSONResponse({"error":"missing url"}, status_code=400)

    attempts = {"basic": [], "yt_dlp": [], "playwright": [], "fallback_regex": []}

    # 1) basic
    basic_links = simple_http_extract(url)
    attempts["basic"] = basic_links
    if basic_links:
        return {"method":"basic", "links": basic_links, "attempts": attempts}

    # 2) yt-dlp
    ylinks = yt_dlp_extract(url)
    attempts["yt_dlp"] = ylinks
    if ylinks:
        return {"method":"yt_dlp", "links": ylinks, "attempts": attempts}

    # 3) playwright
    plinks = []
    try:
        plinks = playwright_extract(url)
    except Exception:
        plinks = []
    attempts["playwright"] = plinks
    if plinks:
        return {"method":"playwright", "links": plinks, "attempts": attempts}

    # 4) fallback regex (from main HTML)
    try:
        r = requests.get(url, headers={"User-Agent":"Mozilla/5.0"}, timeout=12)
        fall = fallback_regex(r.text, url)
    except Exception:
        fall = []
    attempts["fallback_regex"] = fall
    if fall:
        return {"method":"fallback_regex", "links": fall, "attempts": attempts}

    # nothing found
    return {"method":"none", "links": [], "attempts": attempts}

@app.get("/extract_stream_sse")
def extract_stream_sse(url: str = Query(None)):
    if not url:
        def ev_none():
            yield "data: {\"error\":\"missing url\"}\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(ev_none(), media_type="text/event-stream")

    def event_generator():
        attempts = {"basic": [], "yt_dlp": [], "playwright": [], "fallback_regex": []}
        yield f"data: TENTANDO: requisição simples (requests/BS4)\n\n"
        try:
            basic_links = simple_http_extract(url)
            attempts["basic"] = basic_links
            if basic_links:
                yield f"data: ACHOU via basic: {json.dumps(basic_links)}\n\n"
                yield f"data: {json.dumps({'status':'done','result':{'method':'basic','links':basic_links,'attempts':attempts}})}\n\n"
                yield "data: [DONE]\n\n"
                return
            else:
                yield "data: (nenhum link via basic)\n\n"
        except Exception as e:
            yield f"data: Erro basic: {str(e)}\n\n"

        # yt-dlp
        if HAS_YTDLP:
            yield "data: TENTANDO: yt-dlp\n\n"
            try:
                ylinks = yt_dlp_extract(url)
                attempts["yt_dlp"] = ylinks
                if ylinks:
                    yield f"data: ACHOU via yt-dlp: {json.dumps(ylinks)}\n\n"
                    yield f"data: {json.dumps({'status':'done','result':{'method':'yt_dlp','links':ylinks,'attempts':attempts}})}\n\n"
                    yield "data: [DONE]\n\n"
                    return
                else:
                    yield "data: (nenhum link via yt-dlp)\n\n"
            except Exception as e:
                yield f"data: Erro yt-dlp: {str(e)}\n\n"
        else:
            yield "data: yt-dlp não disponível no ambiente.\n\n"

        # playwright
        if HAS_PLAYWRIGHT:
            yield "data: TENTANDO: Playwright (execução JS e interceptação de rede)\n\n"
            try:
                plinks = playwright_extract(url)
                attempts["playwright"] = plinks
                if plinks:
                    yield f"data: ACHOU via Playwright: {json.dumps(plinks)}\n\n"
                    yield f"data: {json.dumps({'status':'done','result':{'method':'playwright','links':plinks,'attempts':attempts}})}\n\n"
                    yield "data: [DONE]\n\n"
                    return
                else:
                    yield "data: (nenhum link via Playwright)\n\n"
            except Exception as e:
                yield f"data: Erro Playwright: {str(e)}\n\n"
        else:
            yield "data: Playwright não disponível no ambiente.\n\n"

        # fallback
        yield "data: TENTANDO: fallback (regex/HTML)\n\n"
        try:
            r = requests.get(url, headers={"User-Agent":"Mozilla/5.0"}, timeout=12)
            fall = fallback_regex(r.text, url)
            attempts["fallback_regex"] = fall
            if fall:
                yield f"data: ACHOU via fallback: {json.dumps(fall)}\n\n"
                yield f"data: {json.dumps({'status':'done','result':{'method':'fallback_regex','links':fall,'attempts':attempts}})}\n\n"
                yield "data: [DONE]\n\n"
                return
            else:
                yield "data: nenhum link encontrado em todas as tentativas.\n\n"
        except Exception as e:
            yield f"data: Erro fallback: {str(e)}\n\n"

        yield f"data: {json.dumps({'status':'done','result':{'method':'none','links':[],'attempts':attempts}})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")

# small health root
@app.get("/")
def root():
    return {"status":"ok","msg":"Service root - extractor ready"}
 