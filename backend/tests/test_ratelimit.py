"""
Rate limit das rotas de autenticação (app/ratelimit.py) e sua aplicação em
login/cadastro/esqueci-senha/redefinir-senha (app/main.py) -- a barreira
contra força bruta/spam nessas rotas, que rodam sem sessão. Banco sqlite em
memória, sem HTTP. Rode com:  cd backend && pytest
"""
import pytest
from fastapi import BackgroundTasks, HTTPException, Response
from starlette.requests import Request
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import ratelimit as rl
from app.main import (
    auth_login, auth_cadastro, auth_esqueci_senha, auth_redefinir_senha,
    LoginIn, CadastroIn, EsqueciSenhaIn, RedefinirSenhaIn,
)
from app.models import Base, Usuario
from app import auth as _auth


def _sessao():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def _req(ip="1.2.3.4"):
    return Request({"type": "http", "headers": [], "client": (ip, 12345)})


def _usuario(db, email="teste@teste.com", senha="Senha123!", ativo=True, verificado=True):
    u = Usuario(nome="Teste", email=email, senha_hash=_auth.hash_senha(senha),
               ativo=ativo, email_verificado=verificado)
    db.add(u)
    db.commit()
    return u


# --------- ratelimit.checar / limpar (unidade, sem depender de HTTP) --------- #

def test_checar_libera_ate_o_limite():
    for _ in range(3):
        rl.checar("k1", limite=3, janela_seg=60)   # não deve levantar


def test_checar_bloqueia_ao_passar_do_limite():
    for _ in range(3):
        rl.checar("k2", limite=3, janela_seg=60)
    with pytest.raises(HTTPException) as exc:
        rl.checar("k2", limite=3, janela_seg=60)
    assert exc.value.status_code == 429
    assert "Retry-After" in exc.value.headers


def test_checar_chaves_diferentes_nao_se_afetam():
    for _ in range(3):
        rl.checar("k3a", limite=3, janela_seg=60)
    rl.checar("k3b", limite=3, janela_seg=60)   # chave diferente, não bloqueada


def test_checar_libera_de_novo_apos_a_janela_passar(monkeypatch):
    agora = [1000.0]
    monkeypatch.setattr(rl.time, "monotonic", lambda: agora[0])
    for _ in range(3):
        rl.checar("k4", limite=3, janela_seg=60)
    with pytest.raises(HTTPException):
        rl.checar("k4", limite=3, janela_seg=60)
    agora[0] += 61   # passou da janela
    rl.checar("k4", limite=3, janela_seg=60)   # não deve levantar mais


def test_limpar_reseta_contador():
    for _ in range(3):
        rl.checar("k5", limite=3, janela_seg=60)
    rl.limpar("k5")
    rl.checar("k5", limite=3, janela_seg=60)   # não deve levantar


def test_ip_cliente_usa_x_forwarded_for_quando_presente():
    req = Request({"type": "http", "headers": [(b"x-forwarded-for", b"9.9.9.9, 10.0.0.1")],
                   "client": ("127.0.0.1", 1)})
    assert rl.ip_cliente(req) == "9.9.9.9"


def test_ip_cliente_cai_pro_ip_da_conexao_sem_o_header():
    assert rl.ip_cliente(_req("5.6.7.8")) == "5.6.7.8"


# --------- /api/auth/login --------- #

def test_login_bloqueia_apos_muitas_tentativas_com_mesmo_email():
    db = _sessao()
    _usuario(db, email="alvo@teste.com", senha="SenhaCerta1!")
    for _ in range(6):
        with pytest.raises(HTTPException) as exc:
            auth_login(LoginIn(email="alvo@teste.com", senha="errada"), _req(), Response(), db)
        assert exc.value.status_code == 401   # ainda dentro do limite, senha errada de verdade
    with pytest.raises(HTTPException) as exc:
        auth_login(LoginIn(email="alvo@teste.com", senha="errada"), _req(), Response(), db)
    assert exc.value.status_code == 429   # 7ª tentativa: bloqueado pelo rate limit, não pela senha


def test_login_com_sucesso_zera_o_contador_do_email():
    db = _sessao()
    _usuario(db, email="ok@teste.com", senha="SenhaCerta1!")
    for _ in range(5):
        with pytest.raises(HTTPException):
            auth_login(LoginIn(email="ok@teste.com", senha="errada"), _req(), Response(), db)
    auth_login(LoginIn(email="ok@teste.com", senha="SenhaCerta1!"), _req(), Response(), db)   # acerta
    # mais tentativas erradas depois de um login certo -- contador zerou, não bloqueia de cara
    with pytest.raises(HTTPException) as exc:
        auth_login(LoginIn(email="ok@teste.com", senha="errada"), _req(), Response(), db)
    assert exc.value.status_code == 401


def test_login_bloqueia_por_ip_mesmo_com_emails_diferentes():
    """Achado do desenho: força bruta distribuída entre várias contas a
    partir de UMA máquina precisa ser pega pelo limite de IP, não só o de
    e-mail (que sozinho nunca dispararia aqui, já que cada e-mail é novo)."""
    db = _sessao()
    for i in range(20):
        with pytest.raises(HTTPException) as exc:
            auth_login(LoginIn(email=f"inexistente{i}@teste.com", senha="x"), _req("8.8.8.8"), Response(), db)
        assert exc.value.status_code == 401
    with pytest.raises(HTTPException) as exc:
        auth_login(LoginIn(email="mais-um@teste.com", senha="x"), _req("8.8.8.8"), Response(), db)
    assert exc.value.status_code == 429


def test_login_de_ip_diferente_nao_e_afetado_pelo_limite_de_outro_ip():
    db = _sessao()
    _usuario(db, email="livre@teste.com", senha="SenhaCerta1!")
    for i in range(20):
        with pytest.raises(HTTPException):
            auth_login(LoginIn(email=f"x{i}@teste.com", senha="y"), _req("1.1.1.1"), Response(), db)
    # IP diferente, e-mail existente -- não deve ser afetado pelo estouro do outro IP
    r = auth_login(LoginIn(email="livre@teste.com", senha="SenhaCerta1!"), _req("2.2.2.2"), Response(), db)
    assert r == {"ok": True}


# --------- /api/auth/cadastro --------- #

def test_cadastro_bloqueia_apos_muitas_tentativas_do_mesmo_ip():
    db = _sessao()
    for i in range(5):
        auth_cadastro(CadastroIn(nome="Fulano", email=f"novo{i}@teste.com", senha="Senha123!"),
                      _req("3.3.3.3"), Response(), BackgroundTasks(), db)
    with pytest.raises(HTTPException) as exc:
        auth_cadastro(CadastroIn(nome="Fulano", email="novo-demais@teste.com", senha="Senha123!"),
                      _req("3.3.3.3"), Response(), BackgroundTasks(), db)
    assert exc.value.status_code == 429


def test_cadastro_de_ip_diferente_nao_e_bloqueado():
    db = _sessao()
    for i in range(5):
        auth_cadastro(CadastroIn(nome="Fulano", email=f"a{i}@teste.com", senha="Senha123!"),
                      _req("4.4.4.4"), Response(), BackgroundTasks(), db)
    # IP diferente: não deve ser afetado
    r = auth_cadastro(CadastroIn(nome="Fulano", email="b@teste.com", senha="Senha123!"),
                      _req("5.5.5.5"), Response(), BackgroundTasks(), db)
    assert r["ok"] is True


# --------- /api/auth/esqueci-senha e /api/auth/redefinir-senha --------- #

def test_esqueci_senha_bloqueia_por_email_mesmo_sem_smtp():
    db = _sessao()
    for _ in range(3):
        auth_esqueci_senha(EsqueciSenhaIn(email="vitima@teste.com"), _req(), BackgroundTasks(), db)
    with pytest.raises(HTTPException) as exc:
        auth_esqueci_senha(EsqueciSenhaIn(email="vitima@teste.com"), _req(), BackgroundTasks(), db)
    assert exc.value.status_code == 429


def test_redefinir_senha_bloqueia_por_ip_apos_muitas_tentativas():
    db = _sessao()
    for _ in range(10):
        with pytest.raises(HTTPException) as exc:
            auth_redefinir_senha(RedefinirSenhaIn(token="invalido", senha="Senha123!"), _req("6.6.6.6"), db)
        assert exc.value.status_code == 400   # token inválido, ainda dentro do limite
    with pytest.raises(HTTPException) as exc:
        auth_redefinir_senha(RedefinirSenhaIn(token="invalido", senha="Senha123!"), _req("6.6.6.6"), db)
    assert exc.value.status_code == 429
