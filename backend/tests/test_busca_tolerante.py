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
    sql = str(_condicoes_busca_item("tinta caixa", True)[1].compile(
        compile_kwargs={"literal_binds": True}))
    assert " OR " in sql and "[cç]x" in sql and sql.count(" OR ") == 1
    sql_f = str(_condicoes_busca_item("tnta", True, fuzzy=True)[0].compile(
        compile_kwargs={"literal_binds": True}))
    assert "<%" in sql_f


def test_sql_numero_e_medida_com_lookaround():
    sql = str(_condicoes_busca_item("75g", True)[0].compile(compile_kwargs={"literal_binds": True}))
    assert "(?<![0-9])75" in sql
