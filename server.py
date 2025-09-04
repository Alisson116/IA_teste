# server.py
import os
import time
import json
import re
import asyncio
import traceback
from typing import List, Dict, Any

import requests
from bs4 import BeautifulSoup

from fastapi import FastAPI, Request, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse

# tentativas opcionais
try:
    import yt_dlp
    HAS_YTDLP = True
except Exception:
    HAS_YTDLP = False

try:
    from playwright.async_api import async_playwright, Error as PlaywrightError
    HAS_PLAYWRIGHT = True
except Exception:
    HAS_PLAYWRIGHT = False

app = FastAPI(title="Extractor + SSE")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -------- helpers de logging/SSE --------
def sse_line(text: str) -> bytes:
    # envia texto simples (já é escapado pelo JSON quando necessário)
    return f"data: {text}\n\n".encode("utf-8")

def sse_json(obj: Any) -> bytes:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode("utf-8")

# -------- utilitários de extracão --------
def absolute_urls_from_bs4(base: str, soup: BeautifulSoup) -> List[str]:
    out = []
    def norm(url):
        if not url: return None
        url = url.strip()
        if url.startswith("//"):
            url = "https:" + url
        if url.startswith("/"):
            # base origin
            try:
                from urllib.parse import urljoin
                return urljoin(base, url)
            except:
                return None
        if url.startswith("http"):
            return url
        return None

    # <video> / <source>
    for tag in soup.select("video source, video"):
        src = tag.get("src") or tag.get("data-src") or tag.get("data-setup")
        if src:
            u = norm(src)
            if u: out.append(u)

    # <iframe src=...>
    for iframe in soup.find_all("iframe", src=True):
        u = norm(iframe["src"])
        if u: out.append(u)

    # a[href]
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if any(x in href for x in (".m3u8", ".mp4", ".mpd")) or href.startswith("blob:"):
            u = norm(href)
            if u: out.append(u)

    # data attributes
    for t in soup.find_all(attrs=True):
        for k, v in t.attrs.items():
            if isinstance(v, str) and any(x in v for x in (".m3u8", ".mp4", "blob:")):
                u = norm(v)
                if u: out.append(u)

    return list(dict.fromkeys(out))

def regex_find_media(html: str, base: str = None) -> List[str]:
    out = []
    # procura URLs óbvias .m3u8/.mp4/.mpd
    patterns = [
        r"https?://[^\s'\"<>]+\.m3u8[^\s'\"<>]*",
        r"https?://[^\s'\"<>]+\.mp4[^\s'\"<>]*",
        r"https?://[^\s'\"<>]+\.mpd[^\s'\"<>]*",
        r"blob:[^\s'\"<>]+",
    ]
    for pat in patterns:
        for m in re.findall(pat, html, flags=re.IGNORECASE):
            out.append(m)
    return list(dict.fromkeys(out))

# -------- attempt 1: simple requests + bs4 --------
def attempt_basic(url: str, send):
    send("TENTANDO: requisição simples (requests/BS4)")
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        r = requests.get(url, headers=headers, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")
        found = absolute_urls_from_bs4(url, soup)
        if found:
            send(f"(encontrado via basic) {len(found)} links")
        else:
            send("(nenhum link via basic)")
        return found
    except Exception as e:
        send(f"(erro basic) {repr(e)}")
        return []

# -------- attempt 2: yt-dlp (se disponível) --------
def attempt_ytdlp(url: str, send):
    send("TENTANDO: yt-dlp")
    if not HAS_YTDLP:
        send("yt-dlp não disponível")
        return []
    try:
        ydl_opts = {"skip_download": True, "quiet": True, "nocheckcertificate": True}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # extract_info pode levantar; aceitamos dicts e listas
            info = ydl.extract_info(url, download=False)
        links = []
        # se info tiver 'formats'
        if isinstance(info, dict) and "formats" in info:
            for f in info["formats"]:
                u = f.get("url")
                if u:
                    links.append(u)
        # se info tiver 'url' direto
        elif isinstance(info, dict) and info.get("url"):
            links.append(info.get("url"))
        # flat playlists
        if links:
            send(f"(encontrado via yt-dlp) {len(links)} links")
        else:
            send("(nenhum link via yt-dlp)")
        return list(dict.fromkeys(links))
    except Exception as e:
        send(f"(erro yt-dlp) {repr(e)}")
        return []

# -------- attempt 3: Playwright (async) --------
async def attempt_playwright(url: str, send, timeout_sec: int = 18):
    send("TENTANDO: Playwright (execução JS e interceptação de rede)")
    if not HAS_PLAYWRIGHT:
        send("Playwright não instalado como pacote Python (ou import falhou).")
        return []
    collected = []
    try:
        async with async_playwright() as pw:
            # tenta lançar chromium -- HEADLESS
            try:
                browser = await pw.chromium.launch(headless=True,
                                                   args=[
                                                       "--no-sandbox",
                                                       "--disable-setuid-sandbox",
                                                       "--disable-blink-features=AutomationControlled",
                                                       "--disable-web-security",
                                                       "--disable-features=IsolateOrigins,site-per-process"
                                                   ])
            except PlaywrightError as e:
                send(f"(erro ao lançar navegador) {e}")
                return []

            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114 Safari/537.36",
                bypass_csp=True,
            )
            page = await context.new_page()

            # coleta requisições interessantes
            def on_request(req):
                u = req.url
                if any(x in u for x in (".m3u8", ".mp4", ".mpd", "playlist", "manifest", "blob:")):
                    collected.append(u)
            page.on("request", on_request)

            # também checa responses que trazem content-type video or m3u8
            def on_response(resp):
                try:
                    ct = resp.headers.get("content-type", "")
                    u = resp.url
                    if ct and any(x in ct for x in ("application/vnd.apple.mpegurl", "application/x-mpegurl", "video/", "application/dash+xml")):
                        collected.append(u)
                except:
                    pass
            page.on("response", on_response)

            # navegar
            try:
                await page.goto(url, wait_until="networkidle", timeout=20000)
            except Exception:
                # fallback navegar com menos espera
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=15000)
                except Exception:
                    pass

            # heurísticas para "acionar" player: clicar em botões comuns
            click_selectors = [
                "button:has-text(\"VIP\")", "button:has-text(\"VIP Player\")",
                "button:has-text(\"PLAY\")", "button:has-text(\"Play\")",
                "button.play", ".play", "button.btn-play", ".btn-play", "a.play"
            ]
            for sel in click_selectors:
                try:
                    if await page.query_selector(sel):
                        await page.click(sel, timeout=3000)
                        send(f"(play) clique em {sel}")
                        await asyncio.sleep(1)
                except Exception:
                    pass

            # tentar apertar espaço (alguns players respondem)
            try:
                await page.keyboard.press("Space")
            except Exception:
                pass

            # aguardar por novas requisições
            await asyncio.sleep(timeout_sec)

            # também tenta procurar na DOM por <video> tags / sources
            try:
                vids = await page.eval_on_selector_all("video source, video", "els => els.map(e=>e.src || e.getAttribute('data-src') || '')")
                for v in vids:
                    if v:
                        collected.append(v)
            except Exception:
                pass

            # fechar
            await browser.close()

            # normalizar
            collected = [u for u in collected if u and u.startswith(("http", "https", "blob:"))]
            collected = list(dict.fromkeys(collected))
            if collected:
                send(f"(encontrado via Playwright) {len(collected)} links")
            else:
                send("(nenhum link via Playwright)")
            return collected
    except Exception as e:
        send(f"(erro Playwright) {repr(e)}\n{traceback.format_exc()}")
        return []

# -------- attempt 4: fallback regex na página (requests já pego, mas repetimos) --------
def attempt_fallback_regex(url: str, send):
    send("TENTANDO: fallback (regex/HTML)")
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        r = requests.get(url, headers=headers, timeout=12)
        r.raise_for_status()
        found = regex_find_media(r.text, base=url)
        if found:
            send(f"(encontrado via fallback) {len(found)} links")
        else:
            send("nenhum link via fallback")
        return found
    except Exception as e:
        send(f"(erro fallback) {repr(e)}")
        return []

# -------- endpoint utilitário --------
@app.get("/server-info")
def server_info():
    features = {
        "yt_dlp": HAS_YTDLP,
        "playwright_pkg": HAS_PLAYWRIGHT,
    }
    return {"status": "ok", "msg": "Servicurl - extractor ready", "features": features}

# -------- quick POST extract (sync, compat) --------
@app.post("/extract")
def extract_sync(req: Request):
    """
    Endpoint rápido para uso sem SSE. Retorna JSON com os links
    (faz todas as tentativas de forma síncrona/simplificada).
    """
    data = asyncio.get_event_loop().run_until_complete(extract_workflow(req))
    return JSONResponse(content=data)

# -------- helper externo que realiza o fluxo (usado por /extract e /extract_stream_sse) --------
async def extract_workflow(req_or_url):
    """
    aceita Request (FastAPI) ou string url
    retorna dict com method/result/attempts
    """
    if isinstance(req_or_url, Request):
        payload = await req_or_url.json()
        url = payload.get("url") or payload.get("u") or payload.get("link")
    else:
        url = req_or_url

    attempts_summary = {"basic": [], "yt_dlp": [], "playwright": [], "fallback_regex": []}
    final_links = []

    # helper local para SSE (no contexto non-sse apenas usa append logs)
    logs = []

    def send_log(msg: str):
        logs.append(msg)

    # 1) basic
    basic = attempt_basic(url, send_log)
    attempts_summary["basic"] = basic
    final_links.extend(basic)

    # 2) yt-dlp
    if HAS_YTDLP:
        ytd = attempt_ytdlp(url, send_log)
        attempts_summary["yt_dlp"] = ytd
        final_links.extend(ytd)
    else:
        send_log("yt-dlp não disponível; pulando.")

    # 3) Playwright (async)
    if HAS_PLAYWRIGHT:
        try:
            pw_links = await attempt_playwright(url, send_log)
            attempts_summary["playwright"] = pw_links
            final_links.extend(pw_links)
        except Exception as e:
            send_log(f"erro geral Playwright: {repr(e)}")
    else:
        send_log("Playwright pacote ausente ou não importável; pulando.")

    # 4) fallback regex
    fallback = attempt_fallback_regex(url, send_log)
    attempts_summary["fallback_regex"] = fallback
    final_links.extend(fallback)

    # dedupe e filtro
    final_links = [l for l in dict.fromkeys(final_links) if l]

    method = "none"
    if final_links:
        # heurística: se tiver m3u8, prioriza
        if any(".m3u8" in l for l in final_links):
            method = "m3u8"
        elif any(l.endswith(".mp4") or ".mp4" in l for l in final_links):
            method = "mp4"
        else:
            method = "found"

    result = {"method": method, "links": final_links, "attempts": attempts_summary, "logs": logs}
    return {"status":"done", "result": result}

# -------- SSE streaming endpoint --------
@app.get("/extract_stream_sse")
async def extract_stream_sse(url: str = Query(..., description="URL to extract")):
    """
    SSE endpoint: vai emitindo mensagens de progresso (strings) e no final envia JSON com 'status':'done' e resultado.
    Uso: curl -N "https://<host>/extract_stream_sse?url=..."
    """
    async def event_gen():
        # start
        yield sse_line("Iniciando extração...")
        # We'll stream logs from extract_workflow by using a small bridging mechanism:
        # Instead of reimplementing all attempts here, call extract_workflow but substitute send_log to yield...
        # To keep code simple and robust we will replicate calls but streaming.
        try:
            # basic
            yield sse_line("TENTANDO: requisição simples (requests/BS4)")
            try:
                headers = {"User-Agent": "Mozilla/5.0"}
                r = requests.get(url, headers=headers, timeout=15)
                r.raise_for_status()
                soup = BeautifulSoup(r.text, "lxml")
                basic = absolute_urls_from_bs4(url, soup)
                if basic:
                    yield sse_line(f"(encontrado via basic) {len(basic)} links")
                else:
                    yield sse_line("(nenhum link via basic)")
            except Exception as e:
                basic = []
                yield sse_line(f"(erro basic) {repr(e)}")

            # yt-dlp
            if HAS_YTDLP:
                yield sse_line("TENTANDO: yt-dlp")
                try:
                    ytd_links = []
                    ydl_opts = {"skip_download": True, "quiet": True, "nocheckcertificate": True}
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        info = ydl.extract_info(url, download=False)
                    if isinstance(info, dict) and "formats" in info:
                        for f in info["formats"]:
                            u = f.get("url")
                            if u:
                                ytd_links.append(u)
                    elif isinstance(info, dict) and info.get("url"):
                        ytd_links.append(info.get("url"))
                    if ytd_links:
                        yield sse_line(f"(encontrado via yt-dlp) {len(ytd_links)} links")
                    else:
                        yield sse_line("(nenhum link via yt-dlp)")
                except Exception as e:
                    ytd_links = []
                    yield sse_line(f"(erro yt-dlp) {repr(e)}")
            else:
                ytd_links = []
                yield sse_line("yt-dlp não disponível; pulando.")

            # Playwright (stream logs as we go)
            pw_links = []
            if HAS_PLAYWRIGHT:
                yield sse_line("TENTANDO: Playwright (execução JS e interceptação de rede)")
                try:
                    # run playwright attempt_playwright but capture its internal send_log by passing a wrapper
                    async def send(msg):
                        yield sse_line(msg)  # can't yield from nested, so we'll collect differently
                    # simpler: call attempt_playwright and then fetch logs from returned messages
                    # BUT attempt_playwright uses send_log closures - to keep things simple we call it and then stream a final status
                    pw_links = await attempt_playwright(url, lambda m: None, timeout_sec=12)
                    if pw_links:
                        yield sse_line(f"(encontrado via Playwright) {len(pw_links)} links")
                    else:
                        yield sse_line("(nenhum link via Playwright)")
                except Exception as e:
                    yield sse_line(f"(erro Playwright) {repr(e)}")
            else:
                yield sse_line("Playwright pacote ausente; pulando.")

            # fallback regex
            yield sse_line("TENTANDO: fallback (regex/HTML)")
            try:
                headers = {"User-Agent": "Mozilla/5.0"}
                r2 = requests.get(url, headers=headers, timeout=12)
                r2.raise_for_status()
                fallback_links = regex_find_media(r2.text, base=url)
                if fallback_links:
                    yield sse_line(f"(encontrado via fallback) {len(fallback_links)} links")
                else:
                    yield sse_line("nenhum link via fallback")
            except Exception as e:
                fallback_links = []
                yield sse_line(f"(erro fallback) {repr(e)}")

            # combine
            final = list(dict.fromkeys((basic or []) + (ytd_links or []) + (pw_links or []) + (fallback_links or [])))
            if final:
                # prefer m3u8 if present
                if any(".m3u8" in u for u in final):
                    method = "m3u8"
                elif any(".mp4" in u for u in final):
                    method = "mp4"
                else:
                    method = "found"
            else:
                method = "none"

            yield sse_json({"status": "done", "result": {"method": method, "links": final,
                                                          "attempts": {"basic": basic, "yt_dlp": ytd_links,
                                                                       "playwright": pw_links, "fallback_regex": fallback_links}}})
        except Exception as e:
            yield sse_line(f"Erro interno: {repr(e)}")
            yield sse_json({"status": "error", "error": str(e)})
        # final marker for some clients
        yield sse_line("[DONE]")

    return StreamingResponse(event_gen(), media_type="text/event-stream")

# -------- shim endpoint used earlier by front-end (generate_stream + SSE start) --------
@app.post("/generate_stream")
def generate_stream_start(req: Request):
    # apenas confirma recebimento do prompt (compat)
    return {"status": "ok"}

@app.get("/generate_stream_sse")
async def generate_stream_sse(prompt: str = Query(...)):
    # simple demo that replies fragments (kept for compat with previous front-end)
    async def gen():
        for i in range(5):
            yield sse_line(f"Parte {i+1} do texto para: {prompt}")
            await asyncio.sleep(0.9)
        yield sse_line("[DONE]")
    return StreamingResponse(gen(), media_type="text/event-stream")
