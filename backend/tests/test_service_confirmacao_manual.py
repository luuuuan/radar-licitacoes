"""
Testes da preservação de confirmação manual de item através do recálculo
(banco sqlite em memória, sem HTTP). Rode com:  cd backend && pytest
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.matching import engine as engine_mod
from app.models import Base, Usuario, Edital, ItemEdital, Produto, Match
from app.service import _gerar_matches_usuario, _mesclar_confirmacoes_manuais, _match_engajado


def _mockar_reranker_acha_marcador(monkeypatch):
    """O motor real (`certo`, "Marcador de texto amarelo") precisa achar o
    item por conta própria nesses testes de integração — sem isso, sem
    DEEPINFRA_API_KEY (zerada por padrão em todo teste, ver conftest.py) o
    motor não acha sinal nenhum e o edital nem vira Match."""
    monkeypatch.setattr(settings, "DEEPINFRA_API_KEY", "fake-key")
    def _fake_rerank(query, documentos, timeout=30, api_key=None, tentativas=2, modelo=None):
        return [0.95 if "marcador" in d.lower() else 0.0 for d in documentos]
    monkeypatch.setattr(engine_mod, "_rerank", _fake_rerank)


def _sessao():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def _semear(db):
    """Catálogo com 2 produtos: `certo` casa por texto com o item (é o que o
    motor escolheria sozinho); `outro` é um produto qualquer, existente no
    catálogo, mas que o motor não escolheria pra este item — simula o
    usuário confirmando manualmente um produto DIFERENTE do palpite do
    motor (caso real: NCM/score bateu, mas o item físico era outro)."""
    u = Usuario(nome="Teste", email="t@t.com", senha_hash="x")
    db.add(u)
    db.commit()
    certo = Produto(usuario_id=u.id, descricao="Marcador de texto amarelo",
                    palavras_chave="marcador, texto, amarelo")
    outro = Produto(usuario_id=u.id, descricao="Grampeador de mesa metalico",
                    palavras_chave="grampeador, mesa, metalico")
    db.add_all([certo, outro])
    ed = Edital(fonte="PNCP", id_externo="ed1", objeto="Aquisicao de material de escritorio",
               orgao="Orgao Teste", uf="SP")
    db.add(ed)
    db.commit()
    db.add(ItemEdital(edital_id=ed.id, numero=1, descricao="Marcador de texto amarelo",
                      quantidade=10, valor_unitario=2.5))
    db.commit()
    return u, ed, certo, outro


# --------- _mesclar_confirmacoes_manuais (função pura) --------- #

def test_mesclar_preserva_item_confirmado_quando_produto_ainda_existe():
    antigo = [{"item": 1, "produto_id": 9, "produto": "Produto X",
              "confianca": "media", "confirmado_manualmente": True}]
    novo = [{"item": 1, "produto_id": 5, "produto": "Produto Y",
            "confianca": "media", "confirmado_manualmente": False}]
    resultado = _mesclar_confirmacoes_manuais(novo, antigo, produtos_validos_ids={9, 5})
    assert resultado[0]["produto_id"] == 9
    assert resultado[0]["confirmado_manualmente"] is True


def test_mesclar_ignora_confirmacao_de_produto_excluido_do_catalogo():
    antigo = [{"item": 1, "produto_id": 9, "produto": "Produto X",
              "confianca": "media", "confirmado_manualmente": True}]
    novo = [{"item": 1, "produto_id": 5, "produto": "Produto Y",
            "confianca": "media", "confirmado_manualmente": False}]
    # produto 9 não está mais em produtos_validos_ids -> cai pro resultado fresco
    resultado = _mesclar_confirmacoes_manuais(novo, antigo, produtos_validos_ids={5})
    assert resultado[0]["produto_id"] == 5
    assert resultado[0]["confirmado_manualmente"] is False


def test_mesclar_preserva_confirmacao_de_nenhum_produto():
    antigo = [{"item": 1, "produto_id": None, "produto": None,
              "confianca": "media", "confirmado_manualmente": True}]
    novo = [{"item": 1, "produto_id": 5, "produto": "Produto Y",
            "confianca": "media", "confirmado_manualmente": False}]
    resultado = _mesclar_confirmacoes_manuais(novo, antigo, produtos_validos_ids={5})
    assert resultado[0]["produto_id"] is None
    assert resultado[0]["confirmado_manualmente"] is True


def test_mesclar_nao_mexe_em_item_nao_confirmado():
    antigo = [{"item": 1, "produto_id": 9, "confianca": "media", "confirmado_manualmente": False}]
    novo = [{"item": 1, "produto_id": 5, "confianca": "alta", "confirmado_manualmente": False}]
    resultado = _mesclar_confirmacoes_manuais(novo, antigo, produtos_validos_ids={5, 9})
    assert resultado == novo


def test_mesclar_sem_detalhe_antigo_retorna_novo_intacto():
    novo = [{"item": 1, "produto_id": 5, "confianca": "alta", "confirmado_manualmente": False}]
    assert _mesclar_confirmacoes_manuais(novo, None, produtos_validos_ids={5}) == novo


# --------- integração via _gerar_matches_usuario --------- #

def test_recalculo_preserva_confirmacao_manual_mesmo_quando_motor_prefere_outro(monkeypatch):
    """Usuário confirmou manualmente um produto DIFERENTE do que o motor
    escolheria por conta própria (o `certo`, que casa por texto) — o
    recálculo não pode sobrescrever essa escolha silenciosamente."""
    _mockar_reranker_acha_marcador(monkeypatch)
    db = _sessao()
    u, ed, certo, outro = _semear(db)
    match = Match(edital_id=ed.id, usuario_id=u.id, score=0.5, nivel="medio",
                  detalhe={"itens": [{
                      "item": 1, "descricao_item": "Marcador de texto amarelo",
                      "produto_id": outro.id, "produto": outro.descricao,
                      "score_item": 0.25, "motivo": "confirmado manualmente",
                      "confianca": "media", "candidatos": [], "confirmado_manualmente": True,
                  }]})
    db.add(match)
    db.commit()

    _gerar_matches_usuario(db, u, recalcular_todos=True, forcar_usar_ia=True)

    db.refresh(match)
    item = match.detalhe["itens"][0]
    assert item["produto_id"] == outro.id
    assert item["confirmado_manualmente"] is True


def test_recalculo_nao_preserva_confirmacao_de_produto_ja_excluido(monkeypatch):
    _mockar_reranker_acha_marcador(monkeypatch)
    db = _sessao()
    u, ed, certo, outro = _semear(db)
    produto_excluido_id = outro.id + 999   # nunca existiu / foi apagado
    match = Match(edital_id=ed.id, usuario_id=u.id, score=0.5, nivel="medio",
                  detalhe={"itens": [{
                      "item": 1, "descricao_item": "Marcador de texto amarelo",
                      "produto_id": produto_excluido_id, "produto": "Produto apagado",
                      "score_item": 0.25, "motivo": "texto", "confianca": "media",
                      "candidatos": [], "confirmado_manualmente": True,
                  }]})
    db.add(match)
    db.commit()

    _gerar_matches_usuario(db, u, recalcular_todos=True, forcar_usar_ia=True)

    db.refresh(match)
    item = match.detalhe["itens"][0]
    # confirmação não sobrevive (produto não existe mais) -> volta pro
    # palpite fresco do motor, que escolhe `certo` (casa por texto)
    assert item["produto_id"] == certo.id
    assert item["confirmado_manualmente"] is False


# --------- _match_engajado / recálculo não apaga Match engajado que virou "fraco" --------- #

def _usuario_sem_catalogo(db, email="fraco@t.com"):
    """Catálogo vazio -> engine.avaliar() sempre devolve nivel="fraco"
    (ver matching/engine.py), sem depender de reranker/palavra-chave — o
    jeito mais direto de forçar esse cenário de forma determinística."""
    u = Usuario(nome="Teste", email=email, senha_hash="x")
    db.add(u)
    ed = Edital(fonte="PNCP", id_externo="ed-fraco", objeto="Servico de manutencao predial",
               orgao="Orgao Teste", uf="SP")
    db.add(ed)
    db.commit()
    db.add(ItemEdital(edital_id=ed.id, numero=1, descricao="Parafuso sextavado inox",
                      quantidade=10, valor_unitario=1.0))
    db.commit()
    return u, ed


def test_match_engajado_true_para_status_diferente_de_novo():
    m = Match(status="vou_participar", lido=False, interessante=False)
    assert _match_engajado(m) is True


def test_match_engajado_true_para_lido_ou_interessante():
    assert _match_engajado(Match(status="novo", lido=True, interessante=False)) is True
    assert _match_engajado(Match(status="novo", lido=False, interessante=True)) is True


def test_match_engajado_true_para_item_confirmado_manualmente():
    m = Match(status="novo", lido=False, interessante=False,
             detalhe={"itens": [{"item": 1, "confirmado_manualmente": True}]})
    assert _match_engajado(m) is True


def test_match_engajado_false_quando_nada_disso():
    m = Match(status="novo", lido=False, interessante=False, detalhe=None)
    assert _match_engajado(m) is False


def test_recalculo_preserva_match_engajado_mesmo_virando_fraco(monkeypatch):
    """Achado real: liberar os botões de status/lido/interessante pra editais
    sem Match automático (ver test_marcar_status_sem_match.py) cria Matches
    "vazios" (score=0, nivel="medio") só porque o usuário marcou algo — sem
    esta guarda, o PRÓXIMO recálculo recalcularia nivel="fraco" (catálogo
    vazio) e apagaria a linha na hora, perdendo o status que o usuário
    acabou de definir."""
    db = _sessao()
    u, ed = _usuario_sem_catalogo(db)
    match = Match(edital_id=ed.id, usuario_id=u.id, score=0.0, nivel="medio",
                  status="vou_participar")
    db.add(match)
    db.commit()

    _gerar_matches_usuario(db, u, recalcular_todos=True, forcar_usar_ia=False)

    ainda_existe = db.query(Match).filter(Match.edital_id == ed.id, Match.usuario_id == u.id).first()
    assert ainda_existe is not None
    assert ainda_existe.status == "vou_participar"
    assert ainda_existe.nivel == "fraco"   # score/nivel atualiza pra refletir a realidade


def test_recalculo_continua_apagando_match_fraco_nao_engajado(monkeypatch):
    """Comportamento de antes preservado: Match "fraco" que o usuário nunca
    tocou (status "novo", não lido, não interessante, nada confirmado)
    continua sendo removido, como sempre foi."""
    db = _sessao()
    u, ed = _usuario_sem_catalogo(db, email="fraco2@t.com")
    match = Match(edital_id=ed.id, usuario_id=u.id, score=0.5, nivel="medio")
    db.add(match)
    db.commit()

    _gerar_matches_usuario(db, u, recalcular_todos=True, forcar_usar_ia=False)

    ainda_existe = db.query(Match).filter(Match.edital_id == ed.id, Match.usuario_id == u.id).first()
    assert ainda_existe is None
