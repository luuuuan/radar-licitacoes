"""
Testes de telegram_menu.py com 2 contatos de Telegram por usuário. Achado
real: o 1º contato a tocar num botão do menu marcava o item como visto pra
CONTA inteira (flags globais no Match/Documento) -- o 2º contato tocando no
mesmo botão depois via a lista vazia, mesmo nunca tendo recebido nada.
Agora cada flag existe em par (slot 1 / slot 2). Rode com: cd backend && pytest
"""
from datetime import date, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import telegram_menu as tm
from app.models import Base, Usuario, Edital, Match, Documento


def _sessao():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def _usuario_2_contatos(db):
    u = Usuario(nome="Teste", email="t@t.com", senha_hash="x",
               notif_telegram=True, telegram_chat_id="111", telegram_chat_id_2="222")
    db.add(u)
    db.commit()
    return u


def _edital_forte(db, usuario):
    ed = Edital(fonte="PNCP", id_externo="ed1", orgao="Orgao Teste", objeto="Aquisicao", uf="SP")
    db.add(ed)
    db.commit()
    m = Match(usuario_id=usuario.id, edital_id=ed.id, score=0.9, nivel="forte")
    db.add(m)
    db.commit()
    return m, ed


def _mock_envio_sempre_ok(monkeypatch):
    monkeypatch.setattr(tm._tg, "enviar_para_chat", lambda *a, **kw: True)
    monkeypatch.setattr(tm._tg, "enviar_menu", lambda *a, **kw: True)


def test_slot_do_chat_resolve_1_e_2():
    u = Usuario(telegram_chat_id="111", telegram_chat_id_2="222")
    assert tm._slot_do_chat(u, "111") == 1
    assert tm._slot_do_chat(u, "222") == 2


def test_segundo_contato_ve_o_item_mesmo_apos_primeiro_ja_ter_visto(monkeypatch):
    """O bug relatado: contato 1 toca no botão "Alta compatibilidade" e
    recebe o item; contato 2 toca no MESMO botão depois -- antes da correção
    isso devolvia lista vazia (o Match já estava "notificado"), agora tem
    que continuar recebendo, porque ELE nunca viu."""
    db = _sessao()
    u = _usuario_2_contatos(db)
    m, ed = _edital_forte(db, u)
    _mock_envio_sempre_ok(monkeypatch)

    enviados_1 = tm.mostrar_categoria(db, u, "forte", "111")
    assert enviados_1 == 1
    assert m.notificado is True
    assert m.notificado_2 is False   # contato 2 ainda não viu

    enviados_2 = tm.mostrar_categoria(db, u, "forte", "222")
    assert enviados_2 == 1   # achado real: isso dava 0 antes da correção
    assert m.notificado_2 is True


def test_mostrar_categoria_de_novo_pro_mesmo_contato_nao_reenvia():
    db = _sessao()
    u = _usuario_2_contatos(db)
    m, ed = _edital_forte(db, u)
    m.notificado = True   # já visto pelo contato 1
    db.commit()

    enviados = tm.mostrar_categoria(db, u, "forte", "111")
    assert enviados == 0


def test_marcar_visto_so_afeta_o_slot_certo():
    db = _sessao()
    u = _usuario_2_contatos(db)
    m, ed = _edital_forte(db, u)

    tm._marcar_visto("forte", m, slot=2)
    assert m.notificado is False
    assert m.notificado_2 is True


def test_contar_pendentes_e_independente_por_slot():
    db = _sessao()
    u = _usuario_2_contatos(db)
    m, ed = _edital_forte(db, u)
    m.notificado = True   # só o contato 1 já viu esse

    c1 = tm.contar_pendentes(db, u, slot=1)
    c2 = tm.contar_pendentes(db, u, slot=2)
    assert c1["forte"] == 0
    assert c2["forte"] == 1


def test_enviar_resumo_nao_manda_pro_contato_que_ja_viu_tudo(monkeypatch):
    db = _sessao()
    u = _usuario_2_contatos(db)
    m, ed = _edital_forte(db, u)
    m.notificado = True   # contato 1 já viu; contato 2 ainda não

    chamadas = []
    monkeypatch.setattr(tm._tg, "enviar_menu",
                        lambda chat_id, *a, **kw: chamadas.append(chat_id) or True)

    resultado = tm.enviar_resumo(db, u)
    assert resultado is True
    assert chamadas == ["222"]   # só o contato 2 recebeu o resumo


def test_pendentes_documento_respeita_slot():
    db = _sessao()
    u = _usuario_2_contatos(db)
    validade = date.today() + timedelta(days=5)
    doc = Documento(usuario_id=u.id, nome="Certidão", data_validade=validade, ativo=True,
                    avisado_para_telegram=validade)   # contato 1 já avisado
    db.add(doc)
    db.commit()

    assert tm._pendentes_documento(db, u, slot=1) == []
    assert tm._pendentes_documento(db, u, slot=2) == [doc]
