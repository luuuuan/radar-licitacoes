"""
Achado real (pedido do usuário): a aba Documentos e a Análise por IA
buscavam a lista de arquivos do edital no PNCP toda vez que a tela era
aberta, mesmo já tendo sido buscada com sucesso antes -- desperdício de
chamada de rede pra um dado que raramente muda. _arquivos_pncp_cache
guarda o resultado em Edital.arquivos_pncp e só busca de novo quando
forcar=True (usado quando o usuário clica "Realizar nova análise") ou
quando ainda não há nada cacheado. Rode com:  cd backend && pytest
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import main as app_main
from app.models import Base, Edital


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


def test_primeira_chamada_busca_ao_vivo_e_salva_no_banco(monkeypatch):
    db = _sessao()
    ed = _edital(db)
    chamadas = []

    def _fake(ed_):
        chamadas.append(1)
        return {"status": "ok", "arquivos": [{"titulo": "Edital", "url": "http://x/e.pdf"}],
                "portal": None}
    monkeypatch.setattr(app_main, "_listar_arquivos_pncp", _fake)

    r = app_main._arquivos_pncp_cache(db, ed)

    assert len(chamadas) == 1
    assert r["arquivos"][0]["titulo"] == "Edital"
    assert ed.arquivos_pncp == {"status": "ok", "arquivos": [{"titulo": "Edital", "url": "http://x/e.pdf"}]}
    assert ed.arquivos_pncp_em is not None


def test_segunda_chamada_sem_forcar_nao_busca_de_novo(monkeypatch):
    db = _sessao()
    ed = _edital(db)
    chamadas = []

    def _fake(ed_):
        chamadas.append(1)
        return {"status": "ok", "arquivos": [{"titulo": "Edital", "url": "http://x/e.pdf"}], "portal": None}
    monkeypatch.setattr(app_main, "_listar_arquivos_pncp", _fake)

    r1 = app_main._arquivos_pncp_cache(db, ed)
    r2 = app_main._arquivos_pncp_cache(db, ed)

    assert len(chamadas) == 1   # só a 1ª chamada bateu no PNCP
    assert r1["arquivos"] == r2["arquivos"]


def test_forcar_busca_de_novo_mesmo_com_cache(monkeypatch):
    db = _sessao()
    ed = _edital(db)
    ed.arquivos_pncp = {"status": "ok", "arquivos": [{"titulo": "Antigo", "url": "http://x/antigo.pdf"}]}
    db.commit()
    chamadas = []

    def _fake(ed_):
        chamadas.append(1)
        return {"status": "ok", "arquivos": [{"titulo": "Retificação", "url": "http://x/ret.pdf"}],
                "portal": None}
    monkeypatch.setattr(app_main, "_listar_arquivos_pncp", _fake)

    r = app_main._arquivos_pncp_cache(db, ed, forcar=True)

    assert len(chamadas) == 1
    assert r["arquivos"][0]["titulo"] == "Retificação"
    assert ed.arquivos_pncp["arquivos"][0]["titulo"] == "Retificação"


def test_falha_de_busca_nao_e_salva_no_banco(monkeypatch):
    db = _sessao()
    ed = _edital(db)
    monkeypatch.setattr(app_main, "_listar_arquivos_pncp",
                        lambda ed_: {"status": "erro_rede", "arquivos": [], "portal": None})

    r = app_main._arquivos_pncp_cache(db, ed)

    assert r["status"] == "erro_rede"
    assert ed.arquivos_pncp is None   # não grava falha passageira como se fosse resultado bom


def test_falha_de_busca_nao_apaga_cache_bom_existente(monkeypatch):
    db = _sessao()
    ed = _edital(db)
    ed.arquivos_pncp = {"status": "ok", "arquivos": [{"titulo": "Edital", "url": "http://x/e.pdf"}]}
    db.commit()
    monkeypatch.setattr(app_main, "_listar_arquivos_pncp",
                        lambda ed_: {"status": "http_503", "arquivos": [], "portal": None})

    r = app_main._arquivos_pncp_cache(db, ed, forcar=True)

    assert r["status"] == "http_503"
    assert ed.arquivos_pncp["arquivos"][0]["titulo"] == "Edital"   # cache antigo intacto


def test_endpoint_documentos_usa_o_cache(monkeypatch):
    db = _sessao()
    ed = _edital(db)
    u = app_main.Usuario(nome="Teste", email="t@t.com", senha_hash="x")
    db.add(u)
    db.commit()
    chamadas = []

    def _fake(ed_):
        chamadas.append(1)
        return {"status": "vazio", "arquivos": [], "portal": None}
    monkeypatch.setattr(app_main, "_listar_arquivos_pncp", _fake)

    app_main.documentos_edital(ed.id, user=u, db=db)
    app_main.documentos_edital(ed.id, user=u, db=db)

    assert len(chamadas) == 1
