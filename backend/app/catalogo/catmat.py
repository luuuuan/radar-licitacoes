"""
Busca de CATMAT (material) e CATSER (serviço) por TEXTO.

Há duas APIs públicas do governo e elas se comportam diferente:

1) compras.dados.gov.br (API "legada", HATEOAS) -> FAZ busca por "contém" no
   campo descrição. É a que serve para o usuário digitar "papel" e achar itens.
     GET https://compras.dados.gov.br/materiais/v1/materiais.json?descricao=PAPEL
     GET https://compras.dados.gov.br/servicos/v1/servicos.json?descricao=LIMPEZA
   Resposta: { "_embedded": { "materiais": [ {id, descricao, ...} ] }, ... }

2) dadosabertos.compras.gov.br (API "nova") -> filtra sobretudo por CÓDIGOS; o
   parâmetro descricaoItem NÃO faz busca parcial (retorna vazio para "papel").
   Mantida apenas como reserva.

Estratégia: tenta a legada (texto) primeiro; se não vier nada, tenta a nova.
Depois filtra localmente para manter só o que casa com o termo digitado.
"""
from __future__ import annotations
import logging

import requests
from rapidfuzz import fuzz

from ..matching.engine import normalizar

log = logging.getLogger("catalogo.catmat")

# API legada (busca por texto/contém)
LEGADO = {
    "material": "https://compras.dados.gov.br/materiais/v1/materiais.json",
    "servico": "https://compras.dados.gov.br/servicos/v1/servicos.json",
}
# API nova (reserva; filtra por código)
NOVA = {
    "material": "https://dadosabertos.compras.gov.br/modulo-material/4_consultarItemMaterial",
    "servico": "https://dadosabertos.compras.gov.br/modulo-servico/5_consultarItemServico",
}

_session = requests.Session()
_session.headers.update({"Accept": "application/json",
                         "User-Agent": "RadarLicitacoes/1.0"})


def _campo(item: dict, *nomes, default=None):
    for n in nomes:
        if n in item and item[n] not in (None, ""):
            return item[n]
    return default


def _extrair_lista(dados):
    """A resposta pode trazer a lista sob chaves diferentes (ou no topo)."""
    if isinstance(dados, list):
        return dados
    if isinstance(dados, dict):
        for chave in ("resultado", "resultados", "data", "items", "itens",
                      "content", "materiais", "servicos"):
            v = dados.get(chave)
            if isinstance(v, list):
                return v
        emb = dados.get("_embedded")
        if isinstance(emb, dict):
            for vv in emb.values():
                if isinstance(vv, list):
                    return vv
    return []


def _normalizar_item(item: dict, tipo: str) -> dict:
    return {
        "tipo": tipo,
        "codigo": _campo(item, "codigoItem", "codigoServico", "codigo",
                         "codigoItemMaterial", "codigoItemServico", "id"),
        "descricao": _campo(item, "descricaoItem", "descricao", "nomeItem",
                            "descricaoItemMaterial", "descricaoItemServico", default=""),
        "pdm": _campo(item, "nomePdm", "pdm"),
        "codigo_pdm": _campo(item, "codigoPdm"),
        "classe": _campo(item, "nomeClasse", "classe"),
        "grupo": _campo(item, "nomeGrupo", "grupo"),
        "unidade": _campo(item, "nomeUnidadeFornecimento", "unidadeFornecimento"),
        "ativo": _campo(item, "statusItem", "status", default=True),
    }


def _consultar(url: str, params: dict, timeout: int):
    """GET único -> (registros, status, url_final, dados)."""
    try:
        resp = _session.get(url, params=params, timeout=timeout)
    except requests.RequestException as e:
        return [], f"erro_rede", url, {"erro": str(e)}
    if resp.status_code != 200:
        return [], f"http_{resp.status_code}", resp.url, {"corpo": resp.text[:300]}
    try:
        dados = resp.json()
    except ValueError:
        return [], "resposta_invalida", resp.url, None
    return _extrair_lista(dados), "ok", resp.url, dados


def buscar(descricao: str, tipo: str = "material",
           tamanho: int = 500, timeout: int = 45, debug: bool = False) -> dict:
    descricao = (descricao or "").strip()
    if len(descricao) < 2:
        return {"status": "termo_curto", "itens": []}

    tipo = tipo if tipo in ("material", "servico") else "material"
    termo = descricao.upper()  # catálogo é em caixa alta
    tentativas = []
    registros, url_final = [], None

    # 1) API legada: busca por texto/contém
    regs, st, url_final, _ = _consultar(LEGADO[tipo],
                                        {"descricao": termo, "offset": 0}, timeout)
    tentativas.append({"fonte": "legado", "url": url_final, "status": st, "registros": len(regs)})
    if regs:
        registros = regs

    # 2) reserva: API nova (filtra por código; descricaoItem raramente ajuda)
    if not registros:
        regs2, st2, url2, _ = _consultar(NOVA[tipo],
                                         {"descricaoItem": termo, "pagina": 1,
                                          "tamanhoPagina": tamanho}, timeout)
        tentativas.append({"fonte": "nova", "url": url2, "status": st2, "registros": len(regs2)})
        if regs2:
            registros, url_final = regs2, url2

    log.info("Catálogo '%s': %d registros (tentativas: %s)",
             descricao, len(registros), tentativas)

    alvo = normalizar(descricao)
    tokens = [t for t in alvo.split() if len(t) >= 2]

    itens = []
    for reg in registros:
        if not isinstance(reg, dict):
            continue
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
        primeiro = registros[0] if registros else None
        resultado["debug"] = {
            "tentativas": tentativas,
            "registros_brutos": len(registros),
            "itens_apos_filtro": len(itens),
            "campos_do_primeiro_registro": list(primeiro.keys()) if isinstance(primeiro, dict) else None,
            "amostra_primeiro_registro": primeiro,
        }
    return resultado
