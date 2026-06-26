"""
Análise de edital com IA (Gemini texto, free tier).

Baixa o PDF do edital publicado no PNCP, extrai o texto e pede ao Gemini um
resumo estruturado: objeto, documentos exigidos para habilitação, prazos,
se exige amostra/visita, e pontos de atenção.

É OPCIONAL e tolerante a falhas:
- sem GEMINI_API_KEY -> status "sem_ia"
- PDF não disponível no PNCP -> status "sem_arquivo"
- PDF escaneado/sem texto extraível -> status "sem_texto"
- erro/timeout da IA -> status "erro_ia"

Nada disso quebra o resto do sistema. A análise é informativa; decisões de
habilitação continuam sendo do usuário (a IA pode errar/omitir).
"""
from __future__ import annotations
import io
import json
import logging

import requests

from .config import settings

log = logging.getLogger("ia.edital")

_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

_PROMPT = """Você é um especialista em licitações públicas brasileiras (Lei 14.133/2021).
Analise o EDITAL abaixo e responda APENAS com um JSON válido (sem texto fora do JSON, sem ```), com exatamente estas chaves:
- "objeto": string. Resumo claro do que está sendo contratado, em 1 a 2 frases.
- "exigencias": array de strings. Documentos/certidões de habilitação exigidos do licitante (ex.: regularidade fiscal federal, FGTS, trabalhista/CNDT, balanço patrimonial, atestado de capacidade técnica, etc.). Vazio se não encontrar.
- "prazos": array de strings. Datas/prazos relevantes como aparecem (abertura, envio de propostas, sessão, entrega).
- "exige_amostra": boolean. true se exigir amostra ou prova de conceito.
- "exige_visita": boolean. true se exigir visita técnica/vistoria.
- "pontos_atencao": array de strings (máx. 6). Cláusulas que merecem atenção: garantia exigida, prazo de entrega curto, exigências específicas, penalidades relevantes.

Regras: não invente nada que não esteja no texto. Se algo não constar, use lista vazia ou false. Responda em português.

OBJETO (resumo do PNCP): {objeto}

TEXTO DO EDITAL (pode estar truncado):
\"\"\"{texto}\"\"\""""


def ia_texto_disponivel(api_key: str | None = None) -> bool:
    return bool(api_key)   # só a chave do próprio usuário (sem fallback global)


def _baixar_texto_pdf(url: str, timeout: int = 45,
                      max_paginas: int = 40, max_chars: int = 24000) -> str:
    try:
        r = requests.get(url, timeout=timeout,
                        headers={"User-Agent": "RadarLicitacoes/1.0"})
    except requests.RequestException:
        return ""
    if r.status_code != 200 or not r.content:
        return ""
    try:
        import pypdf
        leitor = pypdf.PdfReader(io.BytesIO(r.content))
    except Exception:
        return ""
    partes, total = [], 0
    for i, pag in enumerate(leitor.pages):
        if i >= max_paginas:
            break
        try:
            t = pag.extract_text() or ""
        except Exception:
            t = ""
        partes.append(t)
        total += len(t)
        if total > max_chars:
            break
    return "\n".join(partes)[:max_chars]


def _gerar(prompt: str, api_key: str | None = None, timeout: int = 70):
    chave = api_key   # só a chave do próprio usuário (sem fallback global)
    if not chave:
        return None, "sem_chave"
    url = f"{_BASE}/{settings.IA_MODELO_TEXTO}:generateContent"
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"},
    }
    try:
        r = requests.post(url, json=body, timeout=timeout,
                         headers={"x-goog-api-key": chave,
                                  "Content-Type": "application/json"})
    except requests.RequestException as e:
        return None, f"rede:{e}"
    if r.status_code != 200:
        log.warning("Gemini texto HTTP %s: %s", r.status_code, r.text[:200])
        return None, f"http_{r.status_code}"
    try:
        dados = r.json()
        return dados["candidates"][0]["content"]["parts"][0]["text"], "ok"
    except (ValueError, KeyError, IndexError):
        return None, "sem_resposta"


def _parse_json(txt: str):
    try:
        return json.loads(txt)
    except Exception:
        t = txt.strip().strip("`")
        ini, fim = t.find("{"), t.rfind("}")
        if ini >= 0 and fim > ini:
            try:
                return json.loads(t[ini:fim + 1])
            except Exception:
                return None
    return None


def analisar(objeto: str, arquivos: list[dict], api_key: str | None = None) -> dict:
    """arquivos: lista de {titulo, tipo, url} (do endpoint de documentos).
    api_key: chave Gemini do próprio usuário (obrigatória, cai para a global)."""
    if not ia_texto_disponivel(api_key):
        return {"status": "sem_ia"}
    if not arquivos:
        return {"status": "sem_arquivo"}

    # prioriza arquivos cujo título sugere ser o edital
    candidatos = sorted(arquivos, key=lambda a: 0 if "edital" in (a.get("titulo") or "").lower() else 1)
    texto, fonte = "", None
    for a in candidatos[:3]:
        if not a.get("url"):
            continue
        texto = _baixar_texto_pdf(a["url"])
        if len(texto) > 300:
            fonte = a.get("titulo")
            break
    if len(texto) < 300:
        return {"status": "sem_texto"}  # PDF escaneado/imagem ou não extraível

    txt, st = _gerar(_PROMPT.format(objeto=(objeto or "")[:1000], texto=texto), api_key=api_key)
    if st != "ok" or not txt:
        return {"status": "erro_ia", "detalhe": st}
    data = _parse_json(txt)
    if not isinstance(data, dict):
        return {"status": "resposta_invalida"}

    # normaliza saída
    def lista(x):
        return [str(i) for i in x] if isinstance(x, list) else ([str(x)] if x else [])
    return {
        "status": "ok",
        "fonte": fonte,
        "objeto": str(data.get("objeto") or ""),
        "exigencias": lista(data.get("exigencias")),
        "prazos": lista(data.get("prazos")),
        "exige_amostra": bool(data.get("exige_amostra")),
        "exige_visita": bool(data.get("exige_visita")),
        "pontos_atencao": lista(data.get("pontos_atencao")),
    }
