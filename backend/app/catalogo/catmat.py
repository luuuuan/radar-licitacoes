"""
Busca de CATMAT (e CATSER) no catálogo aberto do Compras.gov.br / SIASG.

API oficial de dados abertos (pública, sem autenticação):
  Material (CATMAT):
    GET https://dadosabertos.compras.gov.br/modulo-material/4_consultarItemMaterial
  Serviço (CATSER):
    GET https://dadosabertos.compras.gov.br/modulo-servico/5_consultarItemServico

Parâmetros relevantes:
  descricaoItem  (texto)  -> busca por descrição
  pagina, tamanhoPagina   (máx. 500 por página)
  codigoItem              -> consulta por código exato (o próprio CATMAT/CATSER)

Resposta padronizada:
  { "resultado": [ {...} ], "totalRegistros", "totalPaginas", "paginasRestantes" }

Observação: o catálogo costuma ter VÁRIOS itens para uma descrição genérica
(ex.: "papel A4"), cada um com seu código. Por isso a busca ranqueia os
resultados por similaridade com o termo digitado e devolve os mais prováveis
no topo — quem confirma o código correto é o usuário.
"""
from __future__ import annotations
import logging

import requests
from rapidfuzz import fuzz

from ..matching.engine import normalizar

log = logging.getLogger("catalogo.catmat")

BASE = "https://dadosabertos.compras.gov.br"
ENDPOINTS = {
    "material": f"{BASE}/modulo-material/4_consultarItemMaterial",
    "servico": f"{BASE}/modulo-servico/5_consultarItemServico",
}

_session = requests.Session()
_session.headers.update({"Accept": "application/json",
                         "User-Agent": "RadarLicitacoes/1.0"})


def _campo(item: dict, *nomes, default=None):
    """Lê o primeiro nome de campo existente (a API varia algumas chaves)."""
    for n in nomes:
        if n in item and item[n] not in (None, ""):
            return item[n]
    return default


def _extrair_lista(dados):
    """A API pode devolver a lista sob chaves diferentes (ou no topo)."""
    if isinstance(dados, list):
        return dados
    if isinstance(dados, dict):
        for chave in ("resultado", "resultados", "data", "items", "itens",
                      "content", "_embedded"):
            v = dados.get(chave)
            if isinstance(v, list):
                return v
            if isinstance(v, dict):  # caso _embedded: {algumaLista: [...]}
                for vv in v.values():
                    if isinstance(vv, list):
                        return vv
    return []


def _normalizar_item(item: dict, tipo: str) -> dict:
    return {
        "tipo": tipo,
        "codigo": _campo(item, "codigoItem", "codigoServico", "codigo",
                         "codigoItemMaterial", "codigoItemServico", "id"),
        "descricao": _campo(item, "descricaoItem", "descricao",
                            "nomeItem", "descricaoItemMaterial",
                            "descricaoItemServico", default=""),
        "pdm": _campo(item, "nomePdm", "pdm"),
        "codigo_pdm": _campo(item, "codigoPdm"),
        "classe": _campo(item, "nomeClasse", "classe"),
        "grupo": _campo(item, "nomeGrupo", "grupo"),
        "unidade": _campo(item, "nomeUnidadeFornecimento", "unidadeFornecimento"),
        "ativo": _campo(item, "statusItem", "status", default=True),
    }


def _consultar(url: str, termo: str, tamanho: int, timeout: int):
    """Faz uma consulta e devolve (registros, resp, erro)."""
    params = {"descricaoItem": termo, "pagina": 1, "tamanhoPagina": tamanho}
    try:
        resp = _session.get(url, params=params, timeout=timeout)
    except requests.RequestException as e:
        return [], None, ("erro_rede", str(e))
    if resp.status_code != 200:
        return [], resp, (f"http_{resp.status_code}", resp.text[:300])
    try:
        dados = resp.json()
    except ValueError:
        return [], resp, ("resposta_invalida", None)
    return _extrair_lista(dados), resp, (None, dados)


def buscar(descricao: str, tipo: str = "material",
           tamanho: int = 500, timeout: int = 45, debug: bool = False) -> dict:
    """
    Busca itens do catálogo e devolve {"status": ..., "itens": [...]}.

    Os catálogos do SIASG são gravados em MAIÚSCULAS e a busca por descrição é
    sensível a isso, então tentamos variações do termo (maiúsculo primeiro) e
    ficamos com a primeira que trouxer registros. Depois filtramos do nosso lado
    para manter só o que realmente bate com o termo buscado.
    Status: ok | vazio | termo_curto | erro_rede | http_XXX | resposta_invalida
    """
    descricao = (descricao or "").strip()
    if len(descricao) < 2:
        return {"status": "termo_curto", "itens": []}

    url = ENDPOINTS.get(tipo, ENDPOINTS["material"])

    # variações a tentar, sem repetir (maiúsculas costuma ser o que funciona)
    variacoes, vistos = [], set()
    for v in (descricao.upper(), descricao, descricao.capitalize()):
        if v not in vistos:
            vistos.add(v); variacoes.append(v)

    registros, resp, info = [], None, (None, None)
    termo_usado = None
    for termo in variacoes:
        registros, resp, info = _consultar(url, termo, tamanho, timeout)
        termo_usado = termo
        if info[0] in ("erro_rede", "resposta_invalida") or (info[0] or "").startswith("http_"):
            # erro de verdade: não adianta tentar outras variações
            out = {"status": info[0], "itens": []}
            if debug:
                out["debug"] = {"url": getattr(resp, "url", url), "erro": info[1]}
            return out
        if registros:
            break  # achou registros, para de tentar

    log.info("Catálogo: %d registros brutos para '%s' (termo='%s')",
             len(registros), descricao, termo_usado)

    alvo = normalizar(descricao)
    tokens = [t for t in alvo.split() if len(t) >= 2]

    itens = []
    for reg in registros:
        norm = _normalizar_item(reg, tipo)
        if not norm["codigo"]:
            continue
        desc_norm = normalizar(norm["descricao"])
        pdm_norm = normalizar(norm["pdm"] or "")
        campo = f"{desc_norm} {pdm_norm}"

        contem = bool(tokens) and all(t in campo for t in tokens)
        rel = fuzz.token_set_ratio(alvo, desc_norm) / 100.0
        if contem:
            rel = max(rel, 0.9)
        norm["relevancia"] = round(rel, 3)

        if contem or rel >= 0.5:
            itens.append(norm)

    itens.sort(key=lambda x: (x["ativo"] is True, x["relevancia"]), reverse=True)
    resultado = {"status": "ok" if itens else "vazio", "itens": itens[:30]}
    if debug:
        dados = info[1]
        chaves_topo = list(dados.keys()) if isinstance(dados, dict) else "lista_no_topo"
        primeiro = registros[0] if registros else None
        resultado["debug"] = {
            "url": getattr(resp, "url", url),
            "termo_usado": termo_usado,
            "registros_brutos": len(registros),
            "itens_apos_filtro": len(itens),
            "chaves_no_topo": chaves_topo,
            "campos_do_primeiro_registro": list(primeiro.keys()) if isinstance(primeiro, dict) else None,
            "amostra_primeiro_registro": primeiro,
        }
    return resultado
