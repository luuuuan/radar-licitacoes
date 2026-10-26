"""
Achado real (auditoria de arquitetura pedida pelo usuário): as rotas
administrativas (/api/admin/*, POST /api/config) exigiam só login -- qualquer
conta criada pelo cadastro público conseguia apagar editais de todo mundo,
disparar um backfill em lote ou mudar o que o app inteiro coleta. Protegidas
agora por _exigir_admin (app/main.py), restrito à conta operadora (user id 5,
confirmado com o usuário -- mesmo padrão já usado em
_USUARIOS_SELETOR_RERANKER). Rode com: cd backend && pytest
"""
import pytest
from fastapi import BackgroundTasks, HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import main as app_main
from app.main import (
    _exigir_admin, backfill_unidade_medida_endpoint, backfill_unidade_medida_status,
    podar_editais_orfaos_endpoint, podar_editais_orfaos_status, salvar_config, ConfigIn,
)
from app.models import Base, Usuario


def _sessao():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def _usuario(db, id_forcado: int, email: str) -> Usuario:
    """Força o id (SQLite permite inserir PK explícita) -- _exigir_admin
    decide pelo id da conta, não por nenhum outro campo."""
    u = Usuario(id=id_forcado, nome="Teste", email=email, senha_hash="x")
    db.add(u)
    db.commit()
    return u


def test_exigir_admin_rejeita_conta_comum():
    with pytest.raises(HTTPException) as exc:
        _exigir_admin(Usuario(id=999, nome="Comum", email="comum@t.com", senha_hash="x"))
    assert exc.value.status_code == 403


def test_exigir_admin_aceita_a_conta_operadora():
    _exigir_admin(Usuario(id=5, nome="Admin", email="admin@t.com", senha_hash="x"))   # não levanta


@pytest.mark.parametrize("rota,kwargs", [
    (backfill_unidade_medida_status, {}),
    (podar_editais_orfaos_status, {}),
])
def test_rotas_status_admin_rejeitam_conta_comum(rota, kwargs):
    db = _sessao()
    u = _usuario(db, 42, "comum@t.com")
    with pytest.raises(HTTPException) as exc:
        rota(user=u, **kwargs)
    assert exc.value.status_code == 403


def test_backfill_unidade_medida_rejeita_conta_comum():
    db = _sessao()
    u = _usuario(db, 42, "comum@t.com")
    with pytest.raises(HTTPException) as exc:
        backfill_unidade_medida_endpoint(BackgroundTasks(), edital_id=None, user=u, db=db)
    assert exc.value.status_code == 403


def test_podar_editais_orfaos_rejeita_conta_comum():
    db = _sessao()
    u = _usuario(db, 42, "comum@t.com")
    with pytest.raises(HTTPException) as exc:
        podar_editais_orfaos_endpoint(BackgroundTasks(), user=u)
    assert exc.value.status_code == 403


def test_salvar_config_rejeita_conta_comum():
    db = _sessao()
    u = _usuario(db, 42, "comum@t.com")
    with pytest.raises(HTTPException) as exc:
        salvar_config(ConfigIn(IA_ATIVA="1"), user=u, db=db)
    assert exc.value.status_code == 403


def test_salvar_config_aceita_a_conta_operadora():
    db = _sessao()
    u = _usuario(db, 5, "admin@t.com")
    r = salvar_config(ConfigIn(IA_ATIVA="1"), user=u, db=db)
    assert r["ok"] is True
    assert r["config"]["IA_ATIVA"] == "1"


def test_backfill_unidade_medida_status_aceita_a_conta_operadora():
    db = _sessao()
    u = _usuario(db, 5, "admin@t.com")
    r = backfill_unidade_medida_status(user=u)
    assert r == app_main._backfill_unidade_status
