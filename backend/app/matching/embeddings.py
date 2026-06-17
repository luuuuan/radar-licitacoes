"""
Similaridade semântica via Gemini embeddings (free tier).

Gera um "vetor de significado" para cada texto e compara por cosseno. Pega
sinônimos e linguagem de edital que o casamento por texto puro (TF-IDF) erra.

É OPCIONAL: só liga se houver GEMINI_API_KEY e a chave estiver ativa. Sem isso,
o sistema funciona normalmente apenas com o matching textual.

Endpoint (REST): .../models/{modelo}:batchEmbedContents  (gera vários de uma vez)
Auth: header x-goog-api-key
task_type: SEMANTIC_SIMILARITY (otimiza para medir semelhança de significado)
"""
from __future__ import annotations
import logging
import math

import requests

from ..config import settings

log = logging.getLogger("ia.embeddings")

_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
# cache simples em memória: texto -> vetor (evita re-embeddar o mesmo produto)
_cache: dict[str, list[float]] = {}


def ia_disponivel() -> bool:
    return bool(settings.GEMINI_API_KEY)


def _norm(v: list[float]) -> float:
    return math.sqrt(sum(x * x for x in v)) or 1.0


def cosseno(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    return dot / (_norm(a) * _norm(b))


def embeddings(textos: list[str], timeout: int = 30) -> list[list[float] | None]:
    """Gera embeddings para uma lista de textos. Usa cache e chamada em lote.
    Retorna lista alinhada à entrada; posições que falharem vêm como None."""
    if not ia_disponivel():
        return [None] * len(textos)

    # separa o que já está em cache do que precisa ir à API
    faltando = [t for t in textos if t and t not in _cache]
    faltando = list(dict.fromkeys(faltando))  # únicos, preservando ordem

    if faltando:
        modelo = settings.IA_MODELO_EMBEDDING
        url = f"{_BASE}/{modelo}:batchEmbedContents"
        body = {"requests": [{
            "model": f"models/{modelo}",
            "content": {"parts": [{"text": t}]},
            "taskType": "SEMANTIC_SIMILARITY",
        } for t in faltando]}
        try:
            r = requests.post(url, json=body, timeout=timeout,
                              headers={"x-goog-api-key": settings.GEMINI_API_KEY,
                                       "Content-Type": "application/json"})
            if r.status_code != 200:
                log.warning("Gemini embeddings HTTP %s: %s", r.status_code, r.text[:200])
                return [_cache.get(t) for t in textos]
            dados = r.json().get("embeddings", [])
            for t, emb in zip(faltando, dados):
                vec = emb.get("values") if isinstance(emb, dict) else None
                if vec:
                    _cache[t] = vec
        except (requests.RequestException, ValueError) as e:
            log.warning("Falha ao gerar embeddings: %s", e)

    return [_cache.get(t) for t in textos]
