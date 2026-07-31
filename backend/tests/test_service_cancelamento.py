"""
Testes do cancelamento cooperativo de recálculo/coleta (banco sqlite em
memória, sem HTTP). Rode com:  cd backend && pytest
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, Usuario, Edital, Produto
from app.service import _gerar_matches_usuario


def _sessao():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def _semear(db, n_editais):
    u = Usuario(nome="Teste", email="t@t.com", senha_hash="x")
    db.add(u)
    db.commit()
    db.add(Produto(usuario_id=u.id, descricao="Caneta esferografica azul",
                   palavras_chave="caneta, esferografica, azul"))
    for i in range(n_editais):
        db.add(Edital(fonte="PNCP", id_externo=f"ed{i}", objeto="Aquisicao de caneta esferografica",
                      orgao="Orgao Teste", uf="SP"))
    db.commit()
    return u


def test_cancelamento_para_a_rodada_e_mantem_o_progresso_ja_commitado():
    """deve_cancelar() checado no mesmo ponto dos commits parciais (a cada
    200 editais) — precisa de amostra grande o bastante pra passar por pelo
    menos um checkpoint."""
    db = _sessao()
    u = _semear(db, 250)
    resumo = _gerar_matches_usuario(db, u, recalcular_todos=True, deve_cancelar=lambda: True)
    assert resumo.get("cancelado") is True
    # parou no primeiro checkpoint (200) — não processou os 250 completos
    assert resumo["editais"] <= 200


def test_sem_cancelamento_processa_tudo_normalmente():
    db = _sessao()
    u = _semear(db, 250)
    resumo = _gerar_matches_usuario(db, u, recalcular_todos=True, deve_cancelar=lambda: False)
    assert not resumo.get("cancelado")
    assert resumo["editais"] == 250
