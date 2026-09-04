"""
Testes dos filtros de faixa de valor (valor_min/valor_max) e busca por item
(busca_item) em GET /api/editais. Banco sqlite em memória, sem HTTP — chama
a função da rota diretamente com os parâmetros já resolvidos (o jeito que o
FastAPI resolveria via Query(...), sem depender da injeção de dependência).
Rode com:  cd backend && pytest
"""
from datetime import date, timedelta

from sqlalchemy import create_engine
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
                      data_abertura=None, data_encerramento=None):
    ed = Edital(fonte="PNCP", id_externo=id_externo, orgao="Orgao Teste",
                objeto="Aquisicao", uf="SP", valor_estimado=valor_estimado,
                data_abertura=data_abertura, data_encerramento=data_encerramento)
    db.add(ed)
    db.commit()
    for numero, descricao in enumerate(itens or [], start=1):
        db.add(ItemEdital(edital_id=ed.id, numero=numero, descricao=descricao))
    db.add(Match(usuario_id=usuario.id, edital_id=ed.id, score=0.5, nivel="medio"))
    db.commit()
    return ed


def _edital_sem_match(db, id_externo, itens=None, data_abertura=None, uf="SP",
                      valor_estimado=None, data_encerramento=None):
    ed = Edital(fonte="PNCP", id_externo=id_externo, orgao="Orgao Sem Match",
               objeto="Aquisicao", uf=uf, data_abertura=data_abertura,
               valor_estimado=valor_estimado, data_encerramento=data_encerramento)
    db.add(ed)
    db.commit()
    for numero, descricao in enumerate(itens or [], start=1):
        db.add(ItemEdital(edital_id=ed.id, numero=numero, descricao=descricao))
    db.commit()
    return ed


def _listar(db, user, **kwargs):
    padrao = dict(nivel=None, uf=None, status=None, vista="ativos",
                  apenas_nao_lidos=False, apenas_interessantes=False, hoje=False,
                  tipo="todos", valor_min=None, valor_max=None,
                  data_de=None, data_ate=None, busca_item=None,
                  pagina=1, por_pagina=50)
    padrao.update(kwargs)
    return listar_editais(user=user, db=db, **padrao)


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
