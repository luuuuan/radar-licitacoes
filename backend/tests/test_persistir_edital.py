"""
Achado real (auditoria do agente code-reviewer): _persistir_edital era
insert-only -- um edital já visto (mesmo fonte+id_externo) nunca era
atualizado. Dois efeitos colaterais reais:
1. Se a 1ª coleta salvasse o edital SEM itens (falha transitória na busca de
   itens), ele ficava sem itens PARA SEMPRE -- toda coleta seguinte via o
   id_externo já existente e desistia sem tentar de novo.
2. Se o PNCP estendesse o prazo (data_encerramento) depois da publicação
   inicial, nosso banco continuava com a data antiga -- podendo até sumir
   da aba de ativos (que filtra por data_encerramento >= hoje).
Rode com:  cd backend && pytest
"""
from datetime import date, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.connectors.base import EditalColetado, ItemColetado
from app.models import Base, Edital
from app.service import _persistir_edital


def _sessao():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def _ec(**over):
    base = dict(fonte="PNCP", id_externo="e1", orgao="Órgão A", objeto="Objeto A",
               uf="SP", data_encerramento=date(2026, 1, 1), itens=[])
    base.update(over)
    return EditalColetado(**base)


def test_cria_edital_novo_e_retorna_o_objeto():
    db = _sessao()
    ed = _persistir_edital(db, _ec())
    assert ed is not None
    assert ed.id_externo == "e1"


def test_edital_ja_existente_retorna_none():
    db = _sessao()
    _persistir_edital(db, _ec())
    db.commit()
    assert _persistir_edital(db, _ec()) is None


def test_edital_ja_existente_tem_data_encerramento_atualizada():
    """O achado principal: prazo estendido no PNCP precisa refletir aqui."""
    db = _sessao()
    _persistir_edital(db, _ec(data_encerramento=date(2026, 1, 1)))
    db.commit()

    _persistir_edital(db, _ec(data_encerramento=date(2026, 3, 15)))
    db.commit()

    ed = db.query(Edital).filter_by(id_externo="e1").one()
    # datetime, não date: Edital.data_encerramento agora guarda hora (ver
    # _parse_data_hora em connectors/pncp.py) -- o valor lido de volta do
    # banco é sempre datetime, mesmo quando o fixture manda um date puro
    # (fica meia-noite).
    assert ed.data_encerramento == datetime(2026, 3, 15)


def test_edital_ja_existente_ganha_itens_que_faltavam():
    """O outro achado: 1ª coleta sem itens (falha transitória) não pode
    ficar sem itens pra sempre -- a próxima coleta deve completar."""
    db = _sessao()
    _persistir_edital(db, _ec(itens=[]))
    db.commit()

    novo_item = ItemColetado(numero=1, descricao="Papel A4", quantidade=10, valor_unitario=25.0)
    _persistir_edital(db, _ec(itens=[novo_item]))
    db.commit()

    ed = db.query(Edital).filter_by(id_externo="e1").one()
    assert len(ed.itens) == 1
    assert ed.itens[0].descricao == "Papel A4"


def test_edital_ja_existente_com_itens_nao_duplica_nem_mescla():
    """Não tenta mesclar item a item -- só faz backfill quando não tem
    NENHUM item ainda, pra não arriscar descasar confirmação manual do
    usuário já ligada ao índice de um item existente."""
    db = _sessao()
    item1 = ItemColetado(numero=1, descricao="Item original", quantidade=1, valor_unitario=1.0)
    _persistir_edital(db, _ec(itens=[item1]))
    db.commit()

    item2 = ItemColetado(numero=2, descricao="Item novo (não deveria entrar)", quantidade=1, valor_unitario=1.0)
    _persistir_edital(db, _ec(itens=[item2]))
    db.commit()

    ed = db.query(Edital).filter_by(id_externo="e1").one()
    assert len(ed.itens) == 1
    assert ed.itens[0].descricao == "Item original"


def test_edital_ja_existente_nao_apaga_campo_com_valor_ausente_na_coleta_nova():
    """Se a coleta nova vier com um campo None (ex.: instabilidade parcial
    na resposta), não deve apagar o valor bom já salvo."""
    db = _sessao()
    _persistir_edital(db, _ec(orgao="Órgão Completo"))
    db.commit()

    _persistir_edital(db, _ec(orgao=None))
    db.commit()

    ed = db.query(Edital).filter_by(id_externo="e1").one()
    assert ed.orgao == "Órgão Completo"


def test_edital_ja_existente_atualiza_valor_estimado_mesmo_zero():
    db = _sessao()
    _persistir_edital(db, _ec())
    db.commit()

    ed_antes = db.query(Edital).filter_by(id_externo="e1").one()
    ed_antes.valor_estimado = 100.0
    db.commit()

    _persistir_edital(db, _ec(valor_estimado=0.0))
    db.commit()

    ed = db.query(Edital).filter_by(id_externo="e1").one()
    assert ed.valor_estimado == 0.0


def test_edital_novo_grava_plataforma():
    db = _sessao()
    _persistir_edital(db, _ec(plataforma="BLL Compras"))
    db.commit()

    ed = db.query(Edital).filter_by(id_externo="e1").one()
    assert ed.plataforma == "BLL Compras"


def test_edital_novo_grava_link_sistema_origem():
    """Pedido do usuário: abrir direto na página do pregão na plataforma
    externa (ComprasNet, BLL, etc.) -- mesmo padrão de persistência de
    `plataforma` (nome do sistema), mas guardando a URL específica do
    processo."""
    db = _sessao()
    _persistir_edital(db, _ec(link_sistema_origem="https://bll.org.br/pregao/123"))
    db.commit()

    ed = db.query(Edital).filter_by(id_externo="e1").one()
    assert ed.link_sistema_origem == "https://bll.org.br/pregao/123"


def test_edital_existente_sem_link_sistema_origem_e_completado_na_proxima_coleta():
    db = _sessao()
    _persistir_edital(db, _ec(link_sistema_origem=None))
    db.commit()

    _persistir_edital(db, _ec(link_sistema_origem="https://comprasnet.gov.br/pregao/1"))
    db.commit()

    ed = db.query(Edital).filter_by(id_externo="e1").one()
    assert ed.link_sistema_origem == "https://comprasnet.gov.br/pregao/1"


def test_edital_existente_com_link_sistema_origem_nao_e_apagado_quando_coleta_nova_vem_sem():
    db = _sessao()
    _persistir_edital(db, _ec(link_sistema_origem="https://bll.org.br/pregao/123"))
    db.commit()

    _persistir_edital(db, _ec(link_sistema_origem=None))
    db.commit()

    ed = db.query(Edital).filter_by(id_externo="e1").one()
    assert ed.link_sistema_origem == "https://bll.org.br/pregao/123"


def test_edital_existente_sem_plataforma_e_completado_na_proxima_coleta():
    """Achado real (pedido do usuário: filtro por plataforma): a 1ª coleta de
    um edital ainda ativo pode não ter conseguido reconhecer a plataforma
    (ex.: linkSistemaOrigem ausente naquela resposta) -- a próxima coleta
    (edital ainda dentro do prazo, o PNCP continua devolvendo ele) preenche
    sozinha, sem precisar de backfill manual -- mesmo padrão já usado pra
    orgao/data_encerramento/link."""
    db = _sessao()
    _persistir_edital(db, _ec(plataforma=None))
    db.commit()

    _persistir_edital(db, _ec(plataforma="ComprasNet"))
    db.commit()

    ed = db.query(Edital).filter_by(id_externo="e1").one()
    assert ed.plataforma == "ComprasNet"


def test_edital_existente_com_plataforma_nao_e_apagada_quando_coleta_nova_vem_sem():
    db = _sessao()
    _persistir_edital(db, _ec(plataforma="BLL Compras"))
    db.commit()

    _persistir_edital(db, _ec(plataforma=None))
    db.commit()

    ed = db.query(Edital).filter_by(id_externo="e1").one()
    assert ed.plataforma == "BLL Compras"
