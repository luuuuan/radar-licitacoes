"""
Testes do checkpoint de recálculo completo (recalcular_todos=True) —
permite retomar de onde parou se o processo for interrompido (ex.: deploy
no meio de uma rodada longa) em vez de regastar cota de IA reprocessando
editais já feitos. Banco sqlite em memória, sem HTTP.
Rode com:  cd backend && pytest
"""
from datetime import timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, Usuario, Edital, Match, utcnow
from app.service import _gerar_matches_usuario, RECALCULO_CHECKPOINT_VALIDADE


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


def _edital(db, id_externo, coletado_em):
    ed = Edital(fonte="PNCP", id_externo=id_externo, orgao="Orgao Teste",
               objeto="Aquisicao qualquer", uf="SP", coletado_em=coletado_em)
    db.add(ed)
    db.commit()
    return ed


def _muitos_editais(db, n):
    """N editais simples (sem itens), coletado_em decrescente pela ordem de
    criação (o mais novo primeiro) — o suficiente pra cruzar o limiar de
    commit parcial (200) sem precisar simular chamadas de IA de verdade."""
    base = utcnow()
    out = []
    for i in range(n):
        out.append(_edital(db, f"ed{i}", base - timedelta(minutes=i)))
    return out


def test_sem_checkpoint_processa_todos_editais():
    db = _sessao()
    u = _usuario(db)
    a = _edital(db, "a", utcnow())
    b = _edital(db, "b", utcnow() - timedelta(hours=1))

    resumo = _gerar_matches_usuario(db, u, recalcular_todos=True, forcar_usar_ia=False)

    assert resumo["editais"] == 2


def test_checkpoint_fresco_pula_editais_ja_processados():
    db = _sessao()
    u = _usuario(db)
    a = _edital(db, "a", utcnow())              # mais novo -> processado primeiro
    b = _edital(db, "b", utcnow() - timedelta(hours=1))
    u.recalculo_checkpoint_edital_id = a.id
    u.recalculo_checkpoint_coletado_em = a.coletado_em
    u.recalculo_checkpoint_em = utcnow()
    db.commit()

    resumo = _gerar_matches_usuario(db, u, recalcular_todos=True, forcar_usar_ia=False)

    # só "b" (o que vem DEPOIS do checkpoint na ordem) foi processado
    assert resumo["editais"] == 1


def test_checkpoint_expirado_processa_tudo_de_novo():
    db = _sessao()
    u = _usuario(db)
    a = _edital(db, "a", utcnow())
    b = _edital(db, "b", utcnow() - timedelta(hours=1))
    u.recalculo_checkpoint_edital_id = a.id
    u.recalculo_checkpoint_coletado_em = a.coletado_em
    u.recalculo_checkpoint_em = utcnow() - RECALCULO_CHECKPOINT_VALIDADE - timedelta(minutes=1)
    db.commit()

    resumo = _gerar_matches_usuario(db, u, recalcular_todos=True, forcar_usar_ia=False)

    assert resumo["editais"] == 2


def test_checkpoint_nao_se_aplica_a_recalculo_incremental():
    """recalcular_todos=False (fluxo normal de coleta) ignora o checkpoint —
    ele só existe pro caso de "reavaliar tudo" que pode levar muito tempo."""
    db = _sessao()
    u = _usuario(db)
    a = _edital(db, "a", utcnow())
    b = _edital(db, "b", utcnow() - timedelta(hours=1))
    u.recalculo_checkpoint_edital_id = a.id
    u.recalculo_checkpoint_coletado_em = a.coletado_em
    u.recalculo_checkpoint_em = utcnow()
    db.commit()

    resumo = _gerar_matches_usuario(db, u, recalcular_todos=False, forcar_usar_ia=False)

    assert resumo["editais"] == 2


def test_checkpoint_limpo_apos_rodada_completa_sem_interrupcao():
    db = _sessao()
    u = _usuario(db)
    _edital(db, "a", utcnow())
    u.recalculo_checkpoint_edital_id = None
    db.commit()

    _gerar_matches_usuario(db, u, recalcular_todos=True, forcar_usar_ia=False)

    db.refresh(u)
    assert u.recalculo_checkpoint_em is None
    assert u.recalculo_checkpoint_edital_id is None


def test_checkpoint_gravado_e_preservado_quando_cancelado_no_meio():
    """201 editais (cruza o limiar de commit parcial, 200) + cancelamento
    logo no primeiro checkpoint — os 200 primeiros ficam com o checkpoint
    gravado (não voltam a ser reprocessados do zero na próxima rodada)."""
    db = _sessao()
    u = _usuario(db)
    editais = _muitos_editais(db, 201)

    resumo = _gerar_matches_usuario(db, u, recalcular_todos=True, forcar_usar_ia=False,
                                    deve_cancelar=lambda: True)

    assert resumo.get("cancelado") is True
    db.refresh(u)
    assert u.recalculo_checkpoint_em is not None
    # checkpoint aponta pro 200º edital processado (ordem: mais novo primeiro,
    # editais[0] é o mais novo -> editais[199] é o 200º)
    assert u.recalculo_checkpoint_edital_id == editais[199].id

    # uma nova rodada, retomando, só processa o que sobrou (1 edital)
    resumo2 = _gerar_matches_usuario(db, u, recalcular_todos=True, forcar_usar_ia=False)
    assert resumo2["editais"] == 1
