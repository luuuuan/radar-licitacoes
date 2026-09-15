"""
Testes dos filtros de faixa de valor (valor_min/valor_max) e busca por item
(busca_item) em GET /api/editais. Banco sqlite em memória, sem HTTP — chama
a função da rota diretamente com os parâmetros já resolvidos (o jeito que o
FastAPI resolveria via Query(...), sem depender da injeção de dependência).
Rode com:  cd backend && pytest
"""
from datetime import date, timedelta

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.main import listar_editais
from app.models import Base, Usuario, Edital, ItemEdital, Match


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


def _edital_com_match(db, usuario, id_externo, valor_estimado=None, itens=None,
                      data_abertura=None, data_encerramento=None, plataforma=None, uf="SP",
                      modalidade=None):
    ed = Edital(fonte="PNCP", id_externo=id_externo, orgao="Orgao Teste",
                objeto="Aquisicao", uf=uf, valor_estimado=valor_estimado,
                data_abertura=data_abertura, data_encerramento=data_encerramento,
                plataforma=plataforma, modalidade=modalidade)
    db.add(ed)
    db.commit()
    for numero, descricao in enumerate(itens or [], start=1):
        db.add(ItemEdital(edital_id=ed.id, numero=numero, descricao=descricao))
    db.add(Match(usuario_id=usuario.id, edital_id=ed.id, score=0.5, nivel="medio"))
    db.commit()
    return ed


def _edital_sem_match(db, id_externo, itens=None, data_abertura=None, uf="SP",
                      valor_estimado=None, data_encerramento=None, plataforma=None,
                      modalidade=None):
    ed = Edital(fonte="PNCP", id_externo=id_externo, orgao="Orgao Sem Match",
               objeto="Aquisicao", uf=uf, data_abertura=data_abertura,
               valor_estimado=valor_estimado, data_encerramento=data_encerramento,
               plataforma=plataforma, modalidade=modalidade)
    db.add(ed)
    db.commit()
    for numero, descricao in enumerate(itens or [], start=1):
        db.add(ItemEdital(edital_id=ed.id, numero=numero, descricao=descricao))
    db.commit()
    return ed


def _listar(db, user, **kwargs):
    padrao = dict(nivel=None, uf=None, plataforma=None, modalidade=None, status=None,
                  vista="ativos", apenas_nao_lidos=False, apenas_interessantes=False,
                  hoje=False, tipo="todos", valor_min=None, valor_max=None,
                  data_de=None, data_ate=None, busca_item=None, todos_editais=False,
                  pagina=1, por_pagina=50)
    padrao.update(kwargs)
    return listar_editais(user=user, db=db, **padrao)


def _plataformas(db, user, **kwargs):
    from app.main import listar_plataformas
    padrao = dict(nivel=None, uf=None, modalidade=None, status=None, vista="ativos",
                  apenas_nao_lidos=False, apenas_interessantes=False, hoje=False,
                  tipo="todos", valor_min=None, valor_max=None,
                  data_de=None, data_ate=None, busca_item=None, todos_editais=False)
    padrao.update(kwargs)
    return listar_plataformas(user=user, db=db, **padrao)


def _modalidades(db, user, **kwargs):
    from app.main import listar_modalidades
    padrao = dict(nivel=None, uf=None, plataforma=None, status=None, vista="ativos",
                  apenas_nao_lidos=False, apenas_interessantes=False, hoje=False,
                  tipo="todos", valor_min=None, valor_max=None,
                  data_de=None, data_ate=None, busca_item=None, todos_editais=False)
    padrao.update(kwargs)
    return listar_modalidades(user=user, db=db, **padrao)


def test_valor_min_exclui_editais_abaixo_do_piso():
    db = _sessao()
    u = _usuario(db)
    _edital_com_match(db, u, "ed1", valor_estimado=1000.0)
    _edital_com_match(db, u, "ed2", valor_estimado=50000.0)

    r = _listar(db, u, valor_min=10000)
    assert r["total"] == 1
    assert r["resultados"][0]["valor_estimado"] == 50000.0


def test_valor_max_exclui_editais_acima_do_teto():
    db = _sessao()
    u = _usuario(db)
    _edital_com_match(db, u, "ed1", valor_estimado=1000.0)
    _edital_com_match(db, u, "ed2", valor_estimado=50000.0)

    r = _listar(db, u, valor_max=10000)
    assert r["total"] == 1
    assert r["resultados"][0]["valor_estimado"] == 1000.0


def test_faixa_de_valor_combinada_min_e_max():
    db = _sessao()
    u = _usuario(db)
    _edital_com_match(db, u, "ed1", valor_estimado=1000.0)
    _edital_com_match(db, u, "ed2", valor_estimado=25000.0)
    _edital_com_match(db, u, "ed3", valor_estimado=90000.0)

    r = _listar(db, u, valor_min=10000, valor_max=50000)
    assert r["total"] == 1
    assert r["resultados"][0]["valor_estimado"] == 25000.0


def test_edital_sem_valor_estimado_fica_de_fora_quando_ha_filtro_de_valor():
    """Edital sem valor cadastrado não pode ser confirmado como "dentro da
    faixa" — exclui em vez de mostrar sem checagem nenhuma."""
    db = _sessao()
    u = _usuario(db)
    _edital_com_match(db, u, "ed1", valor_estimado=None)

    r = _listar(db, u, valor_min=100)
    assert r["total"] == 0


# --------- filtro de prazo final (data_de/data_ate) --------- #
# Por pedido explícito do usuário, filtra por data_abertura
# (dataAberturaProposta no PNCP -- início do recebimento de propostas), não
# por data_encerramento (prazo final) -- mesma escolha de
# test_agenda.py/test_compromissos.py.
#
# Datas relativas a hoje (não absolutas): a vista padrão ("ativos") já
# exclui edital com data_abertura no passado -- um teste com data fixa
# no passado quebraria sozinho conforme o tempo passa, sem ter nada a ver
# com o filtro sendo testado aqui.

def test_data_de_exclui_editais_com_prazo_antes():
    import datetime
    hoje = datetime.date.today()
    db = _sessao()
    u = _usuario(db)
    _edital_com_match(db, u, "ed1", data_abertura=hoje + datetime.timedelta(days=1))
    ed2 = _edital_com_match(db, u, "ed2", data_abertura=hoje + datetime.timedelta(days=20))

    r = _listar(db, u, data_de=hoje + datetime.timedelta(days=10))
    assert r["total"] == 1
    assert r["resultados"][0]["edital_id"] == ed2.id


def test_data_ate_exclui_editais_com_prazo_depois():
    import datetime
    hoje = datetime.date.today()
    db = _sessao()
    u = _usuario(db)
    ed1 = _edital_com_match(db, u, "ed1", data_abertura=hoje + datetime.timedelta(days=1))
    _edital_com_match(db, u, "ed2", data_abertura=hoje + datetime.timedelta(days=20))

    r = _listar(db, u, data_ate=hoje + datetime.timedelta(days=10))
    assert r["total"] == 1
    assert r["resultados"][0]["edital_id"] == ed1.id


def test_faixa_de_data_combinada_de_e_ate():
    import datetime
    hoje = datetime.date.today()
    db = _sessao()
    u = _usuario(db)
    _edital_com_match(db, u, "ed1", data_abertura=hoje + datetime.timedelta(days=1))
    ed2 = _edital_com_match(db, u, "ed2", data_abertura=hoje + datetime.timedelta(days=20))
    _edital_com_match(db, u, "ed3", data_abertura=hoje + datetime.timedelta(days=35))

    r = _listar(db, u, data_de=hoje + datetime.timedelta(days=10), data_ate=hoje + datetime.timedelta(days=25))
    assert r["total"] == 1
    assert r["resultados"][0]["edital_id"] == ed2.id


def test_edital_sem_data_abertura_fica_de_fora_quando_ha_filtro_de_data():
    import datetime
    db = _sessao()
    u = _usuario(db)
    _edital_com_match(db, u, "ed1", data_abertura=None)

    r = _listar(db, u, data_de=datetime.date.today())
    assert r["total"] == 0


def test_busca_item_so_mostra_editais_que_pedem_o_item_buscado():
    db = _sessao()
    u = _usuario(db)
    ed1 = _edital_com_match(db, u, "ed1", itens=["Grampeador de mesa 26/6", "Papel A4"])
    _edital_com_match(db, u, "ed2", itens=["Caneta esferografica azul"])

    r = _listar(db, u, busca_item="grampeador")
    assert r["total"] == 1
    assert r["resultados"][0]["edital_id"] == ed1.id


def test_busca_item_case_insensitive():
    db = _sessao()
    u = _usuario(db)
    _edital_com_match(db, u, "ed1", itens=["GRAMPEADOR METAL 20 FOLHAS"])

    r = _listar(db, u, busca_item="grampeador")
    assert r["total"] == 1


def test_busca_item_sem_resultado_quando_nenhum_item_bate():
    db = _sessao()
    u = _usuario(db)
    _edital_com_match(db, u, "ed1", itens=["Papel A4"])

    r = _listar(db, u, busca_item="grampeador")
    assert r["total"] == 0


def test_busca_item_em_branco_nao_filtra_nada():
    db = _sessao()
    u = _usuario(db)
    _edital_com_match(db, u, "ed1", itens=["Papel A4"])

    r = _listar(db, u, busca_item="   ")
    assert r["total"] == 1


# --------- sem_match: editais achados pela busca mas sem Match nenhum --------- #

def test_busca_item_acha_edital_sem_match(monkeypatch):
    """Sem sinal do motor automático (ex.: sem saldo de IA), o edital nunca
    virou Match — a busca por item precisa achar mesmo assim, num campo
    separado (sem_match), já que ele não entra no "resultados" normal."""
    db = _sessao()
    u = _usuario(db)
    ed = _edital_sem_match(db, "ed-sem-match", itens=["Grampeador de mesa 26/6"])

    r = _listar(db, u, busca_item="grampeador")

    assert r["total"] == 0   # nao entra na contagem normal (sem Match)
    assert len(r["sem_match"]) == 1
    assert r["sem_match"][0]["edital_id"] == ed.id
    assert r["sem_match"][0]["itens_batem"] == ["Grampeador de mesa 26/6"]


def test_busca_item_sem_match_nao_repete_edital_que_ja_tem_match():
    db = _sessao()
    u = _usuario(db)
    ed = _edital_com_match(db, u, "ed1", itens=["Grampeador de mesa 26/6"])

    r = _listar(db, u, busca_item="grampeador")

    assert r["total"] == 1
    assert r["resultados"][0]["edital_id"] == ed.id
    assert r["sem_match"] == []   # já apareceu em "resultados", não duplica


def test_sem_match_vazio_quando_busca_item_nao_informada():
    db = _sessao()
    u = _usuario(db)
    _edital_sem_match(db, "ed1", itens=["Grampeador de mesa 26/6"])

    r = _listar(db, u)

    assert r["sem_match"] == []


def test_sem_match_respeita_vista_ativos_por_padrao():
    import datetime
    db = _sessao()
    u = _usuario(db)
    ontem = datetime.date.today() - datetime.timedelta(days=1)
    _edital_sem_match(db, "ed-abertura-passada", itens=["Grampeador de mesa"], data_abertura=ontem)

    r = _listar(db, u, busca_item="grampeador")

    assert r["sem_match"] == []


# --------- "ativo" x "encerrado" usa o prazo EFETIVO (data_encerramento --------- #
# quando existe, senão data_abertura), não só data_abertura --------- #
# Achado real (edital 127082, reportado pelo usuário): data_abertura no
# passado não significa que a janela de propostas fechou -- data_encerramento
# (prazo final no PNCP) pode estar no futuro, e o edital continua aceitando
# propostas normalmente. Ver _dias_restantes_edital em app/main.py.

def test_vista_ativos_inclui_edital_com_abertura_passada_mas_encerramento_futuro():
    db = _sessao()
    u = _usuario(db)
    hoje = date.today()
    ed = _edital_com_match(db, u, "ed1",
                           data_abertura=hoje - timedelta(days=5),
                           data_encerramento=hoje + timedelta(days=10))

    r = _listar(db, u, vista="ativos")

    assert r["total"] == 1
    assert r["resultados"][0]["edital_id"] == ed.id
    assert r["resultados"][0]["dias_restantes"] == 10


def test_vista_ativos_exclui_edital_com_abertura_e_encerramento_passados():
    db = _sessao()
    u = _usuario(db)
    hoje = date.today()
    _edital_com_match(db, u, "ed1",
                      data_abertura=hoje - timedelta(days=20),
                      data_encerramento=hoje - timedelta(days=5))

    r = _listar(db, u, vista="ativos")

    assert r["total"] == 0


def test_dias_restantes_antes_da_abertura_conta_ate_abertura_nao_ate_encerramento():
    db = _sessao()
    u = _usuario(db)
    hoje = date.today()
    ed = _edital_com_match(db, u, "ed1",
                           data_abertura=hoje + timedelta(days=3),
                           data_encerramento=hoje + timedelta(days=40))

    r = _listar(db, u, vista="ativos")

    assert r["resultados"][0]["edital_id"] == ed.id
    assert r["resultados"][0]["dias_restantes"] == 3


def test_sem_match_inclui_edital_com_abertura_passada_mas_encerramento_futuro():
    hoje = date.today()
    db = _sessao()
    u = _usuario(db)
    ed = _edital_sem_match(db, "ed-janela-aberta", itens=["Grampeador de mesa"],
                           data_abertura=hoje - timedelta(days=5),
                           data_encerramento=hoje + timedelta(days=10))

    r = _listar(db, u, busca_item="grampeador")

    assert len(r["sem_match"]) == 1
    assert r["sem_match"][0]["edital_id"] == ed.id
    assert r["sem_match"][0]["dias_restantes"] == 10


def test_sem_match_limitado_a_20_resultados():
    db = _sessao()
    u = _usuario(db)
    for i in range(25):
        _edital_sem_match(db, f"ed{i}", itens=["Grampeador de mesa 26/6"])

    r = _listar(db, u, busca_item="grampeador")

    assert len(r["sem_match"]) == 20


def test_sem_match_nao_faz_n_mais_1_pra_carregar_itens():
    """Achado real (auditoria do agente code-reviewer): o loop que monta
    "itens_batem" acessava ed.itens sem eager loading -- um SELECT extra por
    edital do bloco (até 20, o limite desta consulta), o mesmo padrão que o
    código já evita de propósito pra lista principal (ver itens_por_unidade_map,
    logo acima). selectinload(Edital.itens) reduz isso a no máximo 1 SELECT
    em lote pra todos os editais da página."""
    from sqlalchemy import event

    db = _sessao()
    u = _usuario(db)
    for i in range(20):
        _edital_sem_match(db, f"ed{i}", itens=["Grampeador de mesa 26/6", "Grampeador industrial"])

    queries = []
    engine = db.get_bind()

    def _contar(conn, cursor, statement, parameters, context, executemany):
        queries.append(statement)

    event.listen(engine, "before_cursor_execute", _contar)
    try:
        r = _listar(db, u, busca_item="grampeador")
    finally:
        event.remove(engine, "before_cursor_execute", _contar)

    assert len(r["sem_match"]) == 20
    # busca_item usa EXISTS (SELECT ... FROM itens_edital ...) em VÁRIAS
    # queries desta rota (contagem, resultados, sem_match) -- "itens_edital"
    # aparece em várias delas sem ser um SELECT de linhas completas. O que
    # identifica de fato "carregar as linhas de ItemEdital" (seja o batch do
    # eager load, seja uma lazy load por edital no caso de N+1) é a query
    # começar com "SELECT itens_edital." — nenhuma das EXISTS começa assim.
    selects_itens = [q for q in queries if q.strip().lower().startswith("select itens_edital.")]
    assert len(selects_itens) <= 1, (
        f"esperava no máximo 1 SELECT de linhas de itens_edital (eager load em lote), achou {len(selects_itens)}")


def test_filtro_plataforma_exclui_editais_de_outra_plataforma():
    """Achado real (pedido do usuário: filtro por plataforma/sistema, ex.:
    BLL, ComprasNet) -- vale tanto pro edital que teve análise automática
    (Match) quanto pro que ainda não teve (ver
    test_sem_match_respeita_filtro_de_plataforma logo abaixo)."""
    db = _sessao()
    u = _usuario(db)
    ed_bll = _edital_com_match(db, u, "ed-bll", plataforma="BLL Compras")
    _edital_com_match(db, u, "ed-cn", plataforma="ComprasNet")

    r = _listar(db, u, plataforma=["BLL Compras"])

    assert r["total"] == 1
    assert r["resultados"][0]["edital_id"] == ed_bll.id
    assert r["resultados"][0]["plataforma"] == "BLL Compras"


def test_sem_match_respeita_filtro_de_plataforma():
    db = _sessao()
    u = _usuario(db)
    ed_bll = _edital_sem_match(db, "ed-bll", itens=["Papel A4 75g"], plataforma="BLL Compras")
    _edital_sem_match(db, "ed-cn", itens=["Papel A4 75g"], plataforma="ComprasNet")

    r = _listar(db, u, busca_item="papel a4", plataforma=["BLL Compras"])

    assert len(r["sem_match"]) == 1
    assert r["sem_match"][0]["edital_id"] == ed_bll.id
    assert r["sem_match"][0]["plataforma"] == "BLL Compras"


# --------- filtro por modalidade (tipo de pregão) --------- #

def test_filtro_modalidade_exclui_editais_de_outra_modalidade():
    """Pedido do usuário: filtro por tipo de pregão (ex.: Pregão -
    Eletrônico, Dispensa) -- mesmo padrão do filtro de plataforma."""
    db = _sessao()
    u = _usuario(db)
    ed_pe = _edital_com_match(db, u, "ed-pe", modalidade="Pregão - Eletrônico")
    _edital_com_match(db, u, "ed-disp", modalidade="Dispensa")

    r = _listar(db, u, modalidade=["Pregão - Eletrônico"])

    assert r["total"] == 1
    assert r["resultados"][0]["edital_id"] == ed_pe.id
    assert r["resultados"][0]["modalidade"] == "Pregão - Eletrônico"


def test_listar_modalidades_devolve_valores_distintos_ordenados_sem_nulos():
    db = _sessao()
    u = _usuario(db)
    _edital_com_match(db, u, "ed1", modalidade="Pregão - Eletrônico")
    _edital_com_match(db, u, "ed2", modalidade="Dispensa")
    _edital_com_match(db, u, "ed3", modalidade="Pregão - Eletrônico")   # duplicado, não repete
    _edital_com_match(db, u, "ed4", modalidade=None)                    # sem modalidade, fica de fora

    r = _modalidades(db, u, todos_editais=False)

    assert r["modalidades"] == ["Dispensa", "Pregão - Eletrônico"]


def test_listar_modalidades_respeita_filtro_de_uf():
    """Mesmo achado real do filtro de plataforma: abrir o filtro de
    modalidade tem que oferecer só as modalidades que aparecem nos editais
    já filtrados por UF na tela, não o universo inteiro."""
    db = _sessao()
    u = _usuario(db)
    _edital_com_match(db, u, "ed-sp", modalidade="Pregão - Eletrônico", uf="SP")
    _edital_com_match(db, u, "ed-pr", modalidade="Dispensa", uf="PR")
    _edital_com_match(db, u, "ed-rj", modalidade="Concorrência", uf="RJ")

    r = _modalidades(db, u, uf=["SP", "PR"])

    assert r["modalidades"] == ["Dispensa", "Pregão - Eletrônico"]


def test_listar_modalidades_respeita_filtro_de_plataforma():
    """listar_modalidades aceita `plataforma` -- filtrar por plataforma na
    tela também restringe as modalidades oferecidas no outro filtro."""
    db = _sessao()
    u = _usuario(db)
    _edital_com_match(db, u, "ed-cn", modalidade="Pregão - Eletrônico", plataforma="ComprasNet")
    _edital_com_match(db, u, "ed-bll", modalidade="Dispensa", plataforma="BLL Compras")

    r = _modalidades(db, u, plataforma=["ComprasNet"])

    assert r["modalidades"] == ["Pregão - Eletrônico"]


def test_listar_modalidades_concorda_com_listar_editais_no_mesmo_filtro():
    """Mesmo contrato fixado pro filtro de plataforma: as duas rotas
    (listar_editais e listar_modalidades) têm que concordar sobre quais
    modalidades aparecem pro mesmo conjunto de filtros ativos."""
    db = _sessao()
    u = _usuario(db)
    _edital_com_match(db, u, "ed-sp-barato", modalidade="Pregão - Eletrônico", uf="SP", valor_estimado=500.0)
    _edital_com_match(db, u, "ed-sp-caro", modalidade="Dispensa", uf="SP", valor_estimado=50000.0)
    _edital_com_match(db, u, "ed-rj-caro", modalidade="Concorrência", uf="RJ", valor_estimado=50000.0)

    r_modalidades = _modalidades(db, u, uf=["SP"], valor_min=10000)
    r_editais = _listar(db, u, uf=["SP"], valor_min=10000)
    modalidades_via_editais = {e["modalidade"] for e in r_editais["resultados"]}
    assert modalidades_via_editais == set(r_modalidades["modalidades"]) == {"Dispensa"}


def test_sem_match_respeita_filtro_de_uf():
    """Achado real: selecionar um estado e depois buscar por um item trazia
    editais de QUALQUER estado no bloco "sem análise automática ainda" — essa
    consulta não aplicava o filtro de uf (só a busca principal aplicava)."""
    db = _sessao()
    u = _usuario(db)
    ed_pr = _edital_sem_match(db, "ed-pr", itens=["Papel A4 75g"], uf="PR")
    _edital_sem_match(db, "ed-sp", itens=["Papel A4 75g"], uf="SP")

    r = _listar(db, u, busca_item="papel a4", uf=["PR"])

    assert len(r["sem_match"]) == 1
    assert r["sem_match"][0]["edital_id"] == ed_pr.id


def test_sem_match_respeita_filtro_de_valor():
    db = _sessao()
    u = _usuario(db)
    ed_caro = _edital_sem_match(db, "ed-caro", itens=["Papel A4 75g"], valor_estimado=50000.0)
    _edital_sem_match(db, "ed-barato", itens=["Papel A4 75g"], valor_estimado=1000.0)

    r = _listar(db, u, busca_item="papel a4", valor_min=10000)

    assert len(r["sem_match"]) == 1
    assert r["sem_match"][0]["edital_id"] == ed_caro.id


def test_sem_match_respeita_filtro_de_data_de():
    """Achado real: a busca por item filtrando "início do recebimento a
    partir de X" continuava mostrando, no bloco "sem análise automática",
    editais que abrem ANTES de X -- data_de/data_ate nunca tinham sido
    aplicados nessa consulta (só uf/tipo/valor/hoje foram, numa correção
    anterior que esqueceu esses dois)."""
    db = _sessao()
    u = _usuario(db)
    hoje = date.today()
    ed_depois = _edital_sem_match(db, "ed-depois", itens=["Caneta esferográfica azul"],
                                  data_abertura=hoje + timedelta(days=10))
    _edital_sem_match(db, "ed-antes", itens=["Caneta esferográfica azul"],
                      data_abertura=hoje + timedelta(days=1))

    r = _listar(db, u, busca_item="caneta", data_de=hoje + timedelta(days=5))

    assert len(r["sem_match"]) == 1
    assert r["sem_match"][0]["edital_id"] == ed_depois.id


def test_sem_match_respeita_filtro_de_data_ate():
    db = _sessao()
    u = _usuario(db)
    hoje = date.today()
    ed_antes = _edital_sem_match(db, "ed-antes", itens=["Caneta esferográfica azul"],
                                 data_abertura=hoje + timedelta(days=1))
    _edital_sem_match(db, "ed-depois", itens=["Caneta esferográfica azul"],
                      data_abertura=hoje + timedelta(days=10))

    r = _listar(db, u, busca_item="caneta", data_ate=hoje + timedelta(days=5))

    assert len(r["sem_match"]) == 1
    assert r["sem_match"][0]["edital_id"] == ed_antes.id


def test_listar_plataformas_devolve_valores_distintos_ordenados_sem_nulos():
    db = _sessao()
    u = _usuario(db)
    _edital_com_match(db, u, "ed1", plataforma="ComprasNet")
    _edital_com_match(db, u, "ed2", plataforma="BLL Compras")
    _edital_com_match(db, u, "ed3", plataforma="ComprasNet")   # duplicado, não repete
    _edital_com_match(db, u, "ed4", plataforma=None)           # sem plataforma, fica de fora

    r = _plataformas(db, u, todos_editais=False)

    assert r["plataformas"] == ["BLL Compras", "ComprasNet"]


def test_listar_plataformas_nao_oferece_plataforma_sem_match_do_usuario():
    """Achado real: o filtro oferecia plataformas de editais que nunca
    apareceriam na listagem do usuário (sem Match nenhum, ou Match de OUTRO
    usuário) -- marcar a opção sempre dava "nenhum edital encontrado". A
    lista de opções agora reflete só o que o próprio usuário pode ver."""
    db = _sessao()
    u = _usuario(db)
    outro = Usuario(nome="Outro", email="outro@t.com", senha_hash="x")
    db.add(outro)
    db.commit()

    _edital_com_match(db, u, "ed-meu", plataforma="ComprasNet")
    _edital_sem_match(db, "ed-orfao", plataforma="BLL Compras")            # sem Match nenhum
    _edital_com_match(db, outro, "ed-de-outro", plataforma="Licitanet")    # Match de outro usuário

    r = _plataformas(db, u, todos_editais=False)

    assert r["plataformas"] == ["ComprasNet"]


def test_listar_plataformas_com_todos_editais_devolve_tudo():
    """todos_editais=True (mesmo espírito do parâmetro em GET /api/editais):
    o usuário quer poder filtrar por uma plataforma mesmo que ela nunca
    tenha dado match nenhum com o catálogo dele."""
    db = _sessao()
    u = _usuario(db)
    outro = Usuario(nome="Outro", email="outro3@t.com", senha_hash="x")
    db.add(outro)
    db.commit()

    _edital_com_match(db, u, "ed-meu", plataforma="ComprasNet")
    _edital_sem_match(db, "ed-orfao", plataforma="BLL Compras")
    _edital_com_match(db, outro, "ed-de-outro", plataforma="Licitanet")

    r = _plataformas(db, u, todos_editais=True)

    assert r["plataformas"] == ["BLL Compras", "ComprasNet", "Licitanet"]


def test_listar_plataformas_respeita_filtro_de_uf():
    """Achado real (pedido do usuário): filtrar por UF SP/PR/SC e abrir o
    filtro de plataforma tem que oferecer só as plataformas que aparecem
    nos editais desses estados -- antes o parâmetro `uf` nem existia nessa
    rota, então o filtro de plataforma sempre mostrava TODAS as plataformas
    do usuário, mesmo com outros filtros (UF, valor, data...) já ativos."""
    db = _sessao()
    u = _usuario(db)
    _edital_com_match(db, u, "ed-sp", plataforma="ComprasNet", uf="SP")
    _edital_com_match(db, u, "ed-pr", plataforma="BLL Compras", uf="PR")
    _edital_com_match(db, u, "ed-rj", plataforma="Licitanet", uf="RJ")

    r = _plataformas(db, u, uf=["SP", "PR", "SC"])

    assert r["plataformas"] == ["BLL Compras", "ComprasNet"]


def test_listar_plataformas_respeita_filtro_de_valor():
    db = _sessao()
    u = _usuario(db)
    _edital_com_match(db, u, "ed-barato", plataforma="ComprasNet", valor_estimado=1000.0)
    _edital_com_match(db, u, "ed-caro", plataforma="BLL Compras", valor_estimado=90000.0)

    r = _plataformas(db, u, valor_min=10000)

    assert r["plataformas"] == ["BLL Compras"]


def test_listar_plataformas_respeita_busca_item():
    db = _sessao()
    u = _usuario(db)
    _edital_com_match(db, u, "ed-grampeador", plataforma="ComprasNet",
                      itens=["Grampeador de mesa 26/6"])
    _edital_com_match(db, u, "ed-caneta", plataforma="BLL Compras",
                      itens=["Caneta esferografica azul"])

    r = _plataformas(db, u, busca_item="grampeador")

    assert r["plataformas"] == ["ComprasNet"]


def test_listar_plataformas_respeita_filtro_de_nivel():
    """Achado do code-reviewer: os testes anteriores só cobriam filtros
    NATIVOS do Edital (uf/valor/busca_item) -- nivel é um campo do Match, a
    classe de filtro mais arriscada pra quebrar silenciosamente com
    with_only_columns (WHERE referenciando uma tabela que não está mais no
    SELECT)."""
    db = _sessao()
    u = _usuario(db)
    ed_forte = _edital_com_match(db, u, "ed-forte", plataforma="ComprasNet")
    ed_fraco = _edital_com_match(db, u, "ed-fraco", plataforma="BLL Compras")
    db.execute(select(Match).where(Match.edital_id == ed_forte.id)).scalar_one().nivel = "forte"
    db.execute(select(Match).where(Match.edital_id == ed_fraco.id)).scalar_one().nivel = "fraco"
    db.commit()

    r = _plataformas(db, u, nivel="forte")

    assert r["plataformas"] == ["ComprasNet"]


def test_listar_plataformas_respeita_apenas_nao_lidos():
    db = _sessao()
    u = _usuario(db)
    ed_lido = _edital_com_match(db, u, "ed-lido", plataforma="ComprasNet")
    _edital_com_match(db, u, "ed-nao-lido", plataforma="BLL Compras")
    db.execute(select(Match).where(Match.edital_id == ed_lido.id)).scalar_one().lido = True
    db.commit()

    r = _plataformas(db, u, apenas_nao_lidos=True)

    assert r["plataformas"] == ["BLL Compras"]


def test_listar_plataformas_respeita_filtro_de_status():
    db = _sessao()
    u = _usuario(db)
    ed_participando = _edital_com_match(db, u, "ed-participando", plataforma="ComprasNet")
    _edital_com_match(db, u, "ed-novo", plataforma="BLL Compras")
    m = db.execute(select(Match).where(Match.edital_id == ed_participando.id)).scalar_one()
    m.status = "vou_participar"
    db.commit()

    r = _plataformas(db, u, status="vou_participar")

    assert r["plataformas"] == ["ComprasNet"]


def test_listar_plataformas_concorda_com_listar_editais_no_mesmo_filtro():
    """Achado do architect-reviewer: as duas rotas compartilham
    _query_editais_filtrada, mas nada além do docstring garante que
    continuam de acordo se alguém mudar o comportamento de um filtro numa
    rota sem lembrar da outra -- fixa esse contrato num teste executável
    em vez de só documentado em prosa."""
    db = _sessao()
    u = _usuario(db)
    _edital_com_match(db, u, "ed-sp-barato", plataforma="ComprasNet", uf="SP", valor_estimado=500.0)
    _edital_com_match(db, u, "ed-sp-caro", plataforma="BLL Compras", uf="SP", valor_estimado=50000.0)
    _edital_com_match(db, u, "ed-rj-caro", plataforma="Licitanet", uf="RJ", valor_estimado=50000.0)

    r_editais = _listar(db, u, uf=["SP"], valor_min=10000)
    r_plataformas = _plataformas(db, u, uf=["SP"], valor_min=10000)

    plataformas_via_editais = {e["plataforma"] for e in r_editais["resultados"]}
    assert plataformas_via_editais == set(r_plataformas["plataformas"]) == {"BLL Compras"}


def test_listar_plataformas_combina_todos_editais_com_filtro_de_uf():
    """todos_editais=True e o filtro de UF continuam combinando entre si --
    não é "ou um ou outro"."""
    db = _sessao()
    u = _usuario(db)
    outro = Usuario(nome="Outro", email="outro4@t.com", senha_hash="x")
    db.add(outro)
    db.commit()
    _edital_com_match(db, outro, "ed-outro-sp", plataforma="Licitanet", uf="SP")
    _edital_sem_match(db, "ed-sem-match-pr", plataforma="BLL Compras", uf="PR")
    _edital_sem_match(db, "ed-sem-match-rj", plataforma="ComprasNet", uf="RJ")

    r = _plataformas(db, u, todos_editais=True, uf=["SP", "PR"])

    assert r["plataformas"] == ["BLL Compras", "Licitanet"]


def test_todos_editais_inclui_edital_sem_match():
    """Achado real (pedido do usuário, depois do fix acima): o usuário não
    queria só um filtro "honesto" -- queria ENXERGAR editais de plataformas
    que nunca deram match (ex.: BLL), não só ter essa opção escondida do
    filtro. todos_editais=True muda o universo da listagem pra "qualquer
    edital coletado", com ou sem Match."""
    db = _sessao()
    u = _usuario(db)
    ed_com = _edital_com_match(db, u, "ed-com", plataforma="ComprasNet")
    ed_sem = _edital_sem_match(db, "ed-sem", plataforma="BLL Compras")

    r = _listar(db, u, todos_editais=True)

    ids = {x["edital_id"] for x in r["resultados"]}
    assert ids == {ed_com.id, ed_sem.id}
    assert r["total"] == 2


def test_todos_editais_false_continua_restrito_a_match():
    """Garante que o comportamento padrão (todos_editais=False) não mudou."""
    db = _sessao()
    u = _usuario(db)
    _edital_com_match(db, u, "ed-com", plataforma="ComprasNet")
    _edital_sem_match(db, "ed-sem", plataforma="BLL Compras")

    r = _listar(db, u, todos_editais=False)

    assert r["total"] == 1
    assert r["resultados"][0]["plataforma"] == "ComprasNet"


def test_todos_editais_edital_sem_match_vem_com_campos_de_match_vazios():
    db = _sessao()
    u = _usuario(db)
    _edital_sem_match(db, "ed-sem", plataforma="BLL Compras")

    r = _listar(db, u, todos_editais=True)

    item = r["resultados"][0]
    assert item["match_id"] is None
    assert item["score"] is None
    assert item["nivel"] is None
    assert item["status"] is None
    assert item["lido"] is False
    assert item["interessante"] is False


def test_todos_editais_nao_mostra_match_de_outro_usuario():
    """O que faz um edital "ter match" em todos_editais=True continua sendo
    só o Match DESTE usuário -- não pode vazar score/status/lido de outro."""
    db = _sessao()
    u = _usuario(db)
    outro = Usuario(nome="Outro", email="outro2@t.com", senha_hash="x")
    db.add(outro)
    db.commit()
    _edital_com_match(db, outro, "ed-de-outro", plataforma="Licitanet")

    r = _listar(db, u, todos_editais=True)

    assert len(r["resultados"]) == 1
    assert r["resultados"][0]["match_id"] is None
    assert r["resultados"][0]["score"] is None


def test_todos_editais_com_filtro_de_plataforma_acha_edital_sem_match():
    """O cenário exato reportado: filtrar por uma plataforma (BLL) que só
    aparece em editais sem match não pode mais dar "nenhum edital
    encontrado" quando todos_editais está ligado."""
    db = _sessao()
    u = _usuario(db)
    ed_bll = _edital_sem_match(db, "ed-bll", plataforma="BLL Compras")
    _edital_com_match(db, u, "ed-cn", plataforma="ComprasNet")

    r = _listar(db, u, todos_editais=True, plataforma=["BLL Compras"])

    assert r["total"] == 1
    assert r["resultados"][0]["edital_id"] == ed_bll.id


def test_todos_editais_respeita_vista_ativos():
    """Continua respeitando o filtro de vista (ativo/encerrado) mesmo sem
    Match nenhum -- prazo_efetivo é sempre do Edital, nunca do Match."""
    db = _sessao()
    u = _usuario(db)
    hoje = date.today()
    _edital_sem_match(db, "ed-vencido", plataforma="BLL Compras",
                      data_abertura=hoje - timedelta(days=10))
    ed_ativo = _edital_sem_match(db, "ed-ativo", plataforma="BLL Compras",
                                 data_abertura=hoje + timedelta(days=5))

    r = _listar(db, u, todos_editais=True, vista="ativos")

    ids = {x["edital_id"] for x in r["resultados"]}
    assert ids == {ed_ativo.id}


def test_todos_editais_com_apenas_nao_lidos_nao_exclui_edital_sem_match():
    """Achado do code-reviewer: edital sem Match nenhum é, por definição,
    não lido também (nunca apareceu pro usuário) -- "Todos os editais" +
    "só não lidos" não pode escondê-lo, senão a combinação contradiz a
    própria promessa do checkbox "Todos os editais"."""
    db = _sessao()
    u = _usuario(db)
    ed_sem = _edital_sem_match(db, "ed-sem", plataforma="BLL Compras")
    ed_lido = _edital_com_match(db, u, "ed-lido", plataforma="ComprasNet")
    ed_lido_match = db.execute(select(Match).where(Match.edital_id == ed_lido.id)).scalar_one()
    ed_lido_match.lido = True
    db.commit()

    r = _listar(db, u, todos_editais=True, apenas_nao_lidos=True)

    ids = {x["edital_id"] for x in r["resultados"]}
    assert ids == {ed_sem.id}


def test_todos_editais_com_apenas_nao_lidos_false_nao_muda_comportamento_padrao():
    """Garante que o ajuste acima não afeta o modo normal (todos_editais
    desligado) -- Match sempre existe nesse modo, então a condição extra
    (IS NULL) nunca deveria bater com nada."""
    db = _sessao()
    u = _usuario(db)
    ed_lido = _edital_com_match(db, u, "ed-lido")
    ed_nao_lido = _edital_com_match(db, u, "ed-nao-lido")
    m_lido = db.execute(select(Match).where(Match.edital_id == ed_lido.id)).scalar_one()
    m_lido.lido = True
    db.commit()

    r = _listar(db, u, todos_editais=False, apenas_nao_lidos=True)

    ids = {x["edital_id"] for x in r["resultados"]}
    assert ids == {ed_nao_lido.id}


def test_todos_editais_com_busca_item_e_nao_lido_acha_edital_sem_match():
    """Cenário composto que o code-reviewer apontou como regressão em
    potencial: busca por item + todos_editais + apenas_nao_lidos não pode
    perder um edital sem Match que bate no termo buscado (antes desse
    fix, o filtro de "não lido" excluía silenciosamente qualquer edital
    sem Match, e sem_match fica desligado quando todos_editais=True)."""
    db = _sessao()
    u = _usuario(db)
    ed_sem = _edital_sem_match(db, "ed-sem", itens=["Grampeador de mesa"])

    r = _listar(db, u, todos_editais=True, apenas_nao_lidos=True, busca_item="grampeador")

    ids = {x["edital_id"] for x in r["resultados"]}
    assert ids == {ed_sem.id}
