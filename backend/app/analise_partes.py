"""
Divisão do texto de um edital grande em PARTES que cabem numa chamada de IA
cada, sem descartar nada (ver analisar() em analise_edital.py).

Por que existe: a recuperação por embeddings (v16) escolhe só os trechos mais
parecidos com 9 categorias fixas -- é uma AMOSTRA, não o edital inteiro. Aqui
o edital INTEIRO é lido, parte por parte, e os resultados são mesclados por
código (ver _mesclar_resultados_analise).

Funções puras (sem rede, sem IA) -- fáceis de testar.
"""
from __future__ import annotations
import re

# Cabeçalho que analisar() coloca antes de cada documento baixado e marcador
# de página que _texto_de_pdf_bytes(marcar_paginas=True) coloca em cada página.
_RE_DOC = re.compile(r"=== DOCUMENTO: (.+?) ===")
_RE_PAG = re.compile(r"\[pág\. (\d+)\]")
_SEP_DOCS = "\n\n---\n\n"


def _quebrar_texto_longo(texto: str, max_chars: int) -> list[str]:
    """Quebra um bloco maior que max_chars (ex.: .docx/.odt sem marcador de
    página, ou página de OCR gigante) preferindo fim de parágrafo, depois
    fim de linha, e só em último caso corte seco."""
    out = []
    while len(texto) > max_chars:
        corte = texto.rfind("\n\n", int(max_chars * 0.5), max_chars)
        if corte < 0:
            corte = texto.rfind("\n", int(max_chars * 0.5), max_chars)
        if corte < 0:
            corte = max_chars
        out.append(texto[:corte])
        texto = texto[corte:].lstrip("\n")
    if texto.strip():
        out.append(texto)
    return out


def _unidades(texto: str, max_chars: int) -> list[dict]:
    """Texto combinado -> lista de unidades {doc, pag, texto}: 1 por página
    (quando há marcador), nunca maior que max_chars."""
    unidades: list[dict] = []
    for bloco_doc in texto.split(_SEP_DOCS):
        m = _RE_DOC.match(bloco_doc.lstrip())
        doc = m.group(1) if m else "documento"
        corpo = bloco_doc.lstrip()[m.end():].lstrip("\n") if m else bloco_doc
        paginas = re.split(r"(?=\[pág\. \d+\]\n)", corpo)
        for pedaco in paginas:
            if not pedaco.strip():
                continue
            mp = _RE_PAG.match(pedaco)
            pag = int(mp.group(1)) if mp else None
            for sub in _quebrar_texto_longo(pedaco, max_chars):
                unidades.append({"doc": doc, "pag": pag, "texto": sub})
    return unidades


def _rotulo_paginas(unidades: list[dict]) -> str:
    """Ex.: "Edital.pdf págs. 3-18; Termo.pdf págs. 1-9" -- só pra log/relatório."""
    por_doc: dict[str, list[int]] = {}
    for u in unidades:
        por_doc.setdefault(u["doc"], [])
        if u["pag"] is not None:
            por_doc[u["doc"]].append(u["pag"])
    partes = []
    for doc, pags in por_doc.items():
        partes.append(f"{doc} págs. {min(pags)}-{max(pags)}" if pags else doc)
    return "; ".join(partes)


def dividir_em_partes(texto: str, max_chars: int = 34000, sobreposicao: int = 4000) -> list[dict]:
    """Divide o texto combinado dos documentos em partes de até `max_chars`
    (sem contar cabeçalhos), SEMPRE em fronteira de página (ou, sem marcador
    de página, de parágrafo). Cada parte devolve:
      {"indice": 1.., "total": N, "texto": str, "cobertura": "Edital.pdf págs. 1-20"}
    - Uma parte que começa no meio de um documento recebe o cabeçalho
      "=== DOCUMENTO: X (continuação) ===" -- a IA precisa saber de qual
      arquivo o trecho veio.
    - Sobreposição: a última página da parte anterior (se couber em
      `sobreposicao` chars e for do mesmo documento) é repetida no início da
      seguinte, pra uma tabela/cláusula que atravessa a fronteira não ser
      lida pela metade. Duplicatas são resolvidas na mesclagem.
    """
    unidades = _unidades(texto, max_chars)
    if not unidades:
        return []
    grupos: list[list[dict]] = []
    atual: list[dict] = []
    tamanho = 0
    for u in unidades:
        if atual and tamanho + len(u["texto"]) > max_chars:
            grupos.append(atual)
            atual, tamanho = [], 0
        atual.append(u)
        tamanho += len(u["texto"])
    if atual:
        grupos.append(atual)

    partes = []
    for i, grupo in enumerate(grupos):
        corpo_unidades = list(grupo)
        if i > 0:
            ultima = grupos[i - 1][-1]
            if ultima["doc"] == grupo[0]["doc"] and len(ultima["texto"]) <= sobreposicao:
                corpo_unidades = [ultima] + corpo_unidades
        blocos, doc_atual = [], None
        for u in corpo_unidades:
            if u["doc"] != doc_atual:
                doc_atual = u["doc"]
                primeiro_do_doc = u is corpo_unidades[0] and (i > 0 and grupos[i - 1][-1]["doc"] == u["doc"])
                sufixo = " (continuação)" if primeiro_do_doc else ""
                blocos.append(f"=== DOCUMENTO: {u['doc']}{sufixo} ===")
            blocos.append(u["texto"])
        partes.append({
            "indice": i + 1,
            "total": len(grupos),
            "texto": "\n".join(blocos),
            "cobertura": _rotulo_paginas(grupo),
        })
    return partes


def aviso_de_parte(parte: dict) -> str:
    """Bloco colocado NO INÍCIO do texto de cada parte (o _PROMPT não muda):
    diz à IA que ela vê só um trecho, pra não concluir 'não é exigido' por
    ausência e pra não se declarar incompleta (a completude é calculada pelo
    código, não pelo autorrelato -- ver analisar())."""
    return (
        f"### ATENÇÃO: este texto é o TRECHO {parte['indice']} de {parte['total']} do edital "
        f"({parte['cobertura']}). NÃO é o edital inteiro -- os outros trechos são analisados à "
        "parte e os resultados juntados depois. Preencha SOMENTE o que aparece NESTE trecho: "
        "deixe vazio (\"\" / [] / false) o que não aparecer aqui, e NUNCA conclua que algo 'não é "
        "exigido' só porque não está neste trecho. Responda \"analise_incompleta\": false (a "
        "cobertura do edital inteiro é verificada pelo sistema, não por você). Trechos repetidos "
        "no início (sobreposição com o trecho anterior) podem aparecer duas vezes no resultado "
        "final -- tudo bem.\n\n"
    )


_CAMPOS_ESCALARES = (
    ("dados_orgao", "data_sessao"), ("dados_orgao", "modo_disputa"),
    ("dados_orgao", "plataforma"), ("dados_orgao", "criterio_julgamento"),
    ("dados_proposta", "validade_dias"), ("dados_proposta", "prazo_entrega"),
    ("dados_proposta", "condicoes_pagamento"),
)


def conflitos_entre_partes(resultados: list[dict]) -> list[str]:
    """Campos de valor único em que partes DIFERENTES devolveram valores
    diferentes e não vazios (ex.: duas datas de sessão). A mesclagem mantém o
    1º valor (partes vêm em ordem de prioridade: retificação primeiro), mas o
    usuário precisa saber que houve divergência -- vira aviso em pontos_atencao."""
    avisos = []
    for grupo, campo in _CAMPOS_ESCALARES:
        valores = []
        for r in resultados:
            v = ((r.get(grupo) or {}).get(campo) or "").strip()
            if v and v.lower() not in [x.lower() for x in valores]:
                valores.append(v)
        if len(valores) > 1:
            nome = campo.replace("_", " ")
            avisos.append(f"Valores diferentes para '{nome}' em trechos diferentes do edital: "
                          + " | ".join(valores[:3]) + ". Foi mantido o primeiro (retificação/edital "
                          "principal vêm antes) -- confira no documento.")
    return avisos
