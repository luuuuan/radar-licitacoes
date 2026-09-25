"""
Testes de fuso horário em lembretes.py (e-mail/Telegram) -- achado real
(agentes error-detective/code-reviewer, auditoria de notificações pedida
pelo usuário): as 3 funções (verificar_aberturas/verificar_prazos/
verificar_documentos) usavam date.today() (hora do servidor, UTC em
produção) em vez do dia em Brasília -- mesma classe de bug já corrigida no
sino de notificações in-app e nos filtros de editais. Entre 21h e
meia-noite de Brasília, "hoje" ficava adiantado em 1 dia, deslocando a
janela de aviso das 3 categorias por e-mail/Telegram. Rode com:
cd backend && pytest
"""
from datetime import datetime, date
from unittest.mock import MagicMock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import lembretes
from app.models import Base, Usuario, Edital, Match, Documento


def _sessao():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def _usuario(db, **kwargs):
    kwargs.setdefault("nome", "Teste")
    kwargs.setdefault("email", "t@t.com")
    kwargs.setdefault("senha_hash", "x")
    kwargs.setdefault("ativo", True)
    kwargs.setdefault("avisar_abertura", True)
    kwargs.setdefault("dias_antecedencia", 2)
    u = Usuario(**kwargs)
    db.add(u)
    db.commit()
    return u


def _mockar_agora_noite(monkeypatch):
    """23h30 em Brasília -- date.today() do servidor (UTC) já 'pensa' que é
    2027-01-16."""
    class _DateTimeFalsaNoite(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2027, 1, 15, 23, 30)
    monkeypatch.setattr(lembretes, "datetime", _DateTimeFalsaNoite)


def test_verificar_aberturas_usa_fuso_de_brasilia(monkeypatch):
    _mockar_agora_noite(monkeypatch)
    monkeypatch.setattr(lembretes, "notificar_usuario_lote", MagicMock(return_value=True))

    db = _sessao()
    u = _usuario(db)
    # abre HOJE (15/01) em Brasília -- date.today() (UTC) já acharia que é
    # amanhã e excluiria este edital do filtro "data_abertura >= hoje"
    ed = Edital(fonte="PNCP", id_externo="ed1", orgao="Orgao", objeto="Aquisicao", uf="SP",
               data_abertura=datetime(2027, 1, 15, 10, 0))
    db.add(ed)
    db.commit()
    db.add(Match(usuario_id=u.id, edital_id=ed.id, score=0.9, nivel="forte", abertura_avisada=False))
    db.commit()

    enviados = lembretes.verificar_aberturas(db)

    assert enviados == 1


def test_verificar_prazos_usa_fuso_de_brasilia(monkeypatch):
    _mockar_agora_noite(monkeypatch)
    monkeypatch.setattr(lembretes, "notificar_usuario_lote", MagicMock(return_value=True))

    db = _sessao()
    u = _usuario(db)
    # encerra HOJE (15/01) em Brasília, às 23h59 -- ainda não passou
    ed = Edital(fonte="PNCP", id_externo="ed1", orgao="Orgao", objeto="Aquisicao", uf="SP",
               data_encerramento=datetime(2027, 1, 15, 23, 59))
    db.add(ed)
    db.commit()
    db.add(Match(usuario_id=u.id, edital_id=ed.id, score=0.9, nivel="forte", prazo_avisado=False))
    db.commit()

    enviados = lembretes.verificar_prazos(db)

    assert enviados == 1


def test_verificar_documentos_usa_fuso_de_brasilia(monkeypatch):
    _mockar_agora_noite(monkeypatch)
    monkeypatch.setattr(lembretes, "notificar_usuario_lote", MagicMock(return_value=True))

    db = _sessao()
    u = _usuario(db)
    # vence HOJE (15/01) em Brasília -- date.today() (UTC) já acharia que é
    # 16/01, e (data_validade - hoje).days ficaria -1 ("já vencido" errado)
    doc = Documento(usuario_id=u.id, nome="CND Federal", data_validade=date(2027, 1, 15), ativo=True)
    db.add(doc)
    db.commit()

    enviados = lembretes.verificar_documentos(db)

    assert enviados == 1
