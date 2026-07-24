"""
Normalização morfológica leve (stemming) em português.

Reduz variações de plural/gênero ("computadores" -> "comput", "eletronica"/
"eletronico" -> "eletron") para que o TF-IDF e o casamento por palavra-chave
não percam correspondências óbvias só por causa da flexão da palavra.

Usa o algoritmo Snowball (pt) do nltk: é uma implementação em código puro
(regras hardcoded), sem baixar modelo/corpus nenhum — ao contrário do
stemmer RSLP do nltk, que exige nltk.download(). Mantém o princípio do
projeto de funcionar sem downloads externos.
"""
from __future__ import annotations

try:
    from nltk.stem.snowball import SnowballStemmer
    _stemmer = SnowballStemmer("portuguese")
except Exception:  # nltk ausente/indisponível -> stemming vira no-op
    _stemmer = None

# Palavras curtas ficam como estão: stemming agressivo em token curto tende a
# colidir termos sem relação nenhuma (ex.: "gel", "led", "kit" perdendo a
# última letra por regra genérica). A partir de 4 letras o ganho de recall
# supera o risco de colisão.
_TAM_MINIMO = 4


def radical(token: str) -> str:
    """Radical de UMA palavra já normalizada (sem acento/pontuação)."""
    if _stemmer is None or len(token) < _TAM_MINIMO or not token.isalpha():
        return token
    return _stemmer.stem(token)


def stemizar_texto(texto: str) -> str:
    """Aplica radical() palavra a palavra, preservando a ordem/espaçamento."""
    if not texto:
        return texto
    return " ".join(radical(t) for t in texto.split())
