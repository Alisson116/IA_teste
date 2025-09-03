# server.py
import os
import time
import json
import re
import base64
import shlex
import subprocess
from typing import List, Dict, Any, Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse

# optional imports
try:
    from yt_dlp import YoutubeDL
    HAS_YTDLP = True
except Exception:
    HAS_YTDLP = False

try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except Exception:
    HAS_PLAYWRIGHT = False

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------- helpers ----------
def sse_message(text: str):
    # escape newlines properly
    safe = text.replace("\n", "\\n")
    return f"data: {safe}\n\n"

def normalize_urls(base: str, items):
    out = []
    for u in items:
        if not u: continue
        try:
            out.append(urljoin(base, u))
        except Exception:
            out.append(u)
    # dedupe preserving order
    seen = set()
    out2 = []
    for u in out:
        if u not in seen:
            seen.add(u)
            out2.append(u)
    return out2

# ---------- basic requests / bs4 ----------
def basic_requests_extract(url: str, timeout=10) -> List[str]:
    links = []
    try:
        r = requests.get(url, headers={"User-Agent":"Mozilla/5.0"}, timeout=timeout)
        text = r.text
        soup = BeautifulSoup(text, "lxml")
        # <video> and <source>
        for v in soup.select("video"):
            src = v.get("src")
            if src: links.append(src)
            for s in v.find_all("source"):
                ss = s.get("src")
                if ss: links.append(ss)
        for s in soup.select("source"):
            src = s.get("src")
            if src: links.append(src)
        # search inline scripts for urls
        scripts = soup.find_all("script", src=False)
        for s in scripts:
            t = (s.string or "")[:4000]
            for m in re.findall(r'https?:\\/\\/[^"\\s\\}]+\\.(?:m3u8|mp4)[^"\\s\\}]*', t):
                links.append(m.replace("\\/","/"))
            for m in re.findall(r'(https?://[^"\'\s>]+\\.(?:mp4|m3u8)[^"\']*)', t):
                links.append(m)
        # also look for direct m3u8/mp4 in HTML
        for m in re.findall(r'https?://[^\s"\']+\.(?:m3u8|mp4)[^\s"\']*', text):
            links.append(m)
    except Exception:
        pass
    return normalize_urls(url, links)

# ---------- yt-dlp attempt ----------
def yt_dlp_extract(url: str, timeout=30) -> List[str]:
    if not HAS_YTDLP:
        return []
    links = []
    ydl_opts = {"quiet": True, "skip_download": True, "no_warnings": True}
    try:
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            # check formats
            if isinstance(info, dict):
                if "formats" in info and info["formats"]:
                    for f in info["formats"]:
                        if f.get("url"):
                            links.append(f["url"])
                # requested_formats (sometimes)
                if "requested_formats" in info and info["requested_formats"]:
                    for f in info["requested_formats"]:
                        if f.get("url"):
                            links.append(f["url"])
                # direct url
                if info.get("url"):
                    links.append(info["url"])
    except Exception:
        # fallback: call yt-dlp subprocess -j (some hosts)
        try:
            cmd = ["yt-dlp", "--no-warnings", "-j", url]
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            if p.returncode == 0 and p.stdout:
                try:
                    j = json.loads(p.stdout)
                    if isinstance(j, dict):
                        if "formats" in j:
                            for f in j["formats"]:
                                if f.get("url"): links.append(f["url"])
                        if j.get("url"): links.append(j["url"])
                except Exception:
                    pass
        except Exception:
            pass
    return normalize_urls(url, links)

# ---------- playwright aggressive ----------
def playwright_aggressive_extract(url: str, timeout=35, headless=True, max_wait_for_network=8, try_headful_if_allowed=False, proxy: Optional[str]=None) -> List[str]:
    """
    Aggressive Playwright attempt:
    - inject hooks for fetch/XHR/WebSocket send
    - intercept responses
    - simulate clicks (play), mouse moves, keyboard
    - search inline scripts, base64 strings
    """
    if not HAS_PLAYWRIGHT:
        return []
    collected = set()
    # small JS to hook fetch/XHR/WS
    hook_script = r"""
    (function(){
      try{
        window.__captured_requests = window.__captured_requests || [];
        const origFetch = window.fetch;
        window.fetch = async function(input, init){
          try {
            const resp = await origFetch(input, init);
            try { resp.clone().text().then(t => window.__captured_requests.push({url: resp.url, status: resp.status, text: t.substring(0,3000)})); } catch(e){}
            return resp;
          } catch(e) { throw e; }
        };
      } catch(e){}
      try {
        const origX = window.XMLHttpRequest;
        function HookedXHR(){
          const xhr = new origX();
          const open = xhr.open;
          const send = xhr.send;
          xhr.open = function(method, url){
            this.__url = url;
            return open.apply(this, arguments);
          };
          xhr.send = function(){
            this.addEventListener('load', function(){
              try { window.__captured_requests.push({url: this.__url, status: this.status, text: (this.responseText||'').substring(0,3000)}); } catch(e){}
            });
            return send.apply(this, arguments);
          };
          return xhr;
        }
        window.XMLHttpRequest = HookedXHR;
      } catch(e){}
      try {
        const OrigWS = window.WebSocket;
        if (OrigWS) {
          window.WebSocket = function(url, protocols){
            const ws = protocols ? new OrigWS(url, protocols) : new OrigWS(url);
            try {
              const origSend = ws.send;
              ws.send = function(d){ try{ window.__captured_requests.push({url: url, ws_send: (''+d).substring(0,200)}); }catch(e){} return origSend.apply(this, arguments); };
            } catch(e){}
            return ws;
          };
        }
      } catch(e){}
    })();
    """

    try:
        with sync_playwright() as p:
            browser_args = [
                "--no-sandbox","--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ]
            chromium = p.chromium
            playwright_launch_args = {"headless": headless, "args": browser_args}
            if proxy:
                playwright_launch_args["proxy"] = {"server": proxy}

            browser = chromium.launch(**playwright_launch_args)
            contexts = [
                {"user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36", "viewport": {"width":1280,"height":800}},
                {"user_agent": "Mozilla/5.0 (Linux; Android 12; SM-A105F) AppleWebKit/537.36 Chrome/120 Mobile Safari/537.36", "viewport": {"width":412,"height":915}}
            ]
            for ctx in contexts:
                try:
                    context = browser.new_context(user_agent=ctx["user_agent"], viewport=ctx["viewport"], ignore_https_errors=True, bypass_csp=True)
                    # minor anti-detect
                    context.add_init_script("""
                        Object.defineProperty(navigator, 'webdriver', {get: () => false});
                        Object.defineProperty(navigator, 'languages', {get: () => ['pt-BR','en-US']});
                        Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3]});
                    """)
                    context.add_init_script(hook_script)
                    page = context.new_page()
                    # collect response urls
                    def on_response(resp):
                        try:
                            rurl = resp.url
                            ct = (resp.headers.get("content-type") or "").lower()
                            if re.search(r'\.(mp4|m3u8|manifest|playlist)', rurl, flags=re.I) or any(k in ct for k in ("mpegurl","application/vnd.apple.mpegurl","video/","application/json")):
                                collected.add(rurl)
                        except Exception:
                            pass
                    page.on("response", on_response)

                    # goto
                    page.goto(url, wait_until="domcontentloaded", timeout=timeout*1000)
                    time.sleep(0.6)

                    # try video elements
                    try:
                        vids = page.eval_on_selector_all("video, source", "els => els.map(e => e.src || e.getAttribute('src')).filter(Boolean)")
                        for s in vids:
                            if s: collected.add(urljoin(url, s))
                    except Exception:
                        pass

                    # try clicking on obvious controls
                    click_selectors = [
                        "button[class*=play]", ".jw-icon-play", ".vjs-play-control", "button[aria-label*='Play']",
                        "div.play", "button[title*='play']", "a[title*='play']"
                    ]
                    clicked=False
                    for sel in click_selectors:
                        try:
                            if page.locator(sel).count() > 0:
                                page.locator(sel).first.click(timeout=2000)
                                clicked = True
                                # wait a bit
                                try:
                                    r = page.wait_for_response(lambda r: re.search(r'(m3u8|mp4|manifest|playlist)', r.url, flags=re.I), timeout=3000)
                                    if r and r.url:
                                        collected.add(r.url)
                                except Exception:
                                    pass
                        except Exception:
                            pass

                    # if nothing clicked, attempt center click on video container
                    if not clicked:
                        try:
                            box = page.query_selector("video, .player, #player, div[class*=player]")
                            if box:
                                box.click()
                                time.sleep(0.8)
                        except Exception:
                            pass

                    # wait cycles reading window.__captured_requests
                    for _ in range(max_wait_for_network):
                        time.sleep(1)
                        try:
                            arr = page.evaluate("() => (window.__captured_requests || []).slice(-40)")
                            if arr:
                                for o in arr:
                                    if not isinstance(o, dict): continue
                                    u = o.get("url")
                                    txt = o.get("text","")
                                    if u and re.search(r'\.(m3u8|mp4|manifest|playlist)', u, flags=re.I):
                                        collected.add(u)
                                    # search inside text blobs
                                    for m in re.findall(r'https?:\\/\\/[^"\\s\\}]+\\.(?:m3u8|mp4)[^"\\s\\}]*', str(txt)):
                                        collected.add(m.replace("\\/","/"))
                                    for m in re.findall(r'https?://[^"\'\s>]+\\.(?:mp4|m3u8)[^"\']*', str(txt)):
                                        collected.add(m)
                        except Exception:
                            pass

                    # scan inline scripts for base64 -> decode heuristics
                    try:
                        scripts = page.evaluate("""() => Array.from(document.querySelectorAll('script:not([src])')).map(s=>s.innerText||'').slice(-20)""")
                        for t in scripts:
                            # find long base64-ish strings
                            for b64 in re.findall(r'([A-Za-z0-9+/=]{120,})', t):
                                try:
                                    decoded = base64.b64decode(b64 + "===" ).decode("utf-8", errors="ignore")
                                    for m in re.findall(r'https?://[^"\s\']+\.(?:m3u8|mp4)[^"\s\']*', decoded):
                                        collected.add(m)
                                except Exception:
                                    pass
                            # also general url regex
                            for m in re.findall(r'https?://[^\s"\']+\.(?:m3u8|mp4)[^\s"\']*', t):
                                collected.add(m)
                    except Exception:
                        pass

                    # try common API players (jwplayer)
                    try:
                        jw = page.evaluate("""() => { try { if (window.jwplayer) { const pl = window.jwplayer().getPlaylist(); return pl.map(p=>p.file).filter(Boolean); } } catch(e){} return null; }""")
                        if jw:
                            for s in jw: collected.add(urljoin(url, s))
                    except Exception:
                        pass

                    # try to extract from video.currentSrc
                    try:
                        curr = page.evaluate("""() => { try { const v = document.querySelector('video'); return v && v.currentSrc ? v.currentSrc : null } catch(e) { return null; } }""")
                        if curr:
                            collected.add(urljoin(url, curr))
                    except Exception:
                        pass

                    try:
                        context.close()
                    except Exception:
                        pass

                    if collected:
                        break
                except Exception:
                    try:
                        context.close()
                    except Exception:
                        pass
                    continue
            try:
                browser.close()
            except Exception:
                pass
    except Exception:
        pass

    return normalize_urls(url, list(collected))

# ---------- fallback regex ----------
def fallback_regex_extract(url: str, timeout=10) -> List[str]:
    links = []
    try:
        r = requests.get(url, headers={"User-Agent":"Mozilla/5.0"}, timeout=timeout)
        text = r.text
        # common playlist/file patterns
        for m in re.findall(r'https?://[^\s"\']+\.(?:m3u8|mp4|mpd)[^\s"\']*', text):
            links.append(m)
        # also some JS-assembled urls (http\\u003a etc)
        for m in re.findall(r'(https?:\\\\/\\\\/[^"\\s]+\\.(?:m3u8|mp4)[^"\\s]*)', text):
            links.append(m.replace("\\/","/"))
    except Exception:
        pass
    return normalize_urls(url, links)

# ---------- orchestration ----------
def run_full_extraction(url: str, send_progress) -> Dict[str,Any]:
    result = {"method":"none","links":[],"attempts":{"basic":[],"yt_dlp":[],"playwright":[],"fallback_regex":[]}}
    # 1) basic
    send_progress("TENTANDO: requisição simples (requests/BS4)")
    br = basic_requests_extract(url)
    result["attempts"]["basic"] = br
    if br:
        result["method"] = "basic"
        result["links"] = br
        return result

    # 2) yt-dlp
    send_progress("TENTANDO: yt-dlp")
    ytd = yt_dlp_extract(url)
    result["attempts"]["yt_dlp"] = ytd
    if ytd:
        result["method"] = "yt_dlp"
        result["links"] = ytd
        return result

    # 3) Playwright
    send_progress("TENTANDO: Playwright (execução JS e interceptação de rede)")
    # allow override via env if you want headful tests locally
    headless_env = os.getenv("PLAYWRIGHT_HEADLESS", "1") != "0"
    proxy = os.getenv("EXTRACT_PROXY")  # optional proxy like http://user:pass@x.y:port
    pl = playwright_aggressive_extract(url, headless=headless_env, proxy=proxy, max_wait_for_network=int(os.getenv("PLAYWRIGHT_WAIT", "10")))
    result["attempts"]["playwright"] = pl
    if pl:
        result["method"] = "playwright"
        result["links"] = pl
        return result

    # 4) fallback regex
    send_progress("TENTANDO: fallback (regex/HTML)")
    fb = fallback_regex_extract(url)
    result["attempts"]["fallback_regex"] = fb
    if fb:
        result["method"] = "fallback"
        result["links"] = fb
        return result

    return result

# ---------- endpoints ----------
@app.get("/server-info")
def server_info():
    features = {"yt_dlp": HAS_YTDLP, "playwright": HAS_PLAYWRIGHT}
    return {"status":"ok","msg":"Servidor pronto","features":features}

@app.post("/extract")
async def extract(req: Request):
    data = await req.json()
    url = data.get("url")
    if not url:
        return JSONResponse({"error":"missing url"}, status_code=400)
    def dummy_send_progress(msg: str):
        pass
    result = run_full_extraction(url, dummy_send_progress)
    return {"method": result["method"], "links": result["links"], "attempts": result["attempts"]}

@app.get("/extract_stream_sse")
async def extract_stream_sse(url: str):
    def event_stream():
        def send_progress(msg: str):
            yield sse_message(msg)
        # we want to yield messages sequentially, so wrap calls to run_full_extraction manually
        # We'll re-implement orchestration here so we can yield intermediate SSE messages.
        yield sse_message("Iniciando extração...")
        # 1 basic
        yield sse_message("TENTANDO: requisição simples (requests/BS4)")
        br = basic_requests_extract(url)
        if br:
            yield sse_message(f"(nenhum link via basic)" if not br else f"Links via basic: {json.dumps(br[:6])}")
            yield sse_message(json.dumps({"status":"done","result":{"method":"basic","links":br,"attempts":{"basic":br}}}))
            yield sse_message("[DONE]")
            return
        else:
            yield sse_message("(nenhum link via basic)")

        # 2 yt-dlp
        yield sse_message("TENTANDO: yt-dlp")
        ytd = yt_dlp_extract(url)
        if ytd:
            yield sse_message(f"Links via yt-dlp: {json.dumps(ytd[:6])}")
            yield sse_message(json.dumps({"status":"done","result":{"method":"yt_dlp","links":ytd,"attempts":{"yt_dlp":ytd}}}))
            yield sse_message("[DONE]")
            return
        else:
            yield sse_message("(nenhum link via yt-dlp)")

        # 3 Playwright
        yield sse_message("TENTANDO: Playwright (execução JS e interceptação de rede)")
        headless_env = os.getenv("PLAYWRIGHT_HEADLESS", "1") != "0"
        proxy = os.getenv("EXTRACT_PROXY")
        pl = playwright_aggressive_extract(url, headless=headless_env, proxy=proxy, max_wait_for_network=int(os.getenv("PLAYWRIGHT_WAIT","10")))
        if pl:
            yield sse_message(f"Links via Playwright: {json.dumps(pl[:6])}")
            yield sse_message(json.dumps({"status":"done","result":{"method":"playwright","links":pl,"attempts":{"playwright":pl}}}))
            yield sse_message("[DONE]")
            return
        else:
            yield sse_message("(nenhum link via Playwright)")

        # 4 fallback
        yield sse_message("TENTANDO: fallback (regex/HTML)")
        fb = fallback_regex_extract(url)
        if fb:
            yield sse_message(f"Links via fallback: {json.dumps(fb[:6])}")
            yield sse_message(json.dumps({"status":"done","result":{"method":"fallback","links":fb,"attempts":{"fallback_regex":fb}}}))
            yield sse_message("[DONE]")
            return
        else:
            yield sse_message("nenhum link encontrado em todas as tentativas.")
            yield sse_message(json.dumps({"status":"done","result":{"method":"none","links":[],"attempts":{"basic":[], "yt_dlp":[], "playwright":[], "fallback_regex":[]}}}))
            yield sse_message("[DONE]")

    return StreamingResponse(event_stream(), media_type="text/event-stream")
