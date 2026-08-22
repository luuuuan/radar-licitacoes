"""
GET /api/ganhos: editais marcados "ganho" num mês, com valor/custo/margem
vindos da Proposta salva (nunca do valor estimado do edital) -- ver
_totais_ganhos_mes/ganhos em main.py. Alimenta o card "Editais ganhos" do
painel Início. Banco sqlite em memória, sem HTTP. Rode com:
cd backend && pytest
"""
import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.main import ganhos
from app.models import Base, Usuario, Edital, Match, Proposta


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


def _edital(db, id_externo):
    ed = Edital(fonte="PNCP", id_externo=id_externo, orgao="Orgao Teste",
               objeto="Aquisicao", uf="SP", modalidade="Pregao Eletronico")
    db.add(ed)
    db.commit()
    return ed


def _match_ganho(db, usuario, ed, quando):
    m = Match(usuario_id=usuario.id, edital_id=ed.id, score=0.8, nivel="forte",
             status="ganho", status_atualizado_em=quando)
    db.add(m)
    db.commit()
    return m


def _proposta(db, usuario, ed, itens):
    p = Proposta(usuario_id=usuario.id, edital_id=ed.id, itens=itens)
    db.add(p)
    db.commit()
    return p


def test_soma_valor_custo_margem_a_partir_da_proposta_nao_do_edital():
    db = _sessao()
    u = _usuario(db)
    ed = _edital(db, "ed1")
    ed.valor_estimado = 999999.0   # não pode influenciar em nada
    db.commit()
    _match_ganho(db, u, ed, datetime.datetime(2026, 8, 10))
    _proposta(db, u, ed, [
        {"descricao": "Item 1", "quantidade": 10, "custo_unit": 5.0, "preco_unit": 8.0},
    ])

    r = ganhos(ano=2026, mes=8, user=u, db=db)

    assert r["quantidade"] == 1
    assert r["valor_total"] == 80.0
    assert r["custo_total"] == 50.0
    assert r["margem_total"] == 30.0
    assert r["editais"][0]["tem_proposta"] is True
    assert r["editais"][0]["valor_total"] == 80.0


def test_ganho_sem_proposta_conta_na_quantidade_mas_nao_no_valor():
    db = _sessao()
    u = _usuario(db)
    ed = _edital(db, "ed1")
    _match_ganho(db, u, ed, datetime.datetime(2026, 8, 10))
    # nenhuma Proposta salva de propósito

    r = ganhos(ano=2026, mes=8, user=u, db=db)

    assert r["quantidade"] == 1
    assert r["valor_total"] == 0.0
    assert r["editais"][0]["tem_proposta"] is False
    assert "valor_total" not in r["editais"][0]


def test_ignora_ganho_fora_do_mes():
    db = _sessao()
    u = _usuario(db)
    ed = _edital(db, "ed1")
    _match_ganho(db, u, ed, datetime.datetime(2026, 7, 31, 23, 59))

    r = ganhos(ano=2026, mes=8, user=u, db=db)

    assert r["quantidade"] == 0
    assert r["editais"] == []


def test_ignora_match_com_status_diferente_de_ganho():
    db = _sessao()
    u = _usuario(db)
    ed = _edital(db, "ed1")
    db.add(Match(usuario_id=u.id, edital_id=ed.id, score=0.8, nivel="forte",
                 status="perdido", status_atualizado_em=datetime.datetime(2026, 8, 5)))
    db.commit()

    r = ganhos(ano=2026, mes=8, user=u, db=db)

    assert r["quantidade"] == 0


def test_so_retorna_ganhos_do_proprio_usuario():
    db = _sessao()
    u1 = _usuario(db, email="u1@t.com")
    u2 = _usuario(db, email="u2@t.com")
    ed = _edital(db, "ed1")
    _match_ganho(db, u1, ed, datetime.datetime(2026, 8, 10))

    r = ganhos(ano=2026, mes=8, user=u2, db=db)

    assert r["quantidade"] == 0


def test_soma_varios_ganhos_do_mesmo_mes():
    db = _sessao()
    u = _usuario(db)
    ed1 = _edital(db, "ed1")
    ed2 = _edital(db, "ed2")
    _match_ganho(db, u, ed1, datetime.datetime(2026, 8, 3))
    _match_ganho(db, u, ed2, datetime.datetime(2026, 8, 20))
    _proposta(db, u, ed1, [{"quantidade": 1, "custo_unit": 10.0, "preco_unit": 20.0}])
    _proposta(db, u, ed2, [{"quantidade": 1, "custo_unit": 30.0, "preco_unit": 50.0}])

    r = ganhos(ano=2026, mes=8, user=u, db=db)

    assert r["quantidade"] == 2
    assert r["valor_total"] == 70.0
    assert r["custo_total"] == 40.0
    assert r["margem_total"] == 30.0


def test_comparacao_com_mes_anterior_calcula_variacao():
    db = _sessao()
    u = _usuario(db)
    ed_jul = _edital(db, "ed-jul")
    ed_ago = _edital(db, "ed-ago")
    _match_ganho(db, u, ed_jul, datetime.datetime(2026, 7, 15))
    _proposta(db, u, ed_jul, [{"quantidade": 1, "custo_unit": 50.0, "preco_unit": 100.0}])   # margem 50
    _match_ganho(db, u, ed_ago, datetime.datetime(2026, 8, 15))
    _proposta(db, u, ed_ago, [{"quantidade": 1, "custo_unit": 25.0, "preco_unit": 100.0}])   # margem 75

    r = ganhos(ano=2026, mes=8, user=u, db=db)

    assert r["margem_total"] == 75.0
    assert r["margem_variacao_pct"] == 50.0   # (75-50)/50 * 100


def test_comparacao_com_mes_anterior_sem_dados_vira_none():
    db = _sessao()
    u = _usuario(db)
    ed = _edital(db, "ed1")
    _match_ganho(db, u, ed, datetime.datetime(2026, 8, 10))
    _proposta(db, u, ed, [{"quantidade": 1, "custo_unit": 10.0, "preco_unit": 20.0}])

    r = ganhos(ano=2026, mes=8, user=u, db=db)

    assert r["margem_variacao_pct"] is None


def test_comparacao_atravessa_ano_janeiro_olha_dezembro_anterior():
    db = _sessao()
    u = _usuario(db)
    ed_dez = _edital(db, "ed-dez")
    ed_jan = _edital(db, "ed-jan")
    _match_ganho(db, u, ed_dez, datetime.datetime(2025, 12, 20))
    _proposta(db, u, ed_dez, [{"quantidade": 1, "custo_unit": 10.0, "preco_unit": 20.0}])   # margem 10
    _match_ganho(db, u, ed_jan, datetime.datetime(2026, 1, 5))
    _proposta(db, u, ed_jan, [{"quantidade": 1, "custo_unit": 10.0, "preco_unit": 30.0}])   # margem 20

    r = ganhos(ano=2026, mes=1, user=u, db=db)

    assert r["margem_total"] == 20.0
    assert r["margem_variacao_pct"] == 100.0   # (20-10)/10 * 100
