"""
Achado real (pedido do usuário): "em vez de buscar o doc no pncp, nn
deveria pegar no proprio banco? ja q a busca ja foi realizada" -- tentar
de novo depois de uma falha (PNCP instável, ou só a chamada de IA que
falhou) baixava e extraía o PDF de novo, mesmo já tendo extraído o texto
com sucesso antes. Mesmo raciocínio de _arquivos_pncp_cache
(test_arquivos_pncp_cache.py), um nível mais fundo: ali cacheia a LISTA de
arquivos, aqui cacheia o TEXTO já extraído de dentro deles. Rode com:
cd backend && pytest
"""
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import main as app_main
from app import analise_edital as ia_module
from app.models import Base, Usuario, Edital


def _sessao():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def _edital(db):
    ed = Edital(fonte="PNCP", id_externo="46384111000140-1-000934/2026",
               orgao="Orgao", objeto="Aquisicao", uf="SP")
    db.add(ed)
    db.commit()
    return ed


def _usuario_com_chave(db, monkeypatch):
    u = Usuario(nome="Teste", email="t@t.com", senha_hash="x")
    db.add(u)
    db.commit()
    monkeypatch.setattr(app_main._auth, "decifrar", lambda _cifrada: "fake-key")
    monkeypatch.setattr(ia_module, "ia_texto_disponivel", lambda chave: True)
    return u


def _resposta_gemini_ok(json_texto='{"objeto": "teste"}'):
    r = MagicMock()
    r.status_code = 200
    r.json.return_value = {"candidates": [{"content": {"parts": [{"text": json_texto}]}}]}
    return r


# --------- _texto_pronto_cache (main.py) --------- #

def test_sem_cache_devolve_none():
    ed = Edital(fonte="PNCP", id_externo="x", orgao="O", objeto="Obj", uf="SP")
    assert app_main._texto_pronto_cache(ed, forcar=False) is None


def test_com_cache_devolve_texto_e_fonte():
    ed = Edital(fonte="PNCP", id_externo="x", orgao="O", objeto="Obj", uf="SP")
    ed.texto_analise_ia = "texto ja extraido"
    ed.texto_analise_ia_fonte = "Edital"
    r = app_main._texto_pronto_cache(ed, forcar=False)
    assert r == {"texto": "texto ja extraido", "fonte": "Edital"}


def test_forcar_ignora_cache_mesmo_existindo():
    ed = Edital(fonte="PNCP", id_externo="x", orgao="O", objeto="Obj", uf="SP")
    ed.texto_analise_ia = "texto ja extraido"
    assert app_main._texto_pronto_cache(ed, forcar=True) is None


# --------- analisar(): texto_pronto pula download --------- #

def test_texto_pronto_pula_download_do_pdf(monkeypatch):
    chamou_download = []
    monkeypatch.setattr(ia_module, "_baixar_texto_pdf",
                        lambda *a, **k: chamou_download.append(1))
    with patch("app.analise_edital.requests.post", return_value=_resposta_gemini_ok()):
        resultado = ia_module.analisar(
            "Objeto de teste", arquivos=[], api_key="fake-key",
            texto_pronto={"texto": "x" * 400, "fonte": "Edital (cache)"})
    assert chamou_download == []
    assert resultado["status"] == "ok"
    assert resultado["fonte"] == "Edital (cache)"


def test_texto_pronto_com_arquivos_vazios_nao_vira_sem_arquivo(monkeypatch):
    """Mesmo passando arquivos=[] (a rota não busca mais a lista no PNCP
    quando há texto_pronto), a análise não deve cair em "sem_arquivo" --
    esse status só se aplica ao caminho SEM texto_pronto."""
    with patch("app.analise_edital.requests.post", return_value=_resposta_gemini_ok()):
        resultado = ia_module.analisar(
            "Objeto de teste", arquivos=[], api_key="fake-key",
            texto_pronto={"texto": "x" * 400, "fonte": None})
    assert resultado["status"] == "ok"


# --------- _texto_extraido/_fonte_extraida: presentes só quando a extração deu certo --------- #

def test_ok_inclui_texto_extraido_para_cache(monkeypatch):
    with patch("app.analise_edital._baixar_texto_pdf", return_value=("x" * 400, False)), \
         patch("app.analise_edital.requests.post", return_value=_resposta_gemini_ok()):
        resultado = ia_module.analisar(
            "Objeto", [{"titulo": "Edital", "url": "http://x/e.pdf"}], api_key="fake-key")
    assert resultado["status"] == "ok"
    assert resultado["_texto_extraido"] == "=== DOCUMENTO: Edital ===\n" + "x" * 400
    assert resultado["_fonte_extraida"] == "Edital"


def test_erro_ia_inclui_texto_extraido_mesmo_com_chamada_de_ia_falhando(monkeypatch):
    """A extração do PDF deu certo -- só a chamada de IA em si falhou (ex.:
    cota/rate limit esgotados). O texto já extraído deve poder ser
    cacheado mesmo assim, pra próxima tentativa não baixar de novo."""
    with patch("app.analise_edital._baixar_texto_pdf", return_value=("x" * 400, False)), \
         patch("app.analise_edital.requests.post",
               return_value=MagicMock(status_code=429, json=lambda: {"error": {"code": 429}})):
        resultado = ia_module.analisar(
            "Objeto", [{"titulo": "Edital", "url": "http://x/e.pdf"}], api_key="fake-key")
    assert resultado["status"] == "erro_ia"
    assert resultado["_texto_extraido"] == "=== DOCUMENTO: Edital ===\n" + "x" * 400


def test_resposta_invalida_inclui_texto_extraido(monkeypatch):
    with patch("app.analise_edital._baixar_texto_pdf", return_value=("x" * 400, False)), \
         patch("app.analise_edital.requests.post", return_value=_resposta_gemini_ok(json_texto="não é json")):
        resultado = ia_module.analisar(
            "Objeto", [{"titulo": "Edital", "url": "http://x/e.pdf"}], api_key="fake-key")
    assert resultado["status"] == "resposta_invalida"
    assert resultado["_texto_extraido"] == "=== DOCUMENTO: Edital ===\n" + "x" * 400


def test_sem_arquivo_nao_inclui_texto_extraido():
    resultado = ia_module.analisar("Objeto", [], api_key="fake-key")
    assert resultado["status"] == "sem_arquivo"
    assert "_texto_extraido" not in resultado


def test_erro_download_pdf_nao_inclui_texto_extraido(monkeypatch):
    with patch("app.analise_edital._baixar_texto_pdf", return_value=("", True)):
        resultado = ia_module.analisar(
            "Objeto", [{"titulo": "Edital", "url": "http://x/e.pdf"}], api_key="fake-key")
    assert resultado["status"] == "erro_download_pdf"
    assert "_texto_extraido" not in resultado


def test_sem_texto_nao_inclui_texto_extraido(monkeypatch):
    with patch("app.analise_edital._baixar_texto_pdf", return_value=("", False)):
        resultado = ia_module.analisar(
            "Objeto", [{"titulo": "Edital", "url": "http://x/e.pdf"}], api_key="fake-key")
    assert resultado["status"] == "sem_texto"
    assert "_texto_extraido" not in resultado


# --------- rota GET /api/editais/{id}/analise: persiste e reaproveita o cache --------- #

def test_rota_persiste_texto_mesmo_quando_ia_falha(monkeypatch):
    """O caso real que motivou o pedido: a chamada de IA falha (cota
    esgotada), mas a extração do PDF deu certo -- não pode se perder."""
    db = _sessao()
    u = _usuario_com_chave(db, monkeypatch)
    ed = _edital(db)

    monkeypatch.setattr(app_main, "_listar_arquivos_pncp", lambda ed_: {
        "status": "ok", "arquivos": [{"titulo": "Edital", "url": "http://x/edital.pdf"}], "portal": None})
    monkeypatch.setattr(ia_module, "analisar", lambda *a, **k: {
        "status": "erro_ia", "detalhe": "http_429",
        "_texto_extraido": "texto extraido do pdf", "_fonte_extraida": "Edital"})

    r = app_main.analise_edital(ed.id, forcar=False, user=u, db=db)

    assert r["status"] == "erro_ia"
    assert "_texto_extraido" not in r   # nunca vaza pro JSON devolvido/cacheado
    assert ed.texto_analise_ia == "texto extraido do pdf"
    assert ed.texto_analise_ia_fonte == "Edital"
    assert ed.texto_analise_ia_em is not None


def test_rota_reusa_texto_cacheado_sem_buscar_lista_de_arquivos(monkeypatch):
    db = _sessao()
    u = _usuario_com_chave(db, monkeypatch)
    ed = _edital(db)
    ed.texto_analise_ia = "texto ja cacheado"
    ed.texto_analise_ia_fonte = "Edital"
    db.commit()

    chamou_pncp = []
    monkeypatch.setattr(app_main, "_listar_arquivos_pncp", lambda ed_: chamou_pncp.append(1))

    recebido = {}

    def _analisar_fake(objeto, arquivos, api_key=None, texto_pronto=None):
        recebido["arquivos"] = arquivos
        recebido["texto_pronto"] = texto_pronto
        return {"status": "ok", "versao": ia_module.VERSAO_PROMPT, "objeto": objeto,
                "requisitos_tecnicos": [], "documentos_habilitacao": []}
    monkeypatch.setattr(ia_module, "analisar", _analisar_fake)

    r = app_main.analise_edital(ed.id, forcar=False, user=u, db=db)

    assert chamou_pncp == []   # não precisou buscar a lista de arquivos no PNCP
    assert recebido["texto_pronto"] == {"texto": "texto ja cacheado", "fonte": "Edital"}
    assert r["status"] == "ok"


def test_rota_forcar_ignora_texto_cacheado_e_busca_de_novo(monkeypatch):
    db = _sessao()
    u = _usuario_com_chave(db, monkeypatch)
    ed = _edital(db)
    ed.texto_analise_ia = "texto antigo"
    db.commit()

    monkeypatch.setattr(app_main, "_listar_arquivos_pncp", lambda ed_: {
        "status": "ok", "arquivos": [{"titulo": "Retificação", "url": "http://x/ret.pdf"}], "portal": None})

    recebido = {}

    def _analisar_fake(objeto, arquivos, api_key=None, texto_pronto=None):
        recebido["arquivos"] = arquivos
        recebido["texto_pronto"] = texto_pronto
        return {"status": "ok", "versao": ia_module.VERSAO_PROMPT, "objeto": objeto,
                "requisitos_tecnicos": [], "documentos_habilitacao": [],
                "_texto_extraido": "texto novo", "_fonte_extraida": "Retificação"}
    monkeypatch.setattr(ia_module, "analisar", _analisar_fake)

    r = app_main.analise_edital(ed.id, forcar=True, user=u, db=db)

    assert recebido["texto_pronto"] is None
    assert recebido["arquivos"][0]["titulo"] == "Retificação"
    assert ed.texto_analise_ia == "texto novo"
    assert r["status"] == "ok"
