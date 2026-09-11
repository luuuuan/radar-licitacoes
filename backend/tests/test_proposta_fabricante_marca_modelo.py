"""
Achado real: a proposta exportada não trazia fabricante/marca/modelo do
produto do catálogo -- a cotação (cotacao.xlsx) já mostra essas colunas, mas
_proposta_payload nunca as calculava, porque window._propItens (front) só
guarda numero/descricao/quantidade/custo_unit/preco_unit. A correção busca
o produto CONFIRMADO de hoje pra cada item (mesmo critério de
_linhas_cotacao: confiança alta ou confirmado manualmente) e sobrepõe
fabricante/marca/modelo sempre frescos, o mesmo raciocínio já usado pra
manter a descrição sempre atualizada. Banco sqlite em memória, sem HTTP —
chama a função direto (mesmo padrão de test_exportacao_xlsx.py). Rode com:
cd backend && pytest
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.main import _proposta_payload
from app.models import Base, Usuario, Edital, ItemEdital, Match, Produto, Proposta


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


def test_item_com_produto_confirmado_traz_fabricante_marca_modelo():
    db = _sessao()
    u = _usuario(db)
    ed = Edital(fonte="PNCP", id_externo="ed1", orgao="Orgao Teste", objeto="Aquisicao", uf="SP")
    db.add(ed)
    db.commit()
    prod = Produto(usuario_id=u.id, descricao="Papel A4", fabricante="Suzano",
                   marca="Chamex", modelo="A4 75g")
    db.add(prod)
    db.commit()
    db.add(ItemEdital(edital_id=ed.id, numero=1, descricao="Papel A4 75g",
                      quantidade=10, valor_unitario=50.0))
    db.add(Match(usuario_id=u.id, edital_id=ed.id, score=0.9, nivel="forte",
                detalhe={"itens": [{"item": 1, "produto_id": prod.id, "confianca": "alta"}]}))
    db.commit()
    prop = Proposta(edital_id=ed.id, usuario_id=u.id, itens=[
        {"numero": 1, "descricao": "Papel A4 75g", "quantidade": 10,
         "custo_unit": 30.0, "preco_unit": 50.0},
    ])

    payload = _proposta_payload(ed, prop, u, db)
    assert payload["itens"][0]["fabricante"] == "Suzano"
    assert payload["itens"][0]["marca"] == "Chamex"
    assert payload["itens"][0]["modelo"] == "A4 75g"


def test_item_sem_produto_confirmado_fica_sem_fabricante_marca_modelo():
    """Item que nunca foi confirmado contra o catálogo (sem Match, ou
    Match sem esse número) não inventa dado -- fica None, sem quebrar."""
    db = _sessao()
    u = _usuario(db)
    ed = Edital(fonte="PNCP", id_externo="ed1", orgao="Orgao Teste", objeto="Aquisicao", uf="SP")
    db.add(ed)
    db.commit()
    db.add(ItemEdital(edital_id=ed.id, numero=1, descricao="Item avulso", quantidade=1))
    db.commit()
    prop = Proposta(edital_id=ed.id, usuario_id=u.id, itens=[
        {"numero": 1, "descricao": "Item avulso", "quantidade": 1,
         "custo_unit": 0, "preco_unit": 5.0},
    ])

    payload = _proposta_payload(ed, prop, u, db)
    assert payload["itens"][0]["fabricante"] is None
    assert payload["itens"][0]["marca"] is None
    assert payload["itens"][0]["modelo"] is None


def test_sem_user_e_db_nao_calcula_fabricante_marca_modelo_mas_nao_quebra():
    """Compatibilidade com os testes puros existentes (test_proposta_itens_
    edital.py), que chamam _proposta_payload(ed, prop) sem banco -- user/db
    são opcionais, e sem eles a função simplesmente não tenta buscar
    produto (não dá pra buscar sem sessão de banco)."""
    ed = Edital(id=1, fonte="PNCP", id_externo="ed1", orgao="Orgao Teste", objeto="Aquisicao")
    ed.itens = [ItemEdital(numero=1, descricao="Papel A4", quantidade=10, valor_unitario=25.0)]
    prop = Proposta(edital_id=1, itens=[
        {"numero": 1, "descricao": "Papel A4", "quantidade": 10, "custo_unit": 0, "preco_unit": 25.0},
    ])
    payload = _proposta_payload(ed, prop)
    assert payload["itens"][0]["fabricante"] is None


def test_item_de_confianca_media_nao_confirmada_nao_traz_fabricante():
    """Mesmo critério de _linhas_cotacao: confiança média ainda não
    confirmada pelo usuário é só sugestão -- não deve ser tratada como
    produto confirmado na proposta também."""
    db = _sessao()
    u = _usuario(db)
    ed = Edital(fonte="PNCP", id_externo="ed1", orgao="Orgao Teste", objeto="Aquisicao", uf="SP")
    db.add(ed)
    db.commit()
    prod = Produto(usuario_id=u.id, descricao="Papel A4", fabricante="Suzano")
    db.add(prod)
    db.commit()
    db.add(ItemEdital(edital_id=ed.id, numero=1, descricao="Papel A4 75g", quantidade=10))
    db.add(Match(usuario_id=u.id, edital_id=ed.id, score=0.5, nivel="medio",
                detalhe={"itens": [{"item": 1, "produto_id": prod.id, "confianca": "media"}]}))
    db.commit()
    prop = Proposta(edital_id=ed.id, usuario_id=u.id, itens=[
        {"numero": 1, "descricao": "Papel A4 75g", "quantidade": 10, "custo_unit": 0, "preco_unit": 50.0},
    ])

    payload = _proposta_payload(ed, prop, u, db)
    assert payload["itens"][0]["fabricante"] is None


def test_numero_salvo_como_string_ainda_casa_com_produto_confirmado():
    """Proposta.itens é uma coluna JSON sem validação de tipo (PropostaIn.
    itens é list[dict] livre) -- um "numero" salvo como string ("1" em vez
    de 1) não pode fazer o cruzamento com o produto confirmado (indexado
    por int, vindo de ItemEdital.numero) falhar silenciosamente."""
    db = _sessao()
    u = _usuario(db)
    ed = Edital(fonte="PNCP", id_externo="ed1", orgao="Orgao Teste", objeto="Aquisicao", uf="SP")
    db.add(ed)
    db.commit()
    prod = Produto(usuario_id=u.id, descricao="Papel A4", fabricante="Suzano",
                   marca="Chamex", modelo="A4 75g")
    db.add(prod)
    db.commit()
    db.add(ItemEdital(edital_id=ed.id, numero=1, descricao="Papel A4 75g", quantidade=10))
    db.add(Match(usuario_id=u.id, edital_id=ed.id, score=0.9, nivel="forte",
                detalhe={"itens": [{"item": 1, "produto_id": prod.id, "confianca": "alta"}]}))
    db.commit()
    prop = Proposta(edital_id=ed.id, usuario_id=u.id, itens=[
        {"numero": "1", "descricao": "Papel A4 75g", "quantidade": 10, "custo_unit": 0, "preco_unit": 50.0},
    ])

    payload = _proposta_payload(ed, prop, u, db)
    assert payload["itens"][0]["fabricante"] == "Suzano"
    assert payload["itens"][0]["marca"] == "Chamex"


def test_unidade_medida_vem_sempre_atual_do_item_do_edital():
    """Pedido do usuário: nova coluna UND no PDF -- unidade_medida vem
    sempre fresca de ItemEdital, mesmo raciocínio já usado pra descrição."""
    db = _sessao()
    u = _usuario(db)
    ed = Edital(fonte="PNCP", id_externo="ed1", orgao="Orgao Teste", objeto="Aquisicao", uf="SP")
    db.add(ed)
    db.commit()
    db.add(ItemEdital(edital_id=ed.id, numero=1, descricao="Papel A4 75g",
                      quantidade=10, unidade_medida="Resma"))
    db.commit()
    prop = Proposta(edital_id=ed.id, usuario_id=u.id, itens=[
        {"numero": 1, "descricao": "Papel A4 75g", "quantidade": 10, "custo_unit": 0, "preco_unit": 50.0},
    ])

    payload = _proposta_payload(ed, prop, u, db)
    assert payload["itens"][0]["unidade_medida"] == "Resma"


def test_custo_unit_vem_automatico_do_preco_de_custo_do_produto():
    """Pedido do usuário: a coluna "Custo un." editável saiu da tela da
    Proposta -- o custo (usado só internamente pra calcular a margem) passa
    a vir sempre do preço de custo cadastrado no catálogo (Produto.
    preco_custo) pro produto confirmado, não mais de um valor digitado à
    mão que ninguém mais consegue editar."""
    db = _sessao()
    u = _usuario(db)
    ed = Edital(fonte="PNCP", id_externo="ed1", orgao="Orgao Teste", objeto="Aquisicao", uf="SP")
    db.add(ed)
    db.commit()
    prod = Produto(usuario_id=u.id, descricao="Papel A4", preco_custo=32.5)
    db.add(prod)
    db.commit()
    db.add(ItemEdital(edital_id=ed.id, numero=1, descricao="Papel A4 75g", quantidade=10))
    db.add(Match(usuario_id=u.id, edital_id=ed.id, score=0.9, nivel="forte",
                detalhe={"itens": [{"item": 1, "produto_id": prod.id, "confianca": "alta"}]}))
    db.commit()
    # custo_unit salvo (0, valor antigo de quando a coluna era editável) --
    # tem que ser sobrescrito pelo preco_custo do catálogo, não mantido.
    prop = Proposta(edital_id=ed.id, usuario_id=u.id, itens=[
        {"numero": 1, "descricao": "Papel A4 75g", "quantidade": 10, "custo_unit": 0, "preco_unit": 50.0},
    ])

    payload = _proposta_payload(ed, prop, u, db)
    assert payload["itens"][0]["custo_unit"] == 32.5


def test_custo_unit_sem_produto_confirmado_mantem_valor_salvo():
    """Item sem produto confirmado (nenhum catálogo pra puxar preco_custo)
    não pode perder o custo que já estava salvo -- fica com o que tinha."""
    db = _sessao()
    u = _usuario(db)
    ed = Edital(fonte="PNCP", id_externo="ed1", orgao="Orgao Teste", objeto="Aquisicao", uf="SP")
    db.add(ed)
    db.commit()
    db.add(ItemEdital(edital_id=ed.id, numero=1, descricao="Item avulso", quantidade=1))
    db.commit()
    prop = Proposta(edital_id=ed.id, usuario_id=u.id, itens=[
        {"numero": 1, "descricao": "Item avulso", "quantidade": 1, "custo_unit": 7.5, "preco_unit": 20.0},
    ])

    payload = _proposta_payload(ed, prop, u, db)
    assert payload["itens"][0]["custo_unit"] == 7.5
