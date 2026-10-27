"""
Achado real: declaração exigida num edital não é "certidão com validade" —
o próprio edital costuma fornecer um modelo pronto (Anexo X, só preencher e
assinar) OU exige um texto que a empresa precisa redigir sozinha. Antes
disso, "declaracoes" era só uma lista de strings e a tela oferecia um botão
"+ cadastrar" que não fazia sentido nenhum pra esse tipo de exigência.
Agora a IA devolve, pra cada declaração, se o edital fornece o modelo
(modelo_orgao: true/false/null) e um detalhe curto. Rode com:
cd backend && pytest
"""
from unittest.mock import patch, MagicMock

from app.analise_edital import analisar


def _resposta_gemini(json_texto: str):
    r = MagicMock()
    r.status_code = 200
    r.json.return_value = {"candidates": [{"content": {"parts": [{"text": json_texto}]}}]}
    return r


def _analisar_com_resposta(json_texto: str, texto_pdf: str | None = None) -> dict:
    arquivos = [{"titulo": "Edital", "tipo": "pdf", "url": "http://x/edital.pdf"}]
    # >300 chars, senão analisar() desiste antes de chamar a IA
    texto_pdf = texto_pdf if texto_pdf is not None else "x" * 400
    with patch("app.analise_edital._baixar_texto_pdf", return_value=(texto_pdf, False)), \
         patch("app.analise_edital.requests.post", return_value=_resposta_gemini(json_texto)):
        return analisar("Objeto de teste", arquivos, api_key="fake-key")


def test_declaracoes_no_formato_novo_preserva_modelo_orgao_e_detalhe():
    resultado = _analisar_com_resposta("""{
        "documentos_habilitacao": {"declaracoes": [
            {"nome": "Declaração de ME/EPP", "modelo_orgao": true, "detalhe": "modelo no Anexo IV"},
            {"nome": "Declaração de elaboração independente", "modelo_orgao": false, "detalhe": ""},
            {"nome": "Declaração de idoneidade", "modelo_orgao": null, "detalhe": ""}
        ]}
    }""")
    assert resultado["status"] == "ok"
    decs = resultado["documentos_habilitacao"]["declaracoes"]
    assert len(decs) == 3
    assert decs[0] == {"nome": "Declaração de ME/EPP", "modelo_orgao": True, "detalhe": "modelo no Anexo IV"}
    assert decs[1]["modelo_orgao"] is False
    assert decs[2]["modelo_orgao"] is None


def test_declaracoes_string_solta_da_ia_nao_quebra_a_analise():
    """A IA pode ignorar o formato pedido às vezes — string solta em vez de
    objeto não pode derrubar a análise inteira, só entra sem veredito."""
    resultado = _analisar_com_resposta("""{
        "documentos_habilitacao": {"declaracoes": ["Declaração de idoneidade"]}
    }""")
    assert resultado["status"] == "ok"
    decs = resultado["documentos_habilitacao"]["declaracoes"]
    assert decs == [{"nome": "Declaração de idoneidade", "modelo_orgao": None, "detalhe": ""}]


def test_declaracao_sem_nome_e_descartada():
    resultado = _analisar_com_resposta("""{
        "documentos_habilitacao": {"declaracoes": [
            {"modelo_orgao": true, "detalhe": "sem nome, deve ser ignorada"},
            {"nome": "Declaração válida", "modelo_orgao": true, "detalhe": ""}
        ]}
    }""")
    decs = resultado["documentos_habilitacao"]["declaracoes"]
    assert len(decs) == 1
    assert decs[0]["nome"] == "Declaração válida"


def test_outras_categorias_de_documentos_continuam_lista_de_strings():
    """O formato que a IA manda pra estas 4 categorias mudou (agora exige
    "citacao" de página por item, ver VERSAO_PROMPT v21/_L_ITEM_CITADO) --
    mas o formato de SAÍDA continua lista de strings simples (ao contrário
    de "declaracoes", que é lista de objetos). A citação precisa apontar
    pra uma página que REALMENTE fala do item (v22, ver
    _citacao_confere_com_texto) -- texto_pdf tem marcador de página com
    conteúdo batendo com cada citação."""
    texto_pdf = (
        "[pág. 1]\nTexto de abertura do edital, preenchendo espaço só pra passar do "
        "limite mínimo de caracteres que analisar() exige antes de chamar a IA.\n"
        "[pág. 2]\nCondições gerais da disputa, modo aberto, critério de julgamento "
        "menor preço, sessão pública realizada por meio eletrônico.\n"
        "[pág. 3]\nO licitante deve apresentar Contrato social atualizado e suas "
        "alterações, devidamente registrado no órgão competente.\n"
        "[pág. 4]\nApresentar CND Federal de regularidade fiscal perante a Receita "
        "Federal e a Procuradoria-Geral da Fazenda Nacional.\n"
    )
    resultado = _analisar_com_resposta("""{
        "documentos_habilitacao": {
            "juridica": [{"item": "Contrato social", "citacao": "Edital, pág. 3"}],
            "fiscal_trabalhista": [{"item": "CND Federal", "citacao": "Edital, pág. 4"}],
            "declaracoes": []
        }
    }""", texto_pdf=texto_pdf)
    doc = resultado["documentos_habilitacao"]
    assert doc["juridica"] == ["Contrato social"]
    assert doc["fiscal_trabalhista"] == ["CND Federal"]
    assert doc["declaracoes"] == []


def test_citacao_de_pagina_real_mas_sem_relacao_com_item_e_descartada():
    """Achado real (edital 170354, PE 043/2026 Piraí do Sul, já em
    VERSAO_PROMPT 21): a IA inventou "Atestado de capacidade técnica" e
    "Balanço patrimonial" (nenhum exigido pelo edital) JUNTO com uma
    citação de página plausível -- v21 só exigia que a citação não fosse
    vazia, nunca checava se a página citada tinha qualquer relação com o
    item, então a citação fabricada passava sem checagem. v22 confronta a
    citação contra o CONTEÚDO real da página (_citacao_confere_com_texto):
    página 2 existe, mas fala só de entrega/pagamento -- sem nenhuma
    palavra em comum com "atestado"/"capacidade"/"técnica" ou "balanço"/
    "patrimonial", os dois itens são descartados."""
    texto_pdf = (
        "[pág. 1]\nObjeto: aquisição de materiais de expediente para as secretarias "
        "municipais, conforme especificações detalhadas no Anexo I deste edital de "
        "registro de preços, modalidade pregão eletrônico.\n"
        "[pág. 2]\nRegras de entrega e condições de pagamento do contrato "
        "administrativo, prazo de 10 dias úteis após a ordem de fornecimento, "
        "entrega parcelada conforme necessidade da Administração.\n"
    )
    resultado = _analisar_com_resposta("""{
        "documentos_habilitacao": {
            "tecnica": [{"item": "Atestado de capacidade técnica", "citacao": "Edital, pág. 2"}],
            "economico_financeira": [{"item": "Balanço patrimonial", "citacao": "Edital, pág. 2"}],
            "declaracoes": []
        }
    }""", texto_pdf=texto_pdf)
    doc = resultado["documentos_habilitacao"]
    assert doc["tecnica"] == []
    assert doc["economico_financeira"] == []


def test_categoria_sem_citacao_e_descartada_quando_sem_base_no_texto():
    """Achado real (edital 396/2026, UFSC): a IA listava documento que não
    existe no texto nenhum (INSS, alvará), sem citação alguma -- sem
    citação E sem nenhuma palavra em comum com o texto mandado, o item
    tem que ser descartado (ver itens_citados() em _analisar_texto_unico)."""
    resultado = _analisar_com_resposta("""{
        "documentos_habilitacao": {
            "juridica": [{"item": "Certidão Negativa do INSS", "citacao": ""}],
            "declaracoes": []
        }
    }""")
    assert resultado["documentos_habilitacao"]["juridica"] == []


def test_itens_sem_citacao_que_sobrevivem_mantem_a_ordem_original():
    """Achado real (revisão de debugger): itens com citação e itens sem
    citação (mas que sobrevivem ao filtro por palavra) eram juntados em
    DUAS listas separadas e concatenados no fim -- um item sem citação no
    MEIO da resposta da IA acabava empurrado pro final da lista exibida.
    itens_citados() agora preserva o índice original.

    Texto SEM marcador de página nenhum (ver achado real abaixo, v23,
    sobre por que isso é necessário pra este cenário ainda ser possível):
    sem paginação, nenhum dos 3 itens é verificável por página, então os 3
    caem no filtro fraco por palavra -- o do meio sobrevive por achar
    "procuração"/"anexo" no texto, os outros 2 por achar "contrato"/
    "social"/"registro"/"comercial"."""
    texto_pdf = (
        "O licitante deve apresentar Contrato social atualizado e suas respectivas "
        "alterações contratuais registradas em órgão competente, Procuração do "
        "representante legal consta no Anexo II se houver, e Registro comercial "
        "da empresa perante a junta comercial -- texto corrido, sem marcador de "
        "página, só pra passar do limite mínimo de caracteres que analisar() exige."
    )
    resultado = _analisar_com_resposta("""{
        "documentos_habilitacao": {
            "juridica": [
                {"item": "Contrato social", "citacao": "Edital, pág. 2"},
                {"item": "Procuração do representante legal", "citacao": ""},
                {"item": "Registro comercial", "citacao": "Edital, pág. 3"}
            ],
            "declaracoes": []
        }
    }""", texto_pdf=texto_pdf)
    assert resultado["documentos_habilitacao"]["juridica"] == [
        "Contrato social", "Procuração do representante legal", "Registro comercial",
    ]


def test_item_sem_citacao_mas_com_palavra_em_comum_sobrevive_mesmo_com_paginacao():
    """Achado real (usuário conferiu a análise em produção IMEDIATAMENTE
    depois do deploy de v22, mesmo edital 170354 que motivou a correção
    original): os itens inventados continuaram aparecendo via o filtro
    fraco. v23 tentou descartar DIRETO (sem filtro fraco) qualquer item
    sem citação verificável sempre que o texto tivesse paginação -- mas
    isso também descartou documento REAL (a IA é tão inconsistente pra
    citar item real quanto item inventado), então v24 reverteu: o filtro
    fraco volta a valer como rede de segurança SEMPRE, com ou sem
    paginação. Esta é a LIMITE CONHECIDO documentada (não resolvida):
    item sem citação mas com palavra genérica em comum ainda sobrevive,
    mesmo em texto paginado -- este teste trava esse comportamento
    aceito de propósito, pra não ser revertido de novo sem querer."""
    texto_pdf = (
        "[pág. 1]\nObjeto: aquisição de materiais de expediente, especificação "
        "técnica detalhada de cada item conforme capacidade de uso contínuo, "
        "para atender as secretarias municipais de forma contínua e regular.\n"
        "[pág. 2]\nRegras de entrega e condições de pagamento do contrato "
        "administrativo, prazo de 10 dias úteis após a ordem de fornecimento, "
        "entrega parcelada conforme necessidade da Administração Pública.\n"
    )
    resultado = _analisar_com_resposta("""{
        "documentos_habilitacao": {
            "tecnica": [{"item": "Atestado de capacidade técnica", "citacao": ""}],
            "declaracoes": []
        }
    }""", texto_pdf=texto_pdf)
    assert resultado["documentos_habilitacao"]["tecnica"] == ["Atestado de capacidade técnica"]
