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




def playwright_extract(url: str, timeout=35, max_wait_for_network=6) -> List[str]:
    """
    Playwright agressivo: hooks de fetch/XHR antes da navegação, intercepta responses,
    simula cliques/scrolls, tenta múltiplas heurísticas (jwplayer, video tags, JSON embutido).
    Retorna lista de links (mp4/m3u8/etc).
    """
    if not HAS_PLAYWRIGHT:
        return []

    collected = set()
    attempts = {"init_hooks": False, "page_clicks": [], "xhr_hits": [], "responses": []}

    hook_script = r"""
    // coleta simples global
    (function(){
      window.__captured_requests = window.__captured_requests || [];
      // hook fetch
      const origFetch = window.fetch;
      window.fetch = async function(input, init){
        try{
          const resp = await origFetch(input, init);
          try {
            const clone = resp.clone();
            clone.text().then(txt=>{
              window.__captured_requests.push({url: resp.url, status: resp.status, text: txt.substring(0,3000)});
            }).catch(()=>{ window.__captured_requests.push({url: resp.url, status: resp.status}); });
          }catch(e){}
          return resp;
        }catch(e){
          throw e;
        }
      };
      // hook XHR
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
            try {
              const txt = this.responseText;
              window.__captured_requests.push({url: this.__url, status: this.status, text: (txt||'').substring(0,3000)});
            } catch(e){}
          });
          return send.apply(this, arguments);
        };
        return xhr;
      }
      window.XMLHttpRequest = HookedXHR;
      // hook WebSocket send (log only)
      try {
        const OrigWS = window.WebSocket;
        window.WebSocket = function(url, proto){
          const ws = proto ? new OrigWS(url, proto) : new OrigWS(url);
          try {
            const origSend = ws.send;
            ws.send = function(d){ try{ window.__captured_requests.push({url: url, ws_send: (''+d).substring(0,200)}); }catch(e){} return origSend.apply(this, arguments); };
          } catch(e){}
          return ws;
        };
      } catch(e){}
    })();
    """

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
            ])

            # várias tentativas com diferentes contextos (desktop, mobile)
            contexts = [
                {"user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36", "viewport": {"width":1280,"height":800}, "is_mobile": False},
                {"user_agent": "Mozilla/5.0 (Linux; Android 12; SM-A105F) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Mobile Safari/537.36", "viewport": {"width":360,"height":780}, "is_mobile": True}
            ]

            for ctx_conf in contexts:
                try:
                    context = browser.new_context(
                        user_agent=ctx_conf["user_agent"],
                        viewport=ctx_conf["viewport"],
                        java_script_enabled=True,
                        ignore_https_errors=True,
                        bypass_csp=True,
                        extra_http_headers={"referer": url}
                    )

                    # spoof navigator
                    context.add_init_script("""
                        Object.defineProperty(navigator, 'webdriver', {get: () => false});
                        Object.defineProperty(navigator, 'languages', {get: () => ['pt-BR','en-US']});
                        Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3]});
                    """)

                    # add fetch/XHR hooks before navigation
                    context.add_init_script(hook_script)
                    attempts["init_hooks"] = True

                    page = context.new_page()
                    # attach response listener
                    def on_response(resp):
                        try:
                            rurl = resp.url
                            ct = (resp.headers.get("content-type") or "").lower()
                            if re.search(r'\.(mp4|m3u8|manifest|playlist)', rurl, flags=re.I) or any(k in ct for k in ("mpegurl","application/vnd.apple.mpegurl","video/","application/json")):
                                collected.add(rurl)
                                attempts["responses"].append(rurl)
                        except Exception:
                            pass
                    page.on("response", on_response)

                    # visit
                    page.goto(url, wait_until="domcontentloaded", timeout=timeout*1000)

                    # small sleep to let inline scripts run
                    time.sleep(0.8)

                    # Try: find video tag sources in DOM
                    try:
                        vids = page.eval_on_selector_all("video, source", "els => els.map(e => e.src || e.getAttribute('src')).filter(Boolean)")
                        for s in vids:
                            if s:
                                collected.add(requests.compat.urljoin(url, s))
                    except Exception:
                        pass

                    # Try to click on play button(s) / center of video
                    click_selectors = [
                        "button[class*=play]", ".jw-icon-play", ".vjs-play-control", "button[aria-label*='Play']",
                        "div.play", "button[title*='play']", "a[title*='play']"
                    ]
                    clicked_any = False
                    for sel in click_selectors:
                        try:
                            els = page.locator(sel)
                            if els.count() > 0:
                                els.nth(0).scroll_into_view_if_needed()
                                els.nth(0).click(timeout=2000)
                                attempts["page_clicks"].append(sel)
                                clicked_any = True
                                # wait some XHRs
                                try:
                                    r = page.wait_for_response(lambda r: re.search(r'(m3u8|mp4|manifest|playlist)', r.url, flags=re.I), timeout=3000)
                                    if r and r.url:
                                        collected.add(r.url)
                                except Exception:
                                    pass
                        except Exception:
                            pass

                    # if no obvious button, click center of potential video container
                    if not clicked_any:
                        try:
                            box = page.query_selector("video, div[class*=player], #player, .player")
                            if box:
                                box.click()
                                attempts["page_clicks"].append("center_click_video")
                                # wait requests
                                try:
                                    r = page.wait_for_response(lambda r: re.search(r'(m3u8|mp4|manifest|playlist)', r.url, flags=re.I), timeout=3000)
                                    if r and r.url:
                                        collected.add(r.url)
                                except Exception:
                                    pass
                        except Exception:
                            pass

                    # wait a little to capture background requests
                    wait_cycles = max_wait_for_network
                    for _ in range(wait_cycles):
                        time.sleep(1)
                        try:
                            # inspect window.__captured_requests populated by hook
                            arr = page.evaluate("() => (window.__captured_requests || []).slice(-20)")
                            if arr:
                                for o in arr:
                                    u = o.get("url") if isinstance(o, dict) else None
                                    txt = o.get("text") if isinstance(o, dict) else ""
                                    if u and re.search(r'\.(m3u8|mp4|manifest|playlist)', u, flags=re.I):
                                        collected.add(u)
                                    if isinstance(txt, str):
                                        for m in re.findall(r'https?:\\/\\/[^"\\s\\}]+\\.(?:m3u8|mp4)[^"\\s\\}]*', txt):
                                            collected.add(m.replace("\\/","/"))
                                        for m in re.findall(r'(https?://[^"\\s\']+\\.(?:mp4|m3u8)[^"\\s\']*)', txt):
                                            collected.add(m)
                                # also try to parse embedded JSON on page for direct links
                                inline = page.evaluate("""() => {
                                    const hits = [];
                                    document.querySelectorAll('script:not([src])').forEach(s=>{
                                      const t = (s.innerText||'');
                                      if(t && (t.includes('m3u8')||t.includes('.mp4'))) hits.push(t.substring(0,4000));
                                    });
                                    return hits.slice(-10);
                                }""")
                                if inline:
                                    for txt in inline:
                                        for m in re.findall(r'https?:\\/\\/[^"\\s\\}]+\\.(?:m3u8|mp4)[^"\\s\\}]*', txt):
                                            collected.add(m.replace("\\/","/"))
                                        for m in re.findall(r'(https?://[^"\\s\']+\\.(?:mp4|m3u8)[^"\\s\']*)', txt):
                                            collected.add(m)
                        except Exception:
                            pass

                    # try specific player apis (jwplayer/videojs/hls)
                    try:
                        jw = page.evaluate("""() => {
                            try { if (window.jwplayer) return (window.jwplayer().getPlaylist||(()=>[]))().map(p=>p.file).filter(Boolean); } catch(e){} return null;
                        }""")
                        if jw:
                            for s in jw: collected.add(requests.compat.urljoin(url, s))
                    except Exception:
                        pass

                    try:
                        vjs = page.evaluate("""() => {
                            try {
                                const found = [];
                                document.querySelectorAll('video').forEach(v => { if(v.currentSrc) found.push(v.currentSrc); });
                                return found;
                            } catch(e) { return null; }
                        }""")
                        if vjs:
                            for s in vjs: collected.add(requests.compat.urljoin(url, s))
                    except Exception:
                        pass

                    # close context for this attempt
                    try:
                        context.close()
                    except Exception:
                        pass

                    # if we already found links, break early
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

    # normaliza urls
    out = []
    for u in collected:
        if not u:
            continue
        try:
            out.append(requests.compat.urljoin(url, u))
        except Exception:
            out.append(u)
    # dedupe and return
    return list(dict.fromkeys(out))


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
 