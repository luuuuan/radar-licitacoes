"""
_anexar_cobertura_lotes cruza os lotes identificados pela análise por IA
(resultado["lotes"], cacheado por edital) com os itens que o catálogo do
usuário confirma de verdade (mesmo critério de "item compatível" usado na
Cotação/Proposta: confiança alta ou confirmado manualmente) — é POR USUÁRIO,
então roda em toda leitura, nunca fica preso no cache da análise em si (ver
docstring em main.py). Banco sqlite em memória, sem HTTP. Rode com:
cd backend && pytest
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.main import _anexar_cobertura_lotes
from app.models import Base, Usuario, Edital, Match, Produto


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


def _edital(db):
    ed = Edital(fonte="PNCP", id_externo="ed1", orgao="Orgao Teste", objeto="Aquisicao", uf="SP")
    db.add(ed)
    db.commit()
    return ed


def _confirmar_itens(db, u, ed, *numeros, confianca="alta"):
    prod = Produto(usuario_id=u.id, descricao="Produto genérico")
    db.add(prod)
    db.commit()
    itens = [{"item": n, "produto_id": prod.id, "confianca": confianca} for n in numeros]
    db.add(Match(usuario_id=u.id, edital_id=ed.id, score=0.9, nivel="forte", detalhe={"itens": itens}))
    db.commit()


def test_lote_totalmente_coberto_pelo_catalogo():
    db = _sessao()
    u = _usuario(db)
    ed = _edital(db)
    _confirmar_itens(db, u, ed, 1, 2, 3)
    resultado = {"status": "ok", "lotes": [{"numero": "1", "itens": [1, 2, 3], "descricao": "Papelaria"}]}

    r = _anexar_cobertura_lotes(resultado, ed, u, db)

    assert r["lotes_cobertura"] == [{
        "numero": "1", "descricao": "Papelaria", "itens": [1, 2, 3],
        "cobre_tudo": True, "itens_faltando": [],
    }]


def test_lote_parcialmente_coberto_lista_itens_faltando():
    db = _sessao()
    u = _usuario(db)
    ed = _edital(db)
    _confirmar_itens(db, u, ed, 1, 3)   # falta o item 2
    resultado = {"status": "ok", "lotes": [{"numero": "1", "itens": [1, 2, 3], "descricao": "Papelaria"}]}

    r = _anexar_cobertura_lotes(resultado, ed, u, db)

    assert r["lotes_cobertura"][0]["cobre_tudo"] is False
    assert r["lotes_cobertura"][0]["itens_faltando"] == [2]


def test_lote_sem_nenhum_item_confirmado_nao_cobre():
    db = _sessao()
    u = _usuario(db)
    ed = _edital(db)
    resultado = {"status": "ok", "lotes": [{"numero": "1", "itens": [1, 2], "descricao": "x"}]}

    r = _anexar_cobertura_lotes(resultado, ed, u, db)

    assert r["lotes_cobertura"][0]["cobre_tudo"] is False
    assert r["lotes_cobertura"][0]["itens_faltando"] == [1, 2]


def test_varios_lotes_cada_um_com_cobertura_independente():
    db = _sessao()
    u = _usuario(db)
    ed = _edital(db)
    _confirmar_itens(db, u, ed, 1, 2, 4)   # lote 1 (itens 1,2) coberto; lote 2 (itens 3,4) não
    resultado = {"status": "ok", "lotes": [
        {"numero": "1", "itens": [1, 2], "descricao": "A"},
        {"numero": "2", "itens": [3, 4], "descricao": "B"},
    ]}

    r = _anexar_cobertura_lotes(resultado, ed, u, db)

    por_numero = {l["numero"]: l for l in r["lotes_cobertura"]}
    assert por_numero["1"]["cobre_tudo"] is True
    assert por_numero["2"]["cobre_tudo"] is False
    assert por_numero["2"]["itens_faltando"] == [3]


def test_item_de_confianca_media_nao_confirmado_nao_conta_como_coberto():
    """Mesmo critério já usado em Cotação/Proposta: confiança média é só
    sugestão, não vira "compatível" até o usuário confirmar manualmente."""
    db = _sessao()
    u = _usuario(db)
    ed = _edital(db)
    _confirmar_itens(db, u, ed, 1, confianca="media")
    resultado = {"status": "ok", "lotes": [{"numero": "1", "itens": [1], "descricao": "x"}]}

    r = _anexar_cobertura_lotes(resultado, ed, u, db)

    assert r["lotes_cobertura"][0]["cobre_tudo"] is False


def test_sem_lotes_no_resultado_nao_adiciona_lotes_cobertura():
    db = _sessao()
    u = _usuario(db)
    ed = _edital(db)
    resultado = {"status": "ok", "lotes": []}

    r = _anexar_cobertura_lotes(resultado, ed, u, db)

    assert "lotes_cobertura" not in r


def test_status_diferente_de_ok_nao_processa():
    db = _sessao()
    u = _usuario(db)
    ed = _edital(db)
    resultado = {"status": "sem_ia"}

    r = _anexar_cobertura_lotes(resultado, ed, u, db)

    assert "lotes_cobertura" not in r
