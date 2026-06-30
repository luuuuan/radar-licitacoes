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
import hashlib
import logging
import math
import time

import requests

from ..config import settings

log = logging.getLogger("ia.embeddings")

_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
# cache simples em memória: texto -> vetor (evita re-embeddar o mesmo texto)
_cache: dict[str, list[float]] = {}

# disjuntor: por CHAVE (cada usuário tem sua própria cota no free tier).
# epoch (segundos) até quando as chamadas daquela chave ficam pausadas.
_bloqueado_ate: dict[str, float] = {}
_COOLDOWN_429 = 6 * 3600        # pausa 6h ao estourar a cota
_COOLDOWN_ERRO = 5 * 60         # pausa curta em erro genérico/instabilidade


def _id_chave(api_key: str) -> str:
    """Hash curto só para identificar a chave nos logs/estado (nunca a chave em si)."""
    return hashlib.sha256(api_key.encode()).hexdigest()[:16]


def ia_disponivel(api_key: str | None = None) -> bool:
    return bool(api_key)   # só a chave do próprio usuário (sem fallback global)


def ia_bloqueada(api_key: str | None = None) -> bool:
    """True se as chamadas DESSA chave estão em pausa (cota estourada recentemente)."""
    if not api_key:
        return False
    return time.time() < _bloqueado_ate.get(_id_chave(api_key), 0.0)


def segundos_para_liberar(api_key: str | None = None) -> int:
    if not api_key:
        return 0
    return max(0, int(_bloqueado_ate.get(_id_chave(api_key), 0.0) - time.time()))


def _pausar(api_key: str, segundos: int, motivo: str):
    chave_id = _id_chave(api_key)
    novo = time.time() + segundos
    # só loga/estende quando realmente muda o estado (evita spam)
    if novo > _bloqueado_ate.get(chave_id, 0.0):
        _bloqueado_ate[chave_id] = novo
        horas = round(segundos / 3600, 1)
        log.warning("IA semântica pausada por ~%sh (%s) [chave %s...]. Usando só o cache até liberar.",
                    horas, motivo, chave_id[:6])


def _norm(v: list[float]) -> float:
    return math.sqrt(sum(x * x for x in v)) or 1.0


def cosseno(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    return dot / (_norm(a) * _norm(b))


def embeddings(textos: list[str], timeout: int = 30,
               api_key: str | None = None) -> list[list[float] | None]:
    """Gera embeddings para uma lista de textos. Usa cache e chamada em lote.
    `api_key` permite usar a chave Gemini do próprio usuário (cai para a global
    se não vier). Retorna lista alinhada à entrada; sem vetor vem como None."""
    chave = api_key   # só a chave do próprio usuário (sem fallback global)
    if not chave:
        return [None] * len(textos)

    # se a cota DESSA chave estourou recentemente, nem tenta — devolve o que tiver em cache
    if ia_bloqueada(chave):
        return [_cache.get(t) for t in textos]

    faltando = [t for t in textos if t and t not in _cache]
    faltando = list(dict.fromkeys(faltando))  # únicos, preservando ordem

    if faltando:
        modelo = settings.IA_MODELO_EMBEDDING
        url = f"{_BASE}/{modelo}:batchEmbedContents"
        # gemini-embedding-2 ignora silenciosamente "taskType" (parâmetro
        # descontinuado nesse modelo) — o jeito certo de pedir embeddings
        # otimizados para similaridade simétrica agora é prefixar o texto.
        # Para gemini-embedding-001 (legado) o taskType ainda funciona.
        usa_prefixo = "embedding-2" in modelo
        if usa_prefixo:
            body = {"requests": [{
                "model": f"models/{modelo}",
                "content": {"parts": [{"text": f"task: sentence similarity | query: {t}"}]},
            } for t in faltando]}
        else:
            body = {"requests": [{
                "model": f"models/{modelo}",
                "content": {"parts": [{"text": t}]},
                "taskType": "SEMANTIC_SIMILARITY",
            } for t in faltando]}
        try:
            r = requests.post(url, json=body, timeout=timeout,
                              headers={"x-goog-api-key": chave,
                                       "Content-Type": "application/json"})
            if r.status_code == 429:
                _pausar(chave, _COOLDOWN_429, "cota diária do plano gratuito atingida")
                return [_cache.get(t) for t in textos]
            if r.status_code != 200:
                _pausar(chave, _COOLDOWN_ERRO, f"HTTP {r.status_code}")
                return [_cache.get(t) for t in textos]
            dados = r.json().get("embeddings", [])
            for t, emb in zip(faltando, dados):
                vec = emb.get("values") if isinstance(emb, dict) else None
                if vec:
                    _cache[t] = vec
        except (requests.RequestException, ValueError) as e:
            _pausar(chave, _COOLDOWN_ERRO, f"falha de rede ({type(e).__name__})")
            return [_cache.get(t) for t in textos]

    return [_cache.get(t) for t in textos]
