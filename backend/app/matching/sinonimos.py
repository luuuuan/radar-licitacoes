"""
Sinônimos e abreviações comuns em editais de licitação.

O TF-IDF e o casamento por palavra-chave só enxergam o texto literal — não
sabem que "notebook" e "computador portátil" são a mesma coisa. Este módulo
mapeia variações conhecidas para uma forma canônica única, aplicado ANTES do
stemming (as chaves/valores aqui já passaram por normalizar(), mas ainda sem
stem).

Não é uma lista exaustiva nem específica de um catálogo — é um ponto de
partida com termos comuns em editais de informática, material de expediente,
limpeza/higiene e EPI. Adicione entradas aqui conforme forem aparecendo
falsos negativos reais (edital que deveria bater e não bateu por diferença
de nome).
"""
from __future__ import annotations
import re

# variante normalizada -> forma canônica (também normalizada, sem acento)
SINONIMOS: dict[str, str] = {
    # informática
    "notebook": "computador portatil",
    "laptop": "computador portatil",
    "note": "computador portatil",
    "cpu": "computador",
    "desktop": "computador",
    "impressora multifuncional": "multifuncional",
    "mfp": "multifuncional",
    "no break": "nobreak",
    "pen drive": "pendrive",
    "hd externo": "disco rigido externo",
    "monitor de video": "monitor",
    # material de expediente / papelaria
    "papel a4": "papel sulfite a4",
    "caneta esferografica": "caneta",
    "caneta ballpoint": "caneta",
    "lapis grafite": "lapis",
    "lapis preto": "lapis",
    "pasta suspensa": "pasta",
    "pasta az": "pasta arquivo",
    "grampeador de mesa": "grampeador",
    # limpeza / higiene
    "alcool em gel": "alcool gel",
    "alcool etilico 70": "alcool gel",
    "sabonete liquido": "sabonete",
    "detergente neutro": "detergente",
    "agua sanitaria": "hipoclorito de sodio",
    # EPI
    "epi": "equipamento de protecao individual",
    "luva de procedimento": "luva",
    "luva nitrilica": "luva",
    "luva de latex": "luva",
    "mascara descartavel": "mascara",
    "mascara cirurgica": "mascara",
    "mascara n95": "mascara",
    "oculos de protecao": "oculos protecao",
    # mobiliário
    "cadeira giratoria": "cadeira",
    "mesa de escritorio": "mesa",
    "armario de aco": "armario",
}

# ordenado do mais longo pro mais curto -> uma frase inteira ("impressora
# multifuncional") é substituída antes de um pedaço dela ("multifuncional")
# ser considerado isoladamente em outro contexto.
_PADROES = [
    (re.compile(rf"\b{re.escape(chave)}\b"), SINONIMOS[chave])
    for chave in sorted(SINONIMOS, key=len, reverse=True)
]


def aplicar_sinonimos(texto_normalizado: str) -> str:
    """Substitui variantes conhecidas pela forma canônica. Espera texto já
    passado por normalizar() (sem acento/pontuação, minúsculo)."""
    if not texto_normalizado:
        return texto_normalizado
    for padrao, canonico in _PADROES:
        texto_normalizado = padrao.sub(canonico, texto_normalizado)
    return texto_normalizado
