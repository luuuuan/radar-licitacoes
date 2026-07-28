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

from .atributos import AtributosTecnicos, estado_caracteristica, extrair_atributos, familia_unidade


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


def validar(item_texto: str, produto_texto: str) -> ResultadoValidacao:
    """Extrai os requisitos do item e valida — atalho para quando só há UM
    produto candidato. Ao validar vários candidatos contra o mesmo item,
    prefira extrair_atributos(item_texto) uma vez e chamar
    validar_com_requisitos() por candidato, para não repetir a extração do
    lado do item a cada chamada."""
    return validar_com_requisitos(extrair_atributos(item_texto), produto_texto)


def validar_com_requisitos(req: AtributosTecnicos, produto_texto: str) -> ResultadoValidacao:
    of = extrair_atributos(produto_texto)
    pendencias: list[Pendencia] = []

    # 1) atributos numéricos (capacidade, velocidade, dimensão...)
    for exigido in req.numericos:
        familia_exigida, valor_exigido_canon = familia_unidade(exigido.unidade, exigido.valor)
        candidatos = [o for o in of.numericos if familia_unidade(o.unidade, o.valor)[0] == familia_exigida]
        if not candidatos:
            pendencias.append(Pendencia(
                "numerico",
                f"item exige {exigido.bruto} — produto não informa '{exigido.unidade}' na descrição",
                critico=False,
            ))
            continue
        # compara pelo valor já convertido pra mesma escala (família) — "2
        # litros" e "2000 ml" são o MESMO valor, não uma ambiguidade.
        valores_canon = {familia_unidade(c.unidade, c.valor)[1] for c in candidatos}
        if len(valores_canon) > 1:
            # candidatos com valor efetivamente diferente (mesmo já convertidos
            # pra mesma escala) — podem ser o mesmo atributo redito com um
            # valor diferente, ou dois atributos distintos que só coincidem em
            # unidade/família — regex não consegue desambiguar com segurança.
            lista = ", ".join(c.bruto for c in candidatos)
            pendencias.append(Pendencia(
                "numerico",
                f"produto menciona múltiplos valores de '{exigido.unidade}' ({lista}) — confirmar manualmente qual se refere ao item",
                critico=False,
            ))
        melhor = max(candidatos, key=lambda o: familia_unidade(o.unidade, o.valor)[1])
        _, melhor_canon = familia_unidade(melhor.unidade, melhor.valor)
        if not _compara(melhor_canon, exigido.operador, valor_exigido_canon):
            pendencias.append(Pendencia(
                "numerico",
                f"item exige {exigido.bruto} — produto oferece {melhor.bruto}",
                critico=True,
            ))

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
