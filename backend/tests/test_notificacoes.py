"""
Pedido do usuário: central de notificações no app, informando (1) editais
que o usuário marcou "Vou participar" com prazo de propostas fechando, (2)
documentos de habilitação vencendo, (3) análises por IA que concluíram
depois que ele saiu da tela do edital -- todas clicáveis, redirecionando
pro lugar certo. Tudo computado ao vivo (GET /api/notificacoes), sem
tabela de eventos nova. Rode com:  cd backend && pytest
"""
from datetime import date, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.main import notificacoes, STATUS_PARTICIPACAO
from app.models import Base, Usuario, Edital, Match, Documento


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


def _edital(db, id_externo="ed1", data_encerramento=None, analise_em=None):
    ed = Edital(fonte="PNCP", id_externo=id_externo, orgao="Orgao Teste", objeto="Aquisicao", uf="SP",
               data_encerramento=data_encerramento, analise_em=analise_em)
    db.add(ed)
    db.commit()
    return ed


# --------- 1. prazo fechando + vou participar --------- #

def test_prazo_fechando_aparece_quando_vou_participar_e_dentro_do_limiar():
    db = _sessao()
    u = _usuario(db)
    ed = _edital(db, data_encerramento=date.today() + timedelta(days=1))
    db.add(Match(usuario_id=u.id, edital_id=ed.id, score=0.5, nivel="medio", status=STATUS_PARTICIPACAO))
    db.commit()

    r = notificacoes(user=u, db=db)

    tipos = [i["tipo"] for i in r["itens"]]
    assert "prazo" in tipos
    item = next(i for i in r["itens"] if i["tipo"] == "prazo")
    assert item["edital_id"] == ed.id
    assert item["aba"] == "proposta"


def test_prazo_fechando_nao_aparece_sem_vou_participar():
    db = _sessao()
    u = _usuario(db)
    ed = _edital(db, data_encerramento=date.today() + timedelta(days=1))
    db.add(Match(usuario_id=u.id, edital_id=ed.id, score=0.5, nivel="medio", status="novo"))
    db.commit()

    r = notificacoes(user=u, db=db)

    assert not any(i["tipo"] == "prazo" for i in r["itens"])


def test_prazo_fechando_nao_aparece_fora_do_limiar():
    db = _sessao()
    u = _usuario(db)
    ed = _edital(db, data_encerramento=date.today() + timedelta(days=settings.LEMBRETE_PRAZO_DIAS + 5))
    db.add(Match(usuario_id=u.id, edital_id=ed.id, score=0.5, nivel="medio", status=STATUS_PARTICIPACAO))
    db.commit()

    r = notificacoes(user=u, db=db)

    assert not any(i["tipo"] == "prazo" for i in r["itens"])


def test_prazo_aparece_quando_interessante_mesmo_sem_vou_participar():
    """Pedido do usuário: mesmo critério do lembrete por e-mail/Telegram
    (interessante OU forte) -- não só quem marcou "Vou participar"."""
    db = _sessao()
    u = _usuario(db)
    ed = _edital(db, data_encerramento=date.today() + timedelta(days=1))
    db.add(Match(usuario_id=u.id, edital_id=ed.id, score=0.5, nivel="medio",
                status="novo", interessante=True))
    db.commit()

    r = notificacoes(user=u, db=db)

    assert any(i["tipo"] == "prazo" and i["edital_id"] == ed.id for i in r["itens"])


def test_prazo_aparece_quando_nivel_forte_mesmo_sem_vou_participar():
    db = _sessao()
    u = _usuario(db)
    ed = _edital(db, data_encerramento=date.today() + timedelta(days=1))
    db.add(Match(usuario_id=u.id, edital_id=ed.id, score=0.9, nivel="forte", status="novo"))
    db.commit()

    r = notificacoes(user=u, db=db)

    assert any(i["tipo"] == "prazo" and i["edital_id"] == ed.id for i in r["itens"])


def test_prazo_ja_encerrado_nao_aparece():
    db = _sessao()
    u = _usuario(db)
    ed = _edital(db, data_encerramento=date.today() - timedelta(days=1))
    db.add(Match(usuario_id=u.id, edital_id=ed.id, score=0.5, nivel="medio", status=STATUS_PARTICIPACAO))
    db.commit()

    r = notificacoes(user=u, db=db)

    assert not any(i["tipo"] == "prazo" for i in r["itens"])


# --------- 2. editais de alta compatibilidade abrindo em breve --------- #
# Mesmo critério do lembrete por e-mail (lembretes.verificar_aberturas):
# nível forte, dentro da janela de Usuario.dias_antecedencia (default 2),
# só se Usuario.avisar_abertura. Pedido do usuário: clicar não abre UM
# edital específico -- abre a lista de Editais filtrada por nível forte.

def test_abertura_aparece_quando_forte_e_dentro_da_janela():
    db = _sessao()
    u = _usuario(db)  # avisar_abertura=True, dias_antecedencia=2 por padrão
    ed = _edital(db, data_encerramento=None)
    ed.data_abertura = date.today() + timedelta(days=1)
    db.add(Match(usuario_id=u.id, edital_id=ed.id, score=0.9, nivel="forte"))
    db.commit()

    r = notificacoes(user=u, db=db)

    item = next(i for i in r["itens"] if i["tipo"] == "abertura")
    assert item["filtro"] == {"nivel": "forte"}


def test_abertura_nao_aparece_com_nivel_medio():
    db = _sessao()
    u = _usuario(db)
    ed = _edital(db)
    ed.data_abertura = date.today() + timedelta(days=1)
    db.add(Match(usuario_id=u.id, edital_id=ed.id, score=0.5, nivel="medio"))
    db.commit()

    r = notificacoes(user=u, db=db)

    assert not any(i["tipo"] == "abertura" for i in r["itens"])


def test_abertura_nao_aparece_fora_da_janela_de_dias_antecedencia():
    db = _sessao()
    u = _usuario(db)
    ed = _edital(db)
    ed.data_abertura = date.today() + timedelta(days=u.dias_antecedencia + 5)
    db.add(Match(usuario_id=u.id, edital_id=ed.id, score=0.9, nivel="forte"))
    db.commit()

    r = notificacoes(user=u, db=db)

    assert not any(i["tipo"] == "abertura" for i in r["itens"])


def test_abertura_nao_aparece_quando_usuario_desligou_aviso():
    db = _sessao()
    u = _usuario(db)
    u.avisar_abertura = False
    ed = _edital(db)
    ed.data_abertura = date.today() + timedelta(days=1)
    db.add(Match(usuario_id=u.id, edital_id=ed.id, score=0.9, nivel="forte"))
    db.commit()

    r = notificacoes(user=u, db=db)

    assert not any(i["tipo"] == "abertura" for i in r["itens"])


def test_abertura_agrupa_varios_editais_num_unico_item():
    db = _sessao()
    u = _usuario(db)
    ed1 = _edital(db, id_externo="ed1")
    ed1.data_abertura = date.today()
    ed2 = _edital(db, id_externo="ed2")
    ed2.data_abertura = date.today() + timedelta(days=1)
    db.add(Match(usuario_id=u.id, edital_id=ed1.id, score=0.9, nivel="forte"))
    db.add(Match(usuario_id=u.id, edital_id=ed2.id, score=0.9, nivel="forte"))
    db.commit()

    r = notificacoes(user=u, db=db)

    itens_abertura = [i for i in r["itens"] if i["tipo"] == "abertura"]
    assert len(itens_abertura) == 1
    assert "2 editais" in itens_abertura[0]["detalhe"]


# --------- 3. documentos vencendo --------- #

def test_documento_vencendo_aparece_dentro_do_limiar():
    db = _sessao()
    u = _usuario(db)
    db.add(Documento(usuario_id=u.id, nome="CND Federal",
                     data_validade=date.today() + timedelta(days=1), ativo=True))
    db.commit()

    r = notificacoes(user=u, db=db)

    item = next(i for i in r["itens"] if i["tipo"] == "documento")
    assert item["nome"] == "CND Federal"


def test_documento_inativo_nao_aparece():
    db = _sessao()
    u = _usuario(db)
    db.add(Documento(usuario_id=u.id, nome="CND Federal",
                     data_validade=date.today() + timedelta(days=1), ativo=False))
    db.commit()

    r = notificacoes(user=u, db=db)

    assert not any(i["tipo"] == "documento" for i in r["itens"])


def test_documento_sem_data_validade_nao_aparece():
    db = _sessao()
    u = _usuario(db)
    db.add(Documento(usuario_id=u.id, nome="Contrato Social", data_validade=None, ativo=True))
    db.commit()

    r = notificacoes(user=u, db=db)

    assert not any(i["tipo"] == "documento" for i in r["itens"])


def test_documento_ja_vencido_tambem_aparece():
    """Um documento vencido é mais urgente que um vencendo em breve, não
    menos -- continua aparecendo (não só "vence em breve")."""
    db = _sessao()
    u = _usuario(db)
    db.add(Documento(usuario_id=u.id, nome="CND Federal",
                     data_validade=date.today() - timedelta(days=1), ativo=True))
    db.commit()

    r = notificacoes(user=u, db=db)

    item = next(i for i in r["itens"] if i["tipo"] == "documento")
    assert "vencido" in item["detalhe"]


# --------- 4. análise concluída fora da tela --------- #

def test_analise_concluida_depois_da_ultima_visita_aparece():
    db = _sessao()
    u = _usuario(db)
    ed = _edital(db, analise_em=datetime.utcnow())
    db.add(Match(usuario_id=u.id, edital_id=ed.id, score=0.5, nivel="medio",
                interagido_em=datetime.utcnow() - timedelta(hours=2)))
    db.commit()

    r = notificacoes(user=u, db=db)

    item = next(i for i in r["itens"] if i["tipo"] == "analise")
    assert item["edital_id"] == ed.id
    assert item["aba"] == "analise"


def test_analise_concluida_antes_da_ultima_visita_a_aba_analise_nao_aparece():
    """Usuário já viu o resultado (reabriu a aba Análise depois que a
    análise terminou) -- ver a aba É a dispensa, não precisa de estado à
    parte."""
    db = _sessao()
    u = _usuario(db)
    ed = _edital(db, analise_em=datetime.utcnow() - timedelta(hours=2))
    db.add(Match(usuario_id=u.id, edital_id=ed.id, score=0.5, nivel="medio",
                interagido_em=datetime.utcnow(), analise_vista_em=datetime.utcnow()))
    db.commit()

    r = notificacoes(user=u, db=db)

    assert not any(i["tipo"] == "analise" for i in r["itens"])


def test_analise_concluida_continua_aparecendo_apos_visitar_outra_aba():
    """Achado real (usuário reportou não ter sido notificado): reabrir o
    edital por outro motivo qualquer (itens, cotação, documentos) atualiza
    interagido_em mas NÃO analise_vista_em -- a notificação não pode
    sumir só por causa disso, senão o usuário nunca chega a vê-la."""
    db = _sessao()
    u = _usuario(db)
    ed = _edital(db, analise_em=datetime.utcnow() - timedelta(hours=1))
    db.add(Match(usuario_id=u.id, edital_id=ed.id, score=0.5, nivel="medio",
                interagido_em=datetime.utcnow(),   # visitou de novo (outra aba)...
                analise_vista_em=None))            # ...mas nunca abriu a aba Análise
    db.commit()

    r = notificacoes(user=u, db=db)

    item = next(i for i in r["itens"] if i["tipo"] == "analise")
    assert item["edital_id"] == ed.id


def test_analise_sem_visita_nenhuma_nao_aparece():
    """Achado real (auditoria dos agentes architect-reviewer/error-detective):
    analise_ia é cache POR EDITAL (não por usuário) -- sem exigir que o
    usuário já tenha visitado esse edital alguma vez, um usuário que NUNCA
    abriu o edital podia ganhar uma notificação sobre uma análise que
    outro usuário pediu, sem nunca sumir sozinha (não tinha motivo pra
    abrir algo que nunca visitou -- vira um fantasma permanente)."""
    db = _sessao()
    u = _usuario(db)
    ed = _edital(db, analise_em=datetime.utcnow())
    db.add(Match(usuario_id=u.id, edital_id=ed.id, score=0.5, nivel="medio", interagido_em=None))
    db.commit()

    r = notificacoes(user=u, db=db)

    assert not any(i["tipo"] == "analise" and i["edital_id"] == ed.id for i in r["itens"])


def test_edital_sem_analise_nenhuma_nao_aparece():
    db = _sessao()
    u = _usuario(db)
    ed = _edital(db, analise_em=None)
    db.add(Match(usuario_id=u.id, edital_id=ed.id, score=0.5, nivel="medio"))
    db.commit()

    r = notificacoes(user=u, db=db)

    assert not any(i["tipo"] == "analise" for i in r["itens"])


# --------- não vaza entre usuários; total bate com a lista --------- #

def test_notificacoes_e_por_usuario():
    db = _sessao()
    u1 = _usuario(db)
    u2 = Usuario(nome="Outro", email="outro@t.com", senha_hash="x")
    db.add(u2)
    db.commit()
    ed = _edital(db, data_encerramento=date.today() + timedelta(days=1))
    db.add(Match(usuario_id=u1.id, edital_id=ed.id, score=0.5, nivel="medio", status=STATUS_PARTICIPACAO))
    db.commit()

    assert notificacoes(user=u2, db=db)["total"] == 0
    assert notificacoes(user=u1, db=db)["total"] == 1


def test_total_bate_com_tamanho_da_lista():
    db = _sessao()
    u = _usuario(db)
    ed = _edital(db, data_encerramento=date.today() + timedelta(days=1),
                analise_em=datetime.utcnow())
    db.add(Match(usuario_id=u.id, edital_id=ed.id, score=0.5, nivel="medio", status=STATUS_PARTICIPACAO,
                interagido_em=datetime.utcnow() - timedelta(hours=2)))
    db.add(Documento(usuario_id=u.id, nome="CND Federal",
                     data_validade=date.today() + timedelta(days=1), ativo=True))
    db.commit()

    r = notificacoes(user=u, db=db)

    assert r["total"] == len(r["itens"]) == 3


# --------- teto por categoria (achado real do agente performance-engineer) --------- #

def test_documentos_vencendo_respeita_teto_por_categoria():
    from app.main import _LIMITE_ITENS_NOTIFICACAO
    db = _sessao()
    u = _usuario(db)
    for i in range(_LIMITE_ITENS_NOTIFICACAO + 5):
        db.add(Documento(usuario_id=u.id, nome=f"Documento {i}",
                         data_validade=date.today() + timedelta(days=1), ativo=True))
    db.commit()

    r = notificacoes(user=u, db=db)

    itens_doc = [i for i in r["itens"] if i["tipo"] == "documento"]
    assert len(itens_doc) == _LIMITE_ITENS_NOTIFICACAO


# --------- botão "Ler Todos" (pedido do usuário) --------- #

def test_ler_todas_esconde_prazo_abertura_documento_no_mesmo_dia():
    from app.main import ler_todas_notificacoes
    db = _sessao()
    u = _usuario(db)
    ed = _edital(db, data_encerramento=date.today() + timedelta(days=1))
    db.add(Match(usuario_id=u.id, edital_id=ed.id, score=0.5, nivel="medio", status=STATUS_PARTICIPACAO))
    db.add(Documento(usuario_id=u.id, nome="CND Federal",
                     data_validade=date.today() + timedelta(days=1), ativo=True))
    db.commit()
    assert notificacoes(user=u, db=db)["total"] == 2

    ler_todas_notificacoes(user=u, db=db)

    r = notificacoes(user=u, db=db)
    assert r["total"] == 0


def test_ler_todas_volta_no_dia_seguinte_se_a_causa_continuar_valendo():
    """Pedido do usuário: prazo/documento voltam sozinhos no dia seguinte,
    mesmo sem nada ter mudado -- são sinais que pioram dia a dia."""
    from app.main import ler_todas_notificacoes
    db = _sessao()
    u = _usuario(db)
    ed = _edital(db, data_encerramento=date.today() + timedelta(days=1))
    db.add(Match(usuario_id=u.id, edital_id=ed.id, score=0.5, nivel="medio", status=STATUS_PARTICIPACAO))
    db.commit()
    ler_todas_notificacoes(user=u, db=db)
    assert notificacoes(user=u, db=db)["total"] == 0

    u.notificacoes_lidas_em = date.today() - timedelta(days=1)   # simula "ontem"
    db.commit()

    assert notificacoes(user=u, db=db)["total"] == 1


def test_ler_todas_marca_analise_vista_e_nao_volta_sozinha(monkeypatch):
    """Pedido do usuário: análise por IA não deve reaparecer sozinha depois
    de "Ler Todos" -- ao contrário de prazo/documento, usa o mesmo campo
    persistente de sempre (analise_vista_em), não o "resto do dia"."""
    from app.main import ler_todas_notificacoes
    db = _sessao()
    u = _usuario(db)
    ed = _edital(db, analise_em=datetime.utcnow())
    db.add(Match(usuario_id=u.id, edital_id=ed.id, score=0.5, nivel="medio",
                interagido_em=datetime.utcnow() - timedelta(hours=2)))
    db.commit()
    assert notificacoes(user=u, db=db)["total"] == 1

    ler_todas_notificacoes(user=u, db=db)

    assert notificacoes(user=u, db=db)["total"] == 0
    # nem "no dia seguinte" (diferente de prazo/documento) -- ver docstring
    u.notificacoes_lidas_em = date.today() - timedelta(days=1)
    db.commit()
    assert notificacoes(user=u, db=db)["total"] == 0


def test_ler_todas_nao_mexe_no_match_de_outro_usuario():
    from app.main import ler_todas_notificacoes
    db = _sessao()
    u1 = _usuario(db)
    u2 = Usuario(nome="Outro", email="outro2@t.com", senha_hash="x")
    db.add(u2)
    db.commit()
    ed = _edital(db, analise_em=datetime.utcnow())
    db.add(Match(usuario_id=u1.id, edital_id=ed.id, score=0.5, nivel="medio",
                interagido_em=datetime.utcnow() - timedelta(hours=2)))
    db.commit()

    ler_todas_notificacoes(user=u2, db=db)

    assert notificacoes(user=u1, db=db)["total"] == 1   # intocado
    assert u2.notificacoes_lidas_em == date.today()      # só o próprio u2 foi marcado
