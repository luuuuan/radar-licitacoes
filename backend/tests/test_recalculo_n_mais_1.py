"""
Achado real (auditoria de banco pedida pelo usuário): _gerar_matches_usuario
(app/service.py) carregava os editais sem eager load de Edital.itens -- o
loop que monta itens_edt (`for i in ed.itens`) disparava 1 SELECT em
itens_edital POR EDITAL. Com recalcular_todos=True (base inteira, dezenas de
milhares de editais numa conta ativa), isso é uma quantidade enorme de
round-trips evitáveis por rodada. Mesmo padrão de teste já usado em
test_editais_filtros.py::test_sem_match_nao_faz_n_mais_1_pra_carregar_itens
pra essa mesma relação, noutra rota.

Achado real #2 (auditoria do agente database-optimizer, numa 1ª versão deste
fix): um selectinload sozinho só protege até o 1º COMMIT PARCIAL do loop (a
cada 200 editais, ver service.py) -- expire_on_commit=True (padrão do
SessionLocal) marca todo objeto da sessão como desatualizado a cada commit,
inclusive os editais ainda não processados, e o próximo acesso a `ed.itens`
cai de volta no lazy load padrão (1 SELECT por edital expirado). Por isso
este teste usa MAIS de 200 editais (o limiar real do commit parcial) -- com
menos que isso, o teste passava mesmo com essa regressão presente, sem
exercitar o cenário que importa.
Rode com: cd backend && pytest
"""
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.models import Base, Usuario, Edital, ItemEdital
from app.service import _gerar_matches_usuario

N_EDITAIS = 250   # > 200: atravessa o limiar do commit parcial do loop


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


def _muitos_editais_com_itens(db, n):
    for i in range(n):
        ed = Edital(fonte="PNCP", id_externo=f"ed{i}", orgao="Orgao Teste",
                   objeto="Aquisicao qualquer", uf="SP")
        db.add(ed)
        db.commit()
        db.add_all([
            ItemEdital(edital_id=ed.id, numero=1, descricao="Item A"),
            ItemEdital(edital_id=ed.id, numero=2, descricao="Item B"),
        ])
        db.commit()


def test_gerar_matches_nao_faz_n_mais_1_pra_carregar_itens_do_edital():
    db = _sessao()
    u = _usuario(db)
    _muitos_editais_com_itens(db, N_EDITAIS)

    queries = []
    engine = db.get_bind()

    def _contar(conn, cursor, statement, parameters, context, executemany):
        queries.append(statement)

    event.listen(engine, "before_cursor_execute", _contar)
    try:
        resumo = _gerar_matches_usuario(db, u, recalcular_todos=True, forcar_usar_ia=False)
    finally:
        event.remove(engine, "before_cursor_execute", _contar)

    assert resumo["editais"] == N_EDITAIS
    # mesmo critério de test_sem_match_nao_faz_n_mais_1_pra_carregar_itens:
    # uma query que carrega LINHAS de itens_edital começa com esse SELECT
    # específico -- seja o lote único do eager load, seja (no caso de N+1)
    # uma repetida por edital (ou, no caso do achado #2, repetida a partir
    # do edital de índice 200, quando o 1º commit parcial expira a sessão).
    selects_itens = [q for q in queries if q.strip().lower().startswith("select itens_edital.")]
    assert len(selects_itens) <= 1, (
        f"esperava no máximo 1 SELECT de linhas de itens_edital (eager load em lote), "
        f"achou {len(selects_itens)} (N+1: um por edital expirado após o commit parcial)"
    )
