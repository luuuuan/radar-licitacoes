"""
Motor de correspondência (matching).

Estratégia em camadas, da mais forte para a mais fraca:

1. Correspondência EXATA de código (NCM, CATMAT/CATSER, EAN) entre um produto
   do catálogo e um item do edital. Quando bate, é o sinal mais confiável.
2. Similaridade TEXTUAL por TF-IDF + cosseno entre a descrição/keywords do
   produto e a descrição do item.
3. Reforço por fuzzy matching de palavras-chave (rapidfuzz) para pegar
   variações de grafia.

Cada item do edital recebe o melhor score contra o catálogo. O edital recebe
um score agregado e um nível: fraco | medio | forte.

Funciona 100% sem GPU e sem baixar modelos. Para busca semântica real
(embeddings), veja matching/embeddings.py e o README.
"""
from __future__ import annotations
import re
import unicodedata
from dataclasses import dataclass, field

from rapidfuzz import fuzz
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from ..config import settings
from .embeddings import embeddings as _ia_embeddings, cosseno as _ia_cosseno, ia_disponivel


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

    def texto_busca(self) -> str:
        return normalizar(f"{self.descricao} {self.palavras_chave or ''}")

    def codigos(self) -> dict[str, str]:
        return {
            "ncm": so_digitos(self.ncm),
            "ean": so_digitos(self.ean),
            "catmat": so_digitos(self.catmat),
            "catser": so_digitos(self.catser),
        }

    def keywords(self) -> list[str]:
        return [normalizar(k) for k in (self.palavras_chave or "").split(",") if k.strip()]


@dataclass
class ItemEdt:
    numero: int | None
    descricao: str
    ncm: str = ""
    catalogo_codigo: str = ""  # CATMAT/CATSER

    def texto_busca(self) -> str:
        return normalizar(self.descricao)


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
    def __init__(self, produtos: list[ProdutoCat], usar_ia: bool = False):
        self.produtos = produtos
        self.usar_ia = bool(usar_ia) and ia_disponivel() and len(produtos) > 0
        self._prod_emb = None  # embeddings dos produtos (gerados sob demanda)
        self._textos_prod = [p.texto_busca() for p in produtos]
        self._vectorizer = None
        self._matriz_prod = None
        if any(self._textos_prod):
            # ngram de caracteres ajuda com termos técnicos/variações
            self._vectorizer = TfidfVectorizer(
                ngram_range=(1, 2), min_df=1, sublinear_tf=True
            )
            try:
                self._matriz_prod = self._vectorizer.fit_transform(self._textos_prod)
            except ValueError:
                self._vectorizer = None

        # Índices reversos de códigos -> produto, para match exato O(1)
        self._idx_codigo: dict[str, list[int]] = {}
        for i, p in enumerate(produtos):
            for tipo, cod in p.codigos().items():
                if cod:
                    self._idx_codigo.setdefault(f"{tipo}:{cod}", []).append(i)

    # ---- score de um único item do edital contra todo o catálogo ----------
    def _score_item(self, item: ItemEdt) -> tuple[float, ProdutoCat | None, str]:
        melhor = 0.0
        melhor_prod: ProdutoCat | None = None
        motivo = ""

        # 1) match exato de código (sinal mais forte)
        item_ncm = so_digitos(item.ncm)
        item_cat = so_digitos(item.catalogo_codigo)
        for chave, valor in (("ncm", item_ncm), ("catmat", item_cat), ("catser", item_cat)):
            if valor and f"{chave}:{valor}" in self._idx_codigo:
                idx = self._idx_codigo[f"{chave}:{valor}"][0]
                return 1.0, self.produtos[idx], f"código {chave.upper()} {valor}"

        texto_item = item.texto_busca()
        if not texto_item:
            return melhor, melhor_prod, motivo

        # 2) similaridade TF-IDF
        if self._vectorizer is not None and self._matriz_prod is not None:
            vec = self._vectorizer.transform([texto_item])
            sims = cosine_similarity(vec, self._matriz_prod)[0]
            j = int(sims.argmax())
            if sims[j] > melhor:
                melhor = float(sims[j])
                melhor_prod = self.produtos[j]
                motivo = "similaridade textual"

        # 3) reforço por palavra-chave (fuzzy) — pega o que o TF-IDF perde
        for p in self.produtos:
            for kw in p.keywords():
                if not kw:
                    continue
                if kw in texto_item:
                    sc = 0.75
                else:
                    sc = fuzz.token_set_ratio(kw, texto_item) / 100.0 * 0.7
                if sc > melhor:
                    melhor = sc
                    melhor_prod = p
                    motivo = f"palavra-chave '{kw}'"

        return melhor, melhor_prod, motivo

    # ---- avalia um edital inteiro -----------------------------------------
    def _emb_produtos(self):
        if self._prod_emb is None:
            self._prod_emb = _ia_embeddings(self._textos_prod)
        return self._prod_emb

    def _ia_score_item(self, item_emb) -> tuple[float, ProdutoCat | None]:
        """Melhor similaridade semântica do item contra os produtos (reescalada)."""
        if not item_emb:
            return 0.0, None
        melhor, prod = 0.0, None
        for j, pe in enumerate(self._emb_produtos()):
            if not pe:
                continue
            c = _ia_cosseno(item_emb, pe)
            if c > melhor:
                melhor, prod = c, self.produtos[j]
        # reescala: abaixo do piso vira 0; piso..1 -> 0..1
        floor = settings.IA_FLOOR
        norm = max(0.0, (melhor - floor) / (1.0 - floor)) if melhor > floor else 0.0
        return norm, prod

    def avaliar(self, objeto: str, itens: list[ItemEdt]) -> ResultadoMatch:
        # Se o edital não trouxe itens detalhados, usa o objeto como um item único.
        alvos = itens if itens else [ItemEdt(numero=None, descricao=objeto or "")]

        # embeddings dos itens em lote (1 chamada), só se a IA estiver ligada
        item_embs = [None] * len(alvos)
        if self.usar_ia:
            textos_itens = [(it.texto_busca() or normalizar(objeto or "")) for it in alvos]
            item_embs = _ia_embeddings(textos_itens)

        scores_itens: list[float] = []
        detalhe: list[dict] = []
        compativeis = 0

        for idx, it in enumerate(alvos):
            sc, prod, motivo = self._score_item(it)

            # reforço pela IA semântica
            if self.usar_ia and item_embs[idx]:
                ia_sc, ia_prod = self._ia_score_item(item_embs[idx])
                w = settings.IA_PESO
                combinado = sc * (1 - w) + ia_sc * w
                if ia_sc > sc and ia_prod is not None:
                    prod = ia_prod
                    motivo = f"semelhança IA ({round(ia_sc, 2)})"
                elif ia_sc > 0 and motivo:
                    motivo = f"{motivo} + IA"
                sc = combinado

            scores_itens.append(sc)
            if sc >= settings.LIMIAR_ITEM:
                compativeis += 1
                detalhe.append({
                    "item": it.numero,
                    "descricao_item": it.descricao[:160],
                    "produto_id": prod.id if prod else None,
                    "produto": prod.descricao if prod else None,
                    "score_item": round(sc, 3),
                    "motivo": motivo,
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
