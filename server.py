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
    """
    Playwright agressivo: spoof UA, intercepta rede, clica em botões 'baixar/download',
    tenta extrair URLs de players JS (jwplayer, videojs, objetos globais) e respostas XHR.
    """
    if not HAS_PLAYWRIGHT:
        return []

    collected = set()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox","--disable-dev-shm-usage"])
            # contexto "persistente" leve: define UA e viewport
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
                viewport={"width":1280, "height":800},
                java_script_enabled=True,
            )

            # Spoof básico: navigator.webdriver = false, languages, plugins
            context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {get: () => false});
                Object.defineProperty(navigator, 'languages', {get: () => ['pt-BR','en-US']});
                Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3]});
            """)

            page = context.new_page()

            # coletores: intercepta respostas e extrai urls óbvias
            def on_response(resp):
                try:
                    rurl = resp.url
                    ct = (resp.headers.get("content-type") or "").lower()
                    # se for mp4 / m3u8 / playlist-like
                    if re.search(r'\.(mp4|m3u8|m3u8\?|manifest|playlist)', rurl, flags=re.I) or \
                       any(k in ct for k in ("mpegurl","application/vnd.apple.mpegurl","mpegurl","video/","application/json")):
                        collected.add(rurl)
                    # pequenas heurísticas: se resposta JSON contiver "url" ou "file"
                    if "application/json" in ct:
                        try:
                            body = resp.text()
                            for match in re.findall(r'"?(?:file|url|source|src)"?\s*:\s*"([^"]+)"', body):
                                if match:
                                    collected.add(requests.compat.urljoin(url, match))
                        except Exception:
                            pass
                except Exception:
                    pass

            page.on("response", on_response)

            # roteamento para log de requests (opcional, não modifica)
            def on_request(req):
                try:
                    rurl = req.url
                    if re.search(r'\.(mp4|m3u8|manifest|playlist)', rurl, flags=re.I):
                        collected.add(rurl)
                except Exception:
                    pass
            page.on("request", on_request)

            # navega e espera a rede estabilizar
            page.goto(url, wait_until="networkidle", timeout=timeout*1000)

            # 1) tenta encontrar <video>, <source> via DOM
            try:
                video_srcs = page.eval_on_selector_all("video, source", "els => els.map(e => e.src || e.getAttribute('src')).filter(Boolean)")
                for s in video_srcs:
                    if s:
                        collected.add(requests.compat.urljoin(url, s))
            except Exception:
                pass

            # 2) tenta detectar players JS conhecidos (jwplayer, videojs, hlsjs config)
            try:
                # jwplayer
                jw = page.evaluate("""() => {
                    try {
                        if (window.jwplayer) {
                            const jw = window.jwplayer();
                            if (jw && jw.getPlaylist) {
                                return jw.getPlaylist().map(p => p.file).filter(Boolean);
                            }
                        }
                    } catch(e){}
                    return null;
                }""")
                if jw:
                    for s in jw:
                        collected.add(requests.compat.urljoin(url, s))
            except Exception:
                pass

            try:
                # videojs (comuns)
                vj = page.evaluate("""() => {
                    try {
                        if (window.videojs) {
                            const vids = [];
                            document.querySelectorAll('video').forEach(v => {
                                if (v.currentSrc) vids.push(v.currentSrc);
                            });
                            return vids;
                        }
                    } catch(e){}
                    return null;
                }""")
                if vj:
                    for s in vj:
                        collected.add(requests.compat.urljoin(url, s))
            except Exception:
                pass

            # 3) procura por variáveis globais comuns / state json embutido
            try:
                cand = page.evaluate("""() => {
                    const hits = [];
                    try {
                        // tenta algumas chaves comuns
                        const keys = ['__PLAYER__', 'playerConfig','INITIAL_STATE','window._player'];
                        for (const k of keys) {
                            try {
                                const v = window[k];
                                if (v && typeof v === 'object') hits.push(JSON.stringify(v));
                            } catch(e){}
                        }
                        // procura scripts JSON embutidos
                        document.querySelectorAll('script[type="application/json"], script:not([src])').forEach(s => {
                            const t = s.innerText || '';
                            if (t.length > 50 && (t.includes('m3u8') || t.includes('mp4') || t.includes('file'))) hits.push(t);
                        });
                    } catch(e){}
                    return hits.slice(0,10);
                }""")
                if cand:
                    for txt in cand:
                        for match in re.findall(r'https?:\\/\\/[^"\\s\\}]+\\.(?:m3u8|mp4)[^"\\s\\}]*', txt):
                            collected.add(match.replace("\\/","/"))
                        # regex plain
                        for match in re.findall(r'(https?://[^"\\s\']+\\.(?:mp4|m3u8)[^"\\s\']*)', txt):
                            collected.add(match)
            except Exception:
                pass

            # 4) tenta clicar em botões / links com texto "baixar" ou "download" (pode acionar chamadas)
            try:
                # localiza possíveis botões/links
                loc = page.locator("text=/baixar|download|baixar vídeo|download video/i")
                count = loc.count()
                for i in range(count):
                    try:
                        el = loc.nth(i)
                        el.click(timeout=3000)
                        # aguarda eventuais XHRs que contenham m3u8/mp4
                        try:
                            resp = page.wait_for_response(lambda r: re.search(r'(m3u8|mp4|manifest|playlist)', r.url, flags=re.I), timeout=4000)
                            if resp and resp.url:
                                collected.add(resp.url)
                        except Exception:
                            pass
                    except Exception:
                        pass
            except Exception:
                pass

            # 5) espera mais um pouco por requests que o player dispare
            time.sleep(1.2)

            # 6) também varre todos os requests/response capturados no contexto (já coletados por handlers)
            # (collected já preenchido via eventos)

            # fecha
            try:
                context.close()
                browser.close()
            except Exception:
                pass

    except Exception:
        pass

    # normaliza e devolve
    out = []
    for u in collected:
        if not u:
            continue
        try:
            out.append(requests.compat.urljoin(url, u))
        except Exception:
            out.append(u)
    # dedupe
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
 