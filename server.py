import os
import re
import json
import time
import html
import traceback
from typing import Optional, List, Dict

import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, Request, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse, PlainTextResponse

# --------- Config ---------
HF_TOKEN = os.getenv("HF_TOKEN", "")
SERP_API_KEY = os.getenv("SERP_API_KEY", "")
# Escolha um modelo público e estável do HF Inference:
HF_MODEL = os.getenv("HF_MODEL", "microsoft/Phi-3-mini-4k-instruct")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}
HTTP_TIMEOUT = 25

# --------- App ---------
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

# ======== Util ========
def clean_text(t: str) -> str:
    if not t:
        return ""
    t = html.unescape(t)
    t = re.sub(r"\s+", " ", t).strip()
    return t

def hf_generate(prompt: str, max_new_tokens: int = 512, temperature: float = 0.7) -> str:
    """
    Chama o endpoint de Inference da Hugging Face (não-stream).
    Em seguida, devolve o texto gerado (ou erro amigável).
    """
    if not HF_TOKEN:
        return "Configure o HF_TOKEN no Railway."
    url = f"https://api-inference.huggingface.co/models/{HF_MODEL}"
    headers = {"Authorization": f"Bearer {HF_TOKEN}"}
    payload = {
        "inputs": prompt,
        "parameters": {
            "max_new_tokens": max_new_tokens,
            "temperature": temperature,
            "return_full_text": False
        }
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=HTTP_TIMEOUT)
        if r.status_code != 200:
            return f"[Erro HF {r.status_code}] {r.text[:200]}"
        data = r.json()
        # Resposta pode vir como lista [{"generated_text": "..."}] ou outro formato
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return clean_text(data[0].get("generated_text", ""))
        # fallback
        return clean_text(str(data))
    except Exception as e:
        return f"[Erro HF] {e}"

def serp_search(query: str, n: int = 5) -> str:
    """Busca rápida no Google via SerpAPI e concatena snippets."""
    if not SERP_API_KEY:
        return "Sem SERP_API_KEY configurada."
    try:
        url = "https://serpapi.com/search.json"
        params = {"q": query, "hl": "pt", "api_key": SERP_API_KEY}
        res = requests.get(url, params=params, timeout=HTTP_TIMEOUT).json()
        out = []
        for r in (res.get("organic_results") or [])[:n]:
            snippet = r.get("snippet") or r.get("title") or ""
            link = r.get("link") or ""
            piece = clean_text(f"{snippet} — {link}") if link else clean_text(snippet)
            if piece:
                out.append(piece)
        return " ".join(out) if out else "Nenhum resultado encontrado."
    except Exception as e:
        return f"[Erro SERP] {e}"

def fetch_url(url: str) -> str:
    """Baixa HTML simples (sem JS) e retorna texto legível."""
    r = requests.get(url, headers=HEADERS, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")
    # remove elementos ruidosos
    for bad in soup(["script", "style", "noscript"]):
        bad.extract()
    text = clean_text(soup.get_text(separator=" "))
    return text[:20000]  # limite pra não estourar prompt

def summarize_page(url: str, question: Optional[str] = None) -> Dict:
    """Baixa a página e pede um resumo ao modelo; opcionalmente responde a uma pergunta sobre ela."""
    try:
        text = fetch_url(url)
    except Exception as e:
        return {"error": f"Falha ao baixar: {e}"}
    sys_prompt = (
        "Você é um assistente que resume páginas web em português de forma fiel, "
        "listando pontos-chave e respondendo perguntas objetivamente."
    )
    if question:
        user_prompt = (
            f"{sys_prompt}\n\nPágina:\n{text}\n\nPergunta do usuário: {question}\n"
            "Responda com uma síntese clara e fontes (se houver no texto)."
        )
    else:
        user_prompt = f"{sys_prompt}\n\nPágina:\n{text}\n\nResuma em 5-10 tópicos úteis."
    answer = hf_generate(user_prompt, max_new_tokens=400)
    return {"summary": answer}

# ======== Extração de mídia (uso legal!) ========
MEDIA_PAT = re.compile(r'(https?://[^\s"\'<>]+?\.(?:mp4|m3u8|webm))(?!\w)', re.I)
BTN_PAT = re.compile(r'\b(baixar|download)\b', re.I)

def extract_media_candidates(html_text: str, base_url: str) -> List[str]:
    """
    Extrai candidatos de mídia simples: <video src>, <source>, links com 'mp4/m3u8/webm',
    e âncoras/botões cujo texto contenha 'baixar'/'download'.
    NÃO contorna DRM, paywalls ou proteções — uso apenas em conteúdo autorizado.
    """
    urls = set()
    soup = BeautifulSoup(html_text, "lxml")

    # <video> e <source>
    for tag in soup.find_all(["video", "source"]):
        src = tag.get("src") or tag.get("data-src")
        if src and MEDIA_PAT.search(src):
            urls.add(src)

    # <a href="*.mp4|*.m3u8|*.webm">
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if MEDIA_PAT.search(href):
            urls.add(href)
        # botões/links "baixar" -> salva o href mesmo que não termine em mp4/m3u8 (pode redirecionar)
        if BTN_PAT.search(a.get_text(" ").strip()):
            urls.add(href)

    # padrões diretos no HTML
    for m in MEDIA_PAT.finditer(html_text):
        urls.add(m.group(1))

    # normaliza relativos simples
    normalized = set()
    from urllib.parse import urljoin
    for u in urls:
        normalized.add(urljoin(base_url, u))
    return list(normalized)

def extract_from_url(page_url: str) -> Dict:
    try:
        resp = requests.get(page_url, headers=HEADERS, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        html_text = resp.text
        candidates = extract_media_candidates(html_text, page_url)

        # segue redirecionamentos de alguns links "baixar"
        followed = []
        for u in candidates[:15]:  # limite
            try:
                r = requests.get(u, headers=HEADERS, timeout=HTTP_TIMEOUT, allow_redirects=True)
                final = r.url
                # apenas se parecer mídia direta
                if MEDIA_PAT.search(final):
                    followed.append(final)
            except Exception:
                pass

        # resultado combinado (únicos)
        out = list(dict.fromkeys(followed + candidates))
        return {"links": out}
    except Exception as e:
        return {"error": str(e)}

# ======== Execução de JavaScript (sandbox) ========
# Usa V8 via py_mini_racer (sem rede, sem fs).
try:
    from py_mini_racer import py_mini_racer
except Exception:
    py_mini_racer = None

def run_js_sandbox(code: str, timeout_ms: int = 250) -> Dict:
    if py_mini_racer is None:
        return {"error": "JS engine indisponível (instale py-mini-racer)."}
    try:
        ctx = py_mini_racer.MiniRacer()
        # Opcional: bloquear funções perigosas se alguma lib injetar (não é o caso aqui)
        result = ctx.eval(code, timeout=timeout_ms)
        # Serializa objetos
        try:
            _ = json.dumps(result)
        except Exception:
            result = str(result)
        return {"result": result}
    except Exception as e:
        return {"error": f"JS error: {e}"}

# ======== IA + Fluxos ========
@app.get("/server-info")
def server_info():
    return {"status": "ok", "msg": "Servidor pronto", "hf_model": HF_MODEL}

@app.post("/ask")
async def ask(req: Request):
    data = await req.json()
    question = data.get("question", "").strip()
    if not question:
        return {"answer": "Pergunta vazia.", "context": ""}
    context = serp_search(question)
    prompt = (
        "Você é um assistente útil. Responda em português, citando fatos do contexto quando fizer sentido.\n\n"
        f"Pergunta: {question}\n"
        f"Contexto buscado: {context}\n\n"
        "Resposta:"
    )
    answer = hf_generate(prompt, max_new_tokens=400)
    return {"answer": answer, "context": context}

@app.post("/browse")
async def browse(req: Request):
    data = await req.json()
    url = data.get("url")
    question = data.get("question")
    if not url:
        return {"error": "Envie o campo 'url'."}
    return summarize_page(url, question=question)

@app.post("/exec_js")
async def exec_js(req: Request):
    data = await req.json()
    code = data.get("code", "")
    if not code:
        return {"error": "Envie o campo 'code'."}
    return run_js_sandbox(code)

@app.post("/extract")
async def extract_sync(req: Request):
    """
    Endpoint rápido: tenta extrair sincronamente (retorna JSON com candidate links).
    Use somente em conteúdo que você tem direito de baixar. Não contorna DRM/paywall.
    """
    data = await req.json()
    page_url = data.get("url")
    if not page_url:
        return {"error": "Envie o campo 'url'."}
    return extract_from_url(page_url)

# ----- Fluxo de streaming para o chat -----

@app.post("/generate_stream")
async def generate_stream(req: Request):
    """
    Compat: apenas confirma antes de abrir o SSE.
    (Mantém o front simples — chamamos o modelo dentro do SSE.)
    """
    return {"ok": True}

@app.get("/generate_stream_sse")
async def generate_stream_sse(prompt: str = Query(..., description="Mensagem do usuário")):
    """
    Gera a resposta REAL (modelo HF) e faz streaming do texto em pedaços.
    Aqui optamos por gerar primeiro (não-stream no HF) e fatiar em chunks
    para o SSE. Em produção, você pode trocar por um modelo/endpoint HF que
    suporte streaming nativo.
    """
    def streamer():
        try:
            # 1) Busca contexto na web (curto)
            ctx = serp_search(prompt, n=4)
            # 2) Monta prompt final
            full_prompt = (
                "Você é um assistente em português. Use o contexto com cautela (verifique consistência). "
                "Se não tiver certeza, diga que não tem certeza.\n\n"
                f"Usuário: {prompt}\n"
                f"Contexto buscado: {ctx}\n\n"
                "Resposta:"
            )
            # 3) Gera com HF
            answer = hf_generate(full_prompt, max_new_tokens=500)
            if not answer:
                answer = "Não consegui gerar resposta agora."

            # 4) Stream em pedaços
            chunk_size = 220  # caracteres por chunk
            i = 0
            while i < len(answer):
                piece = answer[i:i+chunk_size]
                yield f"data: {piece}\n\n"
                i += chunk_size
                time.sleep(0.02)  # micro-throttle pra UX
            yield "data: [DONE]\n\n"
        except Exception as e:
            yield f"data: [ERRO] {e}\n\n"
            yield "data: [DONE]\n\n"

    return StreamingResponse(streamer(), media_type="text/event-stream")

# ----- Fluxo de streaming para extração (progresso) -----
@app.post("/extract_stream")
async def extract_stream_start():
    return {"ok": True}

@app.get("/extract_stream_sse")
async def extract_stream_sse(
    url: Optional[str] = Query(None), query: Optional[str] = Query(None)
):
    """
    SSE async generator que envia progresso e resultado JSON ao final.
    Uso: /extract_stream_sse?url=...  ou ?query=...
    """
    def generator():
        try:
            if not url and not query:
                yield "data: {\"error\":\"Envie url ou query\"}\n\n"
                yield "data: [DONE]\n\n"
                return

            if url:
                yield "data: Iniciando coleta da página...\n\n"
                r = requests.get(url, headers=HEADERS, timeout=HTTP_TIMEOUT)
                r.raise_for_status()
                yield "data: Página baixada. Extraindo candidatos...\n\n"
                found = extract_media_candidates(r.text, url)
                yield f"data: Encontrados {len(found)} candidatos. Tentando seguir redirecionamentos...\n\n"

                # segue alguns redirs
                followed = []
                for u in found[:15]:
                    try:
                        rr = requests.get(u, headers=HEADERS, timeout=HTTP_TIMEOUT, allow_redirects=True)
                        final = rr.url
                        if MEDIA_PAT.search(final):
                            followed.append(final)
                    except Exception:
                        pass

                result = list(dict.fromkeys(followed + found))
                payload = json.dumps({"links": result})[:30000]
                yield f"data: {payload}\n\n"
                yield "data: [DONE]\n\n"
                return

            # (Opcional) query -> busca, pega primeiro link e tenta
            if query:
                yield "data: Buscando páginas relevantes...\n\n"
                serp = serp_search(query, n=3)
                yield f"data: {json.dumps({'serp_snippets': serp[:500]})}\n\n"
                yield "data: [DONE]\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
            yield "data: [DONE]\n\n"

    return StreamingResponse(generator(), media_type="text/event-stream")

# ----- Raiz -----
@app.get("/")
def root():
    return PlainTextResponse("OK — FastAPI online.")

# ----- Executável local -----
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port)
