"""
Testes de POST /api/editais/{id}/itens/{numero}/confirmar. Banco sqlite em
memória, sem HTTP — chama a função da rota diretamente (mesmo padrão de
test_editais_filtros.py).

Achado real: um item cujo score no motor de matching ficou abaixo de
LIMIAR_ITEM_SUGESTAO nunca entra em match.detalhe["itens"] (matching/engine.py
pula ele de propósito, pra não sujar a lista de sugestões com "candidatos"
sem nenhuma relação). Só que a comparação de catálogo por IA (aba Análise por
IA) é uma busca INDEPENDENTE — pode sugerir um produto pra esse mesmo item
mesmo assim. Confirmar essa sugestão batia num 404 ("Item não encontrado"),
porque o endpoint só sabia atualizar um item que já existia no detalhe.

Rode com:  cd backend && pytest
"""
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.main import confirmar_item_edital, ConfirmarItemIn
from app.models import Base, Usuario, Edital, ItemEdital, Match, Produto


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


def _produto(db, usuario, descricao="Grampeador de mesa 26/6"):
    p = Produto(usuario_id=usuario.id, descricao=descricao)
    db.add(p)
    db.commit()
    return p


def _edital(db, itens_numeros=(1, 2)):
    ed = Edital(fonte="PNCP", id_externo="ed1", orgao="Orgao Teste", objeto="Aquisicao", uf="SP")
    db.add(ed)
    db.commit()
    for numero in itens_numeros:
        db.add(ItemEdital(edital_id=ed.id, numero=numero, descricao=f"Item {numero}"))
    db.commit()
    return ed


def test_confirma_item_ja_presente_no_detalhe():
    db = _sessao()
    u = _usuario(db)
    p = _produto(db, u)
    ed = _edital(db)
    match = Match(usuario_id=u.id, edital_id=ed.id, score=0.5, nivel="medio",
                  detalhe={"itens": [{"item": 1, "confianca": "media", "candidatos": []}]})
    db.add(match)
    db.commit()

    r = confirmar_item_edital(ed.id, 1, ConfirmarItemIn(produto_id=p.id), user=u, db=db)

    assert r == {"ok": True}
    db.refresh(match)
    item = next(d for d in match.detalhe["itens"] if d["item"] == 1)
    assert item["produto_id"] == p.id
    assert item["confirmado_manualmente"] is True


def test_confirma_item_que_nunca_entrou_no_detalhe_do_motor():
    """Regressão do 404: item existe no edital mas o motor nunca o colocou em
    match.detalhe (score abaixo de LIMIAR_ITEM_SUGESTAO) — a sugestão veio só
    da Análise por IA. Confirmar precisa CRIAR a entrada, não dar 404."""
    db = _sessao()
    u = _usuario(db)
    p = _produto(db, u)
    ed = _edital(db, itens_numeros=(1, 2))
    match = Match(usuario_id=u.id, edital_id=ed.id, score=0.5, nivel="medio",
                  detalhe={"itens": [{"item": 1, "confianca": "media", "candidatos": []}]})
    db.add(match)
    db.commit()

    r = confirmar_item_edital(ed.id, 2, ConfirmarItemIn(produto_id=p.id), user=u, db=db)

    assert r == {"ok": True}
    db.refresh(match)
    assert len(match.detalhe["itens"]) == 2
    item = next(d for d in match.detalhe["itens"] if d["item"] == 2)
    assert item["produto_id"] == p.id
    assert item["produto"] == p.descricao
    assert item["confirmado_manualmente"] is True
    # o item 1, que já existia, continua intacto
    item1 = next(d for d in match.detalhe["itens"] if d["item"] == 1)
    assert item1["confianca"] == "media"


def test_confirma_item_inexistente_no_edital_continua_404():
    db = _sessao()
    u = _usuario(db)
    p = _produto(db, u)
    ed = _edital(db, itens_numeros=(1,))
    match = Match(usuario_id=u.id, edital_id=ed.id, score=0.5, nivel="medio", detalhe={"itens": []})
    db.add(match)
    db.commit()

    with pytest.raises(HTTPException) as exc:
        confirmar_item_edital(ed.id, 99, ConfirmarItemIn(produto_id=p.id), user=u, db=db)
    assert exc.value.status_code == 404


def test_confirma_com_produto_id_none_marca_nenhuma_destas():
    db = _sessao()
    u = _usuario(db)
    ed = _edital(db, itens_numeros=(1,))
    match = Match(usuario_id=u.id, edital_id=ed.id, score=0.5, nivel="medio",
                  detalhe={"itens": [{"item": 1, "produto_id": 5, "produto": "X", "confianca": "media"}]})
    db.add(match)
    db.commit()

    confirmar_item_edital(ed.id, 1, ConfirmarItemIn(produto_id=None), user=u, db=db)

    db.refresh(match)
    item = next(d for d in match.detalhe["itens"] if d["item"] == 1)
    assert item["produto_id"] is None
    assert item["confirmado_manualmente"] is True
