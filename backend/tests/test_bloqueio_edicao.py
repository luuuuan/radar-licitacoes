"""
Pedido do usuário: bloquear edições do edital (análise por IA, troca/
confirmação de item, preço de cotação, proposta) quando (a) o status já
foi marcado "ganho" (processo resolvido) OU (b) o prazo de propostas
encerrou E nenhum item deste edital foi selecionado (perdeu a janela, sem
engajamento nenhum). Edital "recebendo"/"aguardando", ou encerrado mas com
item confirmado, continua liberado. Rode com:  cd backend && pytest
"""
from datetime import date, timedelta

import pytest
from fastapi import BackgroundTasks, HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.main import (
    _bloqueio_edicao_edital, confirmar_item_edital, definir_preco_cotacao,
    analise_edital_iniciar, salvar_proposta, edital_detalhe, completar_descricao_itens,
    ConfirmarItemIn, PrecoCotacaoIn, PropostaIn,
)
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


def _edital_com_item(db, numero=1, data_abertura=None, data_encerramento=None):
    ed = Edital(fonte="PNCP", id_externo="ed1", orgao="Orgao Teste", objeto="Aquisicao", uf="SP",
               data_abertura=data_abertura, data_encerramento=data_encerramento)
    db.add(ed)
    db.commit()
    db.add(ItemEdital(edital_id=ed.id, numero=numero, descricao="Papel A4 75g",
                      quantidade=10, valor_unitario=50.0))
    db.commit()
    return ed


def _match(db, ed, u, status="novo", detalhe=None):
    m = Match(usuario_id=u.id, edital_id=ed.id, score=0.5, nivel="medio", status=status, detalhe=detalhe)
    db.add(m)
    db.commit()
    return m


HOJE = date.today()
ENCERRADO = dict(data_abertura=HOJE - timedelta(days=20), data_encerramento=HOJE - timedelta(days=5))
RECEBENDO = dict(data_abertura=HOJE - timedelta(days=2), data_encerramento=HOJE + timedelta(days=5))
AGUARDANDO = dict(data_abertura=HOJE + timedelta(days=3), data_encerramento=HOJE + timedelta(days=10))


# --------- _bloqueio_edicao_edital (função pura) --------- #

def test_liberado_quando_aguardando_sem_match():
    db = _sessao()
    u = _usuario(db)
    ed = _edital_com_item(db, **AGUARDANDO)
    assert _bloqueio_edicao_edital(ed, None, u, db) is None


def test_liberado_quando_recebendo_sem_nada_selecionado():
    db = _sessao()
    u = _usuario(db)
    ed = _edital_com_item(db, **RECEBENDO)
    assert _bloqueio_edicao_edital(ed, None, u, db) is None


def test_bloqueado_quando_encerrado_sem_nada_selecionado():
    db = _sessao()
    u = _usuario(db)
    ed = _edital_com_item(db, **ENCERRADO)
    assert _bloqueio_edicao_edital(ed, None, u, db) is not None


def test_liberado_quando_encerrado_mas_com_item_confirmado():
    db = _sessao()
    u = _usuario(db)
    ed = _edital_com_item(db, **ENCERRADO)
    prod = Produto(usuario_id=u.id, descricao="Papel A4")
    db.add(prod)
    db.commit()
    match = _match(db, ed, u, detalhe={
        "itens": [{"item": 1, "produto_id": prod.id, "confianca": "alta", "confirmado_manualmente": True}]
    })

    assert _bloqueio_edicao_edital(ed, match, u, db) is None


def test_bloqueado_quando_status_ganho_mesmo_ainda_recebendo():
    db = _sessao()
    u = _usuario(db)
    ed = _edital_com_item(db, **RECEBENDO)
    match = _match(db, ed, u, status="ganho")

    assert _bloqueio_edicao_edital(ed, match, u, db) is not None


def test_liberado_quando_status_qualquer_outro_e_recebendo():
    db = _sessao()
    u = _usuario(db)
    ed = _edital_com_item(db, **RECEBENDO)
    match = _match(db, ed, u, status="vou_participar")

    assert _bloqueio_edicao_edital(ed, match, u, db) is None


# --------- aplicado nos endpoints --------- #

def test_confirmar_item_bloqueado_quando_encerrado_sem_selecao():
    db = _sessao()
    u = _usuario(db)
    ed = _edital_com_item(db, **ENCERRADO)
    with pytest.raises(HTTPException) as exc:
        confirmar_item_edital(ed.id, 1, ConfirmarItemIn(produto_id=None), user=u, db=db)
    assert exc.value.status_code == 403


def test_confirmar_item_bloqueado_quando_ganho():
    db = _sessao()
    u = _usuario(db)
    ed = _edital_com_item(db, **RECEBENDO)
    _match(db, ed, u, status="ganho")
    with pytest.raises(HTTPException) as exc:
        confirmar_item_edital(ed.id, 1, ConfirmarItemIn(produto_id=None), user=u, db=db)
    assert exc.value.status_code == 403


def test_confirmar_item_liberado_quando_recebendo():
    db = _sessao()
    u = _usuario(db)
    ed = _edital_com_item(db, **RECEBENDO)
    r = confirmar_item_edital(ed.id, 1, ConfirmarItemIn(produto_id=None), user=u, db=db)
    assert r == {"ok": True}


def test_definir_preco_cotacao_bloqueado_quando_encerrado_sem_selecao():
    db = _sessao()
    u = _usuario(db)
    ed = _edital_com_item(db, **ENCERRADO)
    with pytest.raises(HTTPException) as exc:
        definir_preco_cotacao(ed.id, 1, PrecoCotacaoIn(valor=10.0), user=u, db=db)
    assert exc.value.status_code == 403


def test_definir_preco_cotacao_liberado_quando_encerrado_com_selecao():
    db = _sessao()
    u = _usuario(db)
    ed = _edital_com_item(db, **ENCERRADO)
    prod = Produto(usuario_id=u.id, descricao="Papel A4")
    db.add(prod)
    db.commit()
    _match(db, ed, u, detalhe={
        "itens": [{"item": 1, "produto_id": prod.id, "confianca": "alta", "confirmado_manualmente": True}]
    })

    r = definir_preco_cotacao(ed.id, 1, PrecoCotacaoIn(valor=10.0), user=u, db=db)
    assert r == {"ok": True}


def test_analise_iniciar_bloqueada_quando_ganho():
    db = _sessao()
    u = _usuario(db)
    ed = _edital_com_item(db, **RECEBENDO)
    _match(db, ed, u, status="ganho")
    with pytest.raises(HTTPException) as exc:
        analise_edital_iniciar(ed.id, BackgroundTasks(), forcar=False, user=u, db=db)
    assert exc.value.status_code == 403


def test_analise_iniciar_liberada_quando_recebendo():
    db = _sessao()
    u = _usuario(db)
    ed = _edital_com_item(db, **RECEBENDO)
    r = analise_edital_iniciar(ed.id, BackgroundTasks(), forcar=False, user=u, db=db)
    assert r["ok"] is True


def test_completar_descricao_bloqueada_quando_ganho():
    """Achado real (auditoria do agente architect-reviewer): este endpoint é
    irmão de analise_edital_iniciar (mesmo tipo de job pago em IA que
    sobrescreve dado do item) mas tinha ficado sem o guard."""
    db = _sessao()
    u = _usuario(db)
    ed = _edital_com_item(db, **RECEBENDO)
    _match(db, ed, u, status="ganho")
    with pytest.raises(HTTPException) as exc:
        completar_descricao_itens(ed.id, BackgroundTasks(), user=u, db=db)
    assert exc.value.status_code == 403


def test_completar_descricao_bloqueada_quando_encerrado_sem_selecao():
    db = _sessao()
    u = _usuario(db)
    ed = _edital_com_item(db, **ENCERRADO)
    with pytest.raises(HTTPException) as exc:
        completar_descricao_itens(ed.id, BackgroundTasks(), user=u, db=db)
    assert exc.value.status_code == 403


def test_completar_descricao_liberada_quando_recebendo():
    db = _sessao()
    u = _usuario(db)
    ed = _edital_com_item(db, **RECEBENDO)
    r = completar_descricao_itens(ed.id, BackgroundTasks(), user=u, db=db)
    assert r["ok"] is True


def test_salvar_proposta_bloqueada_quando_encerrado_sem_selecao():
    db = _sessao()
    u = _usuario(db)
    ed = _edital_com_item(db, **ENCERRADO)
    with pytest.raises(HTTPException) as exc:
        salvar_proposta(ed.id, PropostaIn(itens=[], observacoes=""), user=u, db=db)
    assert exc.value.status_code == 403


# --------- edital_detalhe expõe bloqueio_edicao (front usa pra desabilitar botão) --------- #

def test_detalhe_expoe_bloqueio_edicao_none_quando_liberado():
    db = _sessao()
    u = _usuario(db)
    ed = _edital_com_item(db, **RECEBENDO)
    r = edital_detalhe(ed.id, user=u, db=db)
    assert r["edital"]["bloqueio_edicao"] is None


def test_detalhe_expoe_bloqueio_edicao_com_motivo_quando_bloqueado():
    db = _sessao()
    u = _usuario(db)
    ed = _edital_com_item(db, **ENCERRADO)
    r = edital_detalhe(ed.id, user=u, db=db)
    assert r["edital"]["bloqueio_edicao"]
    assert isinstance(r["edital"]["bloqueio_edicao"], str)


def test_salvar_proposta_bloqueada_quando_ganho_mesmo_com_selecao_anterior():
    """"ganho" bloqueia mesmo com item selecionado -- é o único caso onde
    ter selecionado item não muda nada (o processo já foi decidido)."""
    db = _sessao()
    u = _usuario(db)
    ed = _edital_com_item(db, **RECEBENDO)
    prod = Produto(usuario_id=u.id, descricao="Papel A4")
    db.add(prod)
    db.commit()
    _match(db, ed, u, status="ganho", detalhe={
        "itens": [{"item": 1, "produto_id": prod.id, "confianca": "alta", "confirmado_manualmente": True}]
    })

    with pytest.raises(HTTPException) as exc:
        salvar_proposta(ed.id, PropostaIn(itens=[], observacoes=""), user=u, db=db)
    assert exc.value.status_code == 403
