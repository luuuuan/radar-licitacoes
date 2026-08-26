"""
Achado do agente debugger em app/configuracoes.py: obter() tratava "linha
existe mas valor é vazio" igual a "nunca configurado", caindo pro fallback
de app/config.py -- pra PNCP_UFS/PNCP_MODALIDADES (onde vazio = "todas", ver
connectors/pncp.py), um usuário limpando o campo no painel nunca conseguia
deixar vazio de propósito: a leitura seguinte revertia pro padrão de
ambiente. Rode com:  cd backend && pytest
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import configuracoes as cfg
from app.config import settings
from app.models import Base


def _sessao():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def test_obter_sem_linha_nenhuma_cai_no_fallback_do_ambiente(monkeypatch):
    monkeypatch.setattr(settings, "PNCP_UFS", "PR,SP")
    db = _sessao()
    assert cfg.obter(db, "PNCP_UFS") == "PR,SP"


def test_obter_com_valor_definido_usa_o_valor_do_painel():
    db = _sessao()
    cfg.definir(db, "PNCP_UFS", "MG")
    assert cfg.obter(db, "PNCP_UFS") == "MG"


def test_obter_com_valor_explicitamente_vazio_respeita_o_vazio_em_vez_do_fallback(monkeypatch):
    """O achado real: limpar o campo (salvar "") tem que valer -- não pode
    reverter pro padrão do ambiente."""
    monkeypatch.setattr(settings, "PNCP_UFS", "PR,SP")
    db = _sessao()
    cfg.definir(db, "PNCP_UFS", "MG")   # define um valor primeiro
    cfg.definir(db, "PNCP_UFS", "")     # depois limpa de propósito
    assert cfg.obter(db, "PNCP_UFS") == ""


def test_definir_com_none_grava_como_string_vazia():
    db = _sessao()
    cfg.definir(db, "PNCP_UFS", None)
    assert cfg.obter(db, "PNCP_UFS") == ""


def test_todas_usa_obter_pra_cada_chave_suportada():
    db = _sessao()
    cfg.definir(db, "PNCP_MODALIDADES", "6,8")
    valores = cfg.todas(db)
    assert valores["PNCP_MODALIDADES"] == "6,8"
    assert set(valores.keys()) == set(cfg._FALLBACK.keys())
