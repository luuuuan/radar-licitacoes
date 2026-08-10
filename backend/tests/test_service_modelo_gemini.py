"""
Teste de _gerar_matches_usuario com modelo_reranker="gemini": decifra a
chave Gemini do PRÓPRIO usuário (gemini_key_cifrada) e repassa pro
MatchingEngine — sem isso, o provedor "gemini" nunca teria chave nenhuma
pra usar, mesmo com o usuário tendo cadastrado uma em "Meu perfil".
Rode com:  cd backend && pytest
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import auth
from app.matching import engine as engine_mod
from app.models import Base, Usuario, Edital, ItemEdital, Produto
from app.service import _gerar_matches_usuario


def _sessao():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def test_modelo_gemini_decifra_a_chave_do_proprio_usuario(monkeypatch):
    db = _sessao()
    u = Usuario(nome="Teste", email="t@t.com", senha_hash="x",
               gemini_key_cifrada=auth.cifrar("minha-chave-gemini"))
    db.add(u)
    db.commit()
    db.add(Produto(usuario_id=u.id, descricao="Produto qualquer do catálogo"))
    ed = Edital(fonte="PNCP", id_externo="ed1", orgao="Orgao Teste", objeto="Aquisicao", uf="SP")
    db.add(ed)
    db.commit()
    db.add(ItemEdital(edital_id=ed.id, numero=1, descricao="Item qualquer"))
    db.commit()

    chaves_recebidas = []
    def _fake_rerank_gemini(query, documentos, api_key=None, timeout=60, tentativas=2):
        chaves_recebidas.append(api_key)
        return [0.0] * len(documentos)   # sinal válido — não achou nada, mas respondeu
    monkeypatch.setattr(engine_mod, "_rerank_gemini", _fake_rerank_gemini)

    _gerar_matches_usuario(db, u, recalcular_todos=True, forcar_usar_ia=True,
                           modelo_reranker="gemini")

    assert chaves_recebidas   # foi chamado ao menos uma vez
    assert set(chaves_recebidas) == {"minha-chave-gemini"}


def test_modelo_deepinfra_nao_tenta_decifrar_chave_gemini(monkeypatch):
    """Sem modelo_reranker="gemini", nem precisa que o usuário tenha uma
    chave Gemini cadastrada — continua usando a DEEPINFRA_API_KEY global."""
    db = _sessao()
    u = Usuario(nome="Teste", email="t@t.com", senha_hash="x", gemini_key_cifrada=None)
    db.add(u)
    db.commit()
    db.add(Produto(usuario_id=u.id, descricao="Produto qualquer do catálogo"))
    ed = Edital(fonte="PNCP", id_externo="ed1", orgao="Orgao Teste", objeto="Aquisicao", uf="SP")
    db.add(ed)
    db.commit()
    db.add(ItemEdital(edital_id=ed.id, numero=1, descricao="Item qualquer"))
    db.commit()

    chamou_deepinfra = []
    monkeypatch.setattr(engine_mod, "_rerank",
                        lambda *a, **kw: chamou_deepinfra.append(1) or None)
    from app.config import settings
    monkeypatch.setattr(settings, "DEEPINFRA_API_KEY", "fake-deepinfra-key")

    resumo = _gerar_matches_usuario(db, u, recalcular_todos=True, forcar_usar_ia=True)

    assert resumo["editais"] == 1
    assert chamou_deepinfra
