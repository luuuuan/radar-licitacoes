"""
Achados do agente debugger em app/telegram_menu.py:

1. mostrar_categoria() marcava um match/documento como "já avisado" mesmo
   quando o envio pro Telegram falhava (rate limit, rede, usuário bloqueou
   o bot) -- sem retry nenhum, esse aviso nunca mais era oferecido, mesmo
   nunca tendo sido entregue de verdade.
2. O ramo de "documento" montava o corpo da mensagem em HTML (Telegram
   parse_mode="HTML") sem escapar nome/emissor/observação -- texto livre
   digitado pelo usuário com "<"/">"/"&" quebra o parse do Telegram (400),
   diferente do ramo de "match" (formato.telegram_item), que já escapava
   tudo com formato._esc.

Rode com:  cd backend && pytest
"""
from datetime import date, timedelta
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import telegram_menu
from app.models import Base, Usuario, Edital, Match, Documento


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


def test_match_nao_marcado_como_notificado_quando_o_envio_falha():
    db = _sessao()
    u = _usuario(db, telegram_chat_id="chat1")
    ed = Edital(fonte="PNCP", id_externo="e1", orgao="Orgao", objeto="Obj", uf="SP")
    db.add(ed)
    db.commit()
    m = Match(usuario_id=u.id, edital_id=ed.id, nivel="forte", score=1.0, notificado=False)
    db.add(m)
    db.commit()

    with patch("app.telegram_menu._tg.enviar_para_chat", return_value=False):
        n = telegram_menu.mostrar_categoria(db, u, "forte", "chat1")

    assert n == 0
    db.refresh(m)
    assert m.notificado is False   # não pode ter marcado como visto -- nunca foi entregue


def test_match_marcado_como_notificado_quando_o_envio_da_certo():
    db = _sessao()
    u = _usuario(db, telegram_chat_id="chat1")
    ed = Edital(fonte="PNCP", id_externo="e2", orgao="Orgao", objeto="Obj", uf="SP")
    db.add(ed)
    db.commit()
    m = Match(usuario_id=u.id, edital_id=ed.id, nivel="forte", score=1.0, notificado=False)
    db.add(m)
    db.commit()

    with patch("app.telegram_menu._tg.enviar_para_chat", return_value=True):
        n = telegram_menu.mostrar_categoria(db, u, "forte", "chat1")

    assert n == 1
    db.refresh(m)
    assert m.notificado is True


def test_documento_nao_marcado_como_avisado_quando_o_envio_falha():
    db = _sessao()
    u = _usuario(db, telegram_chat_id="chat1")
    validade = date.today() + timedelta(days=3)
    d = Documento(usuario_id=u.id, nome="Certidão X", data_validade=validade, ativo=True)
    db.add(d)
    db.commit()

    with patch("app.telegram_menu._tg.enviar_para_chat", return_value=False):
        n = telegram_menu.mostrar_categoria(db, u, "documento", "chat1")

    assert n == 0
    db.refresh(d)
    assert d.avisado_para_telegram is None


def test_documento_marcado_como_avisado_quando_o_envio_da_certo():
    db = _sessao()
    u = _usuario(db, telegram_chat_id="chat1")
    validade = date.today() + timedelta(days=3)
    d = Documento(usuario_id=u.id, nome="Certidão X", data_validade=validade, ativo=True)
    db.add(d)
    db.commit()

    with patch("app.telegram_menu._tg.enviar_para_chat", return_value=True):
        n = telegram_menu.mostrar_categoria(db, u, "documento", "chat1")

    assert n == 1
    db.refresh(d)
    assert d.avisado_para_telegram == validade


def test_documento_com_caracteres_html_no_nome_e_observacao_vai_escapado():
    """Achado real: esse ramo montava o corpo em HTML sem escapar texto
    livre do usuário -- um "<"/"&" no nome ou na observação quebrava o
    parse_mode="HTML" do Telegram (a mensagem falhava com 400)."""
    db = _sessao()
    u = _usuario(db, telegram_chat_id="chat1")
    validade = date.today() + timedelta(days=3)
    d = Documento(usuario_id=u.id, nome="Alvará <renovação> & Cia",
                  orgao_emissor="Prefeitura & Cartório", observacao="taxa > R$50 & < R$100",
                  data_validade=validade, ativo=True)
    db.add(d)
    db.commit()

    chamadas = []
    with patch("app.telegram_menu._tg.enviar_para_chat",
              side_effect=lambda *a, **k: chamadas.append((a, k)) or True):
        telegram_menu.mostrar_categoria(db, u, "documento", "chat1")

    (args, kwargs) = chamadas[0]
    titulo, corpo = args[1], args[2]
    assert "<renovação>" not in titulo
    assert "&lt;renovação&gt;" in titulo
    assert "Prefeitura &amp; Cartório" in corpo
    assert "&gt; R$50 &amp; &lt; R$100" in corpo
