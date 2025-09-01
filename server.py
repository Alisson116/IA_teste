
import os
import time
import json
import asyncio
import subprocess
import shlex
from typing import List, Optional

import requests
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse

HF_TOKEN = os.getenv("HF_TOKEN")
SERP_API_KEY = os.getenv("SERP_API_KEY")
HF_MODEL = os.getenv("HF_MODEL", "gpt2")

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------- utilitários ----------

def search_serpapi(query: str, max_results: int = 5) -> List[str]:
    """Busca rápida via SerpAPI, retorna lista de URLs candidatas."""
    if not SERP_API_KEY:
        return []
    try:
        url = f"https://serpapi.com/search.json?q={requests.utils.quote(query)}&hl=pt&api_key={SERP_API_KEY}"
        r = requests.get(url, timeout=8)
        r.raise_for_status()
        j = r.json()
        urls = []
        for item in j.get("organic_results", [])[:max_results]:
            link = item.get("link") or item.get("url") or item.get("position")
            if link:
                urls.append(link)
        return urls
    except Exception:
        return []


def try_yt_dlp_extract(page_url: str, timeout: int = 30) -> List[str]:
    """Tenta extrair links com yt-dlp (retorna lista de URLs de vídeo)."""
    try:
        cmd = ["yt-dlp", "-j", "--no-warnings", page_url]
        out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, timeout=timeout, text=True)
        info = json.loads(out)
        formats = info.get("formats") or []
        candidates = []
        # coleta urls de formatos
        for f in formats:
            url = f.get("url")
            ext = f.get("ext", "")
            if url and (url.endswith('.mp4') or '.m3u8' in url or ext in ('mp4', 'm3u8')):
                candidates.append(url)
        # dedupe
        return list(dict.fromkeys(candidates))
    except subprocess.CalledProcessError:
        return []
    except Exception:
        return []


async def playwright_extract(url: str, click_texts: Optional[List[str]] = None, wait_seconds: int = 8) -> List[str]:
    """Tenta extrair via Playwright: executa JS, clica em botões com textos e intercepta requests.
    Retorna lista de URLs encontradas (.m3u8/.mp4).
    """
    click_texts = click_texts or ["baixar", "download", "downloadar", "baixar agora", "download video", "baixar vídeo"]
    try:
        from playwright.async_api import async_playwright, TimeoutError as PWTimeout
    except Exception:
        # Playwright não instalado
        return []

    found = set()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context()
        page = await context.new_page()

        async def _on_request(request):
            rurl = request.url
            # filtragem básica
            if rurl.endswith('.mp4') or '.m3u8' in rurl or 'videoplayback' in rurl:
                found.add(rurl)
            # também checar content-type se disponível
        page.on('request', _on_request)

        try:
            await page.goto(url, timeout=25000)
            # tentar procurar e clicar em elementos que contenham textos de "baixar"/"download"
            for txt in click_texts:
                try:
                    # busca por texto (insensível a maiúsculas)
                    locator = page.get_by_text(txt, exact=False)
                    count = await locator.count()
                    if count:
                        for i in range(count):
                            try:
                                await locator.nth(i).click(timeout=3000)
                                await asyncio.sleep(0.5)
                            except Exception:
                                pass
                except PWTimeout:
                    pass
                except Exception:
                    pass

            # esperar tráfego
            await asyncio.sleep(wait_seconds)
        finally:
            await context.close()
            await browser.close()

    return list(found)


# ---------- endpoints ----------

@app.get('/server-info')
def server_info():
    return {"status": "ok", "msg": "Servidor pronto", "hf_model": HF_MODEL}


@app.post('/extract')
async def extract_sync(req: Request):
    """Endpoint rápido: tenta extrair sincronamente (retorna JSON com candidate links)."""
    payload = await req.json()
    page_url = payload.get('url')
    query = payload.get('query')
    if not page_url and not query:
        raise HTTPException(status_code=400, detail='Envie `url` ou `query`.')

    candidates = []
    logs = []

    # se query: faz busca
    urls_to_try = []
    if page_url:
        urls_to_try.append(page_url)
    elif query:
        logs.append(f"Buscando no SerpAPI por: {query}")
        urls_to_try = search_serpapi(query)
        logs.append(f"Candidatos da busca: {urls_to_try}")

    for u in urls_to_try:
        logs.append(f"Tentando yt-dlp em: {u}")
        y = try_yt_dlp_extract(u)
        if y:
            logs.append(f"yt-dlp encontrou: {y}")
            candidates.extend(y)
            break
        else:
            logs.append("yt-dlp não encontrou, tentando Playwright...")
            try:
                y2 = await playwright_extract(u)
                if y2:
                    logs.append(f"Playwright encontrou: {y2}")
                    candidates.extend(y2)
                    break
                else:
                    logs.append("Playwright não encontrou neste candidato.")
            except Exception as e:
                logs.append(f"Erro Playwright: {e}")

    return {"candidates": list(dict.fromkeys(candidates)), "logs": logs}


@app.post('/extract_stream')
async def extract_stream_start(req: Request):
    """Confirmação para frontend (mantive compat com fluxo POST+SSE)."""
    data = await req.json()
    # apenas responde OK — frontend deve então abrir o SSE
    return {"status": "ok"}


@app.get('/extract_stream_sse')
async def extract_stream_sse(url: Optional[str] = None, query: Optional[str] = None):
    """SSE async generator que envia progresso e resultado JSON ao final.
    Uso: /extract_stream_sse?url=...  ou ?query=...
    """
    async def event_generator():
        yield f"data: Iniciando extração...\n\n"
        candidates = []
        logs = []

        urls_to_try = []
        if url:
            urls_to_try.append(url)
            logs.append(f"Recebi URL direta: {url}")
            yield f"data: Recebida URL: {url}\n\n"
        elif query:
            logs.append(f"Recebi query: {query}")
            yield f"data: Buscando no SerpAPI por: {query}\n\n"
            found_urls = search_serpapi(query)
            urls_to_try = found_urls
            yield f"data: Candidatos encontrados: {found_urls}\n\n"
        else:
            yield f"data: Erro: informe url ou query\n\n"
            yield "data: [DONE]\n\n"
            return

        for idx, u in enumerate(urls_to_try):
            yield f"data: Tentando candidato {idx+1}: {u}\n\n"
            # yt-dlp
            yield f"data: Tentando extrair com yt-dlp...\n\n"
            y = try_yt_dlp_extract(u)
            if y:
                yield f"data: Encontrado via yt-dlp: {json.dumps(y)}\n\n"
                candidates.extend(y)
                break
            else:
                yield f"data: yt-dlp não encontrou, entrando no navegador (Playwright)\n\n"
                try:
                    y2 = await playwright_extract(u)
                    if y2:
                        yield f"data: Encontrado via Playwright: {json.dumps(y2)}\n\n"
                        candidates.extend(y2)
                        break
                    else:
                        yield f"data: Playwright não encontrou neste candidato.\n\n"
                except Exception as e:
                    yield f"data: Erro Playwright: {e}\n\n"

        # resultado final
        result = {"candidates": list(dict.fromkeys(candidates)), "logs": logs}
        yield f"data: RESULT: {json.dumps(result)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(event_generator(), media_type='text/event-stream')


# --- util endpoint simples de teste do modelo HuggingFace (opcional) ---
@app.post('/ask')
async def ask(req: Request):
    data = await req.json()
    question = data.get('question', '')
    context = ''
    if question:
        if SERP_API_KEY:
            context = ' '.join(search_serpapi(question)[:3])
        # chama HF apenas se token presente
        if HF_TOKEN:
            headers = {"Authorization": f"Bearer {HF_TOKEN}"}
            payload = {"inputs": f"P: {question}\nC: {context}\nR:"}
            try:
                r = requests.post(f"https://api-inference.huggingface.co/models/{HF_MODEL}", headers=headers, json=payload, timeout=15)
                if r.status_code == 200:
                    resp = r.json()
                    if isinstance(resp, list) and resp:
                        text = resp[0].get('generated_text','')
                    else:
                        text = str(resp)
                else:
                    text = f"Erro HuggingFace {r.status_code}: {r.text[:200]}"
            except Exception as e:
                text = f"Erro ao chamar HuggingFace: {e}"
        else:
            text = "HF_TOKEN não configurado."
    else:
        text = ""
    return {"answer": text, "context": context}


if __name__ == '__main__':
    import uvicorn
    port = int(os.getenv('PORT', 8000))
    print(f"🚀 Rodando FastAPI na porta {port}")
    uvicorn.run('server:app', host='0.0.0.0', port=port, log_level='info')
