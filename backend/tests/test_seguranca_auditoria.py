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
from fastapi.routing import APIRoute
from starlette.requests import Request

from app import auth as _auth_mod
from app import main as app_main
from app.main import (
    _urls_documentacao, _checar_rate_limit_geral, _aplicar_cabecalhos_seguranca,
    _verificar_turnstile, turnstile_config, _exigir_cron_key, verificar_lembretes,
    _ROTAS_PUBLICAS, _PREFIXOS_PUBLICOS, _ROTAS_PROTEGIDAS_POR_CRON_KEY,
)


def _req(ip="1.2.3.4"):
    return Request({"type": "http", "headers": [], "client": (ip, 12345), "query_string": b""})


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


# --------- achado real: /api/lembretes/verificar sem proteção nenhuma --------- #
# (toda a base recebia notificação de qualquer requisição não autenticada) #

def test_lembretes_verificar_exige_chave_de_cron(monkeypatch):
    from fastapi import BackgroundTasks
    monkeypatch.setattr(app_main.settings, "CRON_SECRET", "segredo-do-cron")
    with pytest.raises(HTTPException) as exc:
        verificar_lembretes(BackgroundTasks(), _req())   # sem X-Cron-Key
    assert exc.value.status_code == 403


def test_lembretes_verificar_aceita_com_chave_certa(monkeypatch):
    from fastapi import BackgroundTasks
    monkeypatch.setattr(app_main.settings, "CRON_SECRET", "segredo-do-cron")
    req = Request({"type": "http",
                   "headers": [(b"x-cron-key", b"segredo-do-cron")],
                   "client": ("1.2.3.4", 12345)})
    r = verificar_lembretes(BackgroundTasks(), req)
    assert r["ok"] is True


def test_cron_key_na_query_string_nao_funciona_mais(monkeypatch):
    """Achado real (auditoria de segurança pedida pelo usuário): a chave do
    cron aceitava vir por ?chave=... na URL, sem nenhum chamador real
    usando -- query string vaza pra log de acesso de qualquer proxy no
    caminho. Removido; só o header X-Cron-Key funciona agora."""
    from fastapi import BackgroundTasks
    monkeypatch.setattr(app_main.settings, "CRON_SECRET", "segredo-do-cron")
    req = Request({"type": "http", "headers": [],
                   "client": ("1.2.3.4", 12345), "query_string": b"chave=segredo-do-cron"})
    with pytest.raises(HTTPException) as exc:
        verificar_lembretes(BackgroundTasks(), req)
    assert exc.value.status_code == 403


def test_lembretes_verificar_desligado_sem_cron_secret_configurado(monkeypatch):
    from fastapi import BackgroundTasks
    monkeypatch.setattr(app_main.settings, "CRON_SECRET", "")
    with pytest.raises(HTTPException) as exc:
        verificar_lembretes(BackgroundTasks(), _req())
    assert exc.value.status_code == 503


# --------- "negar por padrão": toda rota /api/* exige login ou está numa --------- #
# --------- allowlist explícita e revisada (achado real: as duas listas --------- #
# --------- abaixo existiam só como documentação, sem nada as conferir) --------- #

def _rotas_api():
    for route in app_main.app.routes:
        if isinstance(route, APIRoute) and route.path.startswith("/api/"):
            yield route


def _exige_login(route: APIRoute) -> bool:
    # só olha dependência DIRETA -- não há hoje nenhuma rota que exija login
    # por trás de uma dependência indireta (ex.: um get_admin_user que por
    # sua vez depende de get_current_user); se isso passar a existir, este
    # teste vai marcar a rota como "sem proteção" por falso-negativo -- o
    # conserto certo é ensinar esta função a reconhecer a nova dependência,
    # NUNCA adicionar a rota à allowlist pública/cron pra calar o teste.
    return any(dep.call is _auth_mod.get_current_user for dep in route.dependant.dependencies)


def test_toda_rota_api_exige_login_ou_esta_na_allowlist():
    """Se uma rota /api/* nova esquecer Depends(get_current_user), este
    teste tem que falhar -- foi exatamente essa classe de bug que deixou
    /api/lembretes/verificar exposta sem proteção. Rota legitimamente
    pública (fluxo de auth, página estática) ou protegida por X-Cron-Key
    em vez de cookie precisa entrar explicitamente numa das allowlists
    abaixo -- nunca ficar de fora calada.

    Limitação conhecida: a allowlist casa por `route.path` sozinho, não por
    (path, método) -- hoje nenhum path tem um 2º método "escondido" sem
    proteção, mas um GET de debug adicionado depois num path já
    allowlisted (ex.: /api/lembretes/verificar) herdaria a isenção sem
    este teste notar. Não vale a complexidade de corrigir até isso
    acontecer de verdade; registrado aqui pra quem for depurar o próximo
    falso-negativo."""
    sem_protecao = []
    for route in _rotas_api():
        if route.path in _ROTAS_PUBLICAS or route.path in _ROTAS_PROTEGIDAS_POR_CRON_KEY:
            continue
        if route.path.startswith(_PREFIXOS_PUBLICOS):
            continue
        if route.path.startswith("/api/telegram/webhook/"):   # autenticado pelo secret no path
            continue
        if not _exige_login(route):
            sem_protecao.append((route.path, sorted(route.methods)))
    assert sem_protecao == [], (
        f"Rota(s) /api/* sem Depends(get_current_user) e fora de toda "
        f"allowlist de rota pública/cron: {sem_protecao}"
    )


# --------- achado real (item 8): subir em produção sem APP_ENCRYPTION_KEY --------- #
# --------- deixava a cifra de dado sensível acoplada à SECRET_KEY --------- #

async def _tentar_lifespan():
    async with app_main.lifespan(app_main.app):
        pass


def test_lifespan_recusa_producao_sem_app_encryption_key(monkeypatch):
    import asyncio
    monkeypatch.setattr(app_main.settings, "APP_BASE_URL", "https://app.exemplo.com")
    monkeypatch.setattr(app_main.settings, "SECRET_KEY", "uma-chave-propria-bem-forte-123456")
    monkeypatch.setattr(app_main.settings, "APP_ENCRYPTION_KEY", "")
    with pytest.raises(RuntimeError, match="APP_ENCRYPTION_KEY"):
        asyncio.run(_tentar_lifespan())


def test_lifespan_sobe_em_producao_com_app_encryption_key_configurada(monkeypatch):
    import asyncio
    monkeypatch.setattr(app_main.settings, "APP_BASE_URL", "https://app.exemplo.com")
    monkeypatch.setattr(app_main.settings, "SECRET_KEY", "uma-chave-propria-bem-forte-123456")
    monkeypatch.setattr(app_main.settings, "APP_ENCRYPTION_KEY", "uma-chave-de-cifra-propria")
    monkeypatch.setattr(app_main, "init_db", lambda: None)   # não bate em banco real neste teste
    asyncio.run(_tentar_lifespan())   # não deve levantar


def test_lifespan_nao_exige_app_encryption_key_fora_de_producao(monkeypatch):
    """Local/dev (sem APP_BASE_URL https) continua liberado sem nenhuma das
    duas chaves -- mesmo padrão já valido pra SECRET_KEY."""
    import asyncio
    monkeypatch.setattr(app_main.settings, "APP_BASE_URL", "http://localhost:8000")
    monkeypatch.setattr(app_main.settings, "APP_ENCRYPTION_KEY", "")
    monkeypatch.setattr(app_main, "init_db", lambda: None)
    asyncio.run(_tentar_lifespan())   # não deve levantar


def test_rotas_protegidas_por_cron_key_de_fato_chamam_exigir_cron_key():
    """Garante que a allowlist de cron-key não vira uma forma de 'esconder'
    uma rota insegura do teste acima -- cada uma precisa CHAMAR
    _exigir_cron_key de verdade, não só constar na allowlist (achado real
    do code-reviewer na 1ª versão deste teste: ele só conferia que os paths
    existiam, nunca que a view function de fato invoca a checagem -- uma
    4ª rota cadastrada em _ROTAS_PROTEGIDAS_POR_CRON_KEY sem chamar
    _exigir_cron_key passava igual, escondida DENTRO da allowlist em vez de
    fora dela). Inspeciona o código-fonte da view function em vez de
    executá-la (mesmo risco de falso-negativo que qualquer checagem
    estática: só pega a chamada direta, não via uma função wrapper)."""
    import inspect
    rotas = {r.path: r for r in _rotas_api() if r.path in _ROTAS_PROTEGIDAS_POR_CRON_KEY}
    assert set(rotas) == _ROTAS_PROTEGIDAS_POR_CRON_KEY
    for path, route in rotas.items():
        codigo = inspect.getsource(route.endpoint)
        assert "_exigir_cron_key(" in codigo, (
            f"{path}: está em _ROTAS_PROTEGIDAS_POR_CRON_KEY mas a view "
            f"function não chama _exigir_cron_key(...)"
        )


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
