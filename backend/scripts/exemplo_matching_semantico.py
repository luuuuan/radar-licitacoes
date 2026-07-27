"""
Exemplo executável do pipeline: matching semântico/textual + validação
técnica + classificação final.

100% offline (sem chave de API): o ranking usa TF-IDF com o MESMO pipeline
de normalização/sinônimos/stemming de matching/engine.py (preparar()), e a
validação usa matching/atributos.py + matching/validacao.py, novos. Se
houver GEMINI_API_KEY configurada, troque top_n() por
MatchingEngine(..., usar_ia=True).avaliar(...) — ver nota no final da saída.

Rodar (a partir de backend/):  python -m scripts.exemplo_matching_semantico
"""
from __future__ import annotations

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from app.matching.engine import ItemEdt, ProdutoCat
from app.matching.validacao import classificar, validar

ITEM = ItemEdt(
    numero=1,
    descricao=(
        "Impressora multifuncional a laser, para impressão de no mínimo 30 "
        "páginas por minuto (ppm), com função duplex automática (frente e "
        "verso), bivolt (110/220V), com bandeja de capacidade para no "
        "mínimo 250 folhas."
    ),
)

PRODUTOS = [
    ProdutoCat(
        id=101,
        descricao=(
            "Impressora multifuncional a laser, 32 ppm, duplex automático, "
            "bivolt (110/220V), bandeja com capacidade para 250 folhas."
        ),
        palavras_chave="impressora, multifuncional, laser",
    ),
    ProdutoCat(
        id=102,
        descricao=(
            "Impressora multifuncional a laser, 32 ppm, duplex automático, "
            "bivolt (110/220V), bandeja com capacidade para 150 folhas."
        ),
        palavras_chave="impressora, multifuncional, laser",
    ),
    ProdutoCat(
        id=103,
        descricao=(
            "Impressora multifuncional a laser, bivolt (110/220V), bandeja "
            "com capacidade para 250 folhas. Demais especificações sob consulta."
        ),
        palavras_chave="impressora, multifuncional, laser",
    ),
]


def top_n(item: ItemEdt, produtos: list[ProdutoCat], n: int = 3):
    """Similaridade TF-IDF (com sinônimos+stemming, igual ao motor de
    produção em engine.py) entre o item e cada produto do catálogo."""
    textos = [p.texto_busca() for p in produtos]
    vectorizer = TfidfVectorizer(ngram_range=(1, 2), min_df=1, sublinear_tf=True)
    matriz = vectorizer.fit_transform(textos)
    vec_item = vectorizer.transform([item.texto_busca()])
    sims = cosine_similarity(vec_item, matriz)[0]
    ranking = sorted(zip(produtos, sims), key=lambda par: par[1], reverse=True)
    return ranking[:n]


def main():
    print(f"ITEM DO EDITAL:\n  {ITEM.descricao}\n")
    print("=" * 100)

    for produto, score in top_n(ITEM, PRODUTOS, n=3):
        validacao = validar(ITEM.descricao, produto.descricao)
        classificacao = classificar(score, validacao)

        print(f"\nProduto #{produto.id}: {produto.descricao}")
        print(f"  Similaridade semântica/textual (TF-IDF cosseno): {score:.3f}")
        if validacao.criticas:
            print("  Pendências CRÍTICAS (reprovam o produto):")
            for p in validacao.criticas:
                print(f"    - {p.descricao}")
        if validacao.avisos:
            print("  Avisos (conferir manualmente, não reprovam sozinhos):")
            for p in validacao.avisos:
                print(f"    - {p.descricao}")
        if not validacao.pendencias:
            print("  Nenhuma pendência técnica encontrada.")
        print(f"  >>> CLASSIFICAÇÃO FINAL: {classificacao}")

    print("\n" + "=" * 100)
    print(
        "Nota: com GEMINI_API_KEY configurada, troque top_n() por\n"
        "MatchingEngine(PRODUTOS, usar_ia=True, gemini_key=...).avaliar(objeto, [ITEM])\n"
        "— o score usado na classificação passa a combinar TF-IDF com embeddings reais\n"
        "(matching/embeddings.py), pegando sinônimos que o TF-IDF sozinho erra\n"
        "(ex.: 'notebook' vs 'computador portátil')."
    )


if __name__ == "__main__":
    main()
