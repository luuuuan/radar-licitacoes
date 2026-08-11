"""
Validação técnica (regra de negócio) entre um item de edital e um produto
candidato do catálogo.

O matching semântico/textual (engine.py) responde "este produto é sobre o
mesmo assunto que o item?". Isso NÃO basta: "impressora bivolt, 32 ppm" e
"impressora bivolt, 18 ppm" são quase idênticas em texto e incompatíveis na
especificação. Este módulo roda DEPOIS que engine.py (ou o ranking
semântico) já escolheu um candidato, e decide se ele de fato CUMPRE o item
— podendo reprovar um candidato mesmo com similaridade 1.0.

Pendência CRÍTICA reprova o produto (vira "Não atende" independente do
score). Pendência de AVISO só sinaliza para conferência manual: a ausência
de uma informação na descrição do produto não é prova de que ele não tem
aquele atributo, só que o catálogo não descreve.
"""
from __future__ import annotations
from dataclasses import dataclass, field

from .atributos import AtributoNumerico, AtributosTecnicos, estado_caracteristica, extrair_atributos, familia_unidade


@dataclass
class Pendencia:
    tipo: str          # "numerico" | "categorico" | "caracteristica"
    descricao: str
    critico: bool        # True = reprova o produto; False = alerta, não bloqueia


@dataclass
class ResultadoValidacao:
    pendencias: list[Pendencia] = field(default_factory=list)
    # False quando o ITEM não tinha nenhum atributo numérico/categórico/
    # característica reconhecível (categoria fora do vocabulário deste
    # módulo) — nesse caso não há nada de fato verificado, e classificar()
    # não deve reportar "Atende" pleno só por falta de pendência.
    verificavel: bool = False

    @property
    def criticas(self) -> list[Pendencia]:
        return [p for p in self.pendencias if p.critico]

    @property
    def avisos(self) -> list[Pendencia]:
        return [p for p in self.pendencias if not p.critico]

    @property
    def atende(self) -> bool:
        return not self.criticas


def _compara(valor_produto: float, operador: str, valor_exigido: float) -> bool:
    if operador == ">=":
        return valor_produto >= valor_exigido
    if operador == "<=":
        return valor_produto <= valor_exigido
    return valor_produto == valor_exigido


def _bruto_grupo(atributos) -> str:
    return " x ".join(a.bruto for a in atributos)


def _formatar_qtd(valor: float) -> str:
    inteiro = int(valor) if valor == int(valor) else valor
    return f"{inteiro:,}".replace(",", ".")


# Unidades que representam CONTAGEM de peças soltas do próprio produto (ex.:
# folha de papel) — as únicas onde itens_por_unidade (campo do catálogo,
# "quantos itens soltos cabem na unidade vendida", ex.: resma = 500 folhas,
# caixa com 10 resmas = 5000) pode legitimamente divergir do texto da
# descrição sem ser erro de leitura. Não generaliza pra outras unidades
# (ex. "volts"/"dígitos"/"rpm") — ali itens_por_unidade é só "quantos
# produtos vêm na caixa", sem relação nenhuma com o número na especificação
# técnica (comparar "8 dígitos" de uma calculadora com "itens_por_unidade=10
# calculadoras por caixa" não faz sentido nenhum).
_UNIDADES_POR_EMBALAGEM = {"folhas"}


def _corrigir_por_embalagem(atributos: list[AtributoNumerico], itens_por_unidade: float | None) -> list[AtributoNumerico]:
    """Caso real (auditoria em produção): "Caixa Papel A4 75g 500 Folhas ...
    - 10 Resmas" tinha valor=500 extraído do texto (a contagem por RESMA,
    granularidade descrita na frase) enquanto itens_por_unidade=5000 (campo
    estruturado do catálogo, sempre o TOTAL de folhas soltas na unidade
    vendida — a caixa inteira) — um item pedindo "5.000 folhas" reprovava
    esse produto por engano, mesmo sendo exatamente a mesma coisa.

    Só substitui quando itens_por_unidade é MAIOR que o valor lido do texto:
    nesse caso o texto plausivelmente descreve um subpacote (resma) dentro
    da embalagem maior (caixa), e itens_por_unidade é a fonte mais confiável
    pro TOTAL. Quando itens_por_unidade é MENOR (ex.: bloco adesivo vendido
    em pacote de 4 BLOCOS, itens_por_unidade=4, cada bloco com 50 folhas)
    ele conta uma granularidade diferente (blocos, não folhas) — não dá pra
    saber com segurança o total de folhas só com esse número, então não
    mexe (mantém o comportamento conservador de antes)."""
    if not itens_por_unidade or itens_por_unidade <= 0:
        return atributos
    corrigidos = []
    for a in atributos:
        if a.unidade in _UNIDADES_POR_EMBALAGEM and itens_por_unidade > a.valor:
            corrigidos.append(AtributoNumerico(
                a.unidade, itens_por_unidade, a.operador,
                f"{_formatar_qtd(itens_por_unidade)} {a.unidade} (embalagem completa — {a.bruto} por unidade menor)",
                a.inferido,
            ))
        else:
            corrigidos.append(a)
    return corrigidos


def _validar_numerico_simples(exigido, candidatos: list) -> list[Pendencia]:
    """Valida UMA exigência numérica isolada contra os candidatos da mesma
    família (comportamento original: procura o "melhor"/maior candidato e
    compara). Usado quando o item exige só UM valor daquela família (ex.:
    "no mínimo 250 folhas"). Para vários valores da mesma família de uma vez
    (par de dimensão, ex.: item "38 x 51"), veja _validar_grupo_dimensao —
    comparar cada um contra "o melhor candidato" isoladamente gera uma
    pendência de "múltiplos valores" PRA CADA um, multiplicando N x M
    mensagens repetidas quando na real é só uma dimensão com dois números."""
    if not candidatos:
        return [Pendencia(
            "numerico",
            f"item exige {exigido.bruto} — produto não informa '{exigido.unidade}' na descrição",
            critico=False,
        )]
    _, valor_exigido_canon = familia_unidade(exigido.unidade, exigido.valor)
    pendencias: list[Pendencia] = []
    # compara pelo valor já convertido pra mesma escala (família) — "2
    # litros" e "2000 ml" são o MESMO valor, não uma ambiguidade.
    valores_canon = {familia_unidade(c.unidade, c.valor)[1] for c in candidatos}
    if len(valores_canon) > 1:
        # candidatos com valor efetivamente diferente (mesmo já convertidos
        # pra mesma escala) — podem ser o mesmo atributo redito com um
        # valor diferente, ou dois atributos distintos que só coincidem em
        # unidade/família — regex não consegue desambiguar com segurança.
        # Caso real: "Papel Sulfite A4 180 g/m² 60 kg Branco..." — "180 g/m²"
        # é a gramatura (o que interessa), "60 kg" é peso de fardo/caixa (uma
        # coisa completamente diferente que só cai na mesma família "massa"
        # por coincidência de unidade) — comparar contra o "maior valor" só
        # porque ele "vence" a comparação numérica reprovava o produto com
        # uma crítica que não faz sentido nenhum fisicamente ("produto
        # oferece 60 kg" numa comparação de gramatura de papel). Já que o
        # próprio aviso abaixo admite não saber qual valor vale, não dá pra
        # também afirmar com confiança que o produto NÃO atende — sem isso
        # dessa vez, não teria como saber se é uma reprovação de verdade.
        lista = ", ".join(c.bruto for c in candidatos)
        pendencias.append(Pendencia(
            "numerico",
            f"produto menciona múltiplos valores de '{exigido.unidade}' ({lista}) — confirmar manualmente qual se refere ao item",
            critico=False,
        ))
        return pendencias
    melhor = max(candidatos, key=lambda o: familia_unidade(o.unidade, o.valor)[1])
    _, melhor_canon = familia_unidade(melhor.unidade, melhor.valor)
    if not _compara(melhor_canon, exigido.operador, valor_exigido_canon):
        if exigido.inferido:
            # a unidade do lado do ITEM foi uma suposição (campo estruturado
            # do PNCP tipo "comprimento: 21" sem unidade no texto, assumida
            # como mm) — não é fato lido do texto, então não pode reprovar
            # sozinha. Ex.: "21" pode muito bem ser 21cm (uma tesoura), não
            # 21mm; a suposição errada não pode virar uma crítica que
            # derruba um produto bom.
            pendencias.append(Pendencia(
                "numerico",
                f"item exige {exigido.bruto} (unidade assumida, não informada no texto) "
                f"— produto oferece {melhor.bruto} — confirmar unidade manualmente",
                critico=False,
            ))
        else:
            pendencias.append(Pendencia(
                "numerico",
                f"item exige {exigido.bruto} — produto oferece {melhor.bruto}",
                critico=True,
            ))
    return pendencias


def _validar_exigidos_soltos(familia: str, exigidos: list, candidatos: list) -> list[Pendencia]:
    """Valida valor(es) "soltos" da mesma família do lado do ITEM — número
    que não veio de uma cadeia explícita "AxB"/"AxBxC" (_pares_dimensao) nem
    de um campo estruturado tipo comprimento/largura/altura (esses dois já
    têm sinal próprio de que os valores pertencem à MESMA especificação, ver
    cadeia_id/inferido em atributos.py).

    Um valor só: comportamento normal (_validar_numerico_simples). Vários
    valores DIFERENTES: dois números soltos que só coincidem em unidade não
    são necessariamente a mesma medida (achado real: item cita "lâmina ...
    cerca de 7 cm" e, numa frase adiante, referencia outro item "(10,8 cm)"
    só como comparação — os dois "cm" acabavam virando um par de dimensão
    inventado "7x10,8cm", reprovando um produto de 21cm que na real atendia).
    Mesma filosofia já usada pro lado ofertado (_validar_numerico_simples,
    "múltiplos valores"): sem como saber qual valor é o requisito de
    verdade, vira aviso pra conferência manual, não reprovação automática."""
    if len(exigidos) == 1:
        return _validar_numerico_simples(exigidos[0], candidatos)
    valores_canon = {familia_unidade(e.unidade, e.valor)[1] for e in exigidos}
    if len(valores_canon) == 1:
        return _validar_numerico_simples(exigidos[0], candidatos)
    lista = ", ".join(e.bruto for e in exigidos)
    return [Pendencia(
        "numerico",
        f"item menciona múltiplos valores de '{familia}' ({lista}) — confirmar manualmente qual é o requisito",
        critico=False,
    )]


def _validar_grupo_dimensao(exigidos: list, candidatos: list) -> list[Pendencia]:
    """Valida um GRUPO de exigências da MESMA família (ex.: par de dimensão
    "38 x 51", que vira 2 AtributoNumerico de comprimento) como CONJUNTO
    contra o que o produto oferece — em vez de cada valor procurar seu
    "melhor" candidato isoladamente (o que gerava uma pendência de
    "múltiplos valores" REPETIDA pra cada valor exigido: um par 2x2 virava 4
    mensagens pra dizer a mesma coisa duas vezes)."""
    bruto_exigido = _bruto_grupo(exigidos)
    if not candidatos:
        sufixo = " (unidade assumida, não informada no texto — confirmar)" if any(e.inferido for e in exigidos) else ""
        return [Pendencia(
            "numerico",
            f"item exige {bruto_exigido}{sufixo} — produto não informa a medida na descrição",
            critico=False,
        )]
    # ordena os dois lados e compara como conjunto — "38 x 51" bate com
    # "51 x 38" (ordem de largura/altura é ambígua na notação "AxB").
    valores_exigidos = sorted(familia_unidade(e.unidade, e.valor)[1] for e in exigidos)
    valores_ofertados = sorted(familia_unidade(c.unidade, c.valor)[1] for c in candidatos)
    if valores_exigidos == valores_ofertados:
        return []
    bruto_ofertado = _bruto_grupo(candidatos)
    if any(e.inferido for e in exigidos):
        # mesma lógica do caso simples: unidade assumida (sem estar no
        # texto) não pode reprovar sozinha, só virar aviso.
        return [Pendencia(
            "numerico",
            f"item exige {bruto_exigido} (unidade assumida, não informada no texto) "
            f"— produto oferece {bruto_ofertado} — confirmar medida manualmente",
            critico=False,
        )]
    return [Pendencia(
        "numerico",
        f"item exige {bruto_exigido} — produto oferece {bruto_ofertado}",
        critico=True,
    )]


def validar(item_texto: str, produto_texto: str, itens_por_unidade: float | None = None) -> ResultadoValidacao:
    """Extrai os requisitos do item e valida — atalho para quando só há UM
    produto candidato. Ao validar vários candidatos contra o mesmo item,
    prefira extrair_atributos(item_texto) uma vez e chamar
    validar_com_requisitos() por candidato, para não repetir a extração do
    lado do item a cada chamada.

    itens_por_unidade: campo do catálogo (quantos itens soltos cabem na
    unidade vendida, ex.: resma = 500 folhas) — ver _corrigir_por_embalagem."""
    return validar_com_requisitos(extrair_atributos(item_texto), produto_texto, itens_por_unidade)


def validar_com_requisitos(req: AtributosTecnicos, produto_texto: str,
                            itens_por_unidade: float | None = None) -> ResultadoValidacao:
    of = extrair_atributos(produto_texto)
    of.numericos = _corrigir_por_embalagem(of.numericos, itens_por_unidade)
    pendencias: list[Pendencia] = []

    # 1) atributos numéricos (capacidade, velocidade, dimensão...) — agrupa
    # por FAMÍLIA, mas só trata como PAR/TRIO de dimensão (comparação de
    # CONJUNTO, _validar_grupo_dimensao) quando os valores de fato vieram
    # JUNTOS no texto: uma cadeia explícita "AxB"/"AxBxC" (cadeia_id) ou um
    # campo estruturado tipo comprimento/largura/altura (inferido=True, ver
    # atributos.py). Valores "soltos" da mesma família que não compartilham
    # nenhum desses dois sinais vão pra _validar_exigidos_soltos — dois
    # números em "cm" perdidos em frases diferentes do texto NÃO são
    # necessariamente a mesma medida (ver seu docstring).
    grupos: dict[tuple, list] = {}
    soltos: dict[str, list] = {}
    for exigido in req.numericos:
        familia, _ = familia_unidade(exigido.unidade, exigido.valor)
        if exigido.cadeia_id is not None:
            grupos.setdefault((familia, "cadeia", exigido.cadeia_id), []).append(exigido)
        elif exigido.inferido:
            grupos.setdefault((familia, "campo_estruturado"), []).append(exigido)
        else:
            soltos.setdefault(familia, []).append(exigido)
    for (familia, *_resto), exigidos in grupos.items():
        candidatos = [o for o in of.numericos if familia_unidade(o.unidade, o.valor)[0] == familia]
        if len(exigidos) >= 2:
            pendencias.extend(_validar_grupo_dimensao(exigidos, candidatos))
        else:
            pendencias.extend(_validar_numerico_simples(exigidos[0], candidatos))
    for familia, exigidos in soltos.items():
        candidatos = [o for o in of.numericos if familia_unidade(o.unidade, o.valor)[0] == familia]
        pendencias.extend(_validar_exigidos_soltos(familia, exigidos, candidatos))

    # 2) atributos categóricos (formato/modelo — igualdade exata, ex.: grampo 26/6)
    for chave, valor_exigido in req.categoricos.items():
        valor_ofertado = of.categoricos.get(chave)
        if valor_ofertado is None:
            pendencias.append(Pendencia(
                "categorico",
                f"item exige {chave} {valor_exigido} — produto não informa o modelo/formato",
                critico=False,
            ))
        elif valor_ofertado != valor_exigido:
            pendencias.append(Pendencia(
                "categorico",
                f"item exige {chave} {valor_exigido} — produto é {valor_ofertado} (incompatível)",
                critico=True,
            ))

    # 3) características obrigatórias (bivolt, duplex, inox, sem fio...)
    for carac in req.caracteristicas:
        estado = estado_caracteristica(carac, of.texto_normalizado)
        if estado == "oposto":
            pendencias.append(Pendencia(
                "caracteristica",
                f"item exige '{carac}' — produto descreve o oposto",
                critico=True,
            ))
        elif estado == "ausente":
            pendencias.append(Pendencia(
                "caracteristica",
                f"item exige '{carac}' — não mencionado na descrição do produto (confirmar manualmente)",
                critico=False,
            ))

    verificavel = bool(req.numericos or req.categoricos or req.caracteristicas)
    return ResultadoValidacao(pendencias, verificavel=verificavel)


# ---------------------------------------------------------------------------
# Classificação final: combina o score semântico/textual (produzido pelo
# MatchingEngine de matching/engine.py, ou por um ranking TF-IDF avulso) com
# o resultado da validação técnica acima.
# ---------------------------------------------------------------------------
LIMIAR_ATENDE = 0.55
# alinhado com settings.LIMIAR_ITEM (config.py) — o corte que o engine.py de
# produção já usa para considerar um item "compatível"; abaixo disso o
# próprio engine nem lista o par como candidato.
LIMIAR_PARCIAL = 0.35


def classificar(score_semantico: float, validacao: ResultadoValidacao) -> str:
    """'Atende' | 'Atende parcialmente' | 'Não atende'.

    Ordem importa: 1) score baixo mata de saída (produto irrelevante,
    validação nem entra); 2) pendência crítica mata mesmo com score alto
    (essa é a regra central: nunca deixar 'parecido' virar 'atende' se um
    requisito obrigatório falhou); 3) só é 'Atende' pleno se além de passar
    na validação não sobrou nenhum aviso pendente de conferência manual E
    havia de fato algo verificável (senão é só o score dizendo 'parecido',
    sem nenhuma validação técnica real por trás — vira 'parcialmente')."""
    if score_semantico < LIMIAR_PARCIAL:
        return "Não atende"
    if validacao.criticas:
        return "Não atende"
    if score_semantico >= LIMIAR_ATENDE and not validacao.avisos and validacao.verificavel:
        return "Atende"
    return "Atende parcialmente"
