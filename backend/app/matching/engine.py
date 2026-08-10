"""
Motor de correspondência (matching).

Estratégia:

1. Correspondência EXATA de código (NCM, CATMAT/CATSER) entre um produto do
   catálogo e um item do edital — sinal FISCAL, ortogonal ao textual. Só
   conta como vencedor automático quando o reranker (abaixo) também
   corrobora que os textos têm relação real: código fiscal é uma
   classificação ampla, não garante ser o MESMO produto físico — achado
   real recorrente em produção, "Álcool Etílico gel 70%" batendo por NCM
   idêntico com "Desinfetante 5 litros Lavanda" (mesma família fiscal de
   produto de limpeza/higiene, zero relação real entre os produtos).

2. Reranker (cross-encoder, Qwen3-Reranker via DeepInfra) julga a
   relevância de CADA produto do catálogo contra o texto do item, numa
   chamada só por item. Substituiu TF-IDF + palavras-chave + embeddings
   bi-encoder (Gemini/BGE-M3): testado contra a API real com casos reais de
   produção (inclusive o caso do Álcool Etílico acima), o reranker separou
   com muito mais clareza um match genuíno (score ~0.9-1.0) de uma
   coincidência textual ou de código fiscal amplo (~0.0-0.01) do que
   qualquer heurística de palavra-chave ou cosseno de embeddings conseguia.

   Achado real em auditoria de produção (ago/2026): mesmo SEM código fiscal
   em comum, o reranker "0.6B" dava score alto (>=0.9, "alta"/automático)
   pra pares sem relação real nenhuma sempre que o catálogo não tinha
   nenhum produto genuinamente equivalente — ex.: remédio (Mesalazina,
   Pregabalina) batendo com cola em spray/sabonete líquido; fralda batendo
   com copo descartável; soro fisiológico batendo com água sanitária (o
   MESMO padrão do caso Álcool Etílico acima, só que sem precisar de código
   fiscal compartilhado pra acontecer). Teste A/B real contra a API
   (9 casos graves reais) mostrou 2 mudanças que resolvem isso: (a) prefixar
   a query com uma instrução explícita pedindo "mesmo item físico, não só
   mesma categoria" (ver `_INSTRUCAO_RERANKER` abaixo); (b) subir de
   Qwen3-Reranker-0.6B pra 4B — o 0.6B com a instrução ainda deixava passar
   6 dos 9 casos graves, o 4B corrigiu os 9 (empatado com o 8B, que custa o
   dobro sem ganho adicional nesses casos).

Cada item do edital recebe o melhor score contra o catálogo. O edital recebe
um score agregado e um nível: fraco | medio | forte.

Sem DEEPINFRA_API_KEY configurada (ou se a chamada ao reranker falhar), o
motor cai pra SÓ o código exato, sem corroboração nenhuma disponível — ou
seja, sem reranker, nenhum código exato vira vencedor automático e não há
nenhum sinal textual. Decisão consciente: sem fallback textual paralelo
(TF-IDF) — o reranker é a única fonte de similaridade textual do motor.
"""
from __future__ import annotations
import re
import unicodedata
from dataclasses import dataclass, field

from ..config import settings
from .embeddings import rerank as _rerank


# ---------------------------------------------------------------------------
# Normalização de texto
# ---------------------------------------------------------------------------
def normalizar(texto: str | None) -> str:
    if not texto:
        return ""
    texto = unicodedata.normalize("NFKD", texto)
    texto = texto.encode("ascii", "ignore").decode("ascii")
    texto = texto.lower()
    texto = re.sub(r"[^a-z0-9\s]", " ", texto)
    texto = re.sub(r"\s+", " ", texto).strip()
    return texto


def so_digitos(codigo: str | None) -> str:
    if not codigo:
        return ""
    return re.sub(r"\D", "", codigo)


# ---------------------------------------------------------------------------
# Estruturas leves (independentes do ORM, para facilitar testes)
# ---------------------------------------------------------------------------
@dataclass
class ProdutoCat:
    id: int
    descricao: str
    ncm: str = ""
    cest: str = ""
    ean: str = ""
    catmat: str = ""
    catser: str = ""
    palavras_chave: str = ""

    def texto(self) -> str:
        """Texto mandado ao reranker — descrição + palavras-chave como
        contexto extra. Sem normalização/stemming agressivos: o reranker é
        um modelo de linguagem natural, entende sinônimo/flexão sozinho e
        só perde precisão com texto truncado em radicais."""
        base = (self.descricao or "").strip()
        if self.palavras_chave:
            return f"{base} ({self.palavras_chave.strip()})"
        return base

    def codigos(self) -> dict[str, str]:
        return {
            "ncm": so_digitos(self.ncm),
            "ean": so_digitos(self.ean),
            "catmat": so_digitos(self.catmat),
            "catser": so_digitos(self.catser),
        }


@dataclass
class ItemEdt:
    numero: int | None
    descricao: str
    ncm: str = ""
    catalogo_codigo: str = ""  # CATMAT/CATSER

    def texto(self) -> str:
        return (self.descricao or "").strip()


@dataclass
class ResultadoMatch:
    score: float
    nivel: str
    itens_compativeis: int
    detalhe: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Motor
# ---------------------------------------------------------------------------
class MatchingEngine:
    # Piso do score do RERANKER pra um candidato de código exato (NCM/
    # CATMAT/CATSER) virar vencedor automático. Testado contra a API real:
    # matches genuínos sempre pontuaram >=0.89 (chegando a 0.9999); falsos
    # positivos por código fiscal amplo (ex.: Álcool Etílico x Desinfetante,
    # mesmo NCM, produtos sem relação nenhuma) sempre <=0.0014. 0.5 dá folga
    # enorme dos dois lados.
    _PISO_RERANKER_CODIGO_EXATO = 0.5

    _MOTIVO_SEMELHANCA = "semelhança (IA)"

    # Prefixo mandado junto com o texto do item em toda chamada ao reranker.
    # Sem isso, o modelo trata "mesma categoria geral" como se fosse "mesmo
    # produto" quando o catálogo não tem nada genuinamente equivalente (ver
    # achado de produção no topo do arquivo) — testado A/B contra a API
    # real: com essa instrução + Qwen3-Reranker-4B, os 9 casos graves reais
    # auditados (remédio↔produto de limpeza, fralda↔copo, etc.) foram
    # corretamente rejeitados, sem baixar o score dos matches genuínos
    # (que continuaram >=0.9 nos casos conferidos).
    _INSTRUCAO_RERANKER = (
        "Instrução: considere relevante APENAS um produto que seja o MESMO "
        "item físico do texto abaixo (mesmo tipo de objeto e uso prático), "
        "NÃO um item apenas parecido ou da mesma categoria geral.\n\nItem: "
    )

    def __init__(self, produtos: list[ProdutoCat], usar_ia: bool = False,
                modelo_reranker: str | None = None):
        self.produtos = produtos
        self._textos_prod = [p.texto() for p in produtos]
        # "usar_ia" aqui é literalmente "o reranker roda ou não" — sem ele,
        # o motor fica só com código exato (sem corroboração, então nem
        # esse vira vencedor automático) e nenhum sinal textual.
        self.usar_ia = bool(usar_ia) and bool(settings.DEEPINFRA_API_KEY) and len(produtos) > 0
        # sobrescreve settings.DEEPINFRA_MODELO_RERANKER só pra essa instância
        # — seletor experimental do recálculo (main.py), restrito a uma conta.
        self._modelo_reranker = modelo_reranker

        # Índices reversos de códigos -> produto, para match exato O(1)
        self._idx_codigo: dict[str, list[int]] = {}
        for i, p in enumerate(produtos):
            for tipo, cod in p.codigos().items():
                if cod:
                    self._idx_codigo.setdefault(f"{tipo}:{cod}", []).append(i)

    def _reranker_scores(self, texto_item: str) -> list[float] | None:
        """Pontua TODO o catálogo contra o item numa chamada só (o reranker
        broadcasta 1 query contra N documentos). None se a IA estiver
        desligada/indisponível ou a chamada falhar — quem chama trata como
        "sem sinal disponível", nunca deixa a exceção subir."""
        if not self.usar_ia or not texto_item or not self._textos_prod:
            return None
        return _rerank(self._INSTRUCAO_RERANKER + texto_item, self._textos_prod,
                       api_key=settings.DEEPINFRA_API_KEY, modelo=self._modelo_reranker)

    def _score_item(self, item: ItemEdt, texto_item: str | None = None,
                    scores: list[float] | None = None) -> tuple[float, ProdutoCat | None, str]:
        texto_item = texto_item if texto_item is not None else item.texto()
        if scores is None:
            scores = self._reranker_scores(texto_item)

        # 1) código exato — vencedor automático SÓ com corroboração do
        # reranker (ver comentário no topo do arquivo). Continua checando os
        # OUTROS tipos de código antes de desistir: NCM sem corroboração mas
        # CATMAT do mesmo item com corroboração ainda vence.
        item_ncm = so_digitos(item.ncm)
        item_cat = so_digitos(item.catalogo_codigo)
        for chave, valor in (("ncm", item_ncm), ("catmat", item_cat), ("catser", item_cat)):
            if valor and f"{chave}:{valor}" in self._idx_codigo:
                idx = self._idx_codigo[f"{chave}:{valor}"][0]
                corroboracao = scores[idx] if scores else 0.0
                if corroboracao >= self._PISO_RERANKER_CODIGO_EXATO:
                    return 1.0, self.produtos[idx], f"código {chave.upper()} {valor}"

        # 2) melhor candidato pelo reranker
        if not scores:
            return 0.0, None, ""
        melhor_idx = max(range(len(scores)), key=lambda i: scores[i])
        melhor_sc = scores[melhor_idx]
        if melhor_sc <= 0:
            return 0.0, None, ""
        return melhor_sc, self.produtos[melhor_idx], self._MOTIVO_SEMELHANCA

    # ---- avalia um edital inteiro -----------------------------------------
    def avaliar(self, objeto: str, itens: list[ItemEdt]) -> ResultadoMatch:
        # Se o edital não trouxe itens detalhados, usa o objeto como um item único.
        alvos = itens if itens else [ItemEdt(numero=None, descricao=objeto or "")]

        scores_itens: list[float] = []
        detalhe: list[dict] = []
        compativeis = 0

        for it in alvos:
            texto_item = it.texto() or (objeto or "")
            scores = self._reranker_scores(texto_item)
            sc, prod, motivo = self._score_item(it, texto_item=texto_item, scores=scores)

            scores_itens.append(sc)
            # nível/score do EDITAL (agregado) continua exatamente o mesmo
            # cálculo de sempre — não passa a depender de confiança por item
            # nem de confirmação manual (isso é escopo separado, ver
            # "confianca"/"candidatos" logo abaixo).
            if sc >= settings.LIMIAR_ITEM:
                compativeis += 1

            # confiança POR ITEM + candidatas — cobre uma faixa mais ampla
            # que só "sc >= LIMIAR_ITEM": é a faixa (LIMIAR_ITEM_SUGESTAO até
            # LIMIAR_ITEM_ALTA) que a UI de "sugestão, confirme" existe pra
            # cobrir. Código NCM/CATMAT exato não é garantia de ser o mesmo
            # produto, por isso SEMPRE lista candidatas, mesmo pra item de
            # confiança alta — o usuário pode trocar mesmo esse caso.
            if sc < settings.LIMIAR_ITEM_SUGESTAO:
                continue

            # código exato só chega aqui como motivo quando JÁ teve
            # corroboração do reranker (ver _score_item) — sem corroboração
            # nenhuma, nem vira "vencedor" por código.
            codigo_exato = motivo.startswith("código ")
            if codigo_exato:
                confianca = "alta"
            elif sc >= settings.LIMIAR_ITEM_ALTA:
                # empate técnico com o 2º colocado = ambíguo mesmo com score
                # alto (achado real: "Grampo para Grampeador" x "Grampeador"
                # pontuaram 1.0 e 0.999 — a IA não tinha certeza de verdade,
                # só um dos dois precisava ganhar por decimais). Sem código
                # exato pra desempatar com um sinal diferente, é mais seguro
                # pedir confirmação do que confiar cego numa margem mínima.
                segundo_melhor = 0.0
                if scores:
                    for i_s, s in enumerate(scores):
                        if prod is not None and self.produtos[i_s].id == prod.id:
                            continue
                        if s > segundo_melhor:
                            segundo_melhor = s
                confianca = "media" if (sc - segundo_melhor) < settings.MARGEM_AMBIGUA_ITEM else "alta"
            else:
                confianca = "media"

            candidatos: list[dict] = []
            if scores:
                ordenados = sorted(range(len(scores)), key=lambda i: -scores[i])
                vistos: set[int] = set()
                if prod is not None:
                    candidatos.append({"produto_id": prod.id, "produto": prod.descricao,
                                       "score": round(sc, 3), "motivo": motivo})
                    vistos.add(prod.id)
                for i_c in ordenados:
                    # mesmo piso de "vale a pena mostrar" usado pra decidir se
                    # o ITEM entra na lista — sem isso, ruído do reranker em
                    # produtos sem relação nenhuma (nunca exatamente 0, ex.:
                    # 0.0014 num caso real testado) aparecia como "candidato",
                    # oferecendo trocar pra um produto claramente errado.
                    if scores[i_c] < settings.LIMIAR_ITEM_SUGESTAO:
                        break
                    p_c = self.produtos[i_c]
                    if p_c.id in vistos:
                        continue
                    candidatos.append({"produto_id": p_c.id, "produto": p_c.descricao,
                                       "score": round(scores[i_c], 3),
                                       "motivo": self._MOTIVO_SEMELHANCA})
                    vistos.add(p_c.id)
                    if len(candidatos) >= 3:
                        break

            detalhe.append({
                "item": it.numero,
                "descricao_item": it.descricao[:160],
                "produto_id": prod.id if prod else None,
                "produto": prod.descricao if prod else None,
                "score_item": round(sc, 3),
                "motivo": motivo,
                "confianca": confianca,
                "candidatos": candidatos,
                "confirmado_manualmente": False,
            })

        if not scores_itens:
            return ResultadoMatch(0.0, "fraco", 0, [])

        melhor_item = max(scores_itens)
        comp = [s for s in scores_itens if s >= settings.LIMIAR_ITEM]
        media_comp = sum(comp) / len(comp) if comp else 0.0
        fracao = compativeis / len(scores_itens)
        # O melhor item domina (é o lead mais forte), reforçado pela QUALIDADE
        # MÉDIA dos itens compatíveis; a fração entra com peso pequeno. Isso
        # evita que 1 item fraco num edital gigante infle o score, mas mantém
        # um casamento exato como lead forte (o que interessa a um fornecedor).
        score = melhor_item * 0.65 + media_comp * 0.25 + min(fracao, 1.0) * 0.10
        score = round(min(score, 1.0), 4)

        if score >= settings.LIMIAR_FORTE:
            nivel = "forte"
        elif score >= settings.LIMIAR_MEDIO:
            nivel = "medio"
        else:
            nivel = "fraco"

        # só é "forte" se cobrir uma fração mínima do edital — exceto quando o
        # melhor item é um casamento (quase) exato, que é lead forte por si só
        if (nivel == "forte" and melhor_item < 0.9
                and settings.FRACAO_MINIMA_FORTE > 0
                and fracao < settings.FRACAO_MINIMA_FORTE):
            nivel = "medio"

        detalhe.sort(key=lambda d: d["score_item"], reverse=True)
        return ResultadoMatch(score, nivel, compativeis, detalhe)


def aplicar_regras_exclusao(objeto: str, itens: list[ItemEdt],
                            termos: list[str], categoria_pncp: str | None,
                            categorias_excluidas: list[str]) -> bool:
    """Retorna True se o edital deve ser IGNORADO."""
    if categoria_pncp and categoria_pncp in categorias_excluidas:
        return True
    alvo = normalizar(objeto) + " " + " ".join(normalizar(i.descricao) for i in itens)
    for termo in termos:
        t = normalizar(termo)
        if t and t in alvo:
            return True
    return False
