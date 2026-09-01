"""
Testes de POST /api/perfil/senha (troca de senha estando logado). Banco
sqlite em memória, sem HTTP. Rode com:  cd backend && pytest
"""
import pytest
from fastapi import BackgroundTasks, HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import auth
from app.main import trocar_senha, TrocarSenhaIn
from app.models import Base, Usuario


def _sessao():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def _usuario(db, senha="SenhaAtual1!"):
    u = Usuario(nome="Teste", email="senha@teste.com", senha_hash=auth.hash_senha(senha))
    db.add(u)
    db.commit()
    return u


def test_troca_senha_com_atual_correta():
    db = _sessao()
    u = _usuario(db)
    r = trocar_senha(TrocarSenhaIn(senha_atual="SenhaAtual1!", senha_nova="SenhaNova2@"),
                     bg=BackgroundTasks(), user=u, db=db)
    assert r == {"ok": True}
    assert auth.conferir_senha("SenhaNova2@", u.senha_hash)
    assert not auth.conferir_senha("SenhaAtual1!", u.senha_hash)


def test_troca_senha_rejeita_senha_atual_incorreta():
    db = _sessao()
    u = _usuario(db)
    hash_original = u.senha_hash
    with pytest.raises(HTTPException) as exc:
        trocar_senha(TrocarSenhaIn(senha_atual="SenhaErrada1!", senha_nova="SenhaNova2@"),
                     bg=BackgroundTasks(), user=u, db=db)
    assert exc.value.status_code == 401
    assert u.senha_hash == hash_original


def test_troca_senha_rejeita_senha_nova_fraca():
    db = _sessao()
    u = _usuario(db)
    hash_original = u.senha_hash
    with pytest.raises(HTTPException) as exc:
        trocar_senha(TrocarSenhaIn(senha_atual="SenhaAtual1!", senha_nova="123"),
                     bg=BackgroundTasks(), user=u, db=db)
    assert exc.value.status_code == 400
    assert u.senha_hash == hash_original


def test_troca_senha_nao_envia_email_sem_smtp_configurado(monkeypatch):
    """Em dev (sem SMTP configurado) a troca continua funcionando normalmente,
    só não dispara o e-mail de aviso -- mesmo padrão de /esqueci-senha."""
    from app.main import _email_mod
    monkeypatch.setattr(_email_mod, "smtp_configurado", lambda: False)
    db = _sessao()
    u = _usuario(db)
    bg = BackgroundTasks()
    r = trocar_senha(TrocarSenhaIn(senha_atual="SenhaAtual1!", senha_nova="SenhaNova2@"),
                     bg=bg, user=u, db=db)
    assert r == {"ok": True}
    assert bg.tasks == []
