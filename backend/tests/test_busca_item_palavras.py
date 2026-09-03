"""
Testes da tokenização por palavra da busca por item (GET /api/editais,
busca_item). Achados reais reportados pelo usuário:
1) buscar "papel a4" não achava um item como "PAPEL SULFITE A4 75G" -- a
   frase inteira precisava aparecer CONTÍGUA no texto (as palavras só
   batiam naquela ordem exata, uma do lado da outra).
2) buscar "caneta" trazia um item chamado "MACANETA PARA FECHADURA DE
   PORTA" (PNCP grava sem cedilha) -- "caneta" é um substring literal de
   "macaneta", sem fronteira de palavra nenhuma.
_condicoes_busca_item/_item_bate_busca (app/main.py) tokenizam em palavras
(cada uma precisa aparecer, em qualquer ordem) e, no Postgres, usam regex
com fronteira de palavra por token. SQLite (usado aqui, nos testes) não
tem esse operador nativamente -- cai pro substring de sempre, então o
teste end-to-end do achado 2 teria que rodar contra Postgres de verdade;
aqui valida-se a função pura (_item_bate_busca, usa o módulo `re` do
Python, não depende de banco nenhum) e o FORMATO da condição SQL gerada
pro Postgres. Rode com: cd backend && pytest
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.main import listar_editais, _condicoes_busca_item, _item_bate_busca
from app.models import Base, Usuario, Edital, ItemEdital


def _sessao():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def _usuario(db):
    u = Usuario(nome="Teste", email="t@t.com", senha_hash="x")
    db.add(u)
    db.commit()
    return u


def _edital_sem_match(db, id_externo, itens):
    ed = Edital(fonte="PNCP", id_externo=id_externo, orgao="Orgao Teste",
               objeto="Aquisicao", uf="SP")
    db.add(ed)
    db.commit()
    for numero, descricao in enumerate(itens, start=1):
        db.add(ItemEdital(edital_id=ed.id, numero=numero, descricao=descricao))
    db.commit()
    return ed


def _listar(db, user, **kwargs):
    padrao = dict(nivel=None, uf=None, status=None, vista="ativos",
                  apenas_nao_lidos=False, apenas_interessantes=False, hoje=False,
                  tipo="todos", valor_min=None, valor_max=None,
                  data_de=None, data_ate=None, busca_item=None,
                  pagina=1, por_pagina=50)
    padrao.update(kwargs)
    return listar_editais(user=user, db=db, **padrao)


# ---------- _item_bate_busca (função pura, independe de banco/dialeto) ----------

def test_item_bate_busca_com_palavras_fora_de_ordem_ou_separadas():
    assert _item_bate_busca("PAPEL SULFITE A4 75G BRANCO", ["papel", "a4"]) is True
    assert _item_bate_busca("A4 PAPEL SULFITE BRANCO", ["papel", "a4"]) is True   # ordem invertida também


def test_item_bate_busca_exige_todas_as_palavras():
    assert _item_bate_busca("PAPEL SULFITE 75G BRANCO", ["papel", "a4"]) is False   # falta "a4"


def test_item_bate_busca_respeita_fronteira_de_palavra():
    """O achado real: "caneta" não pode bater dentro de "macaneta"."""
    assert _item_bate_busca("MACANETA PARA FECHADURA DE PORTA", ["caneta"]) is False
    assert _item_bate_busca("CANETA ESFEROGRÁFICA AZUL", ["caneta"]) is True   # continua batendo no caso normal


# ---------- _condicoes_busca_item (formato da condição SQL) ----------

def test_condicoes_busca_item_tokeniza_uma_condicao_por_palavra():
    condicoes = _condicoes_busca_item("papel a4", eh_postgres=False)
    assert len(condicoes) == 2


def test_condicoes_busca_item_postgres_usa_regex_com_fronteira_de_palavra():
    condicoes = _condicoes_busca_item("caneta", eh_postgres=True)
    sql = str(condicoes[0].compile(compile_kwargs={"literal_binds": True}))
    assert "~" in sql
    assert r"\ycaneta\y" in sql


def test_condicoes_busca_item_sqlite_usa_substring_de_sempre():
    condicoes = _condicoes_busca_item("caneta", eh_postgres=False)
    sql = str(condicoes[0].compile(compile_kwargs={"literal_binds": True}))
    assert "like" in sql.lower()
    assert "%caneta%" in sql


# ---------- ponta a ponta via listar_editais (tokenização, SQLite) ----------

def test_busca_multipalavra_acha_item_com_palavras_fora_de_ordem_contigua():
    """Achado real: buscar "papel a4" não achava "PAPEL SULFITE A4 75G"
    porque a frase inteira precisava ser um substring contíguo."""
    db = _sessao()
    u = _usuario(db)
    ed = _edital_sem_match(db, "ed-papel", itens=["PAPEL SULFITE A4 75G BRANCO"])

    r = _listar(db, u, busca_item="papel a4")

    assert len(r["sem_match"]) == 1
    assert r["sem_match"][0]["edital_id"] == ed.id
    assert r["sem_match"][0]["itens_batem"] == ["PAPEL SULFITE A4 75G BRANCO"]


def test_busca_multipalavra_nao_acha_item_faltando_uma_das_palavras():
    db = _sessao()
    u = _usuario(db)
    _edital_sem_match(db, "ed-papel-generico", itens=["PAPEL SULFITE 75G BRANCO"])   # sem "a4"

    r = _listar(db, u, busca_item="papel a4")

    assert r["sem_match"] == []
