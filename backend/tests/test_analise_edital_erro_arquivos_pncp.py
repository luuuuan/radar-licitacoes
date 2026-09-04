"""
Achado real (edital 127468, reportado pelo usuário): a aba Documentos e a
Análise por IA chamam a MESMA _listar_arquivos_pncp, mas em requisições
separadas (sem cache) -- uma falha passageira ao buscar no PNCP (timeout,
rede, HTTP 5xx) faz a lista de arquivos vir vazia por um motivo bem
diferente de "este edital não tem arquivo publicado". Antes desta correção,
qualquer motivo de lista vazia virava a mesma mensagem enganosa
("sem_arquivo"), mesmo quando o documento existia (como a aba Documentos,
chamada segundos depois, mostrou). GET /api/editais/{id}/analise só trata
como "sem arquivo" quando a busca teve sucesso (status "ok"/"vazio");
qualquer outro status vira "erro_arquivos_pncp", distinto. Rode com:
cd backend && pytest
"""
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


def _usuario_com_chave(db, monkeypatch):
    u = Usuario(nome="Teste", email="t@t.com", senha_hash="x")
    db.add(u)
    db.commit()
    monkeypatch.setattr(app_main._auth, "decifrar", lambda _cifrada: "fake-key")
    monkeypatch.setattr(ia_module, "ia_texto_disponivel", lambda chave: True)
    return u


def test_falha_de_rede_ao_buscar_arquivos_nao_vira_sem_arquivo(monkeypatch):
    db = _sessao()
    u = _usuario_com_chave(db, monkeypatch)
    ed = Edital(fonte="PNCP", id_externo="46384111000140-1-000934/2026",
               orgao="Orgao", objeto="Aquisicao", uf="SP")
    db.add(ed)
    db.commit()

    monkeypatch.setattr(app_main, "_listar_arquivos_pncp",
                        lambda ed_: {"status": "erro_rede", "arquivos": [], "portal": None})
    chamou_ia = []
    monkeypatch.setattr(ia_module, "analisar", lambda *a, **k: chamou_ia.append(1))

    r = app_main.analise_edital(ed.id, forcar=False, user=u, db=db)

    assert r["status"] == "erro_arquivos_pncp"
    assert r["status"] != "sem_arquivo"
    assert chamou_ia == []   # não desperdiça a chamada de IA numa lista vazia por falha de busca


def test_http_5xx_ao_buscar_arquivos_tambem_vira_erro_arquivos_pncp(monkeypatch):
    db = _sessao()
    u = _usuario_com_chave(db, monkeypatch)
    ed = Edital(fonte="PNCP", id_externo="46384111000140-1-000934/2026",
               orgao="Orgao", objeto="Aquisicao", uf="SP")
    db.add(ed)
    db.commit()

    monkeypatch.setattr(app_main, "_listar_arquivos_pncp",
                        lambda ed_: {"status": "http_503", "arquivos": [], "portal": None})

    r = app_main.analise_edital(ed.id, forcar=False, user=u, db=db)

    assert r["status"] == "erro_arquivos_pncp"
    assert r["detalhe"] == "http_503"


def test_busca_com_sucesso_e_genuinamente_vazia_continua_sem_arquivo(monkeypatch):
    """Contraste com os dois testes acima: quando a busca REALMENTE funciona
    (status "vazio") e não há arquivo nenhum, a mensagem "sem arquivo"
    continua correta -- só não pode ser usada quando a busca falhou."""
    db = _sessao()
    u = _usuario_com_chave(db, monkeypatch)
    ed = Edital(fonte="PNCP", id_externo="46384111000140-1-000934/2026",
               orgao="Orgao", objeto="Aquisicao", uf="SP")
    db.add(ed)
    db.commit()

    monkeypatch.setattr(app_main, "_listar_arquivos_pncp",
                        lambda ed_: {"status": "vazio", "arquivos": [], "portal": None})

    r = app_main.analise_edital(ed.id, forcar=False, user=u, db=db)

    assert r["status"] == "sem_arquivo"


def test_arquivo_encontrado_chama_a_ia_normalmente(monkeypatch):
    db = _sessao()
    u = _usuario_com_chave(db, monkeypatch)
    ed = Edital(fonte="PNCP", id_externo="46384111000140-1-000934/2026",
               orgao="Orgao", objeto="Aquisicao", uf="SP")
    db.add(ed)
    db.commit()

    monkeypatch.setattr(app_main, "_listar_arquivos_pncp", lambda ed_: {
        "status": "ok", "arquivos": [{"titulo": "Edital", "url": "http://x/edital.pdf"}],
        "portal": None})
    chamadas = []

    def _analisar_fake(objeto, arquivos, api_key=None):
        chamadas.append(arquivos)
        return {"status": "ok", "versao": ia_module.VERSAO_PROMPT, "objeto": objeto,
                "requisitos_tecnicos": [], "documentos_habilitacao": []}
    monkeypatch.setattr(ia_module, "analisar", _analisar_fake)

    r = app_main.analise_edital(ed.id, forcar=False, user=u, db=db)

    assert r["status"] == "ok"
    assert len(chamadas) == 1
    assert chamadas[0][0]["titulo"] == "Edital"
