"""
Busca de CATMAT (material) e CATSER (serviço) por TEXTO.

Duas fontes públicas do governo, combinadas — nenhuma sozinha resolve:

1) cnbs.estaleiro.serpro.gov.br (API por trás do catalogo.compras.gov.br) ->
   FAZ busca por palavra de verdade. Para material devolve o PDM (Padrão
   Descritivo de Material — o "grupo" do item, ex.: "Calculadora"), não o
   item final com código; para serviço já devolve o código final utilizável.
     GET .../cnbs-api/material/v1/palavra?palavra=CALCULADORA
     GET .../cnbs-api/servico/v1/palavra?palavra=LIMPEZA

2) dadosabertos.compras.gov.br (API "oficial" de dados abertos) -> NÃO faz
   busca por texto (só por código — `descricaoItem` não filtra por conteúdo,
   apesar do nome), mas aceita filtrar itens de material por `codigoPdm`.
   Usada só no 2º passo: dado um PDM candidato, listar os itens (com código)
   daquele PDM.
     GET .../modulo-material/4_consultarItemMaterial?codigoPdm=16563

Estratégia (material): busca por palavra -> PDMs candidatos -> rankeia pelo
termo digitado -> busca os itens dos PDMs mais relevantes -> filtra/rankeia
de novo pelo termo original (o PDM é um grupo amplo; nem todo item dele bate
igualmente bem com o que foi digitado).
Estratégia (serviço): a busca por palavra já devolve o código -> só filtra/
rankeia.
"""
from __future__ import annotations
import logging
from concurrent.futures import ThreadPoolExecutor

import requests
from rapidfuzz import fuzz

from ..matching.engine import normalizar

log = logging.getLogger("catalogo.catmat")

# passo 1: busca por palavra (PDM para material, código final para serviço)
_PALAVRA = {
    "material": "https://cnbs.estaleiro.serpro.gov.br/cnbs-api/material/v1/palavra",
    "servico": "https://cnbs.estaleiro.serpro.gov.br/cnbs-api/servico/v1/palavra",
}
# passo 2 (só material): itens de um PDM específico
_ITENS_DO_PDM = "https://dadosabertos.compras.gov.br/modulo-material/4_consultarItemMaterial"
# quantos PDMs (mais relevantes) drilar no passo 2 — cada um é uma chamada
# HTTP extra, então limita a latência total da busca. Precisa ser generoso:
# o PDM é só o "tipo" do produto (ex.: "Papel para impressão formatado"),
# sem o tamanho/formato (A4, gramatura...) — isso é característica do ITEM,
# não do nome do PDM — então o ranking por texto contra o nome do PDM erra
# fácil se cortar cedo demais (ex.: "papel a4" rankeia mal contra "Papel
# para impressão formatado" só pelo texto, mas os ITENS desse PDM têm "A4"
# na descrição). O ranking final por relevância (contra a descrição do
# ITEM, não do PDM) corrige isso — desde que o PDM certo tenha sido drilado.
_MAX_PDMS = 6

_session = requests.Session()
_session.headers.update({"Accept": "application/json",
                         "User-Agent": "RadarLicitacoes/1.0"})


def _campo(item: dict, *nomes, default=None):
    for n in nomes:
        if n in item and item[n] not in (None, ""):
            return item[n]
    return default


def _extrair_lista(dados):
    """A resposta pode vir como lista solta ou dict com a lista sob outra chave."""
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
                            "descricaoItemMaterial", "descricaoItemServico",
                            "descricaoServicoAcentuado", "nomeServicoAcentuado", default=""),
        "pdm": _campo(item, "nomePdm", "pdm"),
        "codigo_pdm": _campo(item, "codigoPdm", "codigoPDM"),
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
        return [], "erro_rede", url, {"erro": str(e)}
    if resp.status_code != 200:
        return [], f"http_{resp.status_code}", resp.url, {"corpo": resp.text[:300]}
    try:
        dados = resp.json()
    except ValueError:
        return [], "resposta_invalida", resp.url, None
    return _extrair_lista(dados), "ok", resp.url, dados


_STOPWORDS_BUSCA = {"DE", "DA", "DO", "DAS", "DOS", "PARA", "COM", "SEM",
                    "E", "OU", "A", "O", "AS", "OS", "EM", "NO", "NA"}


def _palavras_significativas(termo: str) -> list[str]:
    return [t for t in termo.split() if len(t) >= 3 and t not in _STOPWORDS_BUSCA]


def _consultar_palavra(url: str, termo: str, timeout: int):
    """Busca por palavra com fallback: a API do catálogo não faz "E" entre
    termos — se a frase inteira não bater literalmente em algum lugar,
    devolve vazio mesmo quando as palavras isoladas têm resultado (ex.:
    "limpeza predial" -> 0, mas "limpeza" sozinha -> 70). Quando a frase
    completa não acha nada, tenta palavra por palavra e junta tudo; o
    rankeamento por relevância feito depois (contra o termo original)
    filtra o que realmente interessa."""
    regs, st, url_final, _ = _consultar(url, {"palavra": termo}, timeout)
    tentativas = [{"fonte": "palavra_completa", "url": url_final,
                   "status": st, "registros": len(regs)}]
    if regs:
        return regs, tentativas
    combinados: list[dict] = []
    vistos = set()
    for palavra in _palavras_significativas(termo):
        regs2, st2, url2, _ = _consultar(url, {"palavra": palavra}, timeout)
        tentativas.append({"fonte": f"palavra_fallback_{palavra}", "url": url2,
                           "status": st2, "registros": len(regs2)})
        for r in regs2:
            chave = (r.get("codigoServico") or r.get("codigoPDM")
                    or r.get("codigoPdm") or id(r))
            if chave not in vistos:
                vistos.add(chave)
                combinados.append(r)
    return combinados, tentativas


def _rankear_pdms(pdms: list[dict], descricao: str, top: int = _MAX_PDMS) -> list[dict]:
    """Ordena os PDMs candidatos pela relevância ao termo digitado e corta
    nos `top` melhores — evita drilar (2º passo) PDMs pouco relacionados."""
    alvo = normalizar(descricao)

    def relevancia(p: dict) -> float:
        nome = normalizar(p.get("nomePdm") or p.get("descricaoPDM") or "")
        if not nome:
            return 0.0
        rel = fuzz.token_set_ratio(alvo, nome) / 100.0
        if alvo in nome or nome in alvo:
            rel = max(rel, 0.9)
        return rel

    ativos = [p for p in pdms if p.get("statusPDM", True)] or pdms
    return sorted(ativos, key=relevancia, reverse=True)[:top]


def buscar(descricao: str, tipo: str = "material",
           tamanho: int = 500, timeout: int = 45, debug: bool = False) -> dict:
    descricao = (descricao or "").strip()
    if len(descricao) < 2:
        return {"status": "termo_curto", "itens": []}

    tipo = tipo if tipo in ("material", "servico") else "material"
    termo = descricao.upper()  # catálogo é em caixa alta
    tentativas = []
    registros: list[dict] = []

    if tipo == "servico":
        # serviço não tem o mesmo nível intermediário (PDM) que material — a
        # busca por palavra já devolve o código final utilizável.
        registros, tent = _consultar_palavra(_PALAVRA["servico"], termo, timeout)
        tentativas.extend(tent)
    else:
        pdms, tent = _consultar_palavra(_PALAVRA["material"], termo, timeout)
        tentativas.extend(tent)
        codigos_pdm = [p.get("codigoPDM") or p.get("codigoPdm")
                       for p in _rankear_pdms(pdms, descricao)]
        codigos_pdm = [c for c in codigos_pdm if c]

        def _itens_do_pdm(codigo_pdm):
            return codigo_pdm, _consultar(
                _ITENS_DO_PDM, {"codigoPdm": codigo_pdm, "pagina": 1, "tamanhoPagina": 100}, timeout)

        # cada PDM é uma chamada HTTP independente (mesma API, filtros
        # diferentes) — paralelizar evita que o tempo total vire a SOMA das
        # chamadas em vez do tempo da mais lenta delas.
        if codigos_pdm:
            with ThreadPoolExecutor(max_workers=len(codigos_pdm)) as pool:
                for codigo_pdm, (itens, st2, url2, _) in pool.map(_itens_do_pdm, codigos_pdm):
                    tentativas.append({"fonte": f"dadosabertos_pdm_{codigo_pdm}", "url": url2,
                                       "status": st2, "registros": len(itens)})
                    registros.extend(itens)

    log.info("Catálogo '%s' (%s): %d registros (tentativas: %s)",
             descricao, tipo, len(registros), tentativas)

    alvo = normalizar(descricao)
    tokens = [t for t in alvo.split() if len(t) >= 2]

    itens = []
    codigos_vistos = set()
    for reg in registros:
        if not isinstance(reg, dict):
            continue
        norm = _normalizar_item(reg, tipo)
        if not norm["codigo"] or norm["codigo"] in codigos_vistos:
            continue
        codigos_vistos.add(norm["codigo"])
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
