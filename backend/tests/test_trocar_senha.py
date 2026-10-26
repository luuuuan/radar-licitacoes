"""
Testes de POST /api/perfil/senha (troca de senha estando logado). Banco
sqlite em memória, sem HTTP. Rode com:  cd backend && pytest
"""
import pytest
from fastapi import BackgroundTasks, HTTPException, Response
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
                     bg=BackgroundTasks(), resp=Response(), user=u, db=db)
    assert r == {"ok": True}
    assert auth.conferir_senha("SenhaNova2@", u.senha_hash)
    assert not auth.conferir_senha("SenhaAtual1!", u.senha_hash)


def test_troca_senha_rejeita_senha_atual_incorreta():
    db = _sessao()
    u = _usuario(db)
    hash_original = u.senha_hash
    with pytest.raises(HTTPException) as exc:
        trocar_senha(TrocarSenhaIn(senha_atual="SenhaErrada1!", senha_nova="SenhaNova2@"),
                     bg=BackgroundTasks(), resp=Response(), user=u, db=db)
    assert exc.value.status_code == 401
    assert u.senha_hash == hash_original


def test_troca_senha_rejeita_senha_nova_fraca():
    db = _sessao()
    u = _usuario(db)
    hash_original = u.senha_hash
    with pytest.raises(HTTPException) as exc:
        trocar_senha(TrocarSenhaIn(senha_atual="SenhaAtual1!", senha_nova="123"),
                     bg=BackgroundTasks(), resp=Response(), user=u, db=db)
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
                     bg=bg, resp=Response(), user=u, db=db)
    assert r == {"ok": True}
    assert bg.tasks == []


# --------- achado real (auditoria de segurança pedida pelo usuário, item 4): --------- #
# --------- trocar a senha não invalidava sessões já abertas com a senha --------- #
# --------- antiga (ex.: roubada) -- ver Usuario.token_versao --------- #

def test_troca_senha_incrementa_token_versao():
    db = _sessao()
    u = _usuario(db)
    versao_antes = u.token_versao
    trocar_senha(TrocarSenhaIn(senha_atual="SenhaAtual1!", senha_nova="SenhaNova2@"),
                bg=BackgroundTasks(), resp=Response(), user=u, db=db)
    assert u.token_versao == versao_antes + 1


def test_troca_senha_invalida_token_emitido_antes_da_troca():
    db = _sessao()
    u = _usuario(db)
    token_antigo = auth.criar_token(u.id, token_versao=u.token_versao)
    trocar_senha(TrocarSenhaIn(senha_atual="SenhaAtual1!", senha_nova="SenhaNova2@"),
                bg=BackgroundTasks(), resp=Response(), user=u, db=db)

    from starlette.requests import Request
    headers = [(b"cookie", f"{auth.COOKIE_NOME}={token_antigo}".encode())]
    req = Request({"type": "http", "headers": headers, "client": ("1.2.3.4", 1)})
    with pytest.raises(HTTPException) as exc:
        auth.get_current_user(req, Response(), db)
    assert exc.value.status_code == 401


def test_troca_senha_reemite_cookie_valido_pra_sessao_atual():
    """Quem está trocando a própria senha não pode ser deslogado pelo
    próprio ato de trocar -- o cookie novo (emitido na mesma resposta) já
    carrega a token_versao nova e continua autenticando normalmente."""
    db = _sessao()
    u = _usuario(db)
    u.email_verificado = True   # get_current_user abaixo exige e-mail verificado
    db.commit()
    resp = Response()
    trocar_senha(TrocarSenhaIn(senha_atual="SenhaAtual1!", senha_nova="SenhaNova2@"),
                bg=BackgroundTasks(), resp=resp, user=u, db=db)

    cookie_novo = None
    for nome, valor in resp.raw_headers:
        if nome == b"set-cookie":
            cookie_novo = valor.decode().split(";")[0].split("=", 1)[1]
    assert cookie_novo is not None

    from starlette.requests import Request
    headers = [(b"cookie", f"{auth.COOKIE_NOME}={cookie_novo}".encode())]
    req = Request({"type": "http", "headers": headers, "client": ("1.2.3.4", 1)})
    resultado = auth.get_current_user(req, Response(), db)
    assert resultado.id == u.id
