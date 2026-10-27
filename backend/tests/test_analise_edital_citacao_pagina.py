"""
Testes unitários diretos das funções puras usadas por itens_citados() pra
validar a "citação de página" que a IA devolve pra cada documento de
habilitação contra o CONTEÚDO REAL dessa página -- achado real (edital
170354, PE 043/2026 Piraí do Sul): a defesa anterior (VERSAO_PROMPT 21)
só exigia que a citação existisse, nunca que fosse verdadeira, e a IA
inventou documento (atestado/declaração de capacidade técnica, balanço
patrimonial, comprovante de INSS) junto com uma citação de página
plausível que passava sem checagem alguma.

As funções aqui são puras (sem rede, sem IA) -- os testes de integração
ponta a ponta (via analisar()) ficam em test_analise_edital_declaracoes.py;
este arquivo trava a semântica tri-estado True/False/None de
_citacao_confere_com_texto isoladamente, sem precisar mockar resposta de IA.
Rode com: cd backend && pytest
"""
from app.analise_edital import (
    _citacao_confere_com_texto,
    _paginas_citadas,
    _palavras_significativas_ou_sigla,
    _trechos_da_pagina,
)


def test_paginas_citadas_reconhece_forma_abreviada_e_sem_acento():
    assert _paginas_citadas("Edital, pág. 3") == [3]
    assert _paginas_citadas("Edital, pag 12") == [12]


def test_paginas_citadas_reconhece_palavra_por_extenso_e_plural():
    """Achado real (revisão desta correção): o regex original só casava
    "pág."/"pag" abreviado -- "página"/"páginas"/"págs." (bem comuns em
    português, principalmente no fallback Mistral/Groq, que responde só
    por prosa livre sem o exemplo abreviado do prompt pra copiar) caíam
    direto em None, perdendo a proteção nova silenciosamente."""
    assert _paginas_citadas("Edital, página 3") == [3]
    assert _paginas_citadas("PÁGINA 7") == [7]
    assert _paginas_citadas("Edital, págs. 3 e 45") == [3, 45]


def test_paginas_citadas_reconhece_intervalo_e_lista():
    """Achado real (revisão desta correção): citar um intervalo ("pág. 12
    a 15") só pegava o 1º número -- se a exigência estivesse na borda
    errada do intervalo, um item LEGÍTIMO seria rejeitado."""
    assert _paginas_citadas("Edital, pág. 12 a 15") == [12, 15]
    assert _paginas_citadas("Edital, páginas 3-5") == [3, 5]


def test_paginas_citadas_sem_numero_devolve_lista_vazia():
    assert _paginas_citadas("Edital, item 8.2") == []
    assert _paginas_citadas("") == []


def test_trechos_da_pagina_devolve_todos_os_documentos_sem_nome_na_citacao():
    """Sem nome de documento reconhecível na citação, busca em TODOS os
    blocos -- citação vaga-mas-real sem nome de arquivo explícito não deve
    ser punida (comportamento antigo, preservado de propósito)."""
    texto = (
        "=== DOCUMENTO: Edital ===\n[pág. 2]\nConteúdo do edital na página 2.\n"
        "\n---\n\n"
        "=== DOCUMENTO: Termo de Referência ===\n[pág. 2]\nConteúdo do TR na página 2.\n"
    )
    trechos, achou_marcador = _trechos_da_pagina(texto, 2)
    assert achou_marcador is True
    assert len(trechos) == 2


def test_trechos_da_pagina_restringe_ao_documento_nomeado_na_citacao():
    """Achado real (revisão desta correção, debugger, com reprodução
    concreta): quando a citação nomeia um documento reconhecível, restringe
    a busca a ESSE documento -- sem isso, uma citação pra um documento sem
    relação "confere" só porque outro documento combinado tem, por
    coincidência, a mesma numeração de página."""
    texto = (
        "=== DOCUMENTO: Edital ===\n[pág. 2]\nConteúdo do edital na página 2.\n"
        "\n---\n\n"
        "=== DOCUMENTO: Termo de Referência ===\n[pág. 2]\nConteúdo do TR na página 2.\n"
    )
    trechos, _ = _trechos_da_pagina(texto, 2, citacao="Edital, pág. 2")
    assert len(trechos) == 1
    assert "edital" in trechos[0].lower()


def test_trechos_da_pagina_sem_marcador_nenhum():
    assert _trechos_da_pagina("texto qualquer sem paginação nenhuma", 2) == ([], False)


def test_citacao_confere_com_texto_pagina_real_sustenta_o_item():
    texto = "[pág. 1]\nAbertura.\n[pág. 2]\nO licitante deve apresentar Contrato social atualizado.\n"
    assert _citacao_confere_com_texto("Edital, pág. 2", "Contrato social", texto) is True


def test_citacao_confere_com_texto_pagina_real_sem_relacao_com_item():
    """Achado real (edital 170354): a página existe, mas fala de outro
    assunto -- descarta direto (prova mais forte de invenção que a simples
    ausência de citação)."""
    texto = "[pág. 1]\nAbertura.\n[pág. 2]\nRegras de entrega e pagamento.\n"
    assert _citacao_confere_com_texto("Edital, pág. 2", "Balanço patrimonial", texto) is False


def test_citacao_confere_com_texto_colisao_entre_documentos_combinados():
    """Reprodução exata do achado de debugger: citação "Edital, pág. 2"
    (que não tem nada a ver com o item) não pode mais "confirmar" o item só
    porque a página 2 de OUTRO documento combinado (Termo de Referência)
    compartilha uma palavra com o nome do item inventado."""
    texto = (
        "=== DOCUMENTO: Edital ===\n"
        "[pág. 2]\nCondições gerais da disputa, modo aberto, sessão pública eletrônica.\n"
        "\n---\n\n"
        "=== DOCUMENTO: Termo de Referência ===\n"
        "[pág. 2]\nEspecificação técnica: cada caneta deve ter capacidade de carga de tinta.\n"
    )
    assert _citacao_confere_com_texto(
        "Edital, pág. 2", "Declaração de capacidade técnica", texto) is False


def test_citacao_confere_com_texto_sem_numero_de_pagina_e_none():
    texto = "[pág. 1]\nTexto qualquer.\n"
    assert _citacao_confere_com_texto("Edital, item 8.2", "Contrato social", texto) is None


def test_citacao_confere_com_texto_sem_paginacao_no_texto_e_none():
    """Documento sem marcador de página (.docx/.odt) -- não dá pra julgar,
    cai no filtro fraco por palavra (ver itens_citados), não em False."""
    texto = "Texto corrido sem marcador de página nenhum, bem longo pra não ser trivial."
    assert _citacao_confere_com_texto("Edital, pág. 2", "Contrato social", texto) is None


def test_citacao_confere_com_texto_pagina_citada_nao_existe_em_lugar_nenhum():
    """Achado real (revisão desta correção, debugger): o texto TEM
    paginação, mas a página citada não existe nela -- evidência de
    invenção tão forte quanto citar uma página real sem relação, então
    False (não None: a IA só viu este texto, citar um número que não
    existe em nada disso não é ambíguo)."""
    texto = "[pág. 1]\nAbertura.\n[pág. 2]\nConteúdo.\n"
    assert _citacao_confere_com_texto("Edital, pág. 99", "Contrato social", texto) is False


def test_palavras_significativas_ou_sigla_reconhece_siglas_curtas():
    """Achado real (revisão desta correção, debugger e error-detective,
    independentemente): nome de documento fiscal costuma ser só sigla
    (CRF, CNDT, CND, SICAF -- os PRÓPRIOS exemplos do prompt), abaixo do
    piso de 5+ letras de _palavras_significativas -- sem isso, esses itens
    nunca eram protegidos pela validação de página."""
    assert "crf" in _palavras_significativas_ou_sigla("CRF do FGTS")
    assert "fgts" in _palavras_significativas_ou_sigla("CRF do FGTS")
    assert _palavras_significativas_ou_sigla("CNDT") == {"cndt"}
    assert "sicaf" in _palavras_significativas_ou_sigla("SICAF")


def test_citacao_confere_com_texto_item_sigla_curta_agora_e_verificavel():
    """Mesmo achado acima, mas ponta a ponta na função de validação: item
    cujo nome é só sigla agora é confrontado contra o conteúdo real da
    página, em vez de cair direto em None por falta de palavra pra
    comparar."""
    texto_relacionado = "[pág. 5]\nO licitante deve apresentar o CRF do FGTS atualizado.\n"
    assert _citacao_confere_com_texto("Edital, pág. 5", "CRF do FGTS", texto_relacionado) is True

    texto_sem_relacao = "[pág. 5]\nRegras de entrega e pagamento do contrato.\n"
    assert _citacao_confere_com_texto("Edital, pág. 5", "CRF do FGTS", texto_sem_relacao) is False
