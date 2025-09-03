"""
Server completo para extração + integração HuggingFace (inference API) + SSE
Instruções rápidas (LEIA):
- Este arquivo implementa endpoints:
  - GET  /server-info
  - POST /ask                 -> chamada simples ao HF (non-stream)
  - POST /generate_stream     -> inicia geração (retorna {status:ok})
  - GET  /generate_stream_sse -> SSE que envia resposta do modelo (pequenos fragmentos)
  - POST /extract             -> tentativa síncrona de extrair links (json)
  - POST /extract_stream      -> confirma inicio (compatibilidade frontend)
  - GET  /extract_stream_sse  -> SSE que envia progresso e resultado do extractor

- No topo deste ficheiro há um bloco com o conteúdo sugerido para requirements.txt e notas do Dockerfile.

ATENÇÃO:
- Se usar Playwright no Railway/containers, é necessário executar `playwright install --with-deps` no build (veja notas).
- Configure variáveis de ambiente no Railway: HF_TOKEN, SERP_API_KEY (opcional), HF_MODEL (opcional - padrão gpt2).
- Não coloque tokens no código.

"""

# -------------------- BIBLIOTECA / REQUIREMENTS (copie para requirements.txt) --------------------
# fastapi
# uvicorn[standard]
# requests
# beautifulsoup4
# lxml
# html5lib
# playwright
# yt-dlp
# py-mini-racer
# bs4
# typing-extensions
# (opcional) aiohttp
# --------------------------------------------------------------------------------------------------

import os
import time
import json
import re
import asyncio
from typing import List, Dict, Any, Optional

import requests
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse

# tenta importar yt_dlp e playwright; se não estiverem, o extractor adapta
HAS_YTDLP = False
HAS_PLAYWRIGHT = False
try:
    import yt_dlp as yt_dlp_pkg
    HAS_YTDLP = True
except Exception:
    HAS_YTDLP = False

try:
    from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
    HAS_PLAYWRIGHT = True
except Exception:
    HAS_PLAYWRIGHT = False

# --------------------------------- Config via ENV ---------------------------------
HF_TOKEN = os.getenv("HF_TOKEN", "")
SERP_API_KEY = os.getenv("SERP_API_KEY", "")
HF_MODEL = os.getenv("HF_MODEL", "gpt2")

# tweak defaults
PLAYWRIGHT_TIMEOUT = int(os.getenv("PLAYWRIGHT_TIMEOUT", "30"))
YTDLP_TIMEOUT = int(os.getenv("YTDLP_TIMEOUT", "30"))

app = FastAPI()

# --- CORS ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ----------------- util: search (SerpApi) -----------------
def search_google(query: str) -> str:
    if not SERP_API_KEY:
        return ""
    try:
        url = f"https://serpapi.com/search.json?q={requests.utils.requote_uri(query)}&hl=pt&api_key={SERP_API_KEY}"
        res = requests.get(url, timeout=8).json()
        results = []
        if "organic_results" in res:
            for r in res["organic_results"][:5]:
                results.append(r.get("snippet", ""))
        return " ".join(results)
    except Exception:
        return ""

# ----------------- HF minimal call -----------------
def call_hf_inference(prompt: str, model: str = HF_MODEL, timeout: int = 60) -> Dict[str, Any]:
    """Chama HuggingFace Inference API (sincrono). Retorna dict com keys: ok,bool / text or error."""
    if not HF_TOKEN:
        return {"ok": False, "error": "HF_TOKEN not set"}
    url = f"https://api-inference.huggingface.co/models/{model}"
    headers = {"Authorization": f"Bearer {HF_TOKEN}"}
    payload = {"inputs": prompt}
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
        if resp.status_code == 200:
            try:
                data = resp.json()
                # modelos serverless retornam lista com generated_text ou string
                if isinstance(data, list) and len(data) > 0 and isinstance(data[0], dict):
                    txt = data[0].get("generated_text") or data[0].get("text") or str(data)
                else:
                    # às vezes é um dict simples ou string
                    if isinstance(data, dict) and "generated_text" in data:
                        txt = data.get("generated_text")
                    else:
                        txt = str(data)
                return {"ok": True, "text": txt}
            except Exception as e:
                return {"ok": True, "text": resp.text}
        else:
            return {"ok": False, "error": f"Erro HF {resp.status_code}: {resp.text}"}
    except Exception as e:
        return {"ok": False, "error": f"Erro HF {str(e)}"}

# ----------------- extractor helpers -----------------
from bs4 import BeautifulSoup


def try_basic_requests(url: str, headers: Dict[str, str] = None) -> List[str]:
    """Tenta baixar HTML e procurar links direto a mp4/m3u8 via parsing simples."""
    urls = []
    try:
        h = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        if headers:
            h.update(headers)
        r = requests.get(url, headers=h, timeout=10)
        html = r.text
        # buscar .m3u8/.mp4/.ts
        found = re.findall(r'(https?://[^\s"\']+?\.(?:m3u8|mp4|ts)(?:\?[^\s"\']*)?)', html, flags=re.I)
        for f in found:
            if f not in urls:
                urls.append(f)
        # buscar em <video> tags
        soup = BeautifulSoup(html, "lxml")
        for v in soup.find_all("video"):
            src = v.get('src')
            if src:
                if not src.startswith('http'):
                    src = requests.compat.urljoin(url, src)
                if src not in urls:
                    urls.append(src)
            for s in v.find_all('source'):
                ss = s.get('src')
                if ss and not ss.startswith('http'):
                    ss = requests.compat.urljoin(url, ss)
                if ss and ss not in urls:
                    urls.append(ss)
    except Exception as e:
        print("basic_requests error", e)
    return urls


def try_yt_dlp(url: str, headers: Dict[str, str] = None, timeout: int = 30) -> List[str]:
    """Tenta extrair com yt-dlp (se instalado). Retorna lista de urls encontradas."""
    if not HAS_YTDLP:
        return []
    try:
        opts = {
            "quiet": True,
            "skip_download": True,
            "no_warnings": True,
            "ignoreerrors": True,
            "noplaylist": True,
        }
        if headers:
            opts["http_headers"] = headers
        ydl = yt_dlp_pkg.YoutubeDL(opts)
        info = ydl.extract_info(url, download=False)
        urls = []
        if isinstance(info, dict):
            for f in info.get('formats', []) or []:
                fu = f.get('url')
                if fu and fu not in urls:
                    urls.append(fu)
            if info.get('url') and info.get('url') not in urls:
                urls.append(info.get('url'))
        return urls
    except Exception as e:
        print("yt-dlp error", e)
        return []


async def try_playwright_capture(url: str, timeout: int = PLAYWRIGHT_TIMEOUT) -> List[str]:
    """Usa Playwright (headless) para abrir página, inspecionar requests/responses e DOM.
    Retorna lista de URLs encontradas.
    """
    if not HAS_PLAYWRIGHT:
        return []
    collected: List[str] = []
    seen = set()

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-setuid-sandbox"])
            context = await browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
            page = await context.new_page()

            def record_request(req):
                try:
                    u = req.url
                    if not u:
                        return
                    if re.search(r'\.(m3u8|mp4|ts)(\?.*)?$', u, flags=re.I):
                        if u not in seen:
                            seen.add(u); collected.append(u)
                    if req.resource_type in ("media", "xhr", "fetch"):
                        if u not in seen:
                            seen.add(u); collected.append(u)
                except Exception:
                    pass

            page.on("request", record_request)

            async def on_response(resp):
                try:
                    u = resp.url
                    headers = resp.headers or {}
                    ctype = headers.get('content-type','')
                    if any(x in ctype for x in ("application/vnd.apple.mpegurl", "application/x-mpegURL", "video/", "audio/")):
                        if u not in seen:
                            seen.add(u); collected.append(u)
                    if re.search(r'\.(m3u8|mp4|ts)(\?.*)?$', u, flags=re.I):
                        if u not in seen:
                            seen.add(u); collected.append(u)
                except Exception:
                    pass

            page.on("response", lambda r: asyncio.create_task(on_response(r)))

            try:
                await page.goto(url, wait_until='networkidle', timeout=timeout * 1000)
            except PlaywrightTimeoutError:
                # continua — às vezes networkidle falha em sites muito JS-heavy
                pass

            # espera para que XHRs carreguem
            await asyncio.sleep(2)

            try:
                videos_info = await page.evaluate("""
                () => {
                    const out = [];
                    const vids = Array.from(document.querySelectorAll('video'));
                    for (const v of vids) {
                        try {
                            out.push({
                                src: v.currentSrc || v.src || null,
                                sources: Array.from(v.querySelectorAll('source')).map(s => s.src || s.getAttribute('src'))
                            });
                        } catch(e) {}
                    }
                    const metas = {};
                    document.querySelectorAll('meta').forEach(m => {
                        if(m.getAttribute('property') && m.getAttribute('content')) metas[m.getAttribute('property')] = m.getAttribute('content');
                    });
                    return {videos: out, metas};
                }
                """)
                if videos_info and isinstance(videos_info, dict):
                    for v in videos_info.get('videos', []):
                        for s in ([v.get('src')] + (v.get('sources') or [])):
                            if s and isinstance(s, str):
                                if re.search(r'\.(m3u8|mp4|ts)(\?.*)?$', s, flags=re.I) or s.startswith('blob:'):
                                    if s not in seen:
                                        seen.add(s); collected.append(s)
                metas = videos_info.get('metas', {}) if videos_info else {}
                for k in ("og:video","og:video:url","og:video:secure_url"):
                    mv = metas.get(k)
                    if mv and mv not in seen:
                        seen.add(mv); collected.append(mv)
            except Exception:
                pass

            await asyncio.sleep(3)

            try:
                html = await page.content()
                for m in re.findall(r'(https?://[^\s"\'<>]+?\.(?:m3u8|mp4|ts)(?:\?[^\s"\'<>]*)?)', html, flags=re.I):
                    if m not in seen:
                        seen.add(m); collected.append(m)
            except Exception:
                pass

            await context.close()
            await browser.close()
    except Exception as e:
        print("playwright capture error", e)

    # dedupe/ordenar
    final = []
    for u in collected:
        if u and u not in final:
            final.append(u)
    final.sort(key=lambda x: (0 if ".m3u8" in x.lower() else 1, x))
    return final

# ----------------- Endpoints -----------------
@app.get('/server-info')
def server_info():
    return {
        "status": "ok",
        "msg": "Servidor pronto",
        "features": {"yt_dlp": HAS_YTDLP, "playwright": HAS_PLAYWRIGHT},
        "hf_model": HF_MODEL
    }


@app.post('/ask')
async def ask(req: Request):
    data = await req.json()
    question = data.get('question', '')
    context = search_google(question) if question else ''
    prompt = f"P: {question}\nC: {context}\nR:"
    hf = call_hf_inference(prompt)
    if hf.get('ok'):
        return {"answer": hf.get('text',''), "context": context}
    else:
        return {"answer": None, "error": hf.get('error'), "context": context}


@app.post('/extract')
async def extract(req: Request):
    data = await req.json()
    url = data.get('url')
    headers = data.get('headers') if isinstance(data.get('headers'), dict) else None
    if not url:
        return JSONResponse({"error": "missing url"}, status_code=400)

    attempts = {"basic": [], "yt_dlp": [], "playwright": [], "fallback_regex": []}

    # 1) basic requests/BS4
    basic = try_basic_requests(url, headers=headers)
    attempts['basic'] = basic
    if basic:
        return {"method": "basic", "links": basic, "attempts": attempts}

    # 2) yt-dlp
    ytd = try_yt_dlp(url, headers=headers, timeout=YTDLP_TIMEOUT)
    attempts['yt_dlp'] = ytd
    if ytd:
        return {"method": "yt_dlp", "links": ytd, "attempts": attempts}

    # 3) playwright
    if HAS_PLAYWRIGHT:
        py_links = await try_playwright_capture(url)
        attempts['playwright'] = py_links
        if py_links:
            return {"method": "playwright", "links": py_links, "attempts": attempts}

    # 4) fallback regex on HTML
    try:
        r = requests.get(url, timeout=10)
        found = re.findall(r'(https?://[^\s"\']+?\.(?:m3u8|mp4|ts)(?:\?[^\s"\']*)?)', r.text, flags=re.I)
        attempts['fallback_regex'] = found
        if found:
            return {"method": "fallback_regex", "links": found, "attempts": attempts}
    except Exception:
        pass

    return {"method": "none", "links": [], "attempts": attempts}


@app.post('/extract_stream')
async def extract_stream_start(req: Request):
    # endpoint compat (frontend espera OK antes de abrir SSE)
    return {"status": "ok"}


@app.get('/extract_stream_sse')
async def extract_stream_sse(url: str = None, query: str = None):
    async def generator():
        if not url and not query:
            yield f"data: {json.dumps({'error':'missing url/query'})}\n\n"
            yield "data: [DONE]\n\n"
            return

        target = url or query
        yield "data: TENTANDO: requisição simples (requests/BS4)\n\n"
        basic = try_basic_requests(target)
        yield f"data: {json.dumps({'attempt':'basic','found': basic})}\n\n"
        if basic:
            yield f"data: {json.dumps({'status':'done','result':{'method':'basic','links':basic}})}\n\n"
            yield "data: [DONE]\n\n"
            return

        yield "data: TENTANDO: yt-dlp\n\n"
        ytd = try_yt_dlp(target)
        yield f"data: {json.dumps({'attempt':'yt_dlp','found': ytd})}\n\n"
        if ytd:
            yield f"data: {json.dumps({'status':'done','result':{'method':'yt_dlp','links':ytd}})}\n\n"
            yield "data: [DONE]\n\n"
            return

        yield "data: TENTANDO: Playwright (execução JS e interceptação de rede)\n\n"
        if HAS_PLAYWRIGHT:
            py_links = await try_playwright_capture(target)
            yield f"data: {json.dumps({'attempt':'playwright','found': py_links})}\n\n"
            if py_links:
                yield f"data: {json.dumps({'status':'done','result':{'method':'playwright','links':py_links}})}\n\n"
                yield "data: [DONE]\n\n"
                return
        else:
            yield "data: Playwright não disponível no ambiente.\n\n"

        yield "data: TENTANDO: fallback (regex/HTML)\n\n"
        try:
            r = requests.get(target, timeout=10)
            found = re.findall(r'(https?://[^\s"\']+?\.(?:m3u8|mp4|ts)(?:\?[^\s"\']*)?)', r.text, flags=re.I)
        except Exception:
            found = []
        yield f"data: {json.dumps({'attempt':'fallback_regex','found': found})}\n\n"

        yield f"data: {json.dumps({'status':'done','result':{'method': 'none' if not found else 'fallback_regex','links': found}})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(generator(), media_type='text/event-stream')


@app.post('/generate_stream')
async def generate_stream(req: Request):
    # compat para frontend: confirma que prompt foi recebido
    data = await req.json()
    prompt = data.get('prompt') if isinstance(data, dict) else None
    if not prompt:
        return JSONResponse({"error":"missing prompt"}, status_code=400)
    # opcional: você pode enfileirar ou logar o prompt aqui
    return {"status":"ok"}


@app.get('/generate_stream_sse')
async def generate_stream_sse(prompt: str = ''):
    def generator():
        # 1) criar prompt com contexto
        context = search_google(prompt)
        full_prompt = f"P: {prompt}\nC: {context}\nR:"
        # 2) Chamada HF
        hf = call_hf_inference(full_prompt, model=HF_MODEL, timeout=60)
        if not hf.get('ok'):
            yield f"data: [Erro HF] {hf.get('error')}\n\n"
            yield "data: [DONE]\n\n"
            return
        text = hf.get('text','')
        # para tornar streaming mais amigável, partimos o texto em pedaços
        for i in range(0, len(text), 250):
            part = text[i:i+250]
            yield f"data: {part}\n\n"
            time.sleep(0.12)
        yield "data: [DONE]\n\n"
    return StreamingResponse(generator(), media_type='text/event-stream')


# -------------- run ----------------
if __name__ == '__main__':
    import uvicorn
    port = int(os.getenv('PORT', 8000))
    print(f"🚀 Rodando FastAPI na porta {port}")
    uvicorn.run(app, host='0.0.0.0', port=port)
