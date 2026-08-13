"""
Achado real: abrir a aba "Análise por IA" de um edital JÁ analisado disparava
2 chamadas de IA de novo (verificação de documentos + comparação de
catálogo) a cada abertura, mesmo sem nada ter mudado — lento e sem o usuário
ter pedido. As duas checagens agora ficam cacheadas por (edital, usuário) em
AnaliseIAExtras, versionadas por Usuario.versao_catalogo/versao_documentos
(incrementados nos endpoints de criar/editar/excluir produto e documento).
Sem mudança de versão -> reusa o cache, sem chamar IA. Com mudança -> devolve
o resultado antigo marcado como desatualizado (não gasta IA sozinho); só
forcar=True (botão "Realizar nova análise") recalcula de fato.
Rode com:  cd backend && pytest
"""
import json
from datetime import date
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, Usuario, Edital, ItemEdital, Produto, Documento, AnaliseIAExtras
from app.main import (
    _anexar_verificacao_ia_documentos, _anexar_comparacao_catalogo_ia,
    criar_produto, atualizar_produto, remover_produto, ProdutoIn,
    remover_documento,
)


def _sessao():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def _semear(db):
    u = Usuario(nome="Teste", email="t@t.com", senha_hash="x")
    db.add(u)
    db.commit()
    db.add(Documento(usuario_id=u.id, nome="Certidao", data_validade=date(2030, 1, 1),
                     texto_extraido="CND valida", ativo=True))
    p = Produto(usuario_id=u.id, descricao="Caneta azul")
    db.add(p)
    ed = Edital(fonte="PNCP", id_externo="extras1", orgao="Teste", uf="SP", objeto="Material de escritorio")
    db.add(ed)
    db.commit()
    db.add(ItemEdital(edital_id=ed.id, numero=1, descricao="Caneta azul", valor_unitario=2.0))
    db.commit()
    return u, ed, p


_resposta_docs = json.dumps({"itens": [{"exigido": "CND", "atendido": True, "documento": "Certidao", "observacao": ""}]})


def _resposta_catalogo(produto_id):
    return json.dumps({"itens": [
        {"numero_item": 1, "candidatos": [{"produto_id": produto_id, "justificativa": "ok"}]},
    ]})


def _resultado_base(ed):
    return {"status": "ok", "objeto": ed.objeto, "requisitos_tecnicos": ["Garantia minima"],
           "documentos_habilitacao": {}}


# ---------------------- verificação de documentos ---------------------- #

def test_verificacao_documentos_1a_vez_chama_ia_e_grava_cache():
    db = _sessao()
    u, ed, p = _semear(db)
    with patch("app.analise_edital._gerar") as mock_gerar:
        mock_gerar.return_value = (_resposta_docs, "ok")
        out = _anexar_verificacao_ia_documentos(_resultado_base(ed), ed, u, db, "fake-key")
    assert mock_gerar.call_count == 1
    assert out["verificacao_documentos_ia"]["status"] == "ok"
    cache = db.query(AnaliseIAExtras).filter_by(usuario_id=u.id, edital_id=ed.id).first()
    assert cache is not None
    assert cache.versao_documentos_calc == u.versao_documentos == 0


def test_verificacao_documentos_sem_mudanca_reusa_cache_sem_chamar_ia():
    db = _sessao()
    u, ed, p = _semear(db)
    with patch("app.analise_edital._gerar") as mock_gerar:
        mock_gerar.return_value = (_resposta_docs, "ok")
        _anexar_verificacao_ia_documentos(_resultado_base(ed), ed, u, db, "fake-key")
        out2 = _anexar_verificacao_ia_documentos(_resultado_base(ed), ed, u, db, "fake-key")
    assert mock_gerar.call_count == 1, "a 2a chamada deveria ter reusado o cache"
    assert out2["verificacao_documentos_ia"]["status"] == "ok"
    assert "verificacao_documentos_desatualizada" not in out2


def test_verificacao_documentos_apos_editar_documento_devolve_antigo_marcado_desatualizado():
    db = _sessao()
    u, ed, p = _semear(db)
    with patch("app.analise_edital._gerar") as mock_gerar:
        mock_gerar.return_value = (_resposta_docs, "ok")
        _anexar_verificacao_ia_documentos(_resultado_base(ed), ed, u, db, "fake-key")
        u.versao_documentos += 1   # simula editar/criar/excluir um documento
        db.commit()
        out2 = _anexar_verificacao_ia_documentos(_resultado_base(ed), ed, u, db, "fake-key")
    assert mock_gerar.call_count == 1, "não deve gastar IA sozinho só porque a versão mudou"
    assert out2["verificacao_documentos_ia"]["status"] == "ok", "devolve o resultado anterior, não vazio"
    assert out2["verificacao_documentos_desatualizada"] is True


def test_verificacao_documentos_forcar_recalcula_mesmo_com_cache_valido():
    db = _sessao()
    u, ed, p = _semear(db)
    with patch("app.analise_edital._gerar") as mock_gerar:
        mock_gerar.return_value = (_resposta_docs, "ok")
        _anexar_verificacao_ia_documentos(_resultado_base(ed), ed, u, db, "fake-key")
        out2 = _anexar_verificacao_ia_documentos(_resultado_base(ed), ed, u, db, "fake-key", forcar=True)
    assert mock_gerar.call_count == 2, "forcar=True deve gastar uma nova chamada de IA"
    assert "verificacao_documentos_desatualizada" not in out2


# ---------------------- comparação de catálogo ---------------------- #

def test_comparacao_catalogo_sem_mudanca_reusa_cache_sem_chamar_ia():
    db = _sessao()
    u, ed, p = _semear(db)
    with patch("app.analise_edital._gerar") as mock_gerar:
        mock_gerar.return_value = (_resposta_catalogo(p.id), "ok")
        _anexar_comparacao_catalogo_ia(_resultado_base(ed), ed, u, db, "fake-key")
        out2 = _anexar_comparacao_catalogo_ia(_resultado_base(ed), ed, u, db, "fake-key")
    assert mock_gerar.call_count == 1
    assert len(out2["comparacao_catalogo_ia"]["itens"]) == 1
    assert "comparacao_catalogo_desatualizada" not in out2


def test_comparacao_catalogo_apos_mudar_catalogo_devolve_antigo_marcado_desatualizado():
    db = _sessao()
    u, ed, p = _semear(db)
    with patch("app.analise_edital._gerar") as mock_gerar:
        mock_gerar.return_value = (_resposta_catalogo(p.id), "ok")
        _anexar_comparacao_catalogo_ia(_resultado_base(ed), ed, u, db, "fake-key")
        u.versao_catalogo += 1   # simula criar/editar/excluir um produto
        db.commit()
        out2 = _anexar_comparacao_catalogo_ia(_resultado_base(ed), ed, u, db, "fake-key")
    assert mock_gerar.call_count == 1, "não deve gastar IA sozinho só porque a versão mudou"
    assert len(out2["comparacao_catalogo_ia"]["itens"]) == 1, "devolve o resultado anterior, não vazio"
    assert out2["comparacao_catalogo_desatualizada"] is True


def test_comparacao_catalogo_forcar_recalcula_mesmo_com_cache_valido():
    db = _sessao()
    u, ed, p = _semear(db)
    with patch("app.analise_edital._gerar") as mock_gerar:
        mock_gerar.return_value = (_resposta_catalogo(p.id), "ok")
        _anexar_comparacao_catalogo_ia(_resultado_base(ed), ed, u, db, "fake-key")
        out2 = _anexar_comparacao_catalogo_ia(_resultado_base(ed), ed, u, db, "fake-key", forcar=True)
    assert mock_gerar.call_count == 2
    assert "comparacao_catalogo_desatualizada" not in out2


# ---------------------- versões bumped pelos endpoints de CRUD ---------------------- #

def test_criar_atualizar_excluir_produto_incrementa_versao_catalogo():
    db = _sessao()
    u, ed, p = _semear(db)
    versao_inicial = u.versao_catalogo
    dados = ProdutoIn(descricao="Lapis HB")
    r = criar_produto(dados, user=u, db=db)
    assert u.versao_catalogo == versao_inicial + 1

    atualizar_produto(r["id"], ProdutoIn(descricao="Lapis HB 2"), user=u, db=db)
    assert u.versao_catalogo == versao_inicial + 2

    remover_produto(r["id"], user=u, db=db)
    assert u.versao_catalogo == versao_inicial + 3


def test_excluir_documento_incrementa_versao_documentos():
    db = _sessao()
    u, ed, p = _semear(db)
    doc = db.query(Documento).filter_by(usuario_id=u.id).first()
    versao_inicial = u.versao_documentos
    remover_documento(doc.id, user=u, db=db)
    assert u.versao_documentos == versao_inicial + 1
