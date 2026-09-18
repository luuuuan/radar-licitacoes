"""
Backfill único (não recorrente): a maioria dos editais com hora zerada em
data_abertura/data_encerramento se autocorrige sozinha na próxima vez que
alguém abre a página (ver test_reconsultar_datas_pncp.py), mas um edital já
encerrado -- que ninguém mais vai abrir -- nunca passaria por lá. POST
/api/cron/backfill-datas-pncp varre em lote e corrige, protegido pela mesma
X-Cron-Key de /api/coletar-cron. Rode com:  cd backend && pytest
"""
import datetime

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import main as app_main
from app.models import Base, Edital


def _sessao():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def _edital(db, **kw):
    base = dict(fonte="PNCP", id_externo="46384111000140-1-000934/2026",
               orgao="Orgao", objeto="Aquisicao", uf="SP")
    base.update(kw)
    ed = Edital(**base)
    db.add(ed)
    db.commit()
    return ed


class _RespostaFake:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class _FakeRequest:
    """Só o suficiente pro endpoint ler a chave -- evita subir um
    TestClient/ASGI de verdade só pra 2 campos lidos (mesmo espírito das
    outras suítes deste projeto: testar a função direto, sem framework)."""
    def __init__(self, headers=None, query_params=None):
        self.headers = headers or {}
        self.query_params = query_params or {}


def _resposta_pncp_ok(**overrides):
    payload = {
        "dataAberturaProposta": "2026-09-09T10:00:00",
        "dataEncerramentoProposta": "2026-09-22T08:29:00",
    }
    payload.update(overrides)
    return lambda *a, **k: _RespostaFake(200, payload)


def test_sem_chave_da_403(monkeypatch):
    monkeypatch.setattr(app_main.settings, "CRON_SECRET", "segredo")
    db = _sessao()

    with pytest.raises(HTTPException) as exc:
        app_main.backfill_datas_pncp_cron(_FakeRequest(), limite=50, db=db)
    assert exc.value.status_code == 403


def test_chave_errada_da_403(monkeypatch):
    monkeypatch.setattr(app_main.settings, "CRON_SECRET", "segredo")
    db = _sessao()

    with pytest.raises(HTTPException) as exc:
        app_main.backfill_datas_pncp_cron(
            _FakeRequest(headers={"X-Cron-Key": "chave-errada"}), limite=50, db=db)
    assert exc.value.status_code == 403


def test_cron_desativado_sem_secret_configurado(monkeypatch):
    monkeypatch.setattr(app_main.settings, "CRON_SECRET", "")
    db = _sessao()

    with pytest.raises(HTTPException) as exc:
        app_main.backfill_datas_pncp_cron(_FakeRequest(), limite=50, db=db)
    assert exc.value.status_code == 503


def test_corrige_editais_com_hora_zerada(monkeypatch):
    monkeypatch.setattr(app_main.settings, "CRON_SECRET", "segredo")
    db = _sessao()
    ed1 = _edital(db, id_externo="46384111000140-1-000934/2026",
                 data_abertura=datetime.datetime(2026, 9, 9, 0, 0, 0),
                 data_encerramento=datetime.datetime(2026, 9, 22, 0, 0, 0))
    ed2 = _edital(db, id_externo="46384111000140-1-000935/2026",
                 data_abertura=datetime.datetime(2026, 8, 1, 0, 0, 0))
    monkeypatch.setattr(app_main.requests, "get", _resposta_pncp_ok())

    r = app_main.backfill_datas_pncp_cron(
        _FakeRequest(headers={"X-Cron-Key": "segredo"}), limite=50, db=db)

    assert r["processados"] == 2
    assert r["corrigidos"] == 2
    db.refresh(ed1)
    db.refresh(ed2)
    assert ed1.data_abertura == datetime.datetime(2026, 9, 9, 10, 0, 0)
    assert ed2.data_abertura == datetime.datetime(2026, 9, 9, 10, 0, 0)


def test_edital_com_hora_real_nao_entra_no_lote(monkeypatch):
    monkeypatch.setattr(app_main.settings, "CRON_SECRET", "segredo")
    db = _sessao()
    _edital(db, data_abertura=datetime.datetime(2026, 9, 9, 10, 0, 0),
           data_encerramento=datetime.datetime(2026, 9, 22, 8, 29, 0))
    chamou = []
    monkeypatch.setattr(app_main.requests, "get", lambda *a, **k: chamou.append(1))

    r = app_main.backfill_datas_pncp_cron(
        _FakeRequest(headers={"X-Cron-Key": "segredo"}), limite=50, db=db)

    assert r["processados"] == 0
    assert chamou == []


def test_respeita_o_limite_por_chamada(monkeypatch):
    monkeypatch.setattr(app_main.settings, "CRON_SECRET", "segredo")
    db = _sessao()
    for i in range(5):
        _edital(db, id_externo=f"46384111000140-1-{i:06d}/2026",
               data_abertura=datetime.datetime(2026, 9, 9, 0, 0, 0))
    monkeypatch.setattr(app_main.requests, "get", _resposta_pncp_ok())

    r = app_main.backfill_datas_pncp_cron(
        _FakeRequest(headers={"X-Cron-Key": "segredo"}), limite=2, db=db)

    assert r["processados"] == 2


def test_e_idempotente_editais_corrigidos_somem_da_proxima_varredura(monkeypatch):
    monkeypatch.setattr(app_main.settings, "CRON_SECRET", "segredo")
    db = _sessao()
    _edital(db, data_abertura=datetime.datetime(2026, 9, 9, 0, 0, 0))
    monkeypatch.setattr(app_main.requests, "get", _resposta_pncp_ok())

    r1 = app_main.backfill_datas_pncp_cron(
        _FakeRequest(headers={"X-Cron-Key": "segredo"}), limite=50, db=db)
    r2 = app_main.backfill_datas_pncp_cron(
        _FakeRequest(headers={"X-Cron-Key": "segredo"}), limite=50, db=db)

    assert r1["corrigidos"] == 1
    assert r2["processados"] == 0   # já não sobrou nada zerado


def test_falha_ao_reconsultar_mantem_no_lote_pra_proxima_tentativa(monkeypatch):
    """Edital sumido do PNCP (ou falha de rede passageira): continua
    aparecendo como "processado, não corrigido" -- não quebra o lote, só
    não avança esse item específico (tentativa seguinte tenta de novo)."""
    monkeypatch.setattr(app_main.settings, "CRON_SECRET", "segredo")
    db = _sessao()
    _edital(db, data_abertura=datetime.datetime(2026, 9, 9, 0, 0, 0))
    monkeypatch.setattr(app_main.requests, "get", lambda *a, **k: _RespostaFake(404))

    r = app_main.backfill_datas_pncp_cron(
        _FakeRequest(headers={"X-Cron-Key": "segredo"}), limite=50, db=db)

    assert r["processados"] == 1
    assert r["corrigidos"] == 0
