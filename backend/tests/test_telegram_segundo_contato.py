"""
Pedido do usuário: poder cadastrar um 2º contato do Telegram (ex.: sócio,
outro responsável) que recebe os MESMOS avisos do 1º, de forma
independente — cada um com sua própria vinculação/código, mas
compartilhando a preferência notif_telegram (é "este usuário quer
Telegram", não um canal à parte por contato). Rode com: cd backend && pytest
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import main as app_main
from app.main import telegram_vinculo, telegram_desvincular, telegram_webhook
from app.models import Base, Usuario
from app import telegram_menu


def _sessao():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def _usuario(db, **kw):
    u = Usuario(nome="Teste", email=kw.pop("email", "t@t.com"), senha_hash="x", **kw)
    db.add(u)
    db.commit()
    return u


class _ReqFake:
    def __init__(self, corpo: dict):
        self._corpo = corpo
    async def json(self):
        return self._corpo


def test_vinculo_slot1_e_slot2_geram_codigos_diferentes(monkeypatch):
    monkeypatch.setattr(app_main.settings, "TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setattr(app_main.settings, "TELEGRAM_BOT_USERNAME", "RadarBot")
    db = _sessao()
    u = _usuario(db)

    v1 = telegram_vinculo(slot=1, user=u, db=db)
    v2 = telegram_vinculo(slot=2, user=u, db=db)

    assert v1["codigo"] and v2["codigo"]
    assert v1["codigo"] != v2["codigo"]
    assert v1["conectado"] is False and v2["conectado"] is False
    assert u.telegram_codigo == v1["codigo"]
    assert u.telegram_codigo_2 == v2["codigo"]


def test_vinculo_slot_invalido_da_400():
    db = _sessao()
    u = _usuario(db)
    with pytest.raises(Exception) as exc:
        telegram_vinculo(slot=3, user=u, db=db)
    assert "400" in str(exc.value) or "inválido" in str(exc.value).lower()


def test_desvincular_slot2_nao_mexe_no_slot1():
    db = _sessao()
    u = _usuario(db, telegram_chat_id="chat1", telegram_chat_id_2="chat2")

    telegram_desvincular(slot=2, user=u, db=db)

    assert u.telegram_chat_id == "chat1"   # slot 1 intacto
    assert u.telegram_chat_id_2 is None    # só o slot 2 foi limpo


def test_webhook_start_com_codigo_do_slot2_vincula_chat_id_2(monkeypatch):
    monkeypatch.setattr(app_main.settings, "TELEGRAM_WEBHOOK_SECRET", "segredo")
    with patch("app.notifications.telegram.enviar_para_chat"):
        db = _sessao()
        u = _usuario(db, telegram_codigo="cod-principal", telegram_codigo_2="cod-socio")

        req = _ReqFake({"message": {"text": "/start cod-socio", "chat": {"id": 999}}})
        asyncio.run(telegram_webhook("segredo", req, db))

        db.refresh(u)
        assert u.telegram_chat_id_2 == "999"
        assert u.telegram_chat_id is None   # slot 1 não foi tocado
        assert u.notif_telegram is True


def test_webhook_callback_resolve_usuario_pelo_chat_id_2_e_responde_pra_ele(monkeypatch):
    monkeypatch.setattr(app_main.settings, "TELEGRAM_WEBHOOK_SECRET", "segredo")
    db = _sessao()
    u = _usuario(db, telegram_chat_id="chat-principal", telegram_chat_id_2="chat-socio",
                notif_telegram=True)

    chamadas = []
    with patch("app.telegram_menu.mostrar_categoria", side_effect=lambda *a: chamadas.append(a) or 0) as mock_mostrar, \
         patch("app.telegram_menu.enviar_resumo", return_value=False), \
         patch("app.notifications.telegram.responder_callback"):
        req = _ReqFake({"callback_query": {
            "id": "cbid", "data": "radar:forte",
            "message": {"chat": {"id": "chat-socio"}},
        }})
        asyncio.run(telegram_webhook("segredo", req, db))

    assert mock_mostrar.called
    args = chamadas[0]
    assert args[1].id == u.id            # achou o usuário certo (pelo chat_id_2)
    assert args[2] == "forte"
    assert args[3] == "chat-socio"       # respondeu pro chat que clicou, não pro "principal"


def test_enviar_resumo_manda_pros_dois_contatos_configurados(monkeypatch):
    db = _sessao()
    u = _usuario(db, telegram_chat_id="chat1", telegram_chat_id_2="chat2", notif_telegram=True)

    monkeypatch.setattr(telegram_menu, "contar_pendentes", lambda db, usuario: {"forte": 2})
    chamadas = []
    with patch("app.telegram_menu._tg.enviar_menu",
              side_effect=lambda chat_id, *a, **k: chamadas.append(chat_id) or True):
        ok = telegram_menu.enviar_resumo(db, u)

    assert ok is True
    assert set(chamadas) == {"chat1", "chat2"}


def test_enviar_resumo_sem_nenhum_contato_nao_manda_nada():
    db = _sessao()
    u = _usuario(db, notif_telegram=True)
    assert telegram_menu.enviar_resumo(db, u) is False


def test_mostrar_categoria_manda_pro_chat_id_informado_nao_pro_fixo():
    db = _sessao()
    u = _usuario(db, telegram_chat_id="chat1", telegram_chat_id_2="chat2")
    from app.models import Match, Edital
    ed = Edital(fonte="PNCP", id_externo="e1", orgao="Orgao", objeto="Obj", uf="SP")
    db.add(ed)
    db.commit()
    m = Match(usuario_id=u.id, edital_id=ed.id, nivel="forte", score=1.0, notificado=False)
    db.add(m)
    db.commit()

    chamadas = []
    with patch("app.telegram_menu._tg.enviar_para_chat",
              side_effect=lambda chat_id, *a, **k: chamadas.append(chat_id) or True):
        n = telegram_menu.mostrar_categoria(db, u, "forte", "chat2")

    assert n == 1
    assert chamadas == ["chat2"]   # foi pro chat que "clicou" (chat2), não pro chat1
