"""
Testes de POST /api/editais/{id}/analise/iniciar + GET .../analise/status.
Achado real: a Análise por IA rodava síncrona dentro da request HTTP e podia
passar de 100s+ em editais grandes (resumo + lotes de comparação de
catálogo, até 90s cada) -- o proxy da Railway derrubava a conexão antes de
terminar, o navegador mostrava "Erro ao analisar edital", mas o trabalho
continuava no servidor e salvava o resultado (por isso reabrir a página sem
clicar em nada já mostrava a análise pronta). Agora roda em BackgroundTasks,
mesmo padrão do completar-descrição, só que por (usuário, edital) -- não só
por edital -- porque a comparação de catálogo é por usuário.
Rode com: cd backend && pytest
"""
import pytest
from fastapi import BackgroundTasks
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import main as app_main
from app import analise_edital as ia_module
from app.models import Base, Usuario, Edital


@pytest.fixture(autouse=True)
def _limpa_estado_global():
    """_analise_status/_analise_cancelar são dicts em memória, no MÓDULO
    app.main -- persistem entre testes (cada teste usa um banco novo, mas
    os IDs de usuário/edital reiniciam do 1, então uma chave antiga podia
    vazar pro teste seguinte sem isso)."""
    app_main._analise_status.clear()
    app_main._analise_cancelar.clear()
    yield
    app_main._analise_status.clear()
    app_main._analise_cancelar.clear()


def _sessao_e_fabrica(tmp_path, nome):
    """Devolve (sessão pronta pra usar, sessionmaker) -- a fábrica serve pra
    monkeypatch.setattr(app_main, "SessionLocal", fabrica): _rodar_analise_bg
    abre sua PRÓPRIA sessão (SessionLocal() puro, sem receber `db` de
    parâmetro -- é assim que roda de verdade em produção, fora do ciclo de
    vida da request), então sem isso ele bateria no banco de dev local de
    verdade em vez do banco isolado deste teste (mesmo padrão já usado em
    test_completar_descricao_itens.py)."""
    engine = create_engine(f"sqlite:///{tmp_path / nome}")
    Base.metadata.create_all(engine)
    Fabrica = sessionmaker(bind=engine)
    return Fabrica(), Fabrica


def _usuario_e_edital(db, email="t@t.com"):
    u = Usuario(nome="Teste", email=email, senha_hash="x")
    db.add(u)
    db.commit()
    ed = Edital(fonte="PNCP", id_externo="sem-ref-valida", orgao="Orgao",
               objeto="Aquisicao de material", uf="SP")
    db.add(ed)
    db.commit()
    return u, ed


def test_iniciar_dispara_e_status_reflete_conclusao(monkeypatch, tmp_path):
    db, fabrica = _sessao_e_fabrica(tmp_path, "a.db")
    monkeypatch.setattr(app_main, "SessionLocal", fabrica)
    u, ed = _usuario_e_edital(db)

    monkeypatch.setattr(ia_module, "ia_texto_disponivel", lambda chave: True)
    monkeypatch.setattr(ia_module, "analisar", lambda objeto, arquivos, api_key=None: {
        "status": "ok", "versao": ia_module.VERSAO_PROMPT, "resumo": "ok",
        "objeto": objeto, "requisitos_tecnicos": [], "documentos_habilitacao": []})

    r = app_main.analise_edital_iniciar(ed.id, BackgroundTasks(), forcar=False, user=u, db=db)
    assert r == {"ok": True, "em_andamento": True}

    st = app_main.analise_edital_status(ed.id, user=u)
    assert st["rodando"] is True

    # simula o que bg.add_task teria disparado (sessão isolada de teste, sem
    # depender do event loop do FastAPI rodando de verdade)
    app_main._rodar_analise_bg(ed.id, u.id, False)

    st = app_main.analise_edital_status(ed.id, user=u)
    assert st["rodando"] is False
    assert st["erro"] is None
    assert st["resultado"]["status"] == "ok"


def test_iniciar_com_analise_ja_em_andamento_nao_duplica(tmp_path):
    db, _ = _sessao_e_fabrica(tmp_path, "b.db")
    u, ed = _usuario_e_edital(db)
    app_main._analise_status[(u.id, ed.id)] = {"rodando": True, "erro": None}

    r = app_main.analise_edital_iniciar(ed.id, BackgroundTasks(), forcar=False, user=u, db=db)

    assert r["ok"] is False
    assert r["em_andamento"] is True


def test_iniciar_edital_inexistente_da_404(tmp_path):
    db, _ = _sessao_e_fabrica(tmp_path, "c.db")
    u = Usuario(nome="Teste", email="t@t.com", senha_hash="x")
    db.add(u)
    db.commit()

    with pytest.raises(Exception) as exc:
        app_main.analise_edital_iniciar(99999, BackgroundTasks(), forcar=False, user=u, db=db)
    assert "404" in str(exc.value) or "não encontrado" in str(exc.value).lower()


def test_status_sem_nada_iniciado_retorna_parado(tmp_path):
    db, _ = _sessao_e_fabrica(tmp_path, "d.db")
    u, ed = _usuario_e_edital(db)
    st = app_main.analise_edital_status(ed.id, user=u)
    assert st == {"rodando": False, "erro": None}


def test_bg_com_erro_fica_registrado_no_status_sem_derrubar(monkeypatch, tmp_path):
    db, fabrica = _sessao_e_fabrica(tmp_path, "e.db")
    monkeypatch.setattr(app_main, "SessionLocal", fabrica)
    u, ed = _usuario_e_edital(db)

    monkeypatch.setattr(ia_module, "ia_texto_disponivel", lambda chave: True)
    def _explode(*a, **k):
        raise RuntimeError("falha simulada da IA")
    monkeypatch.setattr(ia_module, "analisar", _explode)

    app_main._analise_status[(u.id, ed.id)] = {"rodando": True, "erro": None}
    app_main._rodar_analise_bg(ed.id, u.id, False)   # não deve levantar

    st = app_main.analise_edital_status(ed.id, user=u)
    assert st["rodando"] is False
    assert "falha simulada" in st["erro"]


def test_status_e_isolado_por_usuario_nao_vaza_catalogo_entre_contas(monkeypatch, tmp_path):
    """A comparação de catálogo é por usuário -- o status de um usuário não
    pode devolver o resultado (com o catálogo) de outro usuário no MESMO
    edital."""
    db, fabrica = _sessao_e_fabrica(tmp_path, "f.db")
    monkeypatch.setattr(app_main, "SessionLocal", fabrica)
    u1, ed = _usuario_e_edital(db, email="u1@t.com")
    u2 = Usuario(nome="Outro", email="u2@t.com", senha_hash="x")
    db.add(u2)
    db.commit()

    monkeypatch.setattr(ia_module, "ia_texto_disponivel", lambda chave: True)
    monkeypatch.setattr(ia_module, "analisar", lambda objeto, arquivos, api_key=None: {
        "status": "ok", "versao": ia_module.VERSAO_PROMPT, "resumo": "ok",
        "objeto": objeto, "requisitos_tecnicos": [], "documentos_habilitacao": []})

    app_main.analise_edital_iniciar(ed.id, BackgroundTasks(), forcar=False, user=u1, db=db)
    app_main._rodar_analise_bg(ed.id, u1.id, False)

    st_u1 = app_main.analise_edital_status(ed.id, user=u1)
    st_u2 = app_main.analise_edital_status(ed.id, user=u2)
    assert st_u1["rodando"] is False and st_u1.get("resultado")
    assert st_u2 == {"rodando": False, "erro": None}   # u2 nunca pediu nada pra este edital
