"""
service.podar_editais_orfaos() -- remove itens_edital + editais que nunca
tiveram nenhum engajamento de usuário (Match/Proposta/Análise por IA) e já
encerraram DE VERDADE (data_encerramento no passado -- fim do recebimento
de propostas, não só o início da janela). Achado real: itens_edital sozinha
passou de 400MB no plano free do banco (teto de 500MB), quase todo esse
espaço eram editais que nunca bateram com catálogo nenhum, coletados 2x/dia
sem nenhuma limpeza.

Achado real #2: a poda usava data_abertura (início da janela de propostas)
em vez de data_encerramento (fim) -- um edital com data_abertura no passado
pode ainda estar com a janela de propostas aberta (data_encerramento no
futuro), e apagá-lo destruiria uma oportunidade ainda válida. Corrigido pra
usar data_encerramento; sem essa data cadastrada, não arrisca apagar.

Banco sqlite em memória, sem HTTP. Rode com:  cd backend && pytest
"""
import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import service
from app.models import (
    Base, Usuario, Edital, ItemEdital, Match, Proposta, AnaliseIAExtras,
)


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


def _edital_com_itens(db, id_externo, data_encerramento, n_itens=2, data_abertura=None):
    ed = Edital(fonte="PNCP", id_externo=id_externo, orgao="Orgao Teste",
               objeto="Aquisicao", uf="SP", data_abertura=data_abertura,
               data_encerramento=data_encerramento)
    db.add(ed)
    db.commit()
    for numero in range(1, n_itens + 1):
        db.add(ItemEdital(edital_id=ed.id, numero=numero, descricao=f"Item {numero}"))
    db.commit()
    return ed


ONTEM = datetime.date.today() - datetime.timedelta(days=1)
AMANHA = datetime.date.today() + datetime.timedelta(days=1)


def test_remove_edital_encerrado_sem_nenhum_engajamento():
    db = _sessao()
    ed = _edital_com_itens(db, "ed1", ONTEM)

    ed_id = ed.id   # captura antes -- depois do DELETE em massa abaixo, o
                    # próprio SQLAlchemy já tira "ed" do identity map sozinho
    r = service.podar_editais_orfaos(db)

    assert r == {"editais_removidos": 1, "itens_removidos": 2}
    assert db.get(Edital, ed_id) is None
    assert db.query(ItemEdital).filter(ItemEdital.edital_id == ed_id).count() == 0


def test_mantem_edital_ainda_ativo_mesmo_sem_engajamento():
    db = _sessao()
    _edital_com_itens(db, "ed1", AMANHA)

    r = service.podar_editais_orfaos(db)

    assert r == {"editais_removidos": 0, "itens_removidos": 0}
    assert db.query(Edital).count() == 1


def test_mantem_edital_sem_data_encerramento():
    """Sem data_encerramento não dá pra saber se já encerrou -- não arrisca
    apagar, mesmo com data_abertura no passado."""
    db = _sessao()
    _edital_com_itens(db, "ed1", None)

    r = service.podar_editais_orfaos(db)

    assert r == {"editais_removidos": 0, "itens_removidos": 0}
    assert db.query(Edital).count() == 1


def test_mantem_edital_com_janela_de_propostas_ainda_aberta():
    """Achado real: data_abertura no passado não significa que encerrou --
    é só o INÍCIO da janela. Um edital com data_abertura ontem mas
    data_encerramento amanhã ainda está aceitando propostas agora."""
    db = _sessao()
    _edital_com_itens(db, "ed1", AMANHA, data_abertura=ONTEM)

    r = service.podar_editais_orfaos(db)

    assert r == {"editais_removidos": 0, "itens_removidos": 0}
    assert db.query(Edital).count() == 1


def test_mantem_edital_encerrado_com_match_mesmo_fraco():
    db = _sessao()
    u = _usuario(db)
    ed = _edital_com_itens(db, "ed1", ONTEM)
    db.add(Match(usuario_id=u.id, edital_id=ed.id, score=0.1, nivel="fraco"))
    db.commit()

    r = service.podar_editais_orfaos(db)

    assert r == {"editais_removidos": 0, "itens_removidos": 0}
    assert db.get(Edital, ed.id) is not None


def test_mantem_edital_encerrado_com_proposta_mas_sem_match():
    """Achado do desenho: dá pra interagir com um edital (cotação/proposta)
    sem nunca ter Match automático -- ver _match_do_usuario_por_edital.
    Confirmar isso ainda é sinal de engajamento real, não pode apagar."""
    db = _sessao()
    ed = _edital_com_itens(db, "ed1", ONTEM)
    db.add(Proposta(edital_id=ed.id, usuario_id=1))
    db.commit()

    r = service.podar_editais_orfaos(db)

    assert r == {"editais_removidos": 0, "itens_removidos": 0}
    assert db.get(Edital, ed.id) is not None


def test_mantem_edital_encerrado_com_analise_ia_mas_sem_match():
    db = _sessao()
    u = _usuario(db)
    ed = _edital_com_itens(db, "ed1", ONTEM)
    db.add(AnaliseIAExtras(usuario_id=u.id, edital_id=ed.id))
    db.commit()

    r = service.podar_editais_orfaos(db)

    assert r == {"editais_removidos": 0, "itens_removidos": 0}
    assert db.get(Edital, ed.id) is not None


def test_remove_varios_e_mantem_os_que_tem_engajamento():
    db = _sessao()
    u = _usuario(db)
    orfao1 = _edital_com_itens(db, "orfao1", ONTEM)
    orfao2 = _edital_com_itens(db, "orfao2", ONTEM)
    com_match = _edital_com_itens(db, "com-match", ONTEM)
    db.add(Match(usuario_id=u.id, edital_id=com_match.id, score=0.8, nivel="forte"))
    ainda_ativo = _edital_com_itens(db, "ativo", AMANHA)
    db.commit()

    r = service.podar_editais_orfaos(db)

    assert r == {"editais_removidos": 2, "itens_removidos": 4}
    restantes = {e.id_externo for e in db.query(Edital).all()}
    assert restantes == {"com-match", "ativo"}


def test_processa_em_lotes_sem_perder_nenhum(monkeypatch):
    """Lote pequeno de propósito, pra forçar mais de uma rodada de DELETE e
    confirmar que nenhum edital elegível fica de fora por causa disso."""
    monkeypatch.setattr(service, "_LOTE_PODA_EDITAIS", 2)
    db = _sessao()
    for i in range(5):
        _edital_com_itens(db, f"orfao{i}", ONTEM, n_itens=1)

    r = service.podar_editais_orfaos(db)

    assert r == {"editais_removidos": 5, "itens_removidos": 5}
    assert db.query(Edital).count() == 0
