"""
GET /api/pipeline -- editais do usuário organizados por Match.status, pra
visão de funil (aba Pipeline, pedido do usuário). Banco sqlite em memória,
sem HTTP. Rode com:  cd backend && pytest
"""
from datetime import datetime, timedelta

from fastapi import HTTPException
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.main import pipeline, pipeline_remover_card, mudar_status, StatusIn
from app.models import Base, Usuario, Edital, Match


def _sessao():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def _usuario(db, email="t@t.com"):
    u = Usuario(nome="Teste", email=email, senha_hash="x")
    db.add(u)
    db.commit()
    return u


def _match(db, usuario, id_externo, status="novo", nivel="medio", score=0.5, data_encerramento=None):
    ed = Edital(fonte="PNCP", id_externo=id_externo, orgao=f"Orgao {id_externo}",
               objeto="Aquisicao", uf="SP", data_encerramento=data_encerramento)
    db.add(ed)
    db.commit()
    db.add(Match(usuario_id=usuario.id, edital_id=ed.id, score=score, nivel=nivel, status=status))
    db.commit()
    return ed


def test_pipeline_agrupa_por_status():
    db = _sessao()
    u = _usuario(db)
    ed_participar = _match(db, u, "ed1", status="vou_participar")
    ed_proposta = _match(db, u, "ed2", status="proposta_enviada")
    ed_ganho = _match(db, u, "ed3", status="ganho")

    r = pipeline(user=u, db=db)

    assert [c["edital_id"] for c in r["colunas"]["vou_participar"]] == [ed_participar.id]
    assert [c["edital_id"] for c in r["colunas"]["proposta_enviada"]] == [ed_proposta.id]
    assert [c["edital_id"] for c in r["colunas"]["ganho"]] == [ed_ganho.id]


def test_pipeline_inclui_nivel_forte_com_status_novo():
    """Fila de triagem: alta compatibilidade que o usuário ainda não decidiu
    o que fazer continua aparecendo na coluna Novo."""
    db = _sessao()
    u = _usuario(db)
    ed = _match(db, u, "ed1", status="novo", nivel="forte")

    r = pipeline(user=u, db=db)

    assert [c["edital_id"] for c in r["colunas"]["novo"]] == [ed.id]


def test_pipeline_exclui_novo_com_nivel_medio_ou_fraco():
    """Achado real (pedido do usuário): sem esse filtro, a coluna "Novo"
    teria TODO match que o motor automático criou -- a maioria nunca vista
    pelo usuário. Só nivel forte entra sem ação nenhuma; médio/fraco só
    aparecem quando o usuário já mexeu no status."""
    db = _sessao()
    u = _usuario(db)
    _match(db, u, "ed-medio", status="novo", nivel="medio")
    _match(db, u, "ed-fraco", status="novo", nivel="fraco")

    r = pipeline(user=u, db=db)

    assert r["colunas"]["novo"] == []


def test_pipeline_nao_mostra_match_de_outro_usuario():
    db = _sessao()
    u1 = _usuario(db, email="u1@t.com")
    u2 = _usuario(db, email="u2@t.com")
    _match(db, u1, "ed1", status="ganho")

    r = pipeline(user=u2, db=db)

    assert all(len(itens) == 0 for itens in r["colunas"].values())


def test_pipeline_ordem_das_colunas_e_o_fluxo_natural():
    db = _sessao()
    u = _usuario(db)

    r = pipeline(user=u, db=db)

    assert r["ordem"] == ["novo", "vou_participar", "proposta_enviada", "ganho", "perdido", "descartado"]


def test_pipeline_card_traz_campos_para_o_kanban():
    db = _sessao()
    u = _usuario(db)
    ed = _match(db, u, "ed1", status="vou_participar", nivel="forte", score=0.9)

    r = pipeline(user=u, db=db)

    card = r["colunas"]["vou_participar"][0]
    assert card["edital_id"] == ed.id
    assert card["orgao"] == "Orgao ed1"
    assert card["nivel"] == "forte"
    assert card["score"] == 0.9
    assert "dias_restantes" in card and "status_prazo" in card


def test_pipeline_exclui_novo_forte_com_edital_ja_encerrado():
    """Pedido do usuário: uma triagem "novo"+forte que nunca foi decidida e
    cujo prazo de propostas já passou não é mais acionável -- só polui a
    coluna Novo. Continua excluída mesmo sendo nivel forte (que normalmente
    é o único jeito de um "novo" aparecer)."""
    db = _sessao()
    u = _usuario(db)
    _match(db, u, "ed1", status="novo", nivel="forte",
           data_encerramento=datetime.utcnow() - timedelta(days=1))

    r = pipeline(user=u, db=db)

    assert r["colunas"]["novo"] == []


def test_pipeline_mantem_novo_forte_com_edital_ainda_nao_encerrado():
    db = _sessao()
    u = _usuario(db)
    ed = _match(db, u, "ed1", status="novo", nivel="forte",
                data_encerramento=datetime.utcnow() + timedelta(days=3))

    r = pipeline(user=u, db=db)

    assert [c["edital_id"] for c in r["colunas"]["novo"]] == [ed.id]


def test_pipeline_remover_card_some_da_pipeline_sem_mexer_no_resto():
    """Botão "excluir" no card do Pipeline (pedido do usuário): esconde só
    da visão de funil, sem tocar em status/lido/interessante/nivel -- nada
    mais no app é afetado."""
    db = _sessao()
    u = _usuario(db)
    ed = _match(db, u, "ed1", status="vou_participar", nivel="forte")

    pipeline_remover_card(edital_id=ed.id, user=u, db=db)

    r = pipeline(user=u, db=db)
    assert r["colunas"]["vou_participar"] == []

    m = db.query(Match).filter(Match.edital_id == ed.id, Match.usuario_id == u.id).one()
    assert m.status == "vou_participar"   # nada mais mudou
    assert m.nivel == "forte"


def test_pipeline_remover_card_404_quando_edital_nao_existe():
    db = _sessao()
    u = _usuario(db)
    with pytest.raises(HTTPException) as exc:
        pipeline_remover_card(edital_id=99999, user=u, db=db)
    assert exc.value.status_code == 404


def test_mudar_status_desfaz_remocao_anterior_do_pipeline():
    """Achado real: sem isso, remover um card do Pipeline seria um beco sem
    saída -- reengajamento explícito (o usuário mudou o status de novo, seja
    arrastando ou pelo select) precisa trazer o card de volta."""
    db = _sessao()
    u = _usuario(db)
    ed = _match(db, u, "ed1", status="vou_participar", nivel="forte")
    pipeline_remover_card(edital_id=ed.id, user=u, db=db)
    assert pipeline(user=u, db=db)["colunas"]["vou_participar"] == []

    mudar_status(edital_id=ed.id, dados=StatusIn(status="proposta_enviada"), user=u, db=db)

    r = pipeline(user=u, db=db)
    assert [c["edital_id"] for c in r["colunas"]["proposta_enviada"]] == [ed.id]
