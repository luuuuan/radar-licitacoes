"""
Extração de atributos técnicos de descrições de itens de edital / produtos.

Regex + listas de domínio (leve, sem modelo de NLP pesado — mesma filosofia
de sinonimos.py e stemming.py: 100% local, sem GPU, sem download de modelo).
Puxa do texto livre:

- atributos NUMÉRICOS com unidade e operador implícito (ex.: "no mínimo 250
  folhas" -> unidade=folhas, valor=250, operador=">=");
- atributos CATEGÓRICOS de formato/modelo (ex.: "grampos 26/6" -> grampo=26/6,
  comparado por igualdade exata, não por número);
- CARACTERÍSTICAS booleanas (ex.: bivolt, duplex, sem fio), com 3 estados
  possíveis no texto de um produto: presente, oposto (contradiz
  explicitamente) ou ausente (não mencionado — não é prova de ausência).

Alimenta validacao.py, que decide se um produto ATENDE tecnicamente a um
item — pergunta diferente de "é sobre o mesmo assunto" (isso é o que o
matching semântico/textual de engine.py já responde).

Importante: aqui NÃO reaproveitamos normalizar()/preparar() de engine.py.
Aquele pipeline remove pontuação (letra "/", aspas, vírgula decimal) e
aplica stemming — ótimo para TF-IDF, mas destrói exatamente a informação que
precisamos aqui ("26/6", "110/220V", "15\"", "2,5 l").
"""
from __future__ import annotations
import re
import unicodedata
from dataclasses import dataclass, field


def _normalizar_leve(texto: str) -> str:
    """Minúsculas + remove acentos, mas preserva dígitos/pontuação — ao
    contrário de engine.normalizar(), que é agressivo demais para atributos."""
    if not texto:
        return ""
    t = unicodedata.normalize("NFKD", texto)
    t = t.encode("ascii", "ignore").decode("ascii")
    return t.lower()


# ---------------------------------------------------------------------------
# Atributos numéricos: unidade canônica -> regex do RÓTULO da unidade
# (o número vem sempre imediatamente antes, capturado à parte)
# ---------------------------------------------------------------------------
_UNIDADES: dict[str, str] = {
    "ppm": r"ppm|paginas?\s*/?\s*min(?:uto)?|pag(?:inas)?\s+por\s+minuto",
    "folhas": r"folhas?|fls\.?",
    "polegadas": r"polegadas?|pol\.?|\"",
    "watts": r"(?:watts?|w)",
    "volts": r"(?:volts?|v)",
    "litros": r"(?:litros?|l)",
    "ml": r"ml",
    "gramas": r"(?:gramas?|g)",
    "kg": r"kg|quilos?|quilogramas?",
    "gb": r"gb|gigabytes?",
    "tb": r"tb|terabytes?",
    "mb": r"mb|megabytes?",
    "ghz": r"ghz",
    "mm": r"mm|milimetros?",
    "cm": r"cm|centimetros?",
    "furos": r"furos?",
    "rpm": r"rpm",
    "dpi": r"dpi",
}

# grupos de milhar (".") são opcionais e vêm antes da vírgula decimal — nessa
# ordem, senão "2.500" (duas mil e quinhentas) seria lido como 2,5.
_NUM = r"(\d{1,3}(?:\.\d{3})+(?:,\d+)?|\d+(?:,\d+)?)"
# \b nas duas pontas evita casar dentro de outra palavra (ex.: "ate" dentro
# de "bateria"/"material" sem isso).
_OPERADOR_MIN = r"\b(?:no minimo|minimo de|minimo|pelo menos|superior a|acima de|a partir de)\b"
_OPERADOR_MAX = r"\b(?:no maximo|maximo de|maximo|nao superior a|inferior a|abaixo de|ate)\b"
_JANELA_CONTEXTO = 30  # chars antes do número, onde procuramos "no mínimo"/"no máximo"


@dataclass
class AtributoNumerico:
    unidade: str
    valor: float
    operador: str   # ">=" | "<=" | "=="
    bruto: str       # trecho reconhecido no texto original, para mensagens legíveis


def _extrair_numericos(texto_norm: str) -> list[AtributoNumerico]:
    achados: list[AtributoNumerico] = []
    for unidade, padrao in _UNIDADES.items():
        # (?!\w) em vez de \b: o rótulo pode terminar em aspas (polegadas),
        # que não é caractere de palavra — \b nunca fecha ali quando a aspas
        # é seguida de espaço/pontuação, que é o caso normal.
        for m in re.finditer(rf"{_NUM}\s*(?:{padrao})(?!\w)", texto_norm):
            valor = float(m.group(1).replace(".", "").replace(",", "."))
            janela = texto_norm[max(0, m.start() - _JANELA_CONTEXTO):m.start()]
            # não deixa o operador de uma cláusula anterior vazar para esta:
            # corta a janela na última pontuação de separação de cláusula.
            pos_sep = max(janela.rfind(","), janela.rfind(";"), janela.rfind("."))
            contexto = janela[pos_sep + 1:] if pos_sep != -1 else janela
            if re.search(_OPERADOR_MIN, contexto):
                operador = ">="
            elif re.search(_OPERADOR_MAX, contexto):
                operador = "<="
            else:
                operador = "=="
            achados.append(AtributoNumerico(unidade, valor, operador, m.group(0).strip()))
    return achados


# ---------------------------------------------------------------------------
# Atributos categóricos (formato/modelo — comparados por igualdade exata,
# nunca por >=/<=). Ex.: compatibilidade de grampo 26/6 vs 23/13.
# ---------------------------------------------------------------------------
def _extrair_categoricos(texto_norm: str) -> dict[str, str]:
    achados: dict[str, str] = {}
    m = re.search(r"grampos?\s*(?:tipo\s*)?(\d+\s*/\s*\d+)", texto_norm)
    if m:
        achados["grampo"] = re.sub(r"\s+", "", m.group(1))
    return achados


# ---------------------------------------------------------------------------
# Características booleanas — cada uma com termos que confirmam a presença
# e termos que a CONTRADIZEM explicitamente. Ausência de qualquer um dos
# dois grupos = "não mencionado", que é sinal fraco (não reprova sozinho).
# ---------------------------------------------------------------------------
_CARACTERISTICAS: dict[str, dict[str, list[str]]] = {
    "bivolt": {
        "presente": ["bivolt", "110/220", "110v/220v", "chave seletora de voltagem",
                     "voltagem automatica", "auto voltagem"],
        "oposto": ["apenas 220v", "somente 220v", "apenas 110v", "somente 110v",
                   "220v apenas", "110v apenas", "nao bivolt"],
    },
    "duplex": {
        "presente": ["duplex automatico", "duplex", "frente e verso automatico",
                     "impressao automatica frente e verso"],
        "oposto": ["duplex manual", "sem duplex", "nao possui duplex", "impressao manual"],
    },
    "sem_fio": {
        "presente": ["sem fio", "wireless", "wi-fi", "wifi"],
        "oposto": ["com fio", "cabeado"],
    },
    "inox": {
        "presente": ["inox", "aco inoxidavel"],
        "oposto": ["plastico", "plastica"],
    },
}


def estado_caracteristica(nome: str, texto_norm: str) -> str:
    """'presente' | 'oposto' | 'ausente' — usado por validacao.py para checar
    se um PRODUTO cumpre uma característica exigida pelo item."""
    termos = _CARACTERISTICAS.get(nome)
    if not termos:
        return "ausente"
    opostos_achados = [t for t in termos.get("oposto", []) if t in texto_norm]
    presentes_achados = [t for t in termos.get("presente", []) if t in texto_norm]
    # um termo "presente" que só aparece como substring de um termo "oposto"
    # encontrado (ex.: "duplex" dentro de "duplex manual") não conta como
    # menção independente — só um sinal de presença fora desse caso vence.
    presentes_independentes = [
        p for p in presentes_achados if not any(p in op for op in opostos_achados)
    ]
    if presentes_independentes:
        return "presente"
    if opostos_achados:
        return "oposto"
    return "ausente"


@dataclass
class AtributosTecnicos:
    texto_normalizado: str
    numericos: list[AtributoNumerico] = field(default_factory=list)
    categoricos: dict[str, str] = field(default_factory=dict)
    caracteristicas: set[str] = field(default_factory=set)  # características citadas como PRESENTES no próprio texto


def extrair_atributos(texto: str) -> AtributosTecnicos:
    t = _normalizar_leve(texto or "")
    caracteristicas = {nome for nome in _CARACTERISTICAS if estado_caracteristica(nome, t) == "presente"}
    return AtributosTecnicos(
        texto_normalizado=t,
        numericos=_extrair_numericos(t),
        categoricos=_extrair_categoricos(t),
        caracteristicas=caracteristicas,
    )
