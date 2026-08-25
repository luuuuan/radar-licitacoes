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
from app.main import (
    completar_descricao_itens, completar_descricao_cancelar,
    _completar_descricao_locks, _completar_descricao_status, _completar_descricao_cancelar,
)
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
    _completar_descricao_cancelar.clear()
    yield
    _completar_descricao_locks.clear()
    _completar_descricao_status.clear()
    _completar_descricao_cancelar.clear()


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
    assert st == {"rodando": False, "erro": None, "status": "ok", "detalhe": None, "atualizados": 1}

    db_check = Session()
    item = db_check.query(ItemEdital).filter_by(edital_id=edital_id, numero=24).first()
    assert item.descricao == "PAPEL A4, CAIXA COM 10 RESMAS DE 500 FOLHAS CADA"
    ed_check = db_check.get(Edital, edital_id)
    assert ed_check.itens_completados_em is not None
    assert ed_check.itens_completados_qtd == 1
    # a trava foi liberada no final -- outra chamada consegue rodar de novo
    assert not app_main._lock_completar_descricao(edital_id).locked()


def test_bg_ok_sem_nenhuma_melhoria_nao_marca_qtd_maior_que_zero(monkeypatch):
    """Achado real: a tela mostrava "Descrições completadas a partir do
    documento oficial" sempre que itens_completados_em estava setado --
    mas isso só significa que uma tentativa TERMINOU (status "ok"), não
    que ela achou algo pra completar. Um "ok" com 0 itens atualizados não
    pode deixar itens_completados_qtd > 0."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(app_main, "SessionLocal", Session)

    db_setup = Session()
    ed = Edital(fonte="PNCP", id_externo="ed1", orgao="Orgao", objeto="Aquisicao", uf="SP")
    db_setup.add(ed)
    db_setup.commit()
    db_setup.add(ItemEdital(edital_id=ed.id, numero=1, descricao="já está completa"))
    db_setup.commit()
    edital_id = ed.id
    db_setup.close()

    monkeypatch.setattr(app_main.settings, "DEEPINFRA_API_KEY", "fake-key")
    monkeypatch.setattr(app_main, "_listar_arquivos_pncp",
                        lambda ed: {"status": "ok", "arquivos": [{"titulo": "edital", "url": "http://x"}]})

    from app import itens_pdf
    monkeypatch.setattr(itens_pdf, "extrair_itens_completos", lambda *a, **k: {"status": "ok", "itens": []})

    app_main._rodar_completar_descricao_bg(edital_id)

    db_check = Session()
    ed_check = db_check.get(Edital, edital_id)
    assert ed_check.itens_completados_em is not None   # tentativa terminou -- não retenta sozinha
    assert ed_check.itens_completados_qtd == 0           # mas não completou nada


def test_bg_falha_na_extracao_nao_marca_como_completado(monkeypatch):
    """Achado real: ed.itens_completados_em era gravado SEMPRE, mesmo quando
    extrair_itens_completos() não conseguia ler nada (ex.: documento
    escaneado sem texto suficiente) -- isso bloqueava pra sempre o disparo
    automático (que só roda quando itens_completados==False), mesmo que uma
    tentativa futura pudesse dar certo (documento reprocessado, OCR
    melhorado etc.). Só marca como completado quando a extração realmente
    terminou com um resultado ("ok")."""
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
    monkeypatch.setattr(itens_pdf, "extrair_itens_completos", lambda *a, **k: {"status": "sem_texto"})

    app_main._rodar_completar_descricao_bg(edital_id)

    st = _completar_descricao_status[edital_id]
    assert st == {"rodando": False, "erro": None, "status": "sem_texto", "detalhe": None, "atualizados": 0}

    db_check = Session()
    ed_check = db_check.get(Edital, edital_id)
    assert ed_check.itens_completados_em is None   # continua elegível pro disparo automático tentar de novo


def test_bg_erro_ia_expoe_o_detalhe_pra_diagnostico(monkeypatch):
    """Achado real: um "erro_ia" em produção não dava pra saber SE ERA
    http_500, rate limit, timeout de rede etc. sem olhar log do servidor --
    resultado.get("detalhe") (já devolvido por itens_pdf.extrair_itens_completos)
    precisa chegar até o status exposto pra quem for investigar."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(app_main, "SessionLocal", Session)

    db_setup = Session()
    ed = Edital(fonte="PNCP", id_externo="ed1", orgao="Orgao", objeto="Aquisicao", uf="SP")
    db_setup.add(ed)
    db_setup.commit()
    db_setup.add(ItemEdital(edital_id=ed.id, numero=24, descricao="curta"))
    db_setup.commit()
    edital_id = ed.id
    db_setup.close()

    monkeypatch.setattr(app_main.settings, "DEEPINFRA_API_KEY", "fake-key")
    monkeypatch.setattr(app_main, "_listar_arquivos_pncp",
                        lambda ed: {"status": "ok", "arquivos": [{"titulo": "edital", "url": "http://x"}]})

    from app import itens_pdf
    monkeypatch.setattr(itens_pdf, "extrair_itens_completos",
                        lambda *a, **k: {"status": "erro_ia", "detalhe": "http_500"})

    app_main._rodar_completar_descricao_bg(edital_id)

    assert _completar_descricao_status[edital_id] == {
        "rodando": False, "erro": None, "status": "erro_ia", "detalhe": "http_500", "atualizados": 0}


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


def test_endpoint_cancelar_seta_a_flag_por_edital():
    r = completar_descricao_cancelar(77, user=_usuario())
    assert r["ok"] is True
    assert _completar_descricao_cancelar[77] is True


def test_bg_cancelado_antes_da_etapa_cara_nao_chama_a_ia_e_marca_status(monkeypatch):
    """Cooperativo, mesmo espírito de _analise_cancelar: se o cancelamento
    chegou (por uma requisição concorrente de /cancelar) antes da etapa cara
    (documento + IA) começar, pula ela inteira -- nunca gasta a chamada à
    toa. O cancelamento chega DURANTE a etapa rápida (_listar_arquivos_pncp)
    pra simular fielmente a janela real: o pop defensivo do início já
    aconteceu, então só um cancelamento que chega DEPOIS dele conta."""
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

    def _listar_e_cancelar_concorrentemente(ed):
        _completar_descricao_cancelar[edital_id] = True
        return {"status": "ok", "arquivos": [{"titulo": "edital", "url": "http://x"}]}
    monkeypatch.setattr(app_main, "_listar_arquivos_pncp", _listar_e_cancelar_concorrentemente)

    from app import itens_pdf
    chamou_ia = []
    monkeypatch.setattr(itens_pdf, "extrair_itens_completos",
                        lambda *a, **k: chamou_ia.append(1) or {"status": "ok", "itens": []})

    app_main._rodar_completar_descricao_bg(edital_id)

    assert chamou_ia == []
    assert _completar_descricao_status[edital_id] == {
        "rodando": False, "erro": None, "status": "cancelado", "atualizados": 0}
    assert not app_main._lock_completar_descricao(edital_id).locked()


def test_bg_nao_herda_cancelamento_de_uma_rodada_anterior(monkeypatch):
    """Mesmo padrão defensivo de _analise_cancelar.pop: uma flag de
    cancelamento deixada de uma rodada anterior (já resolvida) não pode
    fazer a PRÓXIMA rodada ser pulada silenciosamente."""
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

    _completar_descricao_cancelar[edital_id] = True   # deixado de uma rodada anterior já resolvida
    app_main._rodar_completar_descricao_bg(edital_id)

    st = _completar_descricao_status[edital_id]
    assert st == {"rodando": False, "erro": None, "status": "ok", "detalhe": None, "atualizados": 1}
