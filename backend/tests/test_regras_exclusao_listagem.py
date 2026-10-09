"""
Achado real (usuário pediu auditoria em produção): regras de exclusão
(RegraExclusao tipo="termo") só eram aplicadas na hora de criar/recalcular
Match (service.py) -- um edital que o motor corretamente nunca vira Match
por bater numa regra continuava aparecendo normalmente em GET /api/editais
(modo "todos os editais" e no bloco "sem_match" da busca por item), porque
essas duas consultas nunca conheciam as regras de exclusão.

Escopo desta correção: só edital SEM Match. Um edital que já tem Match
(criado antes da regra existir) continua aparecendo até o usuário rodar um
recálculo completo -- isso é outra frente, não desta listagem.

Banco sqlite em memória, sem HTTP -- chama listar_editais direto (mesmo
padrão de test_editais_filtros.py). Rode com:  cd backend && pytest
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.main import listar_editais
from app.models import Base, Usuario, Edital, ItemEdital, Match, RegraExclusao


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


def _regra(db, usuario, valor, tipo="termo", ativo=True):
    r = RegraExclusao(usuario_id=usuario.id, tipo=tipo, valor=valor, ativo=ativo)
    db.add(r)
    db.commit()
    return r


def _edital(db, id_externo, objeto="Aquisicao de produtos", itens=None, com_match_para=None,
           score=0.5, nivel="medio"):
    ed = Edital(fonte="PNCP", id_externo=id_externo, orgao="Orgao Teste", objeto=objeto, uf="SP")
    db.add(ed)
    db.commit()
    for numero, descricao in enumerate(itens or [], start=1):
        db.add(ItemEdital(edital_id=ed.id, numero=numero, descricao=descricao))
    if com_match_para is not None:
        db.add(Match(usuario_id=com_match_para.id, edital_id=ed.id, score=score, nivel=nivel))
    db.commit()
    return ed


def _listar(db, user, **kwargs):
    padrao = dict(nivel=None, uf=None, plataforma=None, modalidade=None, status=None,
                  vista="ativos", apenas_nao_lidos=False, apenas_interessantes=False,
                  hoje=False, tipo="todos", valor_min=None, valor_max=None,
                  data_de=None, data_ate=None, data_fim_de=None, data_fim_ate=None,
                  busca_item=None, todos_editais=False,
                  pagina=1, por_pagina=50)
    padrao.update(kwargs)
    return listar_editais(user=user, db=db, **padrao)


def test_todos_editais_esconde_edital_sem_match_que_bate_termo_no_objeto():
    # achado: unaccent_imutavel (ver _condicao_nao_excluido_sem_match) só
    # existe no Postgres -- sqlite (banco destes testes) compara sem tirar
    # acento, então o objeto de teste já vem sem acento de propósito, pra
    # testar a LÓGICA do filtro (match/não-match, escopo, isolamento por
    # usuário) sem depender de uma extensão que só existe em produção.
    db = _sessao()
    u = _usuario(db)
    _regra(db, u, "servico")
    excluido = _edital(db, "ed-servico", objeto="Contratacao de empresa para prestacao de servicos graficos")
    mantido = _edital(db, "ed-produto", objeto="Aquisicao de papel A4 e canetas")

    r = _listar(db, u, todos_editais=True)

    ids = {x["edital_id"] for x in r["resultados"]}
    assert excluido.id not in ids
    assert mantido.id in ids


def test_todos_editais_esconde_edital_sem_match_que_bate_termo_no_item():
    db = _sessao()
    u = _usuario(db)
    _regra(db, u, "servico")
    excluido = _edital(db, "ed-item-servico", objeto="Registro de precos diversos",
                       itens=["Prestacao de servico de limpeza predial"])
    mantido = _edital(db, "ed-item-produto", objeto="Registro de precos diversos",
                      itens=["Resma de papel A4 75g"])

    r = _listar(db, u, todos_editais=True)

    ids = {x["edital_id"] for x in r["resultados"]}
    assert excluido.id not in ids
    assert mantido.id in ids


def test_todos_editais_nao_mexe_em_edital_que_ja_tem_match():
    """Escopo da correção: só edital SEM Match. Um Match já existente
    (criado antes da regra, ou por qualquer outro motivo) continua
    aparecendo -- limpar esse caso é o recálculo completo, não esta
    listagem."""
    db = _sessao()
    u = _usuario(db)
    _regra(db, u, "servico")
    com_match = _edital(db, "ed-servico-com-match",
                        objeto="Contratação de prestação de serviços gráficos",
                        com_match_para=u, nivel="forte", score=0.9)

    r = _listar(db, u, todos_editais=True)

    ids = {x["edital_id"] for x in r["resultados"]}
    assert com_match.id in ids


def test_todos_editais_regra_inativa_nao_filtra():
    db = _sessao()
    u = _usuario(db)
    _regra(db, u, "servico", ativo=False)
    ed = _edital(db, "ed-servico-regra-inativa", objeto="Prestação de serviços gráficos")

    r = _listar(db, u, todos_editais=True)

    ids = {x["edital_id"] for x in r["resultados"]}
    assert ed.id in ids


def test_todos_editais_sem_regra_nenhuma_comportamento_inalterado():
    db = _sessao()
    u = _usuario(db)
    ed = _edital(db, "ed-servico-sem-regra", objeto="Prestação de serviços gráficos")

    r = _listar(db, u, todos_editais=True)

    ids = {x["edital_id"] for x in r["resultados"]}
    assert ed.id in ids


def test_todos_editais_regra_e_isolada_por_usuario():
    db = _sessao()
    u1 = _usuario(db)
    u2 = Usuario(nome="Outro", email="outro@t.com", senha_hash="x")
    db.add(u2)
    db.commit()
    _regra(db, u1, "servico")
    ed = _edital(db, "ed-servico-outro-usuario", objeto="Prestação de serviços gráficos")

    r = _listar(db, u2, todos_editais=True)

    ids = {x["edital_id"] for x in r["resultados"]}
    assert ed.id in ids   # regra é do u1, não filtra pra u2


def test_busca_item_sem_match_tambem_respeita_regra_de_exclusao():
    """Mesmo achado, no outro lugar que lista edital sem Match: o bloco
    'sem_match' que a busca por item usa pra achar editais que o motor
    nunca viu."""
    db = _sessao()
    u = _usuario(db)
    _regra(db, u, "servico")
    excluido = _edital(db, "ed-busca-servico", objeto="Registro de precos diversos",
                       itens=["Prestacao de servico de grampeador e papelaria"])
    mantido = _edital(db, "ed-busca-produto", objeto="Registro de precos diversos",
                      itens=["Grampeador de mesa 26/6"])

    r = _listar(db, u, busca_item="grampeador")

    ids_sem_match = {x["edital_id"] for x in r["sem_match"]}
    assert excluido.id not in ids_sem_match
    assert mantido.id in ids_sem_match
