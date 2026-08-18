"""
Achado real: completar_descricao_itens() rodava dentro da própria request
HTTP — lê o documento inteiro + calcula embeddings + chama a IA, com
retentativa se a DeepInfra estiver lenta, e podia passar de 100-160s num
edital grande. O proxy do Render derrubava a conexão com 502 antes disso
terminar (o trabalho continuava no servidor e salvava no final, mas o
navegador nunca via o resultado). Mesmo padrão de segundo plano + polling
já usado pelo recálculo (ver test_recalcular_modelo_reranker.py), só que a
trava é por EDITAL (não por usuário) — ItemEdital.descricao é
compartilhada entre todo mundo que vê aquele edital. Rode com:
cd backend && pytest
"""
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import main as app_main
from app.main import completar_descricao_itens, _completar_descricao_locks, _completar_descricao_status
from app.models import Base, Edital, ItemEdital


def _usuario(id=1):
    return SimpleNamespace(id=id)


def _sessao():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


@pytest.fixture(autouse=True)
def _limpa_estado_global():
    """Os dicts de trava/status são globais por edital_id — limpa antes/
    depois de cada teste pra um teste não vazar estado pro outro."""
    _completar_descricao_locks.clear()
    _completar_descricao_status.clear()
    yield
    _completar_descricao_locks.clear()
    _completar_descricao_status.clear()


def test_edital_nao_existe_da_404():
    db = _sessao()
    bt = BackgroundTasks()
    with pytest.raises(Exception) as exc:
        completar_descricao_itens(999, bt, user=_usuario(), db=db)
    assert "404" in str(exc.value) or "não encontrado" in str(exc.value).lower()


def test_dispara_em_segundo_plano_e_retorna_na_hora():
    db = _sessao()
    ed = Edital(fonte="PNCP", id_externo="ed1", orgao="Orgao", objeto="Aquisicao", uf="SP")
    db.add(ed)
    db.commit()

    bt = BackgroundTasks()
    r = completar_descricao_itens(ed.id, bt, user=_usuario(), db=db)

    assert r == {"ok": True, "em_andamento": True}
    assert len(bt.tasks) == 1
    assert bt.tasks[0].args == (ed.id,)
    assert _completar_descricao_status[ed.id] == {"rodando": True, "erro": None}


def test_ja_em_andamento_nao_dispara_de_novo():
    db = _sessao()
    ed = Edital(fonte="PNCP", id_externo="ed1", orgao="Orgao", objeto="Aquisicao", uf="SP")
    db.add(ed)
    db.commit()
    _completar_descricao_locks[ed.id] = __import__("threading").Lock()
    _completar_descricao_locks[ed.id].acquire()

    bt = BackgroundTasks()
    r = completar_descricao_itens(ed.id, bt, user=_usuario(), db=db)

    assert r["ok"] is False
    assert r["em_andamento"] is True
    assert len(bt.tasks) == 0


def test_bg_completa_descricao_e_atualiza_status(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(app_main, "SessionLocal", Session)

    db_setup = Session()
    ed = Edital(fonte="PNCP", id_externo="ed1", orgao="Orgao", objeto="Aquisicao", uf="SP")
    db_setup.add(ed)
    db_setup.commit()
    db_setup.add(ItemEdital(edital_id=ed.id, numero=24, descricao="PAPEL A4 210 X 297 75G/M"))
    db_setup.commit()
    edital_id = ed.id
    db_setup.close()

    monkeypatch.setattr(app_main.settings, "DEEPINFRA_API_KEY", "fake-key")
    monkeypatch.setattr(app_main, "_listar_arquivos_pncp",
                        lambda ed: {"status": "ok", "arquivos": [{"titulo": "edital", "url": "http://x"}]})

    from app import itens_pdf
    monkeypatch.setattr(itens_pdf, "extrair_itens_completos", lambda *a, **k: {
        "status": "ok",
        "itens": [{"numero": 24, "descricao_completa": "PAPEL A4, CAIXA COM 10 RESMAS DE 500 FOLHAS CADA"}],
    })

    app_main._rodar_completar_descricao_bg(edital_id)

    st = _completar_descricao_status[edital_id]
    assert st == {"rodando": False, "erro": None, "status": "ok", "atualizados": 1}

    db_check = Session()
    item = db_check.query(ItemEdital).filter_by(edital_id=edital_id, numero=24).first()
    assert item.descricao == "PAPEL A4, CAIXA COM 10 RESMAS DE 500 FOLHAS CADA"
    ed_check = db_check.get(Edital, edital_id)
    assert ed_check.itens_completados_em is not None
    # a trava foi liberada no final -- outra chamada consegue rodar de novo
    assert not app_main._lock_completar_descricao(edital_id).locked()


def test_bg_sem_chave_deepinfra_marca_status_sem_ia(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(app_main, "SessionLocal", Session)

    db_setup = Session()
    ed = Edital(fonte="PNCP", id_externo="ed1", orgao="Orgao", objeto="Aquisicao", uf="SP")
    db_setup.add(ed)
    db_setup.commit()
    edital_id = ed.id
    db_setup.close()

    monkeypatch.setattr(app_main.settings, "DEEPINFRA_API_KEY", "")

    app_main._rodar_completar_descricao_bg(edital_id)

    assert _completar_descricao_status[edital_id] == {
        "rodando": False, "erro": None, "status": "sem_ia", "atualizados": 0}
    assert not app_main._lock_completar_descricao(edital_id).locked()


def test_bg_erro_inesperado_libera_a_trava_e_registra_no_status(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(app_main, "SessionLocal", Session)

    db_setup = Session()
    ed = Edital(fonte="PNCP", id_externo="ed1", orgao="Orgao", objeto="Aquisicao", uf="SP")
    db_setup.add(ed)
    db_setup.commit()
    edital_id = ed.id
    db_setup.close()

    monkeypatch.setattr(app_main.settings, "DEEPINFRA_API_KEY", "fake-key")
    def _explode(ed):
        raise RuntimeError("PNCP fora do ar")
    monkeypatch.setattr(app_main, "_listar_arquivos_pncp", _explode)

    app_main._rodar_completar_descricao_bg(edital_id)

    st = _completar_descricao_status[edital_id]
    assert st["rodando"] is False
    assert "PNCP fora do ar" in st["erro"]
    assert not app_main._lock_completar_descricao(edital_id).locked()


def test_bg_ja_travado_nao_roda_de_novo_nem_mexe_no_status():
    edital_id = 42
    lock = app_main._lock_completar_descricao(edital_id)
    lock.acquire()
    try:
        _completar_descricao_status[edital_id] = {"rodando": True, "erro": None}
        app_main._rodar_completar_descricao_bg(edital_id)
        # não mexeu em nada -- ainda "rodando" (a chamada real não fez nada)
        assert _completar_descricao_status[edital_id] == {"rodando": True, "erro": None}
    finally:
        lock.release()
