"""
Achado real (usuário reportou): o link "abrir item" espalhado pelo app
(Cotação, comparação de catálogo por IA, "Ver detalhes") sempre caía pro
site do fornecedor (home) em vez do link direto do produto, mesmo o usuário
tendo cadastrado esse link no catálogo -- _produto_json() (serializador
único usado nesses 3 lugares) simplesmente não incluía link_produto no
JSON, então o campo chegava sempre undefined no frontend, que caía no
fallback (fornecedor_site). Rode com:  cd backend && pytest
"""
from app.main import _produto_json
from app.models import Produto


def test_produto_json_inclui_link_produto():
    p = Produto(usuario_id=1, descricao="Caneta azul",
               link_produto="https://fornecedor.example/produto/caneta-azul-123",
               fornecedor_site="https://fornecedor.example")

    r = _produto_json(p)

    assert r["link_produto"] == "https://fornecedor.example/produto/caneta-azul-123"
    assert r["fornecedor_site"] == "https://fornecedor.example"


def test_produto_json_link_produto_none_quando_nao_cadastrado():
    p = Produto(usuario_id=1, descricao="Caneta azul", fornecedor_site="https://fornecedor.example")

    r = _produto_json(p)

    assert r["link_produto"] is None
