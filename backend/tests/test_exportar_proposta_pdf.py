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
