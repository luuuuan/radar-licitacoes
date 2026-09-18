"""
Achado real (usuário reportou, edital 136161): data_abertura/data_encerramento
gravadas ANTES de _parse_data_hora existir (ver connectors/pncp.py) ficaram
com hora zerada (meia-noite) -- a data está certa, só a hora não foi
preservada na coleta original. A coleta diária só busca editais com janela
de proposta ainda ABERTA, então um edital já encerrado nunca se
autocorrigiria sozinho. _datas_pncp_com_hora_zerada detecta esse caso e
_reconsultar_datas_pncp corrige sob demanda (1 chamada direta ao PNCP para
aquele edital específico), disparada de dentro de edital_detalhe. Rode com:
cd backend && pytest
"""
import datetime
import requests

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import main as app_main
from app.main import edital_detalhe
from app.models import Base, Usuario, Edital


def _sessao():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def _usuario(db):
    u = Usuario(nome="Teste", email="t@t.com", senha_hash="x")
    db.add(u)
    db.commit()
    return u


def _edital(db, **kw):
    base = dict(fonte="PNCP", id_externo="46384111000140-1-000934/2026",
               orgao="Orgao", objeto="Aquisicao", uf="SP")
    base.update(kw)
    ed = Edital(**base)
    db.add(ed)
    db.commit()
    return ed


class _RespostaFake:
    def __init__(self, status_code=200, payload=None, json_quebrado=False):
        self.status_code = status_code
        self._payload = payload or {}
        self._json_quebrado = json_quebrado

    def json(self):
        if self._json_quebrado:
            raise ValueError("json quebrado")
        return self._payload


# --------------------- _datas_pncp_com_hora_zerada --------------------- #

def test_detecta_hora_zerada_na_abertura():
    ed = Edital(data_abertura=datetime.datetime(2026, 9, 9, 0, 0, 0))
    assert app_main._datas_pncp_com_hora_zerada(ed) is True


def test_detecta_hora_zerada_no_encerramento():
    ed = Edital(data_encerramento=datetime.datetime(2026, 9, 22, 0, 0, 0))
    assert app_main._datas_pncp_com_hora_zerada(ed) is True


def test_nao_detecta_quando_hora_e_real():
    ed = Edital(data_abertura=datetime.datetime(2026, 9, 9, 10, 0, 0),
               data_encerramento=datetime.datetime(2026, 9, 22, 8, 29, 0))
    assert app_main._datas_pncp_com_hora_zerada(ed) is False


def test_nao_detecta_quando_datas_sao_none():
    ed = Edital()
    assert app_main._datas_pncp_com_hora_zerada(ed) is False


# ------------------------ _reconsultar_datas_pncp ----------------------- #

def test_reconsulta_corrige_hora_zerada_com_dado_real_do_pncp(monkeypatch):
    db = _sessao()
    ed = _edital(db, data_abertura=datetime.datetime(2026, 9, 9, 0, 0, 0),
                data_encerramento=datetime.datetime(2026, 9, 22, 0, 0, 0))

    def _fake_get(url, timeout=None, headers=None):
        assert "46384111000140" in url
        return _RespostaFake(200, {
            "dataAberturaProposta": "2026-09-09T10:00:00",
            "dataEncerramentoProposta": "2026-09-22T08:29:00",
        })
    monkeypatch.setattr(app_main.requests, "get", _fake_get)

    app_main._reconsultar_datas_pncp(ed, db)

    assert ed.data_abertura == datetime.datetime(2026, 9, 9, 10, 0, 0)
    assert ed.data_encerramento == datetime.datetime(2026, 9, 22, 8, 29, 0)


def test_reconsulta_nao_quebra_com_id_externo_invalido(monkeypatch):
    db = _sessao()
    ed = _edital(db, id_externo="formato-invalido-sem-barra",
                data_abertura=datetime.datetime(2026, 9, 9, 0, 0, 0))
    chamou = []
    monkeypatch.setattr(app_main.requests, "get", lambda *a, **k: chamou.append(1))

    app_main._reconsultar_datas_pncp(ed, db)

    assert chamou == []
    assert ed.data_abertura == datetime.datetime(2026, 9, 9, 0, 0, 0)


def test_reconsulta_usa_o_link_quando_id_externo_e_inesperado(monkeypatch):
    """Robustez defensiva: se id_externo vier num formato que _ref_pncp não
    consegue parsear, cai pro link do PNCP (mesmo formato
    cnpj/ano/sequencial, ver _montar_link em connectors/pncp.py), que
    continua confiável."""
    db = _sessao()
    ed = _edital(db, id_externo="formato-sem-numero-de-ano-valido",
                link="https://pncp.gov.br/app/editais/03656200000195/2026/72",
                data_abertura=datetime.datetime(2026, 9, 9, 0, 0, 0))

    def _fake_get(url, timeout=None, headers=None):
        assert "03656200000195" in url and "/2026/72" in url
        return _RespostaFake(200, {
            "dataAberturaProposta": "2026-09-09T10:00:00",
            "dataEncerramentoProposta": "2026-09-22T08:29:00",
        })
    monkeypatch.setattr(app_main.requests, "get", _fake_get)

    app_main._reconsultar_datas_pncp(ed, db)

    assert ed.data_abertura == datetime.datetime(2026, 9, 9, 10, 0, 0)


def test_reconsulta_falha_de_rede_nao_quebra_e_mantem_dado_antigo(monkeypatch):
    db = _sessao()
    ed = _edital(db, data_abertura=datetime.datetime(2026, 9, 9, 0, 0, 0))

    def _fake_get(*a, **k):
        raise requests.exceptions.ConnectionError("falhou")
    monkeypatch.setattr(app_main.requests, "get", _fake_get)

    app_main._reconsultar_datas_pncp(ed, db)   # não deve levantar exceção

    assert ed.data_abertura == datetime.datetime(2026, 9, 9, 0, 0, 0)


def test_reconsulta_http_erro_nao_atualiza(monkeypatch):
    db = _sessao()
    ed = _edital(db, data_abertura=datetime.datetime(2026, 9, 9, 0, 0, 0))
    monkeypatch.setattr(app_main.requests, "get",
                        lambda *a, **k: _RespostaFake(500))

    app_main._reconsultar_datas_pncp(ed, db)

    assert ed.data_abertura == datetime.datetime(2026, 9, 9, 0, 0, 0)


def test_reconsulta_json_invalido_nao_atualiza(monkeypatch):
    db = _sessao()
    ed = _edital(db, data_abertura=datetime.datetime(2026, 9, 9, 0, 0, 0))
    monkeypatch.setattr(app_main.requests, "get",
                        lambda *a, **k: _RespostaFake(200, json_quebrado=True))

    app_main._reconsultar_datas_pncp(ed, db)

    assert ed.data_abertura == datetime.datetime(2026, 9, 9, 0, 0, 0)


def test_reconsulta_edital_sumido_do_pncp_nao_atualiza(monkeypatch):
    """Edital que não existe mais no PNCP (404): mantém a hora errada e
    tenta de novo na próxima abertura, em vez de quebrar a página."""
    db = _sessao()
    ed = _edital(db, data_abertura=datetime.datetime(2026, 9, 9, 0, 0, 0))
    monkeypatch.setattr(app_main.requests, "get",
                        lambda *a, **k: _RespostaFake(404))

    app_main._reconsultar_datas_pncp(ed, db)

    assert ed.data_abertura == datetime.datetime(2026, 9, 9, 0, 0, 0)


# ---------------- integração: edital_detalhe corrige sozinho ------------ #

def test_edital_detalhe_corrige_hora_zerada_automaticamente(monkeypatch):
    db = _sessao()
    u = _usuario(db)
    ed = _edital(db, data_abertura=datetime.datetime(2026, 9, 9, 0, 0, 0),
                data_encerramento=datetime.datetime(2026, 9, 22, 0, 0, 0))

    monkeypatch.setattr(app_main.requests, "get", lambda *a, **k: _RespostaFake(200, {
        "dataAberturaProposta": "2026-09-09T10:00:00",
        "dataEncerramentoProposta": "2026-09-22T08:29:00",
    }))

    r = edital_detalhe(edital_id=ed.id, user=u, db=db)

    assert r["edital"]["data_abertura"] == "2026-09-09T10:00:00"
    assert r["edital"]["data_encerramento"] == "2026-09-22T08:29:00"


def test_edital_detalhe_nao_reconsulta_quando_hora_ja_e_real(monkeypatch):
    db = _sessao()
    u = _usuario(db)
    ed = _edital(db, data_abertura=datetime.datetime(2026, 9, 9, 10, 0, 0),
                data_encerramento=datetime.datetime(2026, 9, 22, 8, 29, 0))
    chamou = []
    monkeypatch.setattr(app_main.requests, "get", lambda *a, **k: chamou.append(1))

    edital_detalhe(edital_id=ed.id, user=u, db=db)

    assert chamou == []   # não desperdiça chamada de rede quando já está correto
