"""
Testes dos dados complementares de cadastro/perfil (endereço já existia;
telefone, representante legal, inscrições, dados bancários e logo são
novos) — usados depois pra timbrar a proposta exportada. Banco sqlite em
memória, sem HTTP. Rode com:  cd backend && pytest
"""
import json

import pytest
from fastapi import BackgroundTasks, HTTPException, Response
from starlette.requests import Request
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import auth
from app.main import (
    auth_cadastro, obter_perfil, salvar_perfil, CadastroIn, PerfilIn,
    _limpar_dados_empresa, _validar_logo_base64,
)
from app.models import Base, Usuario


def _sessao():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def _req(ip="1.2.3.4"):
    """Request mínima só pra rate limit (app.ratelimit.ip_cliente) conseguir
    ler um IP -- não precisa de corpo nem de mais nada do ASGI real aqui."""
    return Request({"type": "http", "headers": [], "client": (ip, 12345)})


_LOGO_VALIDA = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="


# --------- funções puras --------- #

def test_limpar_dados_empresa_filtra_chave_desconhecida():
    r = _limpar_dados_empresa({"telefone": "11999999999", "chave_estranha": "x"})
    assert r == {"telefone": "11999999999"}


def test_limpar_dados_empresa_remove_valor_vazio():
    r = _limpar_dados_empresa({"telefone": "  ", "banco_nome": "Banco X"})
    assert r == {"banco_nome": "Banco X"}


def test_limpar_dados_empresa_com_none_retorna_vazio():
    assert _limpar_dados_empresa(None) == {}


def test_validar_logo_aceita_data_uri_valido():
    assert _validar_logo_base64(_LOGO_VALIDA) == _LOGO_VALIDA


def test_validar_logo_vazio_retorna_none():
    assert _validar_logo_base64(None) is None
    assert _validar_logo_base64("") is None


def test_validar_logo_rejeita_string_sem_prefixo_data_image():
    with pytest.raises(HTTPException) as exc:
        _validar_logo_base64("não é uma imagem")
    assert exc.value.status_code == 400


def test_validar_logo_rejeita_arquivo_grande_demais():
    grande = "data:image/png;base64," + ("A" * 1_300_000)
    with pytest.raises(HTTPException) as exc:
        _validar_logo_base64(grande)
    assert exc.value.status_code == 400


# --------- /api/auth/cadastro --------- #

def test_cadastro_persiste_endereco_dados_empresa_e_logo():
    db = _sessao()
    dados = CadastroIn(
        nome="Empresa Teste", email="empresa@teste.com", senha="Senha123!",
        documento="12345678000199",
        endereco={"cep": "01310-100", "cidade": "São Paulo", "uf": "SP"},
        dados_empresa={"telefone": "11988887777", "representante_legal": "Fulano de Tal",
                      "banco_nome": "Banco X", "banco_agencia": "0001", "banco_conta": "12345-6"},
        logo_base64=_LOGO_VALIDA,
    )
    auth_cadastro(dados, _req(), Response(), BackgroundTasks(), db)

    u = db.query(Usuario).filter(Usuario.email == "empresa@teste.com").first()
    assert u is not None
    assert u.logo_base64 == _LOGO_VALIDA
    endereco = json.loads(auth.decifrar(u.endereco_cifrado))
    assert endereco["cidade"] == "São Paulo"
    empresa = json.loads(auth.decifrar(u.dados_empresa_cifrado))
    assert empresa["telefone"] == "11988887777"
    assert empresa["representante_legal"] == "Fulano de Tal"
    assert empresa["banco_conta"] == "12345-6"


def test_cadastro_sem_dados_complementares_continua_funcionando():
    """Todos os campos novos são opcionais — cadastro básico (só o que já
    existia antes) não pode quebrar."""
    db = _sessao()
    dados = CadastroIn(nome="Fulano", email="fulano@teste.com", senha="Senha123!")
    auth_cadastro(dados, _req(), Response(), BackgroundTasks(), db)

    u = db.query(Usuario).filter(Usuario.email == "fulano@teste.com").first()
    assert u is not None
    assert u.logo_base64 is None
    assert u.dados_empresa_cifrado is None
    assert u.endereco_cifrado is None


def test_cadastro_rejeita_logo_invalida():
    db = _sessao()
    dados = CadastroIn(nome="Fulano", email="fulano2@teste.com", senha="Senha123!",
                       logo_base64="isso não é uma imagem")
    with pytest.raises(HTTPException) as exc:
        auth_cadastro(dados, _req(), Response(), BackgroundTasks(), db)
    assert exc.value.status_code == 400


# --------- /api/perfil --------- #

def _usuario_perfil(db):
    u = Usuario(nome="Teste", email="perfil@teste.com", senha_hash="x")
    db.add(u)
    db.commit()
    return u


def test_perfil_salva_e_le_dados_empresa_e_logo():
    db = _sessao()
    u = _usuario_perfil(db)
    salvar_perfil(PerfilIn(dados_empresa={"telefone": "11977776666"}, logo_base64=_LOGO_VALIDA),
                 user=u, db=db)

    r = obter_perfil(user=u)
    assert r["dados_empresa"]["telefone"] == "11977776666"
    assert r["logo_base64"] == _LOGO_VALIDA


def test_perfil_logo_vazia_remove_a_logo():
    db = _sessao()
    u = _usuario_perfil(db)
    salvar_perfil(PerfilIn(logo_base64=_LOGO_VALIDA), user=u, db=db)
    salvar_perfil(PerfilIn(logo_base64=""), user=u, db=db)

    r = obter_perfil(user=u)
    assert r["logo_base64"] == ""


def test_perfil_sem_logo_no_payload_mantem_a_existente():
    db = _sessao()
    u = _usuario_perfil(db)
    salvar_perfil(PerfilIn(logo_base64=_LOGO_VALIDA), user=u, db=db)
    salvar_perfil(PerfilIn(nome="Novo Nome"), user=u, db=db)   # não menciona logo_base64

    r = obter_perfil(user=u)
    assert r["logo_base64"] == _LOGO_VALIDA


def test_perfil_dados_empresa_filtra_chave_desconhecida_tambem_ao_salvar():
    db = _sessao()
    u = _usuario_perfil(db)
    salvar_perfil(PerfilIn(dados_empresa={"telefone": "119999", "hackzinho": "xxx"}), user=u, db=db)

    r = obter_perfil(user=u)
    assert "hackzinho" not in r["dados_empresa"]
