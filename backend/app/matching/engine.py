"""
Motor de correspondência (matching).

Estratégia em camadas, da mais forte para a mais fraca:

1. Correspondência EXATA de código (NCM, CATMAT/CATSER, EAN) entre um produto
   do catálogo e um item do edital. Quando bate, é o sinal mais confiável.
2. Similaridade TEXTUAL por TF-IDF + cosseno entre a descrição/keywords do
   produto e a descrição do item — o texto passa antes por sinônimos de
   domínio (sinonimos.py) e stemming (stemming.py), pra "notebook"/
   "computador portátil" e "caneta"/"canetas" contarem como o mesmo termo.
3. Reforço por fuzzy matching de palavras-chave (rapidfuzz) para pegar
   variações de grafia.

Cada item do edital recebe o melhor score contra o catálogo. O edital recebe
um score agregado e um nível: fraco | medio | forte.

Funciona 100% sem GPU e sem baixar modelos (o stemmer Snowball do nltk é
código puro, sem download). Para busca semântica real (embeddings), veja
matching/embeddings.py e o README.
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
from .sinonimos import aplicar_sinonimos
from .stemming import radical, stemizar_texto


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


def preparar_natural(texto: str | None) -> str:
    """Normaliza e expande sinônimos, SEM stemming — usado para texto que
    ainda precisa parecer linguagem natural (ex.: enviado à IA semântica,
    que já entende sinônimo/flexão sozinha e piora com radicais truncados
    tipo "comput")."""
    return aplicar_sinonimos(normalizar(texto))


def preparar(texto: str | None) -> str:
    """Pipeline completo usado para comparar textos no matching TEXTUAL
    local (TF-IDF/keyword): normaliza, expande sinônimos/abreviações de
    domínio e aplica stemming. Mais agressivo que preparar_natural() — não
    usar para texto mandado à IA nem em regra de exclusão (onde o termo deve
    casar literalmente com o que o usuário digitou)."""
    return stemizar_texto(preparar_natural(texto))


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
        return preparar(f"{self.descricao} {self.palavras_chave or ''}")

    def texto_natural(self) -> str:
        """Sem stemming — para mandar à IA semântica (embeddings)."""
        return preparar_natural(f"{self.descricao} {self.palavras_chave or ''}")

    def codigos(self) -> dict[str, str]:
        return {
            "ncm": so_digitos(self.ncm),
            "ean": so_digitos(self.ean),
            "catmat": so_digitos(self.catmat),
            "catser": so_digitos(self.catser),
        }

    def keywords(self) -> list[str]:
        return [preparar(k) for k in (self.palavras_chave or "").split(",") if k.strip()]


@dataclass
class ItemEdt:
    numero: int | None
    descricao: str
    ncm: str = ""
    catalogo_codigo: str = ""  # CATMAT/CATSER

    def texto_busca(self) -> str:
        return preparar(self.descricao)

    def texto_natural(self) -> str:
        """Sem stemming — para mandar à IA semântica (embeddings)."""
        return preparar_natural(self.descricao)


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
    def __init__(self, produtos: list[ProdutoCat], usar_ia: bool = False,
                 gemini_key: str | None = None):
        self.produtos = produtos
        self.gemini_key = gemini_key
        self.usar_ia = bool(usar_ia) and ia_disponivel(gemini_key) and len(produtos) > 0
        # orçamento de exploração de sinônimos por coleta (editais sem sinal textual)
        self._orcamento_exploracao = (
            settings.IA_ORCAMENTO_EXPLORACAO
            if (self.usar_ia and settings.IA_EXPLORAR_SEM_SINAL) else 0)
        self._prod_emb = None  # embeddings dos produtos (gerados sob demanda)
        self._textos_prod = [p.texto_busca() for p in produtos]         # stemizado, p/ TF-IDF
        self._textos_prod_naturais = [p.texto_natural() for p in produtos]  # p/ IA
        # pré-computa palavras-chave (normalizar+sinônimos+stemming) UMA VEZ por
        # produto — sem isso, _melhor_por_keywords reprocessaria o catálogo
        # inteiro a cada item de cada edital, o que pesa muito com bases grandes
        # (milhares de editais x itens x produtos).
        self._keywords_prod = [p.keywords() for p in produtos]
        self._prod_indice = {id(p): i for i, p in enumerate(produtos)}
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

    def _distintivo(self, token: str) -> bool:
        """True se o token pode contar como termo distintivo em comum (não é
        genérico, stopword, unidade de medida ou número solto). Os tokens
        aqui já passaram por stemming (via texto_busca/preparar), por isso a
        checagem usa as versões stemizadas das listas de exclusão."""
        return (len(token) >= 2 and not token.isdigit()
                and token not in self._GENERICAS_RAD
                and token not in self._STOPWORDS_RAD
                and token not in self._UNIDADES_RAD)

    def _tfidf_lote(self, textos: list[str]):
        """Similaridade TF-IDF de cada item do LOTE contra TODO o catálogo,
        numa chamada só ao vectorizer/cosine (em vez de uma por item). O
        sklearn faz validação de entrada (check_array) a cada chamada —
        repetir isso item a item numa base com milhares de editais domina o
        tempo à toa. Retorna a matriz completa (linha i = similaridades do
        item i contra cada produto) — usada tanto pra decidir o vencedor
        quanto pra ranquear as candidatas de cada item (ver _pontuar_produtos);
        None se não há vectorizer (catálogo vazio)."""
        if self._vectorizer is None or self._matriz_prod is None or not textos:
            return None
        vecs = self._vectorizer.transform(textos)
        return cosine_similarity(vecs, self._matriz_prod)

    # Piso de similaridade textual (cosseno TF-IDF do texto INTEIRO contra o
    # candidato ESPECÍFICO da palavra-chave, não contra o melhor de qualquer
    # candidato) exigido pra um match de palavra-chave ISOLADA (0-1 termo
    # específico) virar o candidato escolhido. Calibrado com pares REAIS de
    # produção: candidatos ERRADOS de 1-palavra (ex.: "Ribbon" x "Bobina
    # Térmica" só por "térmica"; "papel"/"metal"/"kraft" batendo em produtos
    # completamente diferentes) sempre ficavam < 0.30; candidatos genuínos
    # (ex.: "clipe" x "Clips Galvanizado") sempre ficavam > 0.34 — 0.30 corta
    # exatamente nesse intervalo.
    _PISO_TFIDF_PRA_PALAVRA_ISOLADA = 0.30

    # Piso de similaridade textual exigido pra um match de CÓDIGO EXATO
    # (NCM/CATMAT/CATSER) virar confiança "alta" automática, em vez de
    # "média" (pedindo confirmação). Achado real em produção: item "Álcool
    # Etílico ... gel ... 70% v/v" casou por NCM idêntico com "Desinfetante
    # 5 litros Lavanda" — mesmo código fiscal (categoria ampla de produto de
    # limpeza/higiene), produtos completamente diferentes, cosseno TF-IDF
    # entre os dois textos ~0. Bem mais baixo que o piso de palavra isolada
    # (0.30) porque código exato já é um sinal bem mais forte por si só — só
    # queremos barrar o caso de ZERO relação textual nenhuma, não exigir
    # semelhança forte.
    _PISO_TFIDF_CODIGO_EXATO = 0.15

    def _sims_item(self, texto_item: str, sims_row=None):
        """Linha de similaridade TF-IDF do item contra cada produto do
        catálogo — usa a linha já calculada em lote (sims_row, vinda de
        _tfidf_lote) quando disponível, senão calcula avulso (uso isolado,
        ex. testes)."""
        if sims_row is not None:
            return sims_row
        if self._vectorizer is not None and self._matriz_prod is not None:
            vec_item = self._vectorizer.transform([texto_item])
            return cosine_similarity(vec_item, self._matriz_prod)[0]
        return [0.0] * len(self.produtos)

    def _pontuar_produtos(self, texto_item: str, sims_row=None) -> list[tuple[float, int, str]]:
        """Pontua TODOS os produtos do catálogo contra o texto do item (não
        só o melhor) — mesma regra de sempre, aplicada a CADA candidato, não
        só ao vencedor:
        - similaridade TF-IDF do texto inteiro como base;
        - palavra-chave substitui quando: 2+ termos específicos (confiança
          alta o bastante pra dispensar corroboração), OU 0-1 termo mas a
          similaridade TF-IDF DESSE MESMO produto já corrobora (piso
          _PISO_TFIDF_PRA_PALAVRA_ISOLADA — achado real: comparar contra o
          melhor candidato QUALQUER, em vez do candidato específico da
          keyword, deixava um candidato errado vencer só por "existir algo
          razoável no catálogo em algum lugar", não por esse candidato em
          particular fazer sentido — ver histórico no commit que introduziu
          isso, "Perfurador Papel" x "Grampeador Metal");
        - anti-coincidência: casamento em só 1 palavra distintiva (ou <10%
          proporcional) nunca vale mais que 0.34, mesmo com TF-IDF/keyword
          score alto (ex.: "Pasta L" vs. item odontológico de "pasta").

        Usada tanto pra decidir o vencedor (resultado[0], ver _score_item)
        quanto pra listar candidatas (resultado[:N]) que o usuário pode
        escolher — inclusive pra item de confiança alta, já que código
        NCM/CATMAT exato não é garantia de ser o mesmo produto (código
        fiscal é amplo; achado real de código batendo com item sem nada a
        ver com o pedido). Retorna [(score, índice_do_produto, motivo), ...]
        só com score > 0, ordenado do maior pro menor."""
        sims = self._sims_item(texto_item, sims_row)
        kw_todos = self._pontuar_keywords_todos(texto_item)
        toks_item = {t for t in texto_item.split() if self._distintivo(t)}

        resultado = []
        for i in range(len(self.produtos)):
            sim_tfidf = float(sims[i])
            sc, motivo = sim_tfidf, "similaridade textual"
            kw = kw_todos.get(i)
            if kw:
                sc_kw, motivo_kw, n_kw = kw
                if sc_kw > sc and (n_kw >= 2 or sim_tfidf >= self._PISO_TFIDF_PRA_PALAVRA_ISOLADA):
                    sc, motivo = sc_kw, motivo_kw
            if sc <= 0:
                continue

            # Anti-coincidência (mesmo raciocínio de sempre, aplicado por
            # candidato agora — não só no vencedor): casamento apoiado em UMA
            # única palavra distintiva em comum (ex.: "papel" entre "Papel A4"
            # e "fragmentadora de papel") nunca é sinal de confiança, mesmo
            # com score alto (ex.: TF-IDF de "Pasta L" vs. item odontológico
            # de "pasta" batendo 1.0 só por causa dessa palavra).
            texto_prod = self._textos_prod[i]
            toks_prod = {t for t in texto_prod.split() if self._distintivo(t)}
            comuns = toks_item & toks_prod
            # item "kit"/"conjunto" com dezenas de peças enumeradas pode ter
            # 2-3 termos batendo por PURA COINCIDÊNCIA — 2 em mais de 100 é
            # proporcionalmente nada. Corte por PROPORÇÃO além do absoluto.
            maior_lado = max(len(toks_item), len(toks_prod), 1)
            fraco = len(comuns) <= 1 or (len(comuns) / maior_lado) < 0.10
            if fraco and sc > 0.34:
                sc = 0.34
                motivo = f"só {len(comuns)} termo(s) em comum de {maior_lado} — fraco"

            resultado.append((sc, i, motivo))

        resultado.sort(key=lambda x: -x[0])
        return resultado

    # ---- score de um único item do edital contra todo o catálogo ----------
    def _score_item(self, item: ItemEdt, texto_busca: str | None = None,
                    sims_row=None) -> tuple[float, ProdutoCat | None, str]:
        texto_item = texto_busca if texto_busca is not None else item.texto_busca()

        # 1) match exato de código (sinal mais forte — mas não infalível: NCM
        # é uma classificação FISCAL, ampla, não garante ser o MESMO produto
        # físico. Continua sendo o melhor palpite disponível mesmo sem
        # corroboração textual nenhuma (motivo "código X"), mas só vira
        # confiança "alta" automática (ver avaliar()) quando o texto também
        # corrobora nem que seja fracamente — ver _PISO_TFIDF_CODIGO_EXATO.
        item_ncm = so_digitos(item.ncm)
        item_cat = so_digitos(item.catalogo_codigo)
        for chave, valor in (("ncm", item_ncm), ("catmat", item_cat), ("catser", item_cat)):
            if valor and f"{chave}:{valor}" in self._idx_codigo:
                idx = self._idx_codigo[f"{chave}:{valor}"][0]
                sim = float(self._sims_item(texto_item, sims_row)[idx]) if texto_item else 0.0
                motivo = f"código {chave.upper()} {valor}"
                if sim < self._PISO_TFIDF_CODIGO_EXATO:
                    motivo += " (sem sinal textual — confirme)"
                return 1.0, self.produtos[idx], motivo

        if not texto_item:
            return 0.0, None, ""

        resultado = self._pontuar_produtos(texto_item, sims_row)
        if not resultado:
            return 0.0, None, ""
        sc, idx, motivo = resultado[0]
        return sc, self.produtos[idx], motivo

    # palavras genéricas demais para casar sozinhas (embalagem/quantidade/etc.)
    _GENERICAS = {
        "kit", "kits", "caixa", "caixas", "cx", "unidade", "unidades", "und", "un",
        "material", "materiais", "conjunto", "conjuntos", "pacote", "pacotes", "pct",
        "peca", "pecas", "item", "itens", "produto", "produtos", "jogo", "jogos",
        "par", "pares", "embalagem", "tipo", "modelo", "diversos", "geral", "linha",
        "aquisicao", "servico", "servicos", "fornecimento", "tamanho",
        # atributos descritivos genéricos demais (não identificam O QUE é o
        # produto, só uma característica dele — ex.: "cor"/"cores" bate tanto
        # em "Lápis de Cor" quanto em "resina odontológica NAS CORES A3,A2",
        # produtos completamente diferentes que só compartilham o atributo)
        "cor", "cores", "bloco", "blocos",
        # "papel" sozinho: bug real em produção — item "Papel Não Clorado"
        # (papel sulfite comum) casava por 1 palavra-chave ("papel") com
        # "Papel Photo 135g Glossy Adesivo" (produto completamente diferente)
        # com o MESMO score de vários "Papel A4 75g Sulfite" corretos também
        # cadastrados — a única palavra em comum sendo "papel" (presente em
        # QUALQUER produto de papel, sulfite/kraft/foto/térmico...) faz o
        # empate ser desfeito por ordem arbitrária no catálogo, não por
        # relevância. Sem "papel" nessa lista o score cai abaixo de
        # LIMIAR_ITEM e cede lugar à similaridade textual (TF-IDF), que
        # naturalmente prefere o produto certo.
        "papel",
    }
    # palavras de ligação (preposições/artigos/conjunções) — não contam como "termo
    # em comum" na checagem de anti-coincidência, senão inflam a contagem sem
    # nenhum sinal real (ex.: "suporte PARA notebook COM cooler" vs "notebook COM
    # bateria" bateriam em "com"/"para" e escapariam da proteção de 1-só-termo).
    _STOPWORDS = {
        "de", "da", "do", "das", "dos", "para", "com", "sem", "em", "e", "ou",
        "a", "o", "as", "os", "um", "uma", "uns", "umas", "no", "na", "nos", "nas",
        "por", "ao", "aos", "que", "se",
    }
    # unidades de medida — pelo mesmo motivo das stopwords: "30" e "cm" aparecem
    # em qualquer item com dimensão (moldura, régua, tecido, cano...), então não
    # são sinal de que dois produtos são parecidos, só de que ambos têm tamanho.
    _UNIDADES = {
        "cm", "mm", "m", "km", "kg", "g", "gr", "mg", "ml", "l", "lt",
        "litro", "litros", "un", "und", "unid", "cx", "pct", "m2", "m3",
        "cm2", "cm3", "pol", "polegada", "polegadas",
    }

    # versões stemizadas das listas acima — o texto comparado nas checagens
    # (_distintivo, _melhor_por_keywords) já passa por preparar()/radical(),
    # então as listas de exclusão precisam do mesmo tratamento pra continuar
    # batendo (ex.: "unidades" vira "unidad"). Como as listas originais já
    # trazem singular E plural, o conjunto resultante cobre as duas formas
    # mesmo quando o stemmer as reduz de jeitos diferentes.
    _GENERICAS_RAD = {radical(w) for w in _GENERICAS}
    _STOPWORDS_RAD = {radical(w) for w in _STOPWORDS}
    _UNIDADES_RAD = {radical(w) for w in _UNIDADES}

    def _pontuar_keywords_todos(self, texto_item: str) -> dict[int, tuple[float, str, int]]:
        """Avalia TODO o catálogo (não só o melhor) contra o texto do item
        somando palavras-chave que casam. Retorna {índice_do_produto:
        (score, motivo, n_específicas), ...} só dos produtos com algum
        termo batendo — o 3º campo da tupla (quantas palavras-chave
        ESPECÍFICAS bateram, sem contar genéricas) é usado por
        _pontuar_produtos pra decidir se esse sinal é forte o bastante pra
        dispensar corroboração da similaridade textual."""
        resultado: dict[int, tuple[float, str, int]] = {}
        for idx_p, (p, kws) in enumerate(zip(self.produtos, self._keywords_prod)):
            especificas, genericas = [], 0
            for kw in kws:
                if not kw or len(kw) < 2:
                    continue
                casou = kw in texto_item
                if not casou:
                    # fuzzy só conta se for praticamente igual (variação de grafia)
                    if fuzz.token_set_ratio(kw, texto_item) / 100.0 >= 0.92:
                        casou = True
                if not casou:
                    continue
                if kw in self._GENERICAS_RAD:
                    genericas += 1
                else:
                    especificas.append(kw)

            # catálogo "enriquecido" (por IA ou manual) tende a cadastrar o
            # MESMO conceito em várias granularidades sobrepostas — ex.:
            # "papel sulfite a4", "papel sulfite", "sulfite" e "papel" como
            # entradas separadas de palavras_chave do mesmo produto. Sem
            # isso, um item que menciona "papel sulfite" bate nas 4 e conta
            # como 4 termos específicos (infla pro tier "3+ = forte"), quando
            # é o MESMO sinal contado repetidas vezes. Mantém só a mais
            # específica (a que não é substring de nenhuma outra que bateu).
            #
            # Primeiro remove duplicata EXATA pós-stem: "grampo" e "grampos"
            # (singular/plural, entradas diferentes nas palavras_chave) viram
            # o mesmo radical "gramp" — sem isso um item que só menciona
            # "grampo" de passagem (ex.: "caderno... ACABAMENTO GRAMPO") bate
            # nas duas e conta como 2 termos em vez de 1.
            especificas = list(dict.fromkeys(especificas))
            especificas = [kw for kw in especificas
                          if not any(kw != outra and kw in outra for outra in especificas)]

            n = len(especificas)
            if n == 0 and genericas == 0:
                continue
            if n == 0:
                sc = 0.20                       # só genéricas -> bem fraco
            elif n == 1:
                sc = 0.35                       # 1 palavra isolada -> fraco
            elif n == 2:
                sc = 0.52                       # 2 palavras -> médio
            else:
                sc = 0.66                       # 3+ palavras -> forte
            if n >= 1 and genericas:
                sc = min(sc + 0.05, 0.90)       # genéricas só reforçam se houver específica

            if n == 0:
                motivo = "termos genéricos (fraco)"
            elif n == 1:
                motivo = f"palavra-chave '{especificas[0]}'"
            else:
                motivo = f"{n} palavras-chave ({', '.join(especificas[:3])})"

            resultado[idx_p] = (sc, motivo, n)
        return resultado

    def _melhor_por_keywords(self, texto_item: str):
        """Retorna (score, produto, motivo, n_especificas) do MELHOR
        candidato por palavra-chave, ou None — ver _pontuar_keywords_todos
        pro catálogo inteiro (usado pra ranquear candidatas, não só achar a
        melhor)."""
        todos = self._pontuar_keywords_todos(texto_item)
        if not todos:
            return None
        idx_p, (sc, motivo, n) = max(todos.items(), key=lambda kv: kv[1][0])
        return (sc, self.produtos[idx_p], motivo, n)

    # ---- avalia um edital inteiro -----------------------------------------
    def _emb_produtos(self):
        if self._prod_emb is None:
            self._prod_emb = _ia_embeddings(self._textos_prod_naturais, api_key=self.gemini_key)
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

        # 1) Score TEXTUAL primeiro (grátis): é a peneira que decide se vale IA.
        #    texto_busca() e o TF-IDF são calculados em LOTE (todos os itens do
        #    edital de uma vez) em vez de item a item — editais com dezenas/
        #    centenas de itens pagavam a validação de entrada do sklearn a cada
        #    chamada individual, o que domina o tempo numa base grande.
        textos_alvos = [it.texto_busca() for it in alvos]
        tfidf_lote = self._tfidf_lote(textos_alvos)   # matriz completa ou None
        base = [self._score_item(it, texto_busca=textos_alvos[i],
                                 sims_row=(tfidf_lote[i] if tfidf_lote is not None else None))
               for i, it in enumerate(alvos)]   # (sc, prod, motivo)
        max_txt = max((b[0] for b in base), default=0.0)

        # 2) Quando rodar a IA semântica:
        #    a) edital COM sinal textual (>= IA_MIN_SINAL): refina o candidato.
        #    b) edital SEM sinal (texto ~0): pode ser sinônimo puro que o texto não
        #       pega ("notebook" vs "computador portátil"). Roda IA mesmo assim,
        #       mas só enquanto houver orçamento de exploração nesta coleta.
        tem_sinal = max_txt >= settings.IA_MIN_SINAL
        usar_ia_aqui = False
        if self.usar_ia:
            if tem_sinal:
                usar_ia_aqui = True
            elif self._orcamento_exploracao > 0:
                usar_ia_aqui = True
                self._orcamento_exploracao -= 1
        item_embs = [None] * len(alvos)
        if usar_ia_aqui:
            idxs = [i for i, (sc, _, _) in enumerate(base) if sc < settings.LIMIAR_FORTE]
            textos = [(alvos[i].texto_natural() or preparar_natural(objeto or "")) for i in idxs]
            embs = _ia_embeddings(textos, api_key=self.gemini_key)
            for k, i in enumerate(idxs):
                item_embs[i] = embs[k]

        scores_itens: list[float] = []
        detalhe: list[dict] = []
        compativeis = 0

        for idx, it in enumerate(alvos):
            sc, prod, motivo = base[idx]

            # reforço pela IA semântica (quando aplicável)
            if usar_ia_aqui and item_embs[idx]:
                ia_sc, ia_prod = self._ia_score_item(item_embs[idx])
                # ia_sc == 0 é "sem opinião" (cosseno abaixo do IA_FLOOR), não
                # "sinal negativo" — só combina quando a IA realmente confirmou
                # alguma semelhança, senão um match textual bom seria punido
                # por falta de sinal em vez de por sinal contrário.
                if ia_sc > 0:
                    w = settings.IA_PESO
                    combinado = sc * (1 - w) + ia_sc * w
                    if ia_sc > sc and ia_prod is not None:
                        prod = ia_prod
                        motivo = f"semelhança IA ({round(ia_sc, 2)})"
                    elif motivo:
                        motivo = f"{motivo} + IA"
                    sc = combinado

            scores_itens.append(sc)
            # nível/score do EDITAL (agregado) continua exatamente como
            # sempre foi, direto do motor — não passa a depender de
            # confiança por item nem de confirmação manual (isso é escopo
            # separado, ver "confianca"/"candidatos" logo abaixo).
            if sc >= settings.LIMIAR_ITEM:
                compativeis += 1

            # confiança POR ITEM + candidatas — cobre uma faixa mais ampla
            # que só "sc >= LIMIAR_ITEM": é justamente a faixa mais baixa
            # (LIMIAR_ITEM_SUGESTAO até LIMIAR_ITEM_ALTA) que a UI de
            # "sugestão, confirme" existe pra cobrir, em vez de simplesmente
            # não mostrar nada como antes. Código NCM/CATMAT exato não é
            # garantia de ser o mesmo produto (código fiscal é amplo — achado
            # real de código batendo com item sem nada a ver com o pedido),
            # por isso SEMPRE lista candidatas, mesmo pra item de confiança
            # alta — o usuário pode trocar mesmo esse caso.
            if sc < settings.LIMIAR_ITEM_SUGESTAO:
                continue

            # "(sem sinal textual...)" = código bateu mas o texto não corrobora
            # nem fracamente (ver _score_item) — não confia cego, pede
            # confirmação, MESMO que sc seja 1.0 (score de código exato é
            # fixo em 1.0 de propósito, pra não mexer no score/nível agregado
            # do edital — por isso não pode entrar na comparação normal
            # `sc >= LIMIAR_ITEM_ALTA`, senão o próprio 1.0 destrava "alta"
            # de novo por trás, ignorando a falta de corroboração textual).
            codigo_exato = motivo.startswith("código ")
            codigo_sem_corroboracao = codigo_exato and "sem sinal textual" in motivo
            if codigo_sem_corroboracao:
                confianca = "media"
            else:
                confianca = "alta" if (codigo_exato or sc >= settings.LIMIAR_ITEM_ALTA) else "media"

            sims_row_i = tfidf_lote[idx] if tfidf_lote is not None else None
            candidatos_raw = self._pontuar_produtos(textos_alvos[idx], sims_row_i)
            candidatos: list[dict] = []
            vistos: set[int] = set()
            if prod is not None:
                candidatos.append({"produto_id": prod.id, "produto": prod.descricao,
                                   "score": round(sc, 3), "motivo": motivo})
                vistos.add(prod.id)
            for s_c, i_c, m_c in candidatos_raw:
                p_c = self.produtos[i_c]
                if p_c.id in vistos:
                    continue
                candidatos.append({"produto_id": p_c.id, "produto": p_c.descricao,
                                   "score": round(s_c, 3), "motivo": m_c})
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
