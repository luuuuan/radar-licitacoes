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
    "ppm": r"ppm|pag(?:inas)?\s*/?\s*min(?:uto)?|pag(?:inas)?\s+por\s+minuto",
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
    # "m" (bare) exige (?!/) pra não confundir com taxa/velocidade tipo
    # "2 M/MIN" (2 metros por minuto — velocidade de corte de uma
    # fragmentadora, não comprimento nenhum): "/" não é \w, então o
    # (?!\w) do scan geral não bloqueia isso sozinho.
    "m": r"metros?|m(?!/)",
    "furos": r"furos?",
    "rpm": r"rpm",
    "dpi": r"dpi",
    # capacidade de exibição de calculadora ("12 dígitos") — sem isso o
    # número não batia em NENHUMA unidade reconhecida e um mismatch óbvio
    # (item exige 12 dígitos, produto oferece 8) passava batido sem gerar
    # nenhuma pendência.
    "digitos": r"digitos?|dig\.?",
}

# unidades da MESMA família (só escala métrica, conversão exata e sem
# ambiguidade — nada de polegada<->cm, que exige arredondamento) -> (nome da
# família, fator pra a menor unidade dela). Permite comparar "2 litros" do
# item com "2000 ml" do produto mesmo com rótulos diferentes.
_FAMILIA_UNIDADE: dict[str, tuple[str, float]] = {
    "litros": ("volume", 1000.0), "ml": ("volume", 1.0),
    "kg": ("massa", 1000.0), "gramas": ("massa", 1.0),
    "tb": ("armazenamento", 1_000_000.0), "gb": ("armazenamento", 1_000.0), "mb": ("armazenamento", 1.0),
    "cm": ("comprimento", 10.0), "mm": ("comprimento", 1.0), "m": ("comprimento", 1000.0),
}


def familia_unidade(unidade: str, valor: float) -> tuple[str, float]:
    """(família, valor convertido pra menor unidade da família) — unidade sem
    família conhecida vira (a própria unidade, valor inalterado), então a
    comparação cai de volta em exigir o mesmo rótulo, como antes."""
    familia, fator = _FAMILIA_UNIDADE.get(unidade, (unidade, 1.0))
    return familia, valor * fator


def _e_marca_3m(unidade: str, valor: float, bruto: str) -> bool:
    """"3M" é a marca (Post-it, Scotch, fita/adesivo em geral — a categoria
    inteira de papelaria/fita é dominada por ela), não "3 metros". Achado em
    dados reais de produção: "Bloco Adesivo Post-it 38mm 50mm ... 2267 3M -
    4BL" virava um atributo numérico m=3.0 inventado, gerando pendência de
    "medida incompatível" sem sentido nenhum. Não há nenhum caso real visto
    até agora de "3m" bare (glued, sem escrever "metro(s)") sendo uma medida
    de verdade — "3 metros" por extenso continua batendo normalmente, essa
    exclusão só bloqueia o token exato "3m" colado."""
    return unidade == "m" and valor == 3.0 and bruto.strip() == "3m"


# 3 alternativas, nessa ordem (a 1ª que casar vence): grupo(s) de milhar
# ("2.500", opcionalmente com vírgula decimal depois: "2.500,50") -> decimal
# com PONTO fora do padrão BR mas comum em ficha técnica copiada de fora
# ("1.0mm", só 1-2 dígitos depois do ponto — 3 dígitos é sempre milhar, nunca
# decimal) -> dígitos simples com vírgula decimal opcional ("2,5" ou "250").
_NUM = r"(\d{1,3}(?:\.\d{3})+(?:,\d+)?|\d+\.\d{1,2}(?!\d)|\d+(?:,\d+)?)"


def _parse_numero(bruto: str) -> float:
    """Interpreta o número já na convenção certa: ',' é sempre decimal; '.'
    é separador de milhar SE seguido de grupo(s) de exatamente 3 dígitos,
    senão (1-2 dígitos após o único '.', sem vírgula) é decimal mesmo —
    replace(".", "") ingênuo lia "1.0" como 10 em vez de 1.0."""
    if "," in bruto:
        inteiro, _, frac = bruto.partition(",")
        return float(inteiro.replace(".", "") + "." + frac)
    if "." in bruto and len(bruto.rsplit(".", 1)[1]) in (1, 2):
        return float(bruto)
    return float(bruto.replace(".", ""))


# Unidades de comprimento que podem aparecer coladas num número dentro de um
# campo estruturado OU de um par/trio de dimensão (ver abaixo).
_UNIDADE_COMPRIMENTO = r"(?:mm|milimetros?|cm|centimetros?|m(?!/)|metros?)"

# O PNCP costuma descrever especificação de item em campos estruturados —
# "rótulo: número", sem unidade colada ("comprimento: 145", não "145mm").
# Dois tipos, tratados igual na hora de reescrever mas com confiança
# diferente:
#  - dimensão física (comprimento/largura/altura/diâmetro/espessura): a
#    unidade fica implícita no schema do catálogo (CATMAT), não no texto —
#    é uma SUPOSIÇÃO (convenção do catálogo é sempre milímetro), então o
#    resultado fica marcado como inferido=True (nunca reprova sozinho).
#  - rótulo que JÁ NOMEIA a unidade (dígitos/tensão): "dígitos: 12" é tão
#    firme quanto se o texto dissesse "12 dígitos" na ordem natural — não é
#    suposição nenhuma, então NÃO fica marcado como inferido.
# (rótulo_regex) -> (unidade, inferido)
_CAMPOS_ROTULO_UNIDADE: dict[str, tuple[str, bool]] = {
    r"comprimento|largura|altura|diametro|espessura": ("mm", True),
    r"(?:numero\s+(?:de\s+)?|n[oº]?\s*(?:de\s+)?)?digitos": ("digitos", False),
    r"tensao|voltagem": ("volts", False),
}
_CAMPOS_ROTULO_ALT = "|".join(f"(?:{r})" for r in _CAMPOS_ROTULO_UNIDADE)
# Sem isso, "campo: 96 metros" virava "96 mm" por engano (a unidade certa
# já tava ali do lado, só que como PALAVRA separada por espaço, não colada
# no número) — erro de escala de até 1000x. O "\d*" de folga é necessário:
# sem ele, o motor de regex backtrackeia o número pra um prefixo mais curto
# ("9" em vez de "96") e o dígito restante ("6") deixa de bloquear o
# lookahead, deixando "9" passar como se não tivesse unidade na frente.
_CAMPO_ESTRUTURADO_RE = re.compile(
    rf"\b(?P<rotulo>{_CAMPOS_ROTULO_ALT})\s*:\s*(?P<num>{_NUM})"
    + r"(?!\d*[a-z])"
    + rf"(?!\d*\s+{_UNIDADE_COMPRIMENTO}\b)"
)


def _unidade_do_rotulo(rotulo_txt: str) -> tuple[str, bool] | None:
    for padrao, par in _CAMPOS_ROTULO_UNIDADE.items():
        if re.fullmatch(padrao, rotulo_txt):
            return par
    return None


# Cadeia de dimensão físico ("30x40cm", "12mm x 50mm", "12x50" sem unidade
# nenhuma, ou um TRIO/mais "28 X 16 X 7CM" — comprimento x largura x altura é
# super comum em item real do PNCP) — o "x" entre números é o sinal, não
# precisa de mais nada pra reconhecer: por construção do padrão, só casa se
# houver um dígito colado (ou separado por espaço) nos dois lados de CADA
# "x". Cada número pode ter sua PRÓPRIA unidade (mm/cm/m); se só um tiver,
# essa unidade vale pra todos (convenção mais comum: "30x40cm" é 30cm e
# 40cm); se nenhum tiver, assume mm (igual ao campo estruturado acima),
# marcado como inferido=True — nunca reprova um produto sozinho, só vira aviso.
_NUM_UNID_OPC = rf"{_NUM}\s*(?:{_UNIDADE_COMPRIMENTO})?"
_DIMENSAO = re.compile(
    rf"\b{_NUM_UNID_OPC}(?:\s*x\s*{_NUM_UNID_OPC})+(?!\w)"
)
# mesma peça, mas com grupos NOMEADOS — usada por dentro de cada match de
# _DIMENSAO pra separar os números/unidades individuais da cadeia.
_NUM_UNID_NOMEADO = re.compile(rf"(?P<num>{_NUM})\s*(?P<un>{_UNIDADE_COMPRIMENTO})?")


def _unidade_comprimento_canonica(txt: str | None) -> str | None:
    if not txt:
        return None
    if txt.startswith("mm") or txt.startswith("milimetro"):
        return "mm"
    if txt.startswith("cm") or txt.startswith("centimetro"):
        return "cm"
    return "m"


def _pares_dimensao(texto_norm: str) -> tuple[list[tuple[int, int, str, float, str, int]], list[tuple[int, int]]]:
    """Acha cadeias 'NxNx...xN' e devolve (entradas no mesmo formato que o
    scan por unidade de _extrair_numericos usa, com um cadeia_id compartilhado
    entre os membros do MESMO match — pra validacao.py só agrupar como par/
    trio de dimensão valores que vieram juntos, spans SEM unidade explícita —
    pra marcar inferido=True via o mesmo mecanismo de spans_inferidos)."""
    entradas: list[tuple[int, int, str, float, str, int]] = []
    sem_unidade: list[tuple[int, int]] = []
    for m in _DIMENSAO.finditer(texto_norm):
        partes = list(_NUM_UNID_NOMEADO.finditer(m.group(0)))
        unidades = [_unidade_comprimento_canonica(p.group("un")) for p in partes]
        # a unidade explícita mais comum na prática é a do ÚLTIMO número da
        # cadeia ("28 x 16 x 7cm" -> os 3 são cm); usa a última encontrada,
        # senão qualquer uma que apareça, senão assume mm.
        unidade_comum = next((u for u in reversed(unidades) if u), None) or "mm"
        explicita = any(unidades)
        cadeia_id = m.start()
        for parte, unidade_propria in zip(partes, unidades):
            ini = m.start() + parte.start("num")
            fim = m.start() + parte.end("num")
            valor = _parse_numero(parte.group("num"))
            unidade = unidade_propria or unidade_comum
            if _e_marca_3m(unidade, valor, parte.group(0)):
                continue
            entradas.append((ini, fim, unidade, valor, parte.group("num"), cadeia_id))
            if not explicita:
                sem_unidade.append((ini, fim))
    return entradas, sem_unidade


def _expandir_campos_estruturados(texto_norm: str) -> tuple[str, list[tuple[int, int]]]:
    """Reescreve campo estruturado 'rótulo: número' -> 'número unidade' ANTES
    da extração normal, pra reaproveitar toda a lógica de operador/contexto/
    dedup/família que já existe pra número com unidade no texto (não duplica
    nada disso aqui). Cobre tanto "comprimento: 145" -> "145 mm" (unidade
    SUPOSTA, convenção do catálogo) quanto "dígitos: 12" -> "12 digitos" ou
    "tensão: 12" -> "12 volts" (unidade É O PRÓPRIO RÓTULO, fato, não
    suposição) — ver _CAMPOS_ROTULO_UNIDADE pra qual é qual.

    Retorna também os SPANS (posição no texto de saída) de cada substituição
    cuja unidade foi SUPOSTA (não os rótulos que já a nomeiam) —
    _extrair_numericos() usa esses spans pra marcar o AtributoNumerico como
    `inferido=True`, e validacao.py rebaixa pendência baseada nisso de
    crítica pra aviso — "no mínimo 21" pode muito bem ser 21cm (ex.:
    tesoura), não 21mm, então não é fato firme o bastante pra reprovar um
    produto sozinho."""
    partes = []
    spans_inferidos: list[tuple[int, int]] = []
    pos = 0
    pos_saida = 0
    for m in _CAMPO_ESTRUTURADO_RE.finditer(texto_norm):
        unidade, inferido = _unidade_do_rotulo(m.group("rotulo"))
        partes.append(texto_norm[pos:m.start()])
        pos_saida += m.start() - pos
        substituto = f"{m.group('num')} {unidade}"
        partes.append(substituto)
        if inferido:
            spans_inferidos.append((pos_saida, pos_saida + len(substituto)))
        pos_saida += len(substituto)
        pos = m.end()
    partes.append(texto_norm[pos:])
    return "".join(partes), spans_inferidos


# \b nas duas pontas evita casar dentro de outra palavra (ex.: "ate" dentro
# de "bateria"/"material" sem isso). minim[oa]s?/maxim[oa]s? cobre a
# concordância de gênero/número do adjetivo ("capacidade MÍNIMA",
# "quantidades MÁXIMAS") — só "no mínimo"/"no máximo" (advérbio) é invariável.
_OPERADOR_MIN = r"\b(?:no minimo|minim[oa]s?|pelo menos|superior a|acima de|a partir de)\b"
_OPERADOR_MAX = r"\b(?:no maximo|maxim[oa]s?|nao superior a|inferior a|abaixo de|ate)\b"
_JANELA_CONTEXTO = 30  # chars antes do número, onde procuramos "no mínimo"/"no máximo"


@dataclass
class AtributoNumerico:
    unidade: str
    valor: float
    operador: str   # ">=" | "<=" | "=="
    bruto: str       # trecho reconhecido no texto original, para mensagens legíveis
    # True quando a unidade veio de suposição (campo estruturado do PNCP tipo
    # "comprimento: 21" sem unidade no texto, convertido assumindo mm) — não
    # de texto explícito. validacao.py usa isso pra nunca deixar essa
    # suposição sozinha reprovar um produto (vira aviso, não crítica).
    inferido: bool = False
    # id compartilhado entre os membros de uma MESMA cadeia "AxB"/"AxBxC"
    # explícita no texto (ex.: os dois valores de "12x50cm" têm o mesmo
    # cadeia_id; um "7 cm" solto em outra frase não tem nenhum). None quando
    # o valor foi lido isolado (scan normal por unidade) — validacao.py usa
    # isso pra só tratar como PAR/TRIO de dimensão valores que de fato vieram
    # juntos no texto, não qualquer combinação de números soltos que caem na
    # mesma família por coincidência de unidade (ver
    # test_validacao_nao_pareia_numeros_soltos_nao_relacionados_da_mesma_familia).
    cadeia_id: int | None = None


def _extrair_numericos(texto_norm: str, spans_inferidos: list[tuple[int, int]] = ()) -> list[AtributoNumerico]:
    # 1ª passada: acha as ocorrências número+unidade de TODAS as unidades e
    # ordena por posição no texto — precisamos disso pra nunca deixar a
    # janela de contexto de uma ocorrência olhar pra trás do FIM da
    # ocorrência anterior (ela pertence à cláusula anterior, mesmo sem
    # pontuação separando as duas: "no mínimo 30 ppm e bandeja para 250
    # folhas" não tem vírgula, mas "no mínimo" não deveria valer pra folhas).
    brutos = []
    for unidade, padrao in _UNIDADES.items():
        # (?!\w) em vez de \b: o rótulo pode terminar em aspas (polegadas),
        # que não é caractere de palavra — \b nunca fecha ali quando a aspas
        # é seguida de espaço/pontuação, que é o caso normal.
        for m in re.finditer(rf"{_NUM}\s*(?:{padrao})(?!\w)", texto_norm):
            valor = _parse_numero(m.group(1))
            bruto = m.group(0).strip()
            if _e_marca_3m(unidade, valor, bruto):
                continue
            brutos.append((m.start(), m.end(), unidade, valor, bruto, None))

    # pares de dimensão ("30x40cm", "12x50"...) — pode se sobrepor com o scan
    # acima quando os DOIS lados já têm unidade explícita (ex.: "40cm" seria
    # achado pelos dois caminhos); inofensivo, o dedup abaixo por
    # (unidade, valor, operador) colapsa a repetição.
    pares, spans_sem_unidade = _pares_dimensao(texto_norm)
    brutos.extend(pares)
    spans_inferidos = list(spans_inferidos) + spans_sem_unidade

    brutos.sort(key=lambda b: b[0])

    achados: list[AtributoNumerico] = []
    fim_anterior = 0
    for inicio, fim, unidade, valor, bruto, cadeia_id in brutos:
        janela = texto_norm[max(0, inicio - _JANELA_CONTEXTO, fim_anterior):inicio]
        # também corta na última pontuação de separação de cláusula dentro
        # da janela (reforça o corte acima quando há vírgula/ponto no meio).
        pos_sep = max(janela.rfind(","), janela.rfind(";"), janela.rfind("."))
        contexto = janela[pos_sep + 1:] if pos_sep != -1 else janela
        if re.search(_OPERADOR_MIN, contexto):
            operador = ">="
        elif re.search(_OPERADOR_MAX, contexto):
            operador = "<="
        else:
            operador = "=="
        inferido = any(inicio < s_fim and fim > s_inicio for s_inicio, s_fim in spans_inferidos)
        # quando a unidade é SUPOSTA (campo estruturado tipo "comprimento:
        # 13" virou "13 mm" internamente pra reaproveitar o scan normal),
        # não mostra esse "mm" pro usuário como se estivesse no texto — só
        # o número, que é o que de fato foi lido. Mostrar "13 mm" bem do
        # lado de "(medida estimada, não confirmada no texto do edital)" é
        # contraditório e passa confiança que a unidade não tem.
        bruto_exibido = bruto.split()[0] if inferido else bruto
        achados.append(AtributoNumerico(unidade, valor, operador, bruto_exibido, inferido, cadeia_id))
        fim_anterior = fim

    # dedup por (unidade, valor, operador): texto de edital real costuma
    # repetir a mesma especificação em mais de uma frase — sem isso, cada
    # menção virava uma pendência duplicada na validação. Quando a mesma
    # ocorrência é achada pelos dois caminhos (o último número de uma cadeia
    # "AxBxC" que também tem unidade colada, ex.: o "7cm" de "28 x 16 x 7cm",
    # bate tanto no scan de par/trio quanto no scan solto por unidade),
    # prefere a versão com cadeia_id — perder essa marca faria esse membro
    # "cair" da cadeia e virar um valor solto por engano.
    vistos: dict[tuple, AtributoNumerico] = {}
    ordem: list[tuple] = []
    for a in achados:
        chave = (a.unidade, a.valor, a.operador)
        if chave not in vistos:
            vistos[chave] = a
            ordem.append(chave)
        elif vistos[chave].cadeia_id is None and a.cadeia_id is not None:
            vistos[chave] = a
    return [vistos[chave] for chave in ordem]


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
    # Achado real (auditoria do agente code-reviewer): "plástico"/"plástica"
    # como oposto de inox era frágil demais -- um produto genuinamente de
    # inox com QUALQUER peça acessória plástica mencionada na descrição
    # (base antiderrapante, pé de borracha/plástico, tampa) e sem repetir
    # literalmente "inox" perto o bastante virava "oposto", que validacao.py
    # trata como reprovação automática CRÍTICA. Sem um par de oposto
    # confiável pra material (ao contrário de "sem_fio" vs "com fio", que
    # são termos específicos de conectividade), fica só como "ausente"
    # (pendência não-crítica, exige confirmação manual) quando não
    # menciona inox — mais seguro que rejeitar sozinho.
    "inox": {
        "presente": ["inox", "aco inoxidavel"],
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
    t, spans_inferidos = _expandir_campos_estruturados(t)
    caracteristicas = {nome for nome in _CARACTERISTICAS if estado_caracteristica(nome, t) == "presente"}
    return AtributosTecnicos(
        texto_normalizado=t,
        numericos=_extrair_numericos(t, spans_inferidos),
        categoricos=_extrair_categoricos(t),
        caracteristicas=caracteristicas,
    )
