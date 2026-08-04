"""
Completa a descrição de itens do edital lendo o(s) PDF(s) publicados no
PNCP — via um modelo de CHAT hospedado na DeepInfra (chave GLOBAL do
operador, DEEPINFRA_API_KEY — não depende da chave Gemini pessoal do
usuário). Complementar à Análise por IA (Gemini): a API estruturada do
PNCP às vezes trunca a descrição do item (achado real: item com a
descrição cortada no meio de uma palavra, provável limite de caracteres no
sistema do próprio órgão ao preencher) — o texto do PDF costuma vir
completo.

É OPCIONAL e tolerante a falhas, mesmo padrão de analise_edital.py:
- sem DEEPINFRA_API_KEY configurada -> status "sem_ia"
- PDF não disponível -> status "sem_arquivo"
- PDF escaneado/sem texto extraível -> status "sem_texto"
- erro/timeout da IA -> status "erro_ia"

Nada disso quebra o resto do sistema — é uma tentativa best-effort de
enriquecer ItemEdital.descricao, nunca uma dependência obrigatória.
"""
from __future__ import annotations
import json
import logging

import requests

from .config import settings
from .analise_edital import _baixar_texto_pdf

log = logging.getLogger("ia.itens_pdf")

_DEEPINFRA_CHAT_URL = "https://api.deepinfra.com/v1/openai/chat/completions"

_PROMPT = """Você é um assistente que lê editais de licitação pública brasileira. Abaixo está o texto de um edital e uma lista de itens já conhecidos (número + descrição curta, como vieram da API oficial do PNCP — às vezes truncada).

Para CADA item da lista, procure no texto do edital a descrição TÉCNICA COMPLETA desse item (geralmente numa tabela ou lista de especificações). Responda APENAS com um JSON válido (sem texto fora do JSON, sem ```), com exatamente esta estrutura:
- "itens": array — só inclua um item aqui quando encontrar o texto correspondente no documento com confiança (mesmo assunto/produto que a descrição curta indica). Cada entrada:
  - "numero_item": o número do item (copiado exatamente da lista de referência, é um número).
  - "descricao_completa": o texto da especificação completa, copiado como está no edital (não resuma, não reescreva, não traduza).

Regras: se não achar o texto de um item, ou não tiver certeza de que é o item certo, simplesmente NÃO o inclua no array — nunca invente ou misture com outro item. Responda em português.

ITENS CONHECIDOS (número: descrição curta atual):
{itens_referencia}

TEXTO DO EDITAL:
\"\"\"{texto}\"\"\""""


def ia_disponivel(api_key: str | None = None) -> bool:
    return bool(api_key)


def _gerar(prompt: str, api_key: str, timeout: int = 90) -> tuple[str | None, str]:
    body = {
        "model": settings.DEEPINFRA_MODELO_CHAT,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }
    try:
        r = requests.post(_DEEPINFRA_CHAT_URL, json=body, timeout=timeout,
                          headers={"Authorization": f"Bearer {api_key}",
                                   "Content-Type": "application/json"})
    except requests.RequestException as e:
        return None, f"rede:{e}"
    if r.status_code != 200:
        log.warning("DeepInfra chat HTTP %s: %s", r.status_code, r.text[:200])
        return None, f"http_{r.status_code}"
    try:
        dados = r.json()
        return dados["choices"][0]["message"]["content"], "ok"
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


def extrair_itens_completos(objeto: str, arquivos: list[dict], itens_atuais: list[dict],
                            api_key: str | None = None) -> dict:
    """arquivos: lista de {titulo, tipo, url} (mesmo formato usado por
    analise_edital.analisar). itens_atuais: lista de {numero, descricao}
    (o que já está salvo, usado só como referência pra IA localizar o texto
    certo no documento — não é reescrito, só complementado quando a IA
    acha algo melhor com confiança)."""
    api_key = api_key or settings.DEEPINFRA_API_KEY
    if not ia_disponivel(api_key):
        return {"status": "sem_ia"}
    if not arquivos:
        return {"status": "sem_arquivo"}
    if not itens_atuais:
        return {"status": "sem_itens"}

    # prioriza o edital principal, depois termo de referência/anexos — mesma
    # heurística de analise_edital.analisar()
    def _prioridade(a):
        t = (a.get("titulo") or "").lower()
        if "edital" in t:
            return 0
        if "termo de referência" in t or "termo referencia" in t or "anexo" in t:
            return 1
        return 2
    candidatos = sorted(arquivos, key=_prioridade)

    # janela de contexto bem maior que a usada pro resumo geral do edital
    # (Gemini, 24000 chars): a tabela de itens pode estar em qualquer página
    # de documentos longos, e o custo por token aqui tende a ser bem menor.
    MAX_TOTAL = 70000
    partes = []
    for a in candidatos[:5]:
        if sum(len(p) for p in partes) >= MAX_TOTAL:
            break
        if not a.get("url"):
            continue
        t = _baixar_texto_pdf(a["url"], max_paginas=80, max_chars=MAX_TOTAL)
        if len(t) > 300:
            partes.append(t)
    texto = "\n\n---\n\n".join(partes)[:MAX_TOTAL]
    if len(texto) < 300:
        return {"status": "sem_texto"}

    itens_ref = "\n".join(f"- item {it['numero']}: {it.get('descricao') or ''}" for it in itens_atuais)
    prompt = _PROMPT.format(itens_referencia=itens_ref[:8000], texto=texto)
    txt, st = _gerar(prompt, api_key=api_key)
    if st != "ok" or not txt:
        return {"status": "erro_ia", "detalhe": st}
    data = _parse_json(txt)
    if not isinstance(data, dict) or not isinstance(data.get("itens"), list):
        return {"status": "resposta_invalida"}

    numeros_validos = {it["numero"] for it in itens_atuais}
    itens = []
    for it in data["itens"]:
        if not isinstance(it, dict):
            continue
        try:
            numero = int(it.get("numero_item"))
        except (TypeError, ValueError):
            continue
        desc = str(it.get("descricao_completa") or "").strip()
        if numero not in numeros_validos or not desc:
            continue
        itens.append({"numero": numero, "descricao_completa": desc})
    return {"status": "ok", "itens": itens}
