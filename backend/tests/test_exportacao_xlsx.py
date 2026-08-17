"""
Achado real: os valores monetários saíam nas planilhas exportadas (cotação
e catálogo) sem nenhuma formatação — número cru tipo "253.37" em vez de
"R$ 253,37". Sem HTTP: chama a rota direto (mesmo padrão de
test_editais_filtros.py) e drena o StreamingResponse manualmente pra
inspecionar as células com openpyxl. Rode com:  cd backend && pytest
"""
import asyncio
import io

import openpyxl
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.main import cotacao_edital, exportar_produtos
from app.models import Base, Usuario, Edital, ItemEdital, Match, Produto


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


def _drenar(response) -> bytes:
    async def _ler():
        partes = []
        async for pedaco in response.body_iterator:
            partes.append(pedaco if isinstance(pedaco, bytes) else pedaco.encode())
        return b"".join(partes)
    return asyncio.run(_ler())


def test_cotacao_xlsx_formata_colunas_de_valor_como_moeda():
    db = _sessao()
    u = _usuario(db)
    ed = Edital(fonte="PNCP", id_externo="ed1", orgao="Orgao Teste",
               objeto="Aquisicao", uf="SP")
    db.add(ed)
    db.commit()
    prod = Produto(usuario_id=u.id, descricao="Papel A4", preco_custo=32.5)
    db.add(prod)
    db.commit()
    item = ItemEdital(edital_id=ed.id, numero=1, descricao="Papel A4 75g",
                      quantidade=10, valor_unitario=50.0)
    db.add(item)
    match = Match(usuario_id=u.id, edital_id=ed.id, score=0.9, nivel="forte",
                  detalhe={"itens": [{"item": 1, "produto_id": prod.id, "confianca": "alta"}]})
    db.add(match)
    db.commit()

    response = cotacao_edital(ed.id, itens=None, fretes=None, user=u, db=db)
    conteudo = _drenar(response)
    wb = openpyxl.load_workbook(io.BytesIO(conteudo))
    ws = wb.active

    # linha 5 é a 1ª linha de item (cabeçalho ocupa até a linha 4)
    for col in ("D", "E", "F", "G"):
        assert ws[f"{col}5"].number_format == "R$ #,##0.00", f"coluna {col} sem formatação de moeda"


def test_catalogo_xlsx_formata_preco_custo_e_venda_como_moeda():
    db = _sessao()
    u = _usuario(db)
    db.add(Produto(usuario_id=u.id, descricao="Papel A4", preco_custo=32.5, preco_venda=45.0))
    db.commit()

    response = exportar_produtos(user=u, db=db)
    conteudo = _drenar(response)
    wb = openpyxl.load_workbook(io.BytesIO(conteudo))
    ws = wb.active

    # linha 2 é a 1ª linha de produto (linha 1 é o cabeçalho)
    assert ws["J2"].number_format == "R$ #,##0.00"
    assert ws["K2"].number_format == "R$ #,##0.00"
