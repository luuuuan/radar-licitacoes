"""
Testes da normalização de preço por unidade da Inteligência de Preço
(sem banco, sem HTTP). Rode com:  cd backend && pytest
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.main import _valor_unitario_normalizado, inteligencia_preco, inteligencia_preco_editais
from app.models import Base, ItemEdital, Usuario, Produto, Edital, Match


def _item(descricao, valor, unidade_medida=None):
    return ItemEdital(descricao=descricao, valor_unitario=valor, unidade_medida=unidade_medida)


def test_normaliza_caixa_com_n_unidades():
    """Caso real (auditoria em produção): "Envelope Kraft" variava de
    R$0,84 a R$164,80 porque alguns editais cotam "1 envelope" e outros
    "caixa com 250 unidades" — o valor bruto não distingue as duas escalas."""
    it = _item("Envelope saco pardo, formato 250 x 353 mm, kraft natural 90 g/m2, "
               "caixa com 250 unidades", 122.10)
    assert _valor_unitario_normalizado(it) == 122.10 / 250


def test_normaliza_abreviacao_und():
    it = _item("Envelope pardo 34x24 caixa com 250 und", 132.60)
    assert _valor_unitario_normalizado(it) == 132.60 / 250


def test_normaliza_parentetico():
    it = _item("ENVELOPE PARDO 22X32 (100 UND)", 75.73)
    assert _valor_unitario_normalizado(it) == 75.73 / 100


def test_normaliza_caixa_com_n_sem_palavra_unidade():
    """"Caixa com 100" sem "unidades"/"un" depois — comum quando o rótulo
    "caixa"/"pacote" já deixa implícito que o número é contagem de peças."""
    it = _item("Envelope 240x340, modelo ouro - Caixa com 100", 61.12)
    assert _valor_unitario_normalizado(it) == 61.12 / 100


def test_nao_normaliza_item_sem_embalagem_multipla():
    it = _item("ENVELOPE SACO KRAFT NATURAL 240X340 75G", 0.85)
    assert _valor_unitario_normalizado(it) == 0.85


def test_nao_normaliza_falso_positivo_com_n_dias():
    """"com N" sem palavra de contagem de peça (unidades/un/peças/folhas)
    logo depois não pode disparar — "garantia com 100 dias" não é embalagem
    de 100 peças."""
    it = _item("Garantia com 100 dias de cobertura", 5.0)
    assert _valor_unitario_normalizado(it) == 5.0


def test_sem_valor_continua_none():
    it = _item("Envelope caixa com 250 unidades", None)
    assert _valor_unitario_normalizado(it) is None
    it2 = _item("Envelope caixa com 250 unidades", 0)
    assert _valor_unitario_normalizado(it2) is None


# --------- achados da auditoria code-reviewer/debugger --------- #
# _valor_unitario_normalizado passou a reaproveitar _qtd_embalagem_pncp/
# _qtd_embalagem_descricao (já usadas em _custo_e_margem) em vez de uma
# regex própria mais fraca -- ver comentário em cima da função em main.py.

def test_normaliza_contendo():
    """"Caixa CONTENDO N unidades" -- fraseado comum em editais (CATMAT) que
    a regex antiga não reconhecia (só "com"/"c/")."""
    it = _item("Caixa contendo 100 unidades", 50.0)
    assert _valor_unitario_normalizado(it) == 0.5


def test_nao_normaliza_peso_como_contagem_de_pecas():
    """"Pacote 500 G" é peso, não 500 peças -- achado real: a regex antiga
    tratava qualquer número logo após a palavra de embalagem como contagem,
    mesmo seguido de unidade de peso/volume."""
    it = _item("Café em pó, pacote 500 g", 15.0)
    assert _valor_unitario_normalizado(it) == 15.0


def test_nao_normaliza_volume_como_contagem_de_pecas():
    it = _item("Detergente concentrado, pacote 500 ml", 8.0)
    assert _valor_unitario_normalizado(it) == 8.0


def test_nao_normaliza_prazo_como_contagem_de_pecas():
    """"Kit 12 MESES de garantia" não é uma embalagem de 12 peças."""
    it = _item("Kit 12 meses de garantia estendida", 200.0)
    assert _valor_unitario_normalizado(it) == 200.0


def test_usa_campo_estruturado_unidade_medida_do_pncp():
    """Achado real: a versão antiga só olhava a descrição -- o campo
    unidadeMedida estruturado do PNCP (ex.: "Embalagem 500 FL", já usado em
    _custo_e_margem) era ignorado na Inteligência de Preço."""
    it = _item("Papel sulfite A4 branco", 250.0, unidade_medida="Embalagem 500 FL")
    assert _valor_unitario_normalizado(it) == 0.5


def _sessao():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def _match(db, user, edital_id, itens_detalhe):
    m = Match(usuario_id=user.id, edital_id=edital_id, score=1.0, nivel="forte",
             detalhe={"itens": itens_detalhe})
    db.add(m)
    return m


def test_banda_de_outlier_do_modal_nao_e_contaminada_por_item_de_outro_produto():
    """Achado real (auditoria code-reviewer/debugger): a rota /editais
    calculava a banda de outlier em cima de TODOS os itens dos editais
    relevantes (qualquer produto), não só os itens deste produto -- um item
    de outro produto no MESMO edital (ex.: uma cadeira bem mais cara)
    deslocava a mediana bruta e podia marcar como "fora do padrão" valores
    que a tela principal (inteligencia_preco) usou normalmente no cálculo.
    Monta 5 editais com um item do produto (preço ~1.00, dentro do padrão)
    + 1 item de OUTRO produto (preço 4500.00, bem mais caro) cada -- sem a
    correção, a presença do item caro contamina a banda e o /editais marca
    os 5 valores válidos como excluídos, mesmo a tela principal usando
    todos os 5."""
    db = _sessao()
    u = Usuario(nome="Teste", email="t@t.com", senha_hash="x")
    db.add(u)
    db.commit()
    produto = Produto(usuario_id=u.id, descricao="Envelope Kraft")
    outro_produto = Produto(usuario_id=u.id, descricao="Cadeira de escritório")
    db.add_all([produto, outro_produto])
    db.commit()

    for i in range(5):
        ed = Edital(fonte="PNCP", id_externo=f"e{i}", orgao="Orgao", objeto="Aquisicao", uf="SP")
        db.add(ed)
        db.commit()
        db.add_all([
            ItemEdital(edital_id=ed.id, numero=1, descricao="Envelope Kraft", valor_unitario=1.0 + i * 0.01),
            ItemEdital(edital_id=ed.id, numero=2, descricao="Cadeira de escritório", valor_unitario=4500.0),
        ])
        _match(db, u, ed.id, [
            {"item": 1, "produto_id": produto.id, "confianca": "alta"},
            {"item": 2, "produto_id": outro_produto.id, "confianca": "alta"},
        ])
        db.commit()

    principal = inteligencia_preco(user=u, db=db)
    linha_produto = next(p for p in principal if p["produto_id"] == produto.id)
    assert linha_produto["ocorrencias"] == 5   # nenhum dos 5 valores reais é outlier de verdade

    detalhe = inteligencia_preco_editais(produto.id, user=u, db=db)
    assert len(detalhe) == 5
    assert all(l["usado_no_calculo"] for l in detalhe)   # nenhum marcado como "fora do padrão"
