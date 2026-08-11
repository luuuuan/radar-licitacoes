"""
Achado real em produção: alguns editais do PNCP têm itens com "numeroItem"
fora de ordem (ou não-sequencial — ver test de detalhe do edital 52263, onde
o PNCP manda números grandes tipo 2246372 em vez de 1,2,3). A relação
Edital.itens não tinha order_by nenhum, então `ed.itens` voltava na ordem
que o banco/driver desse (não necessariamente a ordem de inserção nem a
numérica) — a tela do edital e a planilha de cotação mostravam os itens
embaralhados. Agora a relação sempre vem ordenada por `numero`.
Rode com:  cd backend && pytest
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, Edital, ItemEdital


def _sessao():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def test_itens_do_edital_vem_ordenados_por_numero_mesmo_inseridos_fora_de_ordem():
    db = _sessao()
    ed = Edital(fonte="PNCP", id_externo="ed1", orgao="Orgao Teste",
               objeto="Aquisicao", uf="SP")
    db.add(ed)
    db.commit()

    # insere fora de ordem, como a coleta faz ao concatenar páginas do PNCP
    for numero in [22, 64, 66, 1, 2, 3]:
        db.add(ItemEdital(edital_id=ed.id, numero=numero, descricao=f"Item {numero}"))
    db.commit()

    db.expire_all()
    ed_recarregado = db.get(Edital, ed.id)

    assert [it.numero for it in ed_recarregado.itens] == [1, 2, 3, 22, 64, 66]
