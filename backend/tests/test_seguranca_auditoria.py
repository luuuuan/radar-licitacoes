"""
Itens 11 (rate limit geral), 12 (bot protection/Turnstile no cadastro), 15
(desligar /docs+/redoc+/openapi.json em produção) e 18 (security headers) da
auditoria de segurança pedida pelo usuário -- os outros itens do checklist já
estavam implementados (bcrypt, JWT server-side, Fernet, ownership checks,
queries parametrizadas, etc.) ou dependem do painel do Supabase (RLS/Data
API), fora do alcance deste repositório.
Rode com:  cd backend && pytest
"""
from unittest.mock import patch

import pytest
from fastapi import HTTPException, Response
from starlette.requests import Request

from app import main as app_main
from app.main import (
    _urls_documentacao, _checar_rate_limit_geral, _aplicar_cabecalhos_seguranca,
    _verificar_turnstile, turnstile_config,
)


def _req(ip="1.2.3.4"):
    return Request({"type": "http", "headers": [], "client": (ip, 12345)})


# --------- item 15: /docs, /redoc, /openapi.json desligados em produção --------- #

def test_urls_documentacao_desligadas_em_producao():
    assert _urls_documentacao(True) == (None, None, None)


def test_urls_documentacao_ligadas_fora_de_producao():
    assert _urls_documentacao(False) == ("/docs", "/redoc", "/openapi.json")


# --------- item 18: cabeçalhos de segurança --------- #

def test_cabecalhos_seguranca_padrao_sempre_presentes():
    resp = Response()
    _aplicar_cabecalhos_seguranca(resp, producao=False)
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert resp.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert "geolocation=()" in resp.headers["Permissions-Policy"]
    assert "default-src 'self'" in resp.headers["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in resp.headers["Content-Security-Policy"]


def test_hsts_so_aparece_em_producao():
    resp_dev = Response()
    _aplicar_cabecalhos_seguranca(resp_dev, producao=False)
    assert "Strict-Transport-Security" not in resp_dev.headers

    resp_prod = Response()
    _aplicar_cabecalhos_seguranca(resp_prod, producao=True)
    assert "max-age=31536000" in resp_prod.headers["Strict-Transport-Security"]


# --------- item 11: rate limit geral (qualquer rota /api/*) --------- #

def test_rate_limit_geral_libera_ate_o_limite(monkeypatch):
    monkeypatch.setattr(app_main, "_LIMITE_API_GERAL", 3)
    for _ in range(3):
        _checar_rate_limit_geral("/api/produtos", "10.10.10.1")   # não deve levantar


def test_rate_limit_geral_bloqueia_apos_o_limite(monkeypatch):
    monkeypatch.setattr(app_main, "_LIMITE_API_GERAL", 3)
    for _ in range(3):
        _checar_rate_limit_geral("/api/produtos", "10.10.10.2")
    with pytest.raises(HTTPException) as exc:
        _checar_rate_limit_geral("/api/produtos", "10.10.10.2")
    assert exc.value.status_code == 429


def test_rate_limit_geral_ips_diferentes_nao_se_afetam(monkeypatch):
    monkeypatch.setattr(app_main, "_LIMITE_API_GERAL", 3)
    for _ in range(3):
        _checar_rate_limit_geral("/api/produtos", "10.10.10.3")
    _checar_rate_limit_geral("/api/produtos", "10.10.10.4")   # IP diferente, não bloqueado


def test_rate_limit_geral_ignora_rotas_fora_de_api(monkeypatch):
    monkeypatch.setattr(app_main, "_LIMITE_API_GERAL", 1)
    for _ in range(5):
        _checar_rate_limit_geral("/health", "10.10.10.5")   # nunca conta, nunca bloqueia
        _checar_rate_limit_geral("/", "10.10.10.5")


def test_rate_limit_geral_ignora_coletar_cron(monkeypatch):
    """/api/coletar-cron é chamado pelo GitHub Actions (mesmo IP a cada
    disparo diário) -- não pode ficar sujeito ao limite geral por IP."""
    monkeypatch.setattr(app_main, "_LIMITE_API_GERAL", 1)
    for _ in range(5):
        _checar_rate_limit_geral("/api/coletar-cron", "10.10.10.6")


# --------- item 12: bot protection (Turnstile) no cadastro --------- #

def test_turnstile_config_devolve_none_quando_nao_configurado(monkeypatch):
    monkeypatch.setattr(app_main.settings, "TURNSTILE_SITE_KEY", "")
    assert turnstile_config() == {"site_key": None}


def test_turnstile_config_devolve_a_site_key_quando_configurado(monkeypatch):
    monkeypatch.setattr(app_main.settings, "TURNSTILE_SITE_KEY", "0x-site-key-publica")
    assert turnstile_config() == {"site_key": "0x-site-key-publica"}


def test_verificar_turnstile_nao_verifica_nada_sem_secret_key_configurada(monkeypatch):
    """Mesmo padrão de outras integrações opcionais (SMTP, chave Gemini):
    sem configurar, a funcionalidade que depende dela simplesmente não faz
    nada -- cadastro continua funcionando sem captcha."""
    monkeypatch.setattr(app_main.settings, "TURNSTILE_SECRET_KEY", "")
    _verificar_turnstile(None, _req())   # não deve levantar, mesmo sem token nenhum


def test_verificar_turnstile_exige_token_quando_configurado(monkeypatch):
    monkeypatch.setattr(app_main.settings, "TURNSTILE_SECRET_KEY", "chave-secreta-turnstile")
    with pytest.raises(HTTPException) as exc:
        _verificar_turnstile(None, _req())
    assert exc.value.status_code == 400


def test_verificar_turnstile_rejeita_quando_cloudflare_recusa(monkeypatch):
    monkeypatch.setattr(app_main.settings, "TURNSTILE_SECRET_KEY", "chave-secreta-turnstile")
    with patch("app.main.requests.post") as mock_post:
        mock_post.return_value.json.return_value = {"success": False}
        with pytest.raises(HTTPException) as exc:
            _verificar_turnstile("token-invalido", _req())
    assert exc.value.status_code == 400


def test_verificar_turnstile_aceita_quando_cloudflare_confirma(monkeypatch):
    monkeypatch.setattr(app_main.settings, "TURNSTILE_SECRET_KEY", "chave-secreta-turnstile")
    with patch("app.main.requests.post") as mock_post:
        mock_post.return_value.json.return_value = {"success": True}
        _verificar_turnstile("token-valido", _req())   # não deve levantar


def test_cadastro_bloqueia_sem_captcha_valido_quando_turnstile_configurado(monkeypatch):
    """Prova fim-a-fim (não só a função isolada acima): /api/auth/cadastro
    de verdade chama _verificar_turnstile antes de criar a conta."""
    from fastapi import BackgroundTasks
    from app.main import auth_cadastro, CadastroIn
    from app.models import Base, Usuario
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import sessionmaker

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()

    monkeypatch.setattr(app_main.settings, "TURNSTILE_SECRET_KEY", "chave-secreta-turnstile")
    with patch("app.main.requests.post") as mock_post:
        mock_post.return_value.json.return_value = {"success": False}
        with pytest.raises(HTTPException) as exc:
            auth_cadastro(CadastroIn(nome="Fulano", email="fulano@teste.com", senha="Senha123!",
                                     turnstile_token="token-invalido"),
                         _req("20.20.20.1"), Response(), BackgroundTasks(), db)
    assert exc.value.status_code == 400
    assert db.execute(select(Usuario)).scalars().first() is None   # conta NÃO foi criada


def test_cadastro_aceita_com_captcha_valido_quando_turnstile_configurado(monkeypatch):
    from fastapi import BackgroundTasks
    from app.main import auth_cadastro, CadastroIn
    from app.models import Base, Usuario
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import sessionmaker

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()

    monkeypatch.setattr(app_main.settings, "TURNSTILE_SECRET_KEY", "chave-secreta-turnstile")
    with patch("app.main.requests.post") as mock_post:
        mock_post.return_value.json.return_value = {"success": True}
        r = auth_cadastro(CadastroIn(nome="Fulano", email="fulano2@teste.com", senha="Senha123!",
                                     turnstile_token="token-valido"),
                          _req("20.20.20.2"), Response(), BackgroundTasks(), db)
    assert r["ok"] is True
    assert db.execute(select(Usuario)).scalars().first() is not None


def test_verificar_turnstile_trata_falha_de_rede_como_recusa(monkeypatch):
    """Achado real (defensivo): se o próprio Cloudflare estiver fora do ar,
    o cadastro não pode ficar quebrado indefinidamente por causa disso --
    mas também não pode simplesmente LIBERAR sem checar (isso anularia a
    proteção toda vez que a rede falhar). Trata como recusa (400), igual a
    um captcha rejeitado -- o usuário tenta de novo."""
    import requests as requests_mod
    monkeypatch.setattr(app_main.settings, "TURNSTILE_SECRET_KEY", "chave-secreta-turnstile")
    with patch("app.main.requests.post", side_effect=requests_mod.RequestException("timeout")):
        with pytest.raises(HTTPException) as exc:
            _verificar_turnstile("token-qualquer", _req())
    assert exc.value.status_code == 400
