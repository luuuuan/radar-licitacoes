"""
Similaridade semântica via embeddings (BGE-M3) e reranking (DeepInfra).

`embeddings_deepinfra()`: gera um "vetor de significado" para cada texto,
comparado por cosseno. Usa o BGE-M3 com a chave GLOBAL do operador
(DEEPINFRA_API_KEY) — usada hoje por `itens_pdf.py` pra achar os trechos
mais relevantes de um PDF antes de mandar pra IA de texto.

`rerank()`: cross-encoder (Qwen3-Reranker via DeepInfra, mesma chave global)
— julga a relevância de um texto contra vários outros DIRETAMENTE (em vez de
comparar embeddings isolados por cosseno), muito mais preciso pra "isso é a
mesma coisa?". É o que o motor de matching (matching/engine.py) usa hoje como
sinal principal de compatibilidade item↔produto.

(Existiu também um `embeddings()` via Gemini/BYOK aqui, bi-encoder comparado
por cosseno — foi removido: o motor de matching passou a usar só o reranker
acima, que testou muito mais preciso, e nenhuma outra parte do app chegou a
adotar essa função antes dela ficar sem nenhum ponto de chamada.)

Proteção de cota: a API tem limite. Quando estoura (HTTP 429), um "disjuntor"
pausa as chamadas daquela chave por algumas horas (até a cota resetar), usando
só o cache em memória, e registra UM aviso em vez de inundar o log.
"""
from __future__ import annotations
import hashlib
import logging
import math
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from ..config import settings

log = logging.getLogger("ia.embeddings")

_DEEPINFRA_URL = "https://api.deepinfra.com/v1/openai/embeddings"
# cache simples em memória: texto -> vetor (evita re-embeddar o mesmo texto).
_cache_deepinfra: dict[str, list[float]] = {}

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


def embeddings_deepinfra(textos: list[str], timeout: int = 30,
                         api_key: str | None = None) -> list[list[float] | None]:
    """Gera embeddings via BGE-M3 na DeepInfra (endpoint compatível com a API
    de embeddings da OpenAI). Camada EXTRA opcional, independente da IA do
    Gemini: a chave é GLOBAL (paga pelo operador do app), por isso cai para
    `settings.DEEPINFRA_API_KEY` quando não vier explícita — ao contrário do
    Gemini, não é bring-your-own-key por usuário. Mesmo formato de retorno,
    cache e disjuntor de cota da função `embeddings()`."""
    chave = api_key or settings.DEEPINFRA_API_KEY
    if not chave:
        return [None] * len(textos)

    if ia_bloqueada(chave):
        return [_cache_deepinfra.get(t) for t in textos]

    faltando = [t for t in textos if t and t not in _cache_deepinfra]
    faltando = list(dict.fromkeys(faltando))

    if faltando:
        body = {
            "input": faltando,
            "model": settings.DEEPINFRA_MODELO_EMBEDDING,
            "encoding_format": "float",
        }
        try:
            r = requests.post(_DEEPINFRA_URL, json=body, timeout=timeout,
                              headers={"Authorization": f"Bearer {chave}",
                                       "Content-Type": "application/json"})
            if r.status_code == 429:
                _pausar(chave, _COOLDOWN_429, "cota/limite de taxa atingido")
                return [_cache_deepinfra.get(t) for t in textos]
            if r.status_code != 200:
                _pausar(chave, _COOLDOWN_ERRO, f"HTTP {r.status_code}")
                return [_cache_deepinfra.get(t) for t in textos]
            dados = r.json().get("data", [])
            for t, item in zip(faltando, dados):
                vec = item.get("embedding") if isinstance(item, dict) else None
                if vec:
                    _cache_deepinfra[t] = vec
        except (requests.RequestException, ValueError) as e:
            _pausar(chave, _COOLDOWN_ERRO, f"falha de rede ({type(e).__name__})")
            return [_cache_deepinfra.get(t) for t in textos]

    return [_cache_deepinfra.get(t) for t in textos]


_RERANKER_URL_TPL = "https://api.deepinfra.com/v1/inference/{modelo}"


# Achado real (usuário reportou, catálogo de 3600 produtos): a DeepInfra
# passou a rejeitar a chamada (HTTP 422, "queries... List should have at
# most 1024 items") quando `documentos` passa de 1024 -- o broadcast de 1
# query contra N documentos (ver docstring de rerank()) é validado do lado
# deles como se "queries" tivesse N itens, e N > 1024 estoura o teto.
# Catálogo de 3600 produtos falhava em TODA chamada, SEMPRE, degradando o
# motor pra só TF-IDF (reranker sempre None) sem nenhum aviso visível no
# app, só nos logs do servidor -- um catálogo desse tamanho não é caso
# raro, é só uma conta que cresceu.
_RERANKER_MAX_DOCUMENTOS = 1024


def _rerank_um_lote(query: str, documentos: list[str], url: str, chave: str,
                    timeout: int, tentativas: int) -> list[float] | None:
    """UMA chamada ao reranker (já dentro do teto de _RERANKER_MAX_DOCUMENTOS)
    -- extraído de rerank() pra poder ser chamado várias vezes, uma por
    lote, quando o catálogo inteiro excede o teto (ver rerank())."""
    body = {"queries": [query], "documents": documentos}
    for tentativa in range(1, max(1, tentativas) + 1):
        try:
            r = requests.post(url, json=body, timeout=timeout,
                              headers={"Authorization": f"bearer {chave}",
                                       "Content-Type": "application/json"})
        except requests.RequestException as e:
            if tentativa < tentativas:
                time.sleep(1.5 * tentativa)
                continue
            log.warning("Reranker falha de rede: %s", e)
            return None
        if r.status_code == 429:
            _pausar(chave, _COOLDOWN_429, "cota/limite de taxa atingido (reranker)")
            return None
        if r.status_code == 402:
            # sem saldo — ao contrário do 429 (reseta sozinho depois do
            # cooldown), isso só volta a funcionar quando alguém repuser o
            # saldo manualmente. Mesmo assim pausa (reaproveita o mesmo
            # disjuntor do 429): sem isso, um recálculo de milhares de
            # editais tentava a chamada de TODO item de TODO edital, sempre
            # recusada, gerando uma linha de log por chamada e atrasando a
            # rodada à toa — achado real em produção.
            _pausar(chave, _COOLDOWN_429, "sem saldo (402) na DeepInfra")
            return None
        if r.status_code in (500, 502, 503, 504):
            if tentativa < tentativas:
                time.sleep(1.5 * tentativa)
                continue
            log.warning("Reranker HTTP %s", r.status_code)
            return None
        if r.status_code != 200:
            log.warning("Reranker HTTP %s: %s", r.status_code, r.text[:200])
            return None
        try:
            scores = r.json()["scores"]
        except (ValueError, KeyError):
            return None
        if len(scores) != len(documentos):
            log.warning("Reranker retornou %d scores pra %d documentos",
                       len(scores), len(documentos))
            return None
        return scores
    return None


def rerank(query: str, documentos: list[str], timeout: int = 30,
          api_key: str | None = None, tentativas: int = 2,
          modelo: str | None = None) -> list[float] | None:
    """Pontua a relevância de cada item de `documentos` contra `query`
    (cross-encoder, DeepInfra) — mesma chave global de embeddings_deepinfra.
    `modelo`: sobrescreve settings.DEEPINFRA_MODELO_RERANKER pra essa chamada
    (ver MatchingEngine.modelo_reranker — só usado no seletor experimental do
    recálculo, por enquanto restrito a uma conta).

    IMPORTANTE, testado contra a API real: ela só aceita `queries` e
    `documents` do MESMO TAMANHO, e usar mais de 1 query DISTINTA por
    chamada dá resultado ERRADO silenciosamente (o score parece depender só
    do índice do documento, ignorando qual query foi pareada — nada indica
    isso na resposta, só comparando os números). Por isso esta função só
    aceita UMA query por chamada — a API faz o broadcast dela contra todos
    os documentos automaticamente quando `queries` tem 1 elemento só. NUNCA
    chame com mais de uma query por request.

    `documentos` com mais de _RERANKER_MAX_DOCUMENTOS (catálogo grande) é
    dividido em várias chamadas (mesma query, broadcastada em cada lote —
    continua dentro do único jeito testado como seguro, ver parágrafo
    acima), disparadas EM PARALELO (ver achado real de performance abaixo)
    e os scores concatenados de volta na MESMA ORDEM dos lotes (nunca na
    ordem em que cada chamada termina) -- cada lote é uma pontuação
    independente (mesma pergunta, fatia diferente do catálogo, um lote não
    influencia o score do outro), então rodar em paralelo dá exatamente o
    mesmo resultado de rodar em sequência, só mais rápido.

    Achado real (usuário reportou: catálogo de 3600 produtos, recálculo da
    base inteira com 14652 editais): em sequência, cada item levava ~8s (4
    lotes x ~2s cada, um atrás do outro) -- pra um edital com dezenas de
    itens, e milhares de editais, isso tornava um recálculo completo
    impraticável (dias). As 4 chamadas são independentes uma da outra (ver
    parágrafo acima), então rodá-las em paralelo é seguro e corta o tempo
    de ~8s pra ~2s por item (o tempo do lote mais lento, não a soma de
    todos) -- mais chamadas simultâneas por item, mas não mais chamadas no
    total, e cada uma continua exatamente igual à de antes.

    Retorna None em qualquer falha (sem chave, cota estourada, rede, HTTP
    != 200, resposta com contagem de scores diferente do esperado, OU
    qualquer lote intermediário falhando) — quem chama trata como "sem
    sinal disponível", não deixa a exceção subir."""
    chave = api_key or settings.DEEPINFRA_API_KEY
    if not chave or not documentos:
        return None
    if ia_bloqueada(chave):
        return None
    url = _RERANKER_URL_TPL.format(modelo=modelo or settings.DEEPINFRA_MODELO_RERANKER)
    if len(documentos) <= _RERANKER_MAX_DOCUMENTOS:
        return _rerank_um_lote(query, documentos, url, chave, timeout, tentativas)
    lotes = [documentos[inicio:inicio + _RERANKER_MAX_DOCUMENTOS]
             for inicio in range(0, len(documentos), _RERANKER_MAX_DOCUMENTOS)]
    resultados: list[list[float] | None] = [None] * len(lotes)
    with ThreadPoolExecutor(max_workers=len(lotes)) as executor:
        futuros = {executor.submit(_rerank_um_lote, query, lote, url, chave, timeout, tentativas): i
                  for i, lote in enumerate(lotes)}
        for futuro in as_completed(futuros):
            resultados[futuros[futuro]] = futuro.result()
    if any(r is None for r in resultados):
        return None   # um lote falhando invalida a pontuação do item inteiro -- mesmo contrato de "sem sinal" de hoje, não parcial
    scores: list[float] = []
    for parcial in resultados:
        scores.extend(parcial)
    return scores
