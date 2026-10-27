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
    de "declaracoes", que é lista de objetos)."""
    resultado = _analisar_com_resposta("""{
        "documentos_habilitacao": {
            "juridica": [{"item": "Contrato social", "citacao": "Edital, pág. 3"}],
            "fiscal_trabalhista": [{"item": "CND Federal", "citacao": "Edital, pág. 4"}],
            "declaracoes": []
        }
    }""")
    doc = resultado["documentos_habilitacao"]
    assert doc["juridica"] == ["Contrato social"]
    assert doc["fiscal_trabalhista"] == ["CND Federal"]
    assert doc["declaracoes"] == []


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
    itens_citados() agora preserva o índice original."""
    texto_pdf = "Procuração do representante legal consta no Anexo II. " * 10
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
