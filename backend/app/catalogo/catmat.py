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


def _normalizar_item(item: dict, tipo: str) -> dict:
    return {
        "tipo": tipo,
        "codigo": _campo(item, "codigoItem", "codigoServico", "codigo"),
        "descricao": _campo(item, "descricaoItem", "descricao", default=""),
        "pdm": _campo(item, "nomePdm", "pdm"),
        "codigo_pdm": _campo(item, "codigoPdm"),
        "classe": _campo(item, "nomeClasse", "classe"),
        "grupo": _campo(item, "nomeGrupo", "grupo"),
        "unidade": _campo(item, "nomeUnidadeFornecimento", "unidadeFornecimento"),
        "ativo": _campo(item, "statusItem", "status", default=True),
    }


def buscar(descricao: str, tipo: str = "material",
           tamanho: int = 30, timeout: int = 30) -> list[dict]:
    """
    Busca itens do catálogo por descrição e devolve candidatos ranqueados.
    tipo: "material" (CATMAT) ou "servico" (CATSER).
    """
    descricao = (descricao or "").strip()
    if not descricao:
        return []
    url = ENDPOINTS.get(tipo, ENDPOINTS["material"])
    params = {"descricaoItem": descricao, "pagina": 1, "tamanhoPagina": tamanho}

    try:
        resp = _session.get(url, params=params, timeout=timeout)
    except requests.RequestException as e:
        log.warning("Falha ao consultar catálogo: %s", e)
        return []

    if resp.status_code != 200:
        log.warning("Catálogo retornou HTTP %s: %s", resp.status_code, resp.text[:200])
        return []

    try:
        dados = resp.json()
    except ValueError:
        return []

    registros = dados.get("resultado") if isinstance(dados, dict) else dados
    registros = registros or []

    alvo = normalizar(descricao)
    itens = []
    for reg in registros:
        norm = _normalizar_item(reg, tipo)
        if not norm["codigo"]:
            continue
        # relevância: similaridade entre o termo buscado e a descrição oficial
        norm["relevancia"] = round(
            fuzz.token_set_ratio(alvo, normalizar(norm["descricao"])) / 100.0, 3
        )
        itens.append(norm)

    # mais relevantes primeiro; ativos antes de inativos
    itens.sort(key=lambda x: (x["ativo"] is True, x["relevancia"]), reverse=True)
    return itens
