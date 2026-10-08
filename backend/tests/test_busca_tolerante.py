"""Busca tolerante (app/busca.py): tokens em qualquer ordem, abreviação/sinônimo,
plural, medida colada, stopwords, fuzzy. Rode com: cd backend && pytest"""
import pytest

from app import busca as b
from app.main import _condicoes_busca_item

NOME = "Tinta Pintura A Dedo Cx Com 12 Cores Sortidas"


def bate(texto, termo, fuzzy=False):
    return b.texto_bate(texto, b.tokenizar(termo), fuzzy)


@pytest.mark.parametrize("termo", [
    "Tinta 12 cores", "tinta caixa", "tinta cx", "TINTA CAIXA 12", "cor tinta",
    "tinta 12 unidades", "tinta c/12", "pintura a dedo", "tinta com 12",
])
def test_acha_item_do_exemplo(termo):
    assert bate(NOME, termo)


@pytest.mark.parametrize("termo", ["tinta 120", "tinta corretivo", "tinta pct", "tinta 13 cores"])
def test_nao_acha_quando_nao_deve(termo):
    assert not bate(NOME, termo)


def test_plural_nos_dois_sentidos_e_acento():
    assert bate("CANETAS ESFEROGRÁFICAS AZUIS", "caneta esferografica")
    assert bate("Caneta azul", "canetas")
    assert bate("Papel A4", "papeis a4")


def test_nao_casa_no_meio_da_palavra():
    assert not bate("MACANETA PARA FECHADURA", "caneta")


def test_medida_colada_ou_separada_e_a4():
    assert bate("PAPEL SULFITE A-4 75G", "papel a4 75 g")
    assert bate("Papel A4 75 g/m2", "a4 75g")
    assert not bate("Papel A4 750 g", "a4 75g")


def test_sinonimo_de_produto_e_stopword():
    assert bate("Papel A4 Chamex 75 g", "sulfite")
    assert bate("Grampeador de mesa", "grampeador mesa")


def test_fuzzy_so_quando_pedido():
    assert not bate(NOME, "tnta")
    assert bate(NOME, "tnta", fuzzy=True)
    assert bate("CANETAS ESFEROGRAFICAS", "esferogrfica", fuzzy=True)
    assert not bate(NOME, "tnta 13", fuzzy=True)   # número continua exato


def test_sql_postgres_sinonimo_vira_or_e_fuzzy_usa_trigram():
    """"cx" (sinônimo de "caixa") não tem acento nenhum -- sem classe de
    caractere, mesmo antes da correção do índice/regex sem acento. Troca pra
    "caneta"/sinônimo acentuado seria mais fiel ao achado real, mas o ponto
    deste teste é o OR entre alternativas + "<%" do fuzzy, que continua
    valendo igual."""
    sql = str(_condicoes_busca_item("tinta caixa", True)[1].compile(
        compile_kwargs={"literal_binds": True}))
    assert " OR " in sql and "cx" in sql and sql.count(" OR ") == 1
    assert "unaccent_imutavel" in sql
    sql_f = str(_condicoes_busca_item("tnta", True, fuzzy=True)[0].compile(
        compile_kwargs={"literal_binds": True}))
    assert "<%" in sql_f
    assert "unaccent_imutavel" in sql_f


def test_sql_numero_e_medida_com_lookaround():
    sql = str(_condicoes_busca_item("75g", True)[0].compile(compile_kwargs={"literal_binds": True}))
    assert "(?<![0-9])75" in sql


# ---------------------------------------------------------------------------
# Achado real (auditoria de 5 agentes pedida pelo usuário em cima do commit
# "busca tolerante"): os testes acima do caminho Postgres só conferiam a
# STRING do SQL gerado (contém "~", contém "OR"...), nunca o COMPORTAMENTO
# da regex em si -- foi assim que o bug das classes de acento destruindo o
# índice trigram passou batido (o SQL "parecia certo", só era lento demais).
# Não dá pra rodar a regex de verdade contra um Postgres aqui (suíte roda em
# sqlite), mas dá pra extrair o padrão regex que condicoes_sql() monta e
# testar sua LÓGICA com o motor de regex do Python -- só traduzindo \m/\M
# (fronteira de início/fim de palavra do Postgres) pro equivalente em
# lookaround, que o `re` do Python entende. Não é bit-a-bit idêntico ao
# motor do Postgres, mas pega exatamente a classe de erro que escapou antes:
# um padrão logicamente errado (classe de caractere demais, char a menos,
# fronteira no lugar errado).
# ---------------------------------------------------------------------------
import re as _re


def _extrair_padroes_regex(sql: str) -> list[str]:
    """Tira as strings entre aspas simples de um SQL compilado com
    literal_binds -- nesta suíte, são sempre os padrões regex passados pro
    operador `~`. Ingênuo (não lida com aspas escapadas dentro do padrão),
    mas suficiente pros padrões que busca.py gera (nunca têm aspas)."""
    return _re.findall(r"'([^']*)'", sql)


def _pg_regex_para_python(padrao: str) -> str:
    """\\m/\\M (Postgres: início/fim de PALAVRA, não troca de classe de
    caractere qualquer como \\b) -- aproximação segura aqui porque todo
    texto já passou por normalizar()/unaccent_imutavel (só a-z0-9)."""
    return padrao.replace(r"\m", r"(?<![0-9a-z])").replace(r"\M", r"(?![0-9a-z])")


def _regex_do_token_bate(termo: str, texto_sem_acento: str) -> bool:
    """Monta a condição Postgres real pro termo, extrai o(s) padrão(ões)
    regex, traduz pro equivalente em Python e confere contra o texto (já
    sem acento, simulando o que unaccent_imutavel(lower(descricao)) devolve
    em produção)."""
    condicoes = _condicoes_busca_item(termo, eh_postgres=True)
    sql = str(condicoes[0].compile(compile_kwargs={"literal_binds": True}))
    padroes = _extrair_padroes_regex(sql)
    assert padroes, f"nenhum padrão regex extraído de: {sql}"
    return any(_re.search(_pg_regex_para_python(p), texto_sem_acento) for p in padroes)


def test_regex_postgres_bate_certo_depois_de_traduzida_pro_python():
    # casos que TÊM que bater (equivalente ao caminho Python, texto_bate)
    assert _regex_do_token_bate("caneta", "canetas esferograficas azuis")
    assert _regex_do_token_bate("caneta", "caixa com 50 canetas")
    # casos que NÃO podem bater -- é justamente o que a fronteira de início
    # de palavra (\m) existe pra evitar
    assert not _regex_do_token_bate("caneta", "macaneta para fechadura de porta")
    assert not _regex_do_token_bate("cor", "tinta corretivo escolar")   # achado real (radical curto)


def test_sem_token_devolve_condicao_sempre_falsa_nao_lista_vazia():
    """Achado real (fuzzing pedido pelo usuário): termo que normaliza pra
    nada (só pontuação/emoji/etc) tem que impedir TODO item de bater --
    devolver [] fazia o chamador não acrescentar WHERE nenhum, e um EXISTS
    sem filtro vira "qualquer item serve", trazendo tudo em vez de nada."""
    from sqlalchemy import column, select, literal_column
    coluna = column("descricao")
    for termo in ["???", "...", "---", "😀", "永字八法"]:
        condicoes = b.condicoes_sql(termo, coluna, eh_postgres=True)
        assert len(condicoes) == 1
        # a condição tem que ser estruturalmente "sempre falsa" (false()),
        # não uma condição real que às vezes bate -- confere evoluindo a
        # cláusula numa query mínima: nenhuma linha de uma tabela com 1 row
        # fictícia pode satisfazer.
        q = select(literal_column("1")).where(condicoes[0])
        sql = str(q.compile(compile_kwargs={"literal_binds": True}))
        assert "false" in sql.lower()


def test_sqlite_medida_exige_numero_e_unidade_juntos():
    """Achado real (revisão pedida pelo usuário): o caminho sqlite usava só
    o número da medida (t.texto), descartando a unidade (t.unidade) -- "75
    g" virava `LIKE '%75%'`, batendo em "75 kg", "1750" etc. Confere que a
    condição agora exige as DUAS substrings (ainda é LIKE solto, sqlite não
    tem o regex do Postgres -- aceitável só em dev/teste)."""
    condicoes = _condicoes_busca_item("75 g", eh_postgres=False)
    sql = str(condicoes[0].compile(compile_kwargs={"literal_binds": True}))
    assert "75" in sql and "%g%" in sql.replace(" ", "")
