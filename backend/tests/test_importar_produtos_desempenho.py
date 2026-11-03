"""
Achado real (usuário reportou erro reimportando o próprio catálogo
exportado, ~3600 linhas, contra o Postgres de produção): 2 causas somadas
de round-trip de rede demais por linha.
(1) A busca de "existente" (por id OU por descrição) era 1 SELECT por
linha -- trocado por carregar todo o catálogo do usuário de uma vez e casar
em memória (ver produtos_por_id/produtos_por_desc em importar_produtos).
(2) Mesmo com (1), o db.commit() periódico (a cada _LOTE_COMMIT_IMPORT
linhas) expira todo objeto da sessão por padrão -- tocar de novo num
produto do dict nos lotes seguintes reintroduzia o MESMO SELECT por linha
por outro caminho (recarga automática do objeto expirado). Corrigido
desligando expire_on_commit pela duração do loop, mesmo padrão já usado em
service.py:_gerar_matches_usuario. Rode com:  cd backend && pytest
"""
import asyncio
import io

import openpyxl
from fastapi import UploadFile
from starlette.datastructures import Headers
from sqlalchemy import create_engine, event, select, func
from sqlalchemy.orm import sessionmaker

from app.main import importar_produtos, _LOTE_COMMIT_IMPORT
from app.models import Base, Usuario, Produto


def _sessao():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return engine, Session()


def _usuario(db, email="empresa@t.com"):
    u = Usuario(nome="Empresa Teste", email=email, senha_hash="x")
    db.add(u)
    db.commit()
    return u


def _upload_planilha(linhas, cabec=("id", "descricao", "preco_custo", "preco_venda")):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(list(cabec))
    for linha in linhas:
        ws.append(list(linha))
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return UploadFile(buf, filename="produtos.xlsx", headers=Headers({
        "content-type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}))


def _contar_selects(engine):
    """Context manager simples: devolve uma função que lê a contagem de
    SELECTs disparados contra `engine` desde que foi chamada."""
    contador = {"n": 0}

    @event.listens_for(engine, "before_cursor_execute")
    def _conta(conn, cursor, statement, parameters, context, executemany):
        if statement.strip().upper().startswith("SELECT"):
            contador["n"] += 1
    return contador


def test_reimportar_catalogo_todo_por_id_nao_faz_1_select_por_linha():
    """O caso real que motivou a correção: reimportar o PRÓPRIO catálogo
    exportado (toda linha casa por id, nenhuma é criada) não pode mais
    disparar ~1 SELECT por linha -- só o carregamento em lote do catálogo
    (1 SELECT) e o de fornecedores (1 SELECT), não importa quantas linhas
    a planilha tenha."""
    engine, db = _sessao()
    u = _usuario(db)
    n_produtos = 50
    produtos = [Produto(usuario_id=u.id, descricao=f"Produto {i}", preco_custo=1.0)
               for i in range(n_produtos)]
    db.add_all(produtos)
    db.commit()
    # monta as linhas da planilha ANTES de ligar o contador -- ler p.id/
    # p.descricao aqui já dispara 1 SELECT de recarga por objeto (expirado
    # pelo commit acima, expire_on_commit é True nesta sessão de teste, só
    # dentro de importar_produtos que fica False) -- contar a partir daqui
    # mediria esse ruído do próprio teste, não o código em si.
    linhas = [(p.id, p.descricao, "9,99", "19,99") for p in produtos]

    contador = _contar_selects(engine)
    arquivo = _upload_planilha(linhas)
    resultado = asyncio.run(importar_produtos(arquivo=arquivo, user=u, db=db))

    assert resultado["atualizados"] == n_produtos
    assert resultado["criados"] == 0
    # bem menos que 1 por linha -- só o carregamento em lote (catálogo +
    # fornecedores) e pouquíssimas consultas fixas, não escala com n_produtos.
    assert contador["n"] < 10, f"disparou {contador['n']} SELECTs pra {n_produtos} linhas"


def test_reimportar_catalogo_atravessando_commits_parciais_continua_casando_por_id():
    """Achado real (2ª parte): mesmo com o carregamento em lote, o
    db.commit() a cada _LOTE_COMMIT_IMPORT linhas expirava os objetos e
    reintroduzia o SELECT por linha nos lotes seguintes (via recarga
    automática ao tocar o objeto expirado). Usa mais linhas que
    _LOTE_COMMIT_IMPORT pra atravessar pelo menos 1 commit parcial de
    verdade, e confere que tanto o resultado quanto a contagem de SELECTs
    continuam corretos depois dele."""
    engine, db = _sessao()
    u = _usuario(db)
    n_produtos = _LOTE_COMMIT_IMPORT + 50   # força pelo menos 1 commit parcial no meio
    produtos = [Produto(usuario_id=u.id, descricao=f"Produto {i}", preco_custo=1.0)
               for i in range(n_produtos)]
    db.add_all(produtos)
    db.commit()
    # mesmo motivo do teste anterior: monta as linhas ANTES de ligar o
    # contador, pra não medir o SELECT de recarga do próprio setup do teste.
    linhas = [(p.id, p.descricao, "9,99", "19,99") for p in produtos]

    contador = _contar_selects(engine)
    arquivo = _upload_planilha(linhas)
    resultado = asyncio.run(importar_produtos(arquivo=arquivo, user=u, db=db))

    assert resultado["atualizados"] == n_produtos
    assert resultado["criados"] == 0
    assert resultado["erros"] == []
    assert contador["n"] < 10, f"disparou {contador['n']} SELECTs pra {n_produtos} linhas"
    # confere o dado de verdade, não só o resumo -- todo produto recebeu o
    # preço novo, inclusive os processados ANTES do commit parcial.
    total_com_preco_novo = db.scalar(
        select(func.count(Produto.id)).where(Produto.usuario_id == u.id, Produto.preco_custo == 9.99))
    assert total_com_preco_novo == n_produtos


def test_expire_on_commit_e_restaurado_depois_da_importacao():
    """A correção desliga expire_on_commit só pela DURAÇÃO do loop --
    não pode escapar pra fora da função e mudar o comportamento de outras
    rotas que usam a mesma sessão depois."""
    engine, db = _sessao()
    u = _usuario(db)
    original = db.expire_on_commit
    assert original is True   # padrão do SQLAlchemy, igual a produção

    arquivo = _upload_planilha([("", "Produto Novo", "1,00", "2,00")])
    asyncio.run(importar_produtos(arquivo=arquivo, user=u, db=db))

    assert db.expire_on_commit is original


def test_duas_linhas_com_a_mesma_descricao_nova_nao_duplicam_produto():
    """Achado real (ajustado quando a importação passou a processar em
    lote, não mais linha a linha): a 2ª linha não acha a 1ª em
    produtos_por_desc -- esse dict só é atualizado depois que o LOTE é
    processado (ver _processar_lote), e as duas estão no MESMO lote ainda
    não processado. Em vez de criar produto duplicado, mescla os dados da
    2ª linha na 1ª (que ainda nem foi criada de verdade) -- só sobrescreve
    o que veio preenchido, mesma regra de sempre. Resultado: 1 produto só,
    com os valores da ÚLTIMA linha (5,00/10,00), nunca dois."""
    db = _sessao()[1]
    u = _usuario(db)

    arquivo = _upload_planilha([
        ("", "Produto Duplicado Na Planilha", "1,00", "2,00"),
        ("", "Produto Duplicado Na Planilha", "5,00", "10,00"),
    ])
    resultado = asyncio.run(importar_produtos(arquivo=arquivo, user=u, db=db))

    assert resultado["criados"] == 1
    assert resultado["atualizados"] == 0   # mesclada na 1ª, não é uma 2ª linha separada
    produto = db.execute(select(Produto).where(Produto.usuario_id == u.id)).scalar_one()
    assert produto.preco_custo == 5.0   # valor da última linha venceu, mesma regra de sempre
    total = db.scalar(select(func.count(Produto.id)).where(Produto.usuario_id == u.id))
    assert total == 1
