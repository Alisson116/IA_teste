# server.py
import os
import re
import json
import asyncio
import time
import traceback
from typing import List, Dict, Any, Optional
import requests
from fastapi import FastAPI, Request, Query
from fastapi.responses import JSONResponse, StreamingResponse
from bs4 import BeautifulSoup

# optional imports (yt_dlp, playwright). We import lazily to fail gracefully.
try:
    from yt_dlp import YoutubeDL
    HAS_YTDLP = True
except Exception:
    HAS_YTDLP = False

try:
    from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
    HAS_PLAYWRIGHT = True
except Exception:
    HAS_PLAYWRIGHT = False

app = FastAPI(title="Extractor Service")

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; extractor/1.0; +https://example.com)"}

# ---------------- helpers ----------------

def find_media_in_html(html: str, base_url: Optional[str] = None) -> List[str]:
    urls = set()
    soup = BeautifulSoup(html, "lxml")
    # <video> tags
    for v in soup.find_all("video"):
        src = v.get("src")
        if src:
            urls.add(src)
        for s in v.find_all("source"):
            if s.get("src"):
                urls.add(s.get("src"))
    # iframes (common embed patterns)
    for iframe in soup.find_all("iframe"):
        if iframe.get("src"):
            urls.add(iframe.get("src"))
    # meta og:video
    for tag in soup.find_all(["meta"]):
        if tag.get("property") in ("og:video", "og:video:url") and tag.get("content"):
            urls.add(tag.get("content"))
    # regex find .m3u8/.mp4/.ts
    for m in re.findall(r'(https?://[^\s"\'<>]+?\.(?:m3u8|mp4|ts)(?:\?[^\s"\'<>]*)?)', html, flags=re.I):
        urls.add(m)
    # relative src resolution is omitted for simplicity
    return list(urls)

def try_requests_basic(url: str, timeout: int = 12) -> List[str]:
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        if r.status_code != 200:
            return []
        html = r.text
        return find_media_in_html(html, base_url=url)
    except Exception:
        return []

def try_yt_dlp(url: str, timeout: int = 30) -> List[str]:
    if not HAS_YTDLP:
        return []
    try:
        opts = {
            "quiet": True,
            "skip_download": True,
            "simulate": True,
            "no_warnings": True,
            "ignoreerrors": True,
        }
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            urls = set()
            if not info:
                return []
            # if formats exist, collect format urls
            if isinstance(info, dict):
                formats = info.get("formats") or []
                for f in formats:
                    u = f.get("url")
                    if u:
                        urls.add(u)
                # sometimes 'url' at top level
                topu = info.get("url")
                if topu:
                    urls.add(topu)
            return list(urls)
    except Exception:
        return []

async def try_playwright_capture(url: str, timeout: int = 25) -> List[str]:
    if not HAS_PLAYWRIGHT:
        return []
    collected = set()
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True,
                                              args=["--no-sandbox", "--disable-setuid-sandbox"])
            page = await browser.new_page()
            # collect requests urls
            def on_request(req):
                try:
                    u = req.url
                    if u:
                        collected.add(u)
                except Exception:
                    pass
            page.on("request", on_request)
            # also capture responses (sometimes resource urls are in responses)
            def on_response(resp):
                try:
                    u = resp.url
                    if u:
                        collected.add(u)
                except Exception:
                    pass
            page.on("response", on_response)

            try:
                await page.goto(url, wait_until="networkidle", timeout=timeout * 1000)
            except PlaywrightTimeoutError:
                # try a fallback wait
                try:
                    await asyncio.sleep(2)
                except Exception:
                    pass
            # small delay to catch late requests
            await asyncio.sleep(2)
            # inspect DOM for <video> sources
            try:
                content = await page.content()
                for u in find_media_in_html(content, base_url=url):
                    collected.add(u)
            except Exception:
                pass
            await browser.close()
    except Exception:
        # ensure not to leak exceptions
        # traceback.print_exc()
        pass
    # filter common media extensions
    res = []
    for u in collected:
        if re.search(r'\.(m3u8|mp4|ts)(\?.*)?$', u, flags=re.I):
            res.append(u)
    # dedupe
    return list(dict.fromkeys(res))

# --------------- orchestration ----------------

async def layered_extract(url: str, send_progress=None) -> Dict[str, Any]:
    """
    send_progress: optional callback function that accepts a string to send to SSE.
    Returns dict: {method: str, links: [], attempts: {...}}
    """
    attempts = {}
    # 1) requests basic
    if send_progress:
        await send_progress("TENTANDO: requisição simples (requests/BS4)")
    basic = try_requests_basic(url)
    attempts['basic'] = basic
    if basic:
        return {"method": "basic", "links": basic, "attempts": attempts}

    # 2) yt-dlp
    if send_progress:
        await send_progress("TENTANDO: yt-dlp")
    ytd = try_yt_dlp(url)
    attempts['yt_dlp'] = ytd
    if ytd:
        return {"method": "yt_dlp", "links": ytd, "attempts": attempts}

    # 3) Playwright (headless JS)
    if send_progress:
        await send_progress("TENTANDO: Playwright (execução JS e interceptação de rede)")
    ply = await try_playwright_capture(url)
    attempts['playwright'] = ply
    if ply:
        return {"method": "playwright", "links": ply, "attempts": attempts}

    # 4) fallback: try fetching html and regex again (maybe dynamic query string)
    if send_progress:
        await send_progress("TENTANDO: fallback (regex/HTML)")
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        attempt_regex = re.findall(r'(https?://[^\s"\'<>]+?\.(?:m3u8|mp4|ts)(?:\?[^\s"\'<>]*)?)', r.text, flags=re.I)
    except Exception:
        attempt_regex = []
    attempts['fallback_regex'] = attempt_regex

    return {"method": "none", "links": attempt_regex, "attempts": attempts}

# ---------------- endpoints ----------------

@app.post("/extract")
async def extract_endpoint(req: Request):
    """
    POST JSON: {"url": "https://..."}
    Returns: JSON with method & links & attempts
    """
    try:
        data = await req.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)
    url = data.get("url")
    if not url:
        return JSONResponse({"error": "missing url"}, status_code=400)

    result = await layered_extract(url)
    return JSONResponse(result)

@app.post("/extract_stream")
async def extract_stream_start(req: Request):
    """
    Simple confirmation endpoint for frontends that first POST then open SSE.
    """
    try:
        data = await req.json()
    except Exception:
        data = {}
    url = data.get("url")
    if not url:
        return JSONResponse({"error": "missing url"}, status_code=400)
    # just confirm start OK
    return JSONResponse({"status": "ok", "url": url})

@app.get("/extract_stream_sse")
async def extract_stream_sse(url: str = Query(...)):
    """
    SSE endpoint. Usage: /extract_stream_sse?url=...
    Sends progress messages and the final JSON result as the last data chunk.
    """
    async def event_gen():
        async def send_progress(msg: str):
            payload = f"data: {msg}\n\n"
            await asyncio.sleep(0)  # yield control
            yield payload

        # wrapper to allow layered_extract to call send_progress easily
        messages = []

        async def _send(msg: str):
            messages.append(msg)
        # run layered_extract, passing _send
        try:
            # we will stream progress messages as they are appended
            # Start layered_extract in task and stream messages periodically
            task = asyncio.create_task(layered_extract(url, send_progress=lambda m: _send(m)))
            last_idx = 0
            while not task.done():
                # stream any new messages
                while last_idx < len(messages):
                    m = messages[last_idx]; last_idx += 1
                    yield f"data: {m}\n\n"
                await asyncio.sleep(0.3)
            result = await task
            # stream remaining messages
            while last_idx < len(messages):
                m = messages[last_idx]; last_idx += 1
                yield f"data: {m}\n\n"
            # final JSON payload
            final = json.dumps({"status":"done","result": result}, ensure_ascii=False)
            yield f"data: {final}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as e:
            tb = traceback.format_exc()
            yield f"data: [ERROR] {str(e)}\n\n"
            yield f"data: {tb}\n\n"
            yield "data: [DONE]\n\n"

    return StreamingResponse(event_gen(), media_type="text/event-stream")

@app.get("/server-info")
async def server_info():
    return {"status":"ok","msg":"Servicurl - extractor ready","features": {"yt_dlp": HAS_YTDLP, "playwright": HAS_PLAYWRIGHT}}
