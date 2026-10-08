"""
Busca tolerante por palavras (catálogo de produtos e itens de edital).

Regras (mesmas no front -- ver buscaFiltrar em static/index.html):
- ignora acento/maiúscula; "75g" == "75 g"; "c/12" == "c 12"; "a-4" == "a4"
- cada palavra digitada precisa aparecer no texto, em qualquer ordem (AND)
- palavra comum casa por INÍCIO de palavra ("caneta" acha "canetas"; nunca
  "macaneta"); plural digitado é reduzido ao radical ("cores" -> "cor")
- número casa exato ("12" não casa "120"); número+unidade ("75 g") casa junto
- stopwords (a, com, de...) e unidade de contagem junto de número (12 un)
  são ignoradas
- sinônimos/abreviações vêm de busca_sinonimos.json (cx == caixa ...)
- fuzzy (erro de digitação) só é usado quando a busca estrita não acha nada
"""
import json
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

_CFG_PATH = Path(__file__).with_name("busca_sinonimos.json")


@lru_cache(maxsize=1)
def carregar_config() -> dict:
    with open(_CFG_PATH, encoding="utf-8") as f:
        return json.load(f)


def normalizar(texto: str | None) -> str:
    if not texto:
        return ""
    t = unicodedata.normalize("NFKD", texto.lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r"(?<=\d)[.,](?=\d)", ".", t)      # 0,7 -> 0.7
    t = re.sub(r"\b([a-z])-(?=\d)", r"\1", t)     # a-4 -> a4
    t = re.sub(r"(\d)([a-z])", r"\1 \2", t)       # 75g -> 75 g
    t = re.sub(r"[^a-z0-9.\s]", " ", t)
    t = re.sub(r"(?<!\d)\.|\.(?!\d)", " ", t)
    return re.sub(r"\s+", " ", t).strip()


_RE_NUM = re.compile(r"^\d+(\.\d+)*$")


@dataclass(frozen=True)
class Tok:
    tipo: str                 # "palavra" | "numero" | "medida"
    texto: str                # palavra/número; na medida, o número
    unidade: str = ""         # só medida
    prefixo: bool = True      # palavra: início de palavra (True) ou formas exatas
    formas: tuple = ()        # palavra com prefixo=False: formas aceitas


def radical(p: str) -> str:
    """Reduz plural comum ao radical ("canetas"->"caneta", "cores"->"cor")."""
    if len(p) <= 3 or _RE_NUM.match(p):
        return p
    for suf, rep in (("oes", "ao"), ("aes", "ao"), ("eis", "el"), ("ais", "al"),
                     ("uis", "ul"), ("ois", "ol")):
        if p.endswith(suf) and len(p) > len(suf) + 1:
            return p[: -len(suf)] + rep
    if p.endswith(("res", "zes")) and len(p) > 4:
        return p[:-2]
    if p.endswith("s") and not p.endswith(("ss", "is", "us")):
        return p[:-1]
    return p


def _tok_palavra(p: str) -> Tok:
    r = radical(p)
    if r == p or len(r) >= 5:
        return Tok("palavra", r, prefixo=True)
    # radical curto ("cor"): evita casar "corretivo" -- só as formas exatas
    return Tok("palavra", r, prefixo=False, formas=tuple({p, r, r + "s", r + "es"}))


def tokenizar(termo: str | None) -> list[Tok]:
    cfg = carregar_config()
    stop = set(cfg["stopwords"])
    contagem = set(cfg["unidades_contagem"])
    medidas = set(cfg["unidades_medida"])
    ws = normalizar(termo).split()
    tem_num = any(_RE_NUM.match(w) for w in ws)
    toks: list[Tok] = []
    i = 0
    while i < len(ws):
        w = ws[i]
        if _RE_NUM.match(w):
            if i + 1 < len(ws) and ws[i + 1] in medidas:
                toks.append(Tok("medida", w, unidade=ws[i + 1]))
                i += 2
                continue
            toks.append(Tok("numero", w))
        elif w in stop or (tem_num and w in contagem):
            pass
        else:
            toks.append(_tok_palavra(w))
        i += 1
    if not toks and ws:                       # só stopwords: usa o que foi digitado
        toks = [_tok_palavra(w) for w in ws]
    return toks


@lru_cache(maxsize=1)
def _indice_sinonimos() -> dict[str, list[list[str]]]:
    """palavra normalizada (e seu radical) -> alternativas (cada uma, lista de palavras)."""
    idx: dict[str, list[list[str]]] = {}
    for grupo in carregar_config()["sinonimos"]:
        membros = [normalizar(m).split() for m in grupo]
        for m in membros:
            if len(m) == 1:
                for chave in {m[0], radical(m[0])}:
                    idx.setdefault(chave, [])
                    for alt in membros:
                        if alt not in idx[chave]:
                            idx[chave].append(alt)
    return idx


def alternativas(tok: Tok) -> list[list[Tok]]:
    """Alternativas de um token palavra (a própria + sinônimos); cada alternativa
    é uma lista de Tok que precisam bater TODOS (ex.: "papel a4")."""
    if tok.tipo != "palavra":
        return [[tok]]
    alts: list[list[Tok]] = [[tok]]
    chaves = {tok.texto, *tok.formas}
    vistos = set()
    for ch in chaves:
        for alt in _indice_sinonimos().get(ch, []):
            k = tuple(alt)
            if k in vistos or (len(alt) == 1 and alt[0] in chaves):
                continue
            vistos.add(k)
            nova = [_tok_palavra(w) if not _RE_NUM.match(w) else Tok("numero", w)
                    for w in alt]
            if nova not in alts:          # "caixa"/"caixas" viram o mesmo radical
                alts.append(nova)
    return alts


# ---------------------------------------------------------------------------
# Casamento em Python (usado pra escolher itens mostrados e como referência
# do comportamento do front)
# ---------------------------------------------------------------------------
def _dist_ate(a: str, b: str, k: int) -> int:
    """Damerau-Levenshtein (adjacente) limitado: devolve k+1 se passar de k."""
    if abs(len(a) - len(b)) > k:
        return k + 1
    prev2 = None
    prev = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            c = 0 if a[i - 1] == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + c)
            if prev2 is not None and i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        prev2, prev = prev, cur
        if min(cur) > k:
            return k + 1
    return prev[-1]


def tolerancia(n: int) -> int:
    return 0 if n < 4 else (1 if n <= 7 else 2)


def _palavra_bate(tok: Tok, palavras: list[str], fuzzy: bool) -> bool:
    if tok.prefixo:
        if any(w.startswith(tok.texto) for w in palavras):
            return True
    elif any(w in tok.formas for w in palavras):
        return True
    if fuzzy and tolerancia(len(tok.texto)) and not _RE_NUM.match(tok.texto):
        k = tolerancia(len(tok.texto))
        n = len(tok.texto)
        return any(_dist_ate(tok.texto, w[:n], k) <= k or _dist_ate(tok.texto, w, k) <= k
                   for w in palavras)
    return False


def _tok_simples(tok: Tok, palavras: list[str], fuzzy: bool) -> bool:
    if tok.tipo == "numero":
        return tok.texto in palavras
    if tok.tipo == "medida":
        return any(palavras[i] == tok.texto and palavras[i + 1] == tok.unidade
                   for i in range(len(palavras) - 1))
    return _palavra_bate(tok, palavras, fuzzy)


def _tok_bate(tok: Tok, palavras: list[str], fuzzy: bool) -> bool:
    for i, alt in enumerate(alternativas(tok)):
        # i == 0 é o próprio token; fuzzy só vale pra ele (não pros sinônimos)
        if all(_tok_simples(t, palavras, fuzzy and i == 0) for t in alt):
            return True
    return False


def texto_bate(texto: str | None, tokens: list[Tok], fuzzy: bool = False) -> bool:
    palavras = normalizar(texto).split()
    return bool(tokens) and all(_tok_bate(t, palavras, fuzzy) for t in tokens)


# ---------------------------------------------------------------------------
# Condições SQL (itens de edital)
# ---------------------------------------------------------------------------
_ACENTOS = {"a": "[aáàâãä]", "e": "[eéèêë]", "i": "[iíìîï]",
            "o": "[oóòôõö]", "u": "[uúùûü]", "c": "[cç]"}


def _rx(p: str) -> str:
    return "".join(_ACENTOS.get(ch, re.escape(ch)) for ch in p)


def _rx_numero(n: str) -> str:
    return "[.,]".join(re.escape(x) for x in n.split("."))


def _regex_tok(tok: Tok) -> str:
    if tok.tipo == "numero":
        return r"(?<![0-9])" + _rx_numero(tok.texto) + r"(?![0-9])"
    if tok.tipo == "medida":
        return (r"(?<![0-9])" + _rx_numero(tok.texto) + r"[\s.,-]*" + _rx(tok.unidade)
                + r"(?![a-z])")
    corpo = _rx(tok.texto)
    if re.match(r"^[a-z]\d+$", tok.texto):          # a4 == a-4 == a 4
        corpo = _rx(tok.texto[0]) + r"[\s-]?" + tok.texto[1:] + r"(?![0-9])"
    if tok.prefixo:
        return r"\m" + corpo
    return r"\m(?:" + "|".join(_rx(f) for f in sorted(tok.formas)) + r")\M"


def condicoes_sql(termo: str, coluna, eh_postgres: bool, fuzzy: bool = False) -> list:
    """Uma condição por token (todas precisam valer). `coluna` = lower(descricao)."""
    from sqlalchemy import and_, or_, literal
    conds = []
    for tok in tokenizar(termo):
        alts = []
        for alt in alternativas(tok):
            partes = []
            for t in alt:
                if t.tipo == "palavra" and not eh_postgres:
                    partes.append(coluna.like(f"%{t.texto}%"))
                elif t.tipo != "palavra" and not eh_postgres:
                    partes.append(coluna.like(f"%{t.texto}%"))
                else:
                    partes.append(coluna.op("~")(_regex_tok(t)))
            alts.append(and_(*partes) if len(partes) > 1 else partes[0])
        if fuzzy and eh_postgres and tok.tipo == "palavra" and tolerancia(len(tok.texto)):
            # pg_trgm: "palavra <% texto" usa o índice GIN trigram existente
            alts.append(literal(tok.texto).op("<%")(coluna))
        conds.append(or_(*alts) if len(alts) > 1 else alts[0])
    return conds
