"""
Teste do endpoint GET /api/editais/{id}/proposta.pdf — chama a função da
rota direto (sem HTTP, mesmo padrão dos outros testes de main.py). O CSV
antigo (/api/editais/{id}/proposta.csv) foi removido de propósito, não tem
teste de regressão pra ele. Rode com:  cd backend && pytest
"""
import asyncio

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import auth
from app.main import exportar_proposta_pdf, _dados_remetente
from app.models import Base, Usuario, Edital, ItemEdital


async def _drenar(body_iterator):
    partes = []
    async for pedaco in body_iterator:
        partes.append(pedaco)
    return b"".join(partes)


def _sessao():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def test_dados_remetente_decifra_endereco_e_empresa():
    db = _sessao()
    u = Usuario(nome="Empresa X", email="x@t.com", senha_hash="x",
               doc_cifrado=auth.cifrar("12345678000199"),
               endereco_cifrado=auth.cifrar('{"cidade": "São Paulo", "uf": "SP"}'),
               dados_empresa_cifrado=auth.cifrar('{"telefone": "11999999999"}'))
    r = _dados_remetente(u)
    assert r["nome"] == "Empresa X"
    assert r["documento"] == "12345678000199"
    assert r["endereco"]["cidade"] == "São Paulo"
    assert r["empresa"]["telefone"] == "11999999999"


def test_dados_remetente_sem_nada_cadastrado_nao_quebra():
    db = _sessao()
    u = Usuario(nome="Fulano", email="f@t.com", senha_hash="x")
    r = _dados_remetente(u)
    assert r["endereco"] == {}
    assert r["empresa"] == {}
    assert r["logo_base64"] is None


def test_exportar_proposta_pdf_retorna_pdf_valido():
    db = _sessao()
    u = Usuario(nome="Empresa Teste", email="e@t.com", senha_hash="x")
    db.add(u)
    db.commit()
    ed = Edital(fonte="PNCP", id_externo="ed1", orgao="Orgao Teste",
               objeto="Aquisicao", uf="SP")
    db.add(ed)
    db.commit()
    db.add(ItemEdital(edital_id=ed.id, numero=1, descricao="Papel A4",
                      quantidade=10, valor_unitario=25.0))
    db.commit()

    resp = exportar_proposta_pdf(ed.id, user=u, db=db)

    assert resp.media_type == "application/pdf"
    corpo = asyncio.run(_drenar(resp.body_iterator))
    assert corpo[:4] == b"%PDF"


def _texto_do_pdf(corpo: bytes) -> str:
    import io
    import pypdf
    leitor = pypdf.PdfReader(io.BytesIO(corpo))
    return "\n".join(p.extract_text() or "" for p in leitor.pages)


def test_pdf_mostra_numero_do_item_na_tabela():
    """Pedido do usuário: número do item (conforme o edital) visível na
    proposta, tanto na aba quanto no PDF exportado."""
    db = _sessao()
    u = Usuario(nome="Empresa Teste", email="e2@t.com", senha_hash="x")
    db.add(u)
    db.commit()
    ed = Edital(fonte="PNCP", id_externo="ed2", orgao="Orgao Teste",
               objeto="Aquisicao", uf="SP")
    db.add(ed)
    db.commit()
    db.add(ItemEdital(edital_id=ed.id, numero=24, descricao="Papel A4",
                      quantidade=10, valor_unitario=25.0))
    db.commit()

    resp = exportar_proposta_pdf(ed.id, user=u, db=db)
    corpo = asyncio.run(_drenar(resp.body_iterator))
    texto = _texto_do_pdf(corpo)
    assert "24" in texto


def test_pdf_nao_quebra_com_item_sem_numero():
    """Achado real: o placeholder pra item sem número usava um travessão
    ("—"), fora do conjunto de caracteres da fonte padrão do PDF (helvetica,
    latin-1) -- quebrava a exportação inteira com FPDFUnicodeEncodingException
    pra qualquer proposta com item sem número (ex.: adicionado manualmente
    antes do campo "numero" existir)."""
    from app.models import Proposta
    db = _sessao()
    u = Usuario(nome="Empresa Teste", email="e3@t.com", senha_hash="x")
    db.add(u)
    db.commit()
    ed = Edital(fonte="PNCP", id_externo="ed3", orgao="Orgao Teste",
               objeto="Aquisicao", uf="SP")
    db.add(ed)
    db.commit()
    db.add(Proposta(edital_id=ed.id, usuario_id=u.id, itens=[
        {"descricao": "Item digitado à mão, sem número", "quantidade": 1,
         "custo_unit": 0, "preco_unit": 10.0},
    ]))
    db.commit()

    resp = exportar_proposta_pdf(ed.id, user=u, db=db)   # não pode lançar exceção

    corpo = asyncio.run(_drenar(resp.body_iterator))
    assert corpo[:4] == b"%PDF"


def test_pdf_nao_quebra_com_caracteres_fora_do_latin1_em_qualquer_texto_livre():
    """Achado do agente debugger: o fix acima só cobria UM placeholder
    hardcoded ("—" pra item sem número) -- qualquer outro texto livre
    (descrição de item, nome do órgão, observação, dados da empresa) com
    aspas curvas/travessão/bullet/emoji quebrava do mesmo jeito, sem
    proteção nenhuma. Um caractere de cada família problemática, espalhado
    em vários campos diferentes."""
    from app.models import Proposta
    db = _sessao()
    u = Usuario(nome="Empresa Ltda.", email="e4@t.com", senha_hash="x")
    db.add(u)
    db.commit()
    ed = Edital(fonte="PNCP", id_externo="ed4", orgao="Prefeitura de São Paulo — Secretaria",
               objeto="Aquisição", uf="SP")
    db.add(ed)
    db.commit()
    db.add(Proposta(edital_id=ed.id, usuario_id=u.id,
                    observacoes="Entrega em até 5 dias úteis • sem exceção",
                    itens=[
                        {"numero": 1, "descricao": "Caneta “gel” azul — ponta 0,7mm",
                         "quantidade": 2, "custo_unit": 1.0, "preco_unit": 3.5},
                    ]))
    db.commit()

    resp = exportar_proposta_pdf(ed.id, user=u, db=db)   # não pode lançar exceção

    corpo = asyncio.run(_drenar(resp.body_iterator))
    assert corpo[:4] == b"%PDF"
    texto = _texto_do_pdf(corpo)
    # o texto sobrevive (sem os caracteres fora do latin-1), não sai em branco
    assert "gel" in texto
    assert "ponta" in texto
