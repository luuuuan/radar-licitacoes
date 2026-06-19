"""
Similaridade semântica via Gemini embeddings (free tier).

Gera um "vetor de significado" para cada texto e compara por cosseno. Pega
sinônimos e linguagem de edital que o casamento por texto puro (TF-IDF) erra.

É OPCIONAL: só liga se houver GEMINI_API_KEY e a chave estiver ativa. Sem isso,
o sistema funciona normalmente apenas com o matching textual.

Proteção de cota (free tier): a API tem limite diário. Quando estoura (HTTP 429),
um "disjuntor" pausa as chamadas por algumas horas (até a cota resetar), usando
só o cache em memória, e registra UM aviso em vez de inundar o log.
"""
from __future__ import annotations
import logging
import math
import time

import requests

from ..config import settings

log = logging.getLogger("ia.embeddings")

_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
# cache simples em memória: texto -> vetor (evita re-embeddar o mesmo texto)
_cache: dict[str, list[float]] = {}

# disjuntor: epoch (segundos) até quando as chamadas ficam pausadas
_bloqueado_ate: float = 0.0
_COOLDOWN_429 = 6 * 3600        # pausa 6h ao estourar a cota
_COOLDOWN_ERRO = 5 * 60         # pausa curta em erro genérico/instabilidade


def ia_disponivel() -> bool:
    return bool(settings.GEMINI_API_KEY)


def ia_bloqueada() -> bool:
    """True se as chamadas estão em pausa (cota estourada recentemente)."""
    return time.time() < _bloqueado_ate


def segundos_para_liberar() -> int:
    return max(0, int(_bloqueado_ate - time.time()))


def _pausar(segundos: int, motivo: str):
    global _bloqueado_ate
    novo = time.time() + segundos
    # só loga/estende quando realmente muda o estado (evita spam)
    if novo > _bloqueado_ate:
        _bloqueado_ate = novo
        horas = round(segundos / 3600, 1)
        log.warning("IA semântica pausada por ~%sh (%s). Usando só o cache até liberar.",
                    horas, motivo)


def _norm(v: list[float]) -> float:
    return math.sqrt(sum(x * x for x in v)) or 1.0


def cosseno(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    return dot / (_norm(a) * _norm(b))


def embeddings(textos: list[str], timeout: int = 30) -> list[list[float] | None]:
    """Gera embeddings para uma lista de textos. Usa cache e chamada em lote.
    Retorna lista alinhada à entrada; posições sem vetor vêm como None."""
    if not ia_disponivel():
        return [None] * len(textos)

    # se a cota estourou recentemente, nem tenta — devolve o que tiver em cache
    if ia_bloqueada():
        return [_cache.get(t) for t in textos]

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
            if r.status_code == 429:
                _pausar(_COOLDOWN_429, "cota diária do plano gratuito atingida")
                return [_cache.get(t) for t in textos]
            if r.status_code != 200:
                _pausar(_COOLDOWN_ERRO, f"HTTP {r.status_code}")
                return [_cache.get(t) for t in textos]
            dados = r.json().get("embeddings", [])
            for t, emb in zip(faltando, dados):
                vec = emb.get("values") if isinstance(emb, dict) else None
                if vec:
                    _cache[t] = vec
        except (requests.RequestException, ValueError) as e:
            _pausar(_COOLDOWN_ERRO, f"falha de rede ({type(e).__name__})")
            return [_cache.get(t) for t in textos]

    return [_cache.get(t) for t in textos]
