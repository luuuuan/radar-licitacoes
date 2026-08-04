"""
Testes do cancelamento cooperativo da análise por IA (sem HTTP — chama
_rodar_extras_ia diretamente, mockando as chamadas de IA). A análise por IA
não roda em BackgroundTasks feito coleta/recálculo (é uma request síncrona
comum), então só existe um checkpoint ENTRE as etapas — nunca no meio de uma
chamada já em voo. Rode com:  cd backend && pytest
"""
import json
from datetime import date
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, Usuario, Edital, ItemEdital, Produto, Documento
from app.main import _rodar_extras_ia


def _sessao():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def _semear(db):
    u = Usuario(nome="Teste", email="t@t.com", senha_hash="x")
    db.add(u); db.commit()
    db.add(Documento(usuario_id=u.id, nome="Certidao", data_validade=date(2030, 1, 1),
                     texto_extraido="CND valida", ativo=True))
    db.add(Produto(usuario_id=u.id, descricao="Caneta azul"))
    ed = Edital(fonte="PNCP", id_externo="cancel1", orgao="Teste", uf="SP", objeto="Material de escritorio")
    db.add(ed); db.commit()
    db.add(ItemEdital(edital_id=ed.id, numero=1, descricao="Caneta azul", valor_unitario=2.0))
    db.commit()
    return u, ed


_resposta_docs = json.dumps({"itens": [{"exigido": "CND", "atendido": True, "documento": "Certidao", "observacao": ""}]})
_resposta_catalogo = json.dumps({"itens": [{"numero_item": 1, "produto_id": 1, "justificativa": "ok"}]})


def test_cancelado_antes_de_comecar_nao_chama_ia_nenhuma():
    db = _sessao()
    u, ed = _semear(db)
    resultado = {"status": "ok", "objeto": ed.objeto, "requisitos_tecnicos": [], "documentos_habilitacao": {}}
    with patch("app.analise_edital._gerar") as mock_gerar:
        out = _rodar_extras_ia(dict(resultado), ed, u, db, "fake-key", deve_cancelar=lambda: True)
    assert mock_gerar.called is False
    assert out.get("cancelado") is True
    assert "verificacao_documentos_ia" not in out
    assert "comparacao_catalogo_ia" not in out


def test_cancelado_entre_as_duas_etapas_roda_so_a_primeira():
    """deve_cancelar vira True só DEPOIS da 1ª chamada — a verificação de
    documentos já em voo termina normalmente, mas a comparação de catálogo
    (2ª etapa) é pulada."""
    db = _sessao()
    u, ed = _semear(db)
    resultado = {"status": "ok", "objeto": ed.objeto,
                "requisitos_tecnicos": ["Garantia minima"], "documentos_habilitacao": {}}
    chamadas = {"n": 0}
    def deve_cancelar():
        return chamadas["n"] > 0
    with patch("app.analise_edital._gerar") as mock_gerar:
        mock_gerar.return_value = (_resposta_docs, "ok")
        def _side_effect(*a, **kw):
            chamadas["n"] += 1
            return (_resposta_docs, "ok")
        mock_gerar.side_effect = _side_effect
        out = _rodar_extras_ia(dict(resultado), ed, u, db, "fake-key", deve_cancelar=deve_cancelar)
    assert mock_gerar.call_count == 1, "só a 1a chamada (documentos) deveria ter rodado"
    assert out.get("cancelado") is True
    assert "verificacao_documentos_ia" in out, "a etapa já em andamento deve terminar normalmente"
    assert "comparacao_catalogo_ia" not in out, "a 2a etapa não deve nem começar"


def test_sem_cancelamento_roda_as_duas_etapas_normalmente():
    db = _sessao()
    u, ed = _semear(db)
    resultado = {"status": "ok", "objeto": ed.objeto,
                "requisitos_tecnicos": ["Garantia minima"], "documentos_habilitacao": {}}
    respostas = iter([_resposta_docs, _resposta_catalogo])
    with patch("app.analise_edital._gerar") as mock_gerar:
        mock_gerar.side_effect = lambda *a, **kw: (next(respostas), "ok")
        out = _rodar_extras_ia(dict(resultado), ed, u, db, "fake-key", deve_cancelar=lambda: False)
    assert mock_gerar.call_count == 2
    assert "cancelado" not in out
    assert out["verificacao_documentos_ia"]["status"] == "ok"
    assert out["comparacao_catalogo_ia"]["status"] == "ok"


def test_resultado_nao_ok_nao_roda_nada_independente_de_cancelamento():
    db = _sessao()
    u, ed = _semear(db)
    with patch("app.analise_edital._gerar") as mock_gerar:
        out = _rodar_extras_ia({"status": "sem_arquivo"}, ed, u, db, "fake-key", deve_cancelar=lambda: False)
    assert mock_gerar.called is False
    assert "cancelado" not in out
