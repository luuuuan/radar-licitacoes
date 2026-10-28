"""
Pedido do usuário: botão "Exportar CSV" (aba Editais) -- queria os itens
pedidos pelo órgão na planilha (antes só saía a contagem itens_compativeis,
nunca os itens de verdade). Modal com 2 opções (mutuamente exclusivas):
"coluna" (1 linha por edital, itens concatenados numa coluna só) e "linha"
(1 linha por item). Ao testar a 1ª versão, o usuário encontrou um 2º achado
real: a consulta nunca filtrou por prazo -- editais já encerrados saíam
misturados com os ainda ativos na mesma planilha. Rode com:
cd backend && pytest
"""
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.main import export_csv
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


def _edital_com_match(db, usuario, id_externo, orgao="Orgao Teste", data_abertura=None,
                      data_encerramento=None, nivel="medio", score=0.5, itens_compativeis=0):
    ed = Edital(fonte="PNCP", id_externo=id_externo, orgao=orgao, objeto="Aquisicao",
                uf="SP", data_abertura=data_abertura, data_encerramento=data_encerramento)
    db.add(ed)
    db.commit()
    db.add(Match(usuario_id=usuario.id, edital_id=ed.id, score=score, nivel=nivel,
                itens_compativeis=itens_compativeis))
    db.commit()
    return ed


def _ler_csv(resp):
    """StreamingResponse monta o corpo com um async generator -- precisa
    drenar ele pra ler o conteúdo de verdade, igual o cliente HTTP faria."""
    import asyncio

    async def _drenar():
        partes = []
        async for chunk in resp.body_iterator:
            partes.append(chunk if isinstance(chunk, str) else chunk.decode())
        return "".join(partes)
    return asyncio.run(_drenar())


# --------- filtro de prazo (achado real, usuário testou a exportação) --------- #

def test_export_csv_nao_inclui_edital_com_prazo_encerrado():
    db = _sessao()
    u = _usuario(db)
    hoje = datetime.now()
    _edital_com_match(db, u, "ed-encerrado", orgao="Orgao Encerrado",
                      data_abertura=hoje - timedelta(days=30),
                      data_encerramento=hoje - timedelta(days=5))

    resp = export_csv(nivel=None, itens=None, user=u, db=db)
    conteudo = _ler_csv(resp)

    assert "Orgao Encerrado" not in conteudo


def test_export_csv_inclui_edital_ainda_recebendo_proposta():
    db = _sessao()
    u = _usuario(db)
    hoje = datetime.now()
    _edital_com_match(db, u, "ed-ativo", orgao="Orgao Ativo",
                      data_abertura=hoje - timedelta(days=5),
                      data_encerramento=hoje + timedelta(days=10))

    resp = export_csv(nivel=None, itens=None, user=u, db=db)
    conteudo = _ler_csv(resp)

    assert "Orgao Ativo" in conteudo


def test_export_csv_inclui_edital_ainda_aguardando_abertura():
    """"Ativo" (ver _condicoes_prazo_editais) inclui aguardando E recebendo
    -- um edital que nem abriu ainda também não pode ficar de fora."""
    db = _sessao()
    u = _usuario(db)
    hoje = datetime.now()
    _edital_com_match(db, u, "ed-aguardando", orgao="Orgao Aguardando",
                      data_abertura=hoje + timedelta(days=3), data_encerramento=None)

    resp = export_csv(nivel=None, itens=None, user=u, db=db)
    conteudo = _ler_csv(resp)

    assert "Orgao Aguardando" in conteudo


def test_export_csv_filtra_prazo_tambem_sem_data_encerramento_cadastrada():
    """Sem data_encerramento, cai pro fallback por data_abertura (mesma
    regra de _condicoes_prazo_editais/_status_prazo_edital) -- continua
    filtrando, não passa a exportar tudo só por faltar o dado."""
    db = _sessao()
    u = _usuario(db)
    hoje = datetime.now()
    _edital_com_match(db, u, "ed-sem-fim-velho", orgao="Orgao Sem Fim Velho",
                      data_abertura=hoje - timedelta(days=20), data_encerramento=None)

    resp = export_csv(nivel=None, itens=None, user=u, db=db)
    conteudo = _ler_csv(resp)

    assert "Orgao Sem Fim Velho" not in conteudo


# --------- itens pedidos (coluna / linha) --------- #

def test_export_csv_sem_parametro_itens_nao_muda_colunas_de_antes():
    db = _sessao()
    u = _usuario(db)
    hoje = datetime.now()
    ed = _edital_com_match(db, u, "ed1", data_abertura=hoje - timedelta(days=1),
                           data_encerramento=hoje + timedelta(days=5))
    db.add(ItemEdital(edital_id=ed.id, numero=1, descricao="Caneta azul",
                      quantidade=100, valor_unitario=2.5, unidade_medida="Caixa"))
    db.commit()

    resp = export_csv(nivel=None, itens=None, user=u, db=db)
    conteudo = _ler_csv(resp)

    linhas = conteudo.strip().splitlines()
    assert linhas[0] == "nivel;score;itens_compativeis;orgao;uf;municipio;modalidade;valor_estimado;data_encerramento;objeto;link"
    assert len(linhas) == 2   # cabeçalho + 1 edital, nunca 1 linha por item
    assert "Caneta azul" not in conteudo


def test_export_csv_itens_coluna_concatena_mantendo_uma_linha_por_edital():
    db = _sessao()
    u = _usuario(db)
    hoje = datetime.now()
    ed = _edital_com_match(db, u, "ed1", data_abertura=hoje - timedelta(days=1),
                           data_encerramento=hoje + timedelta(days=5))
    db.add(ItemEdital(edital_id=ed.id, numero=1, descricao="Caneta azul",
                      quantidade=100, valor_unitario=2.5, unidade_medida="Caixa"))
    db.add(ItemEdital(edital_id=ed.id, numero=2, descricao="Papel A4",
                      quantidade=50, valor_unitario=30.0, unidade_medida="Pacote"))
    db.commit()

    resp = export_csv(nivel=None, itens="coluna", user=u, db=db)
    conteudo = _ler_csv(resp)
    linhas = conteudo.strip().splitlines()

    assert linhas[0].endswith(";itens_pedidos")
    assert len(linhas) == 2   # cabeçalho + 1 edital -- itens concatenados, não quebrados
    assert "100.0x Caneta azul" in linhas[1]
    assert "50.0x Papel A4" in linhas[1]


def test_export_csv_itens_linha_quebra_uma_linha_por_item():
    db = _sessao()
    u = _usuario(db)
    hoje = datetime.now()
    ed = _edital_com_match(db, u, "ed1", orgao="Orgao X",
                           data_abertura=hoje - timedelta(days=1),
                           data_encerramento=hoje + timedelta(days=5))
    db.add(ItemEdital(edital_id=ed.id, numero=1, descricao="Caneta azul",
                      quantidade=100, valor_unitario=2.5, unidade_medida="Caixa"))
    db.add(ItemEdital(edital_id=ed.id, numero=2, descricao="Papel A4",
                      quantidade=50, valor_unitario=30.0, unidade_medida="Pacote"))
    db.commit()

    resp = export_csv(nivel=None, itens="linha", user=u, db=db)
    conteudo = _ler_csv(resp)
    linhas = conteudo.strip().splitlines()

    assert len(linhas) == 3   # cabeçalho + 2 itens -- edital se repete nas colunas
    assert linhas[1].startswith("medio;0.5;0;Orgao X;")
    assert linhas[2].startswith("medio;0.5;0;Orgao X;")
    assert "Caneta azul" in linhas[1] and "Papel A4" in linhas[2]


def test_export_csv_itens_linha_edital_sem_nenhum_item_ainda_sai_uma_linha():
    """Edital sem ItemEdital nenhum cadastrado (achado real, ver mesma
    cautela em outras exportações): não pode desaparecer da planilha só
    porque não tem item pra detalhar."""
    db = _sessao()
    u = _usuario(db)
    hoje = datetime.now()
    _edital_com_match(db, u, "ed-sem-item", orgao="Orgao Sem Item",
                      data_abertura=hoje - timedelta(days=1),
                      data_encerramento=hoje + timedelta(days=5))

    resp = export_csv(nivel=None, itens="linha", user=u, db=db)
    conteudo = _ler_csv(resp)

    assert "Orgao Sem Item" in conteudo


def test_export_csv_itens_ordenados_por_numero():
    db = _sessao()
    u = _usuario(db)
    hoje = datetime.now()
    ed = _edital_com_match(db, u, "ed1", data_abertura=hoje - timedelta(days=1),
                           data_encerramento=hoje + timedelta(days=5))
    db.add(ItemEdital(edital_id=ed.id, numero=3, descricao="Terceiro", quantidade=1, valor_unitario=1.0))
    db.add(ItemEdital(edital_id=ed.id, numero=1, descricao="Primeiro", quantidade=1, valor_unitario=1.0))
    db.add(ItemEdital(edital_id=ed.id, numero=2, descricao="Segundo", quantidade=1, valor_unitario=1.0))
    db.commit()

    resp = export_csv(nivel=None, itens="linha", user=u, db=db)
    linhas = _ler_csv(resp).strip().splitlines()

    assert "Primeiro" in linhas[1]
    assert "Segundo" in linhas[2]
    assert "Terceiro" in linhas[3]


def test_export_csv_nivel_continua_filtrando_junto_com_itens():
    db = _sessao()
    u = _usuario(db)
    hoje = datetime.now()
    _edital_com_match(db, u, "ed-forte", orgao="Orgao Forte", nivel="forte",
                      data_abertura=hoje - timedelta(days=1), data_encerramento=hoje + timedelta(days=5))
    _edital_com_match(db, u, "ed-fraco", orgao="Orgao Fraco", nivel="fraco",
                      data_abertura=hoje - timedelta(days=1), data_encerramento=hoje + timedelta(days=5))

    resp = export_csv(nivel="forte", itens="coluna", user=u, db=db)
    conteudo = _ler_csv(resp)

    assert "Orgao Forte" in conteudo
    assert "Orgao Fraco" not in conteudo
