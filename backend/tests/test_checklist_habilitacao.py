"""
Testes do cruzamento fuzzy de documentos exigidos x documentos cadastrados
(sem banco, sem HTTP). Rode com:  cd backend && pytest
"""
from datetime import date, timedelta

from app.checklist_habilitacao import montar


def _doc(nome, dias_para_vencer=365):
    return {"id": 1, "nome": nome, "data_validade": date.today() + timedelta(days=dias_para_vencer), "ativo": True}


def _doc_sem_validade(nome):
    return {"id": 1, "nome": nome, "data_validade": None, "ativo": True}


def test_nao_cruza_declaracao_com_certidao_nao_relacionada():
    """Caso real que motivou tirar declarações do cruzamento por nome de
    vez: 4 declarações (não emprego de trabalho degradante, reserva de
    vagas PCD, ME/EPP) batiam com uma CND cadastrada sem ter nada a ver —
    e como essa CND estava vencida, isso vazava um "vencido" falso pros
    itens errados. Declaração não é mais cruzada contra documentos
    cadastrados (não é "certidão com validade", é texto novo a cada
    edital) — o teste confirma que isso é estruturalmente impossível
    agora, não só "não bateu dessa vez"."""
    exigidos = {
        "juridica": ["Credenciamento prévio ativo no Sistema de Cadastramento Unificado de Fornecedores - Sicaf"],
        "fiscal_trabalhista": [],
        "tecnica": [],
        "economico_financeira": [],
        "declaracoes": [
            {"nome": "Declaração de que não possui empregados executando trabalho degradante ou forçado",
             "modelo_orgao": None, "detalhe": ""},
            {"nome": "Declaração de que cumpre as exigências de reserva de cargos para pessoa com deficiência",
             "modelo_orgao": None, "detalhe": ""},
            {"nome": "Declaração de que cumpre os requisitos estabelecidos no artigo 3º da Lei Complementar nº 123 de 2006",
             "modelo_orgao": None, "detalhe": ""},
        ],
    }
    # a CND cadastrada está VENCIDA — se cruzar por engano com as
    # declarações acima, cada uma vazaria um "vencido" falso.
    usuario = [_doc("CERTIDÃO NEGATIVA DE DÉBITOS RELATIVOS AOS TRIBUTOS FEDERAIS E À DÍVIDA ATIVA DA UNIÃO", dias_para_vencer=-24)]
    resultado = montar(exigidos, usuario)
    declaracoes = [item for item in resultado if item["categoria"] == "Declarações"]
    assert len(declaracoes) == 3
    assert all(item["status"] == "indefinido" for item in declaracoes)
    assert not any(item["status"] == "vencido" for item in resultado)
    # o "Sicaf" (categoria jurídica de verdade) continua sendo cruzado normalmente
    juridica = [item for item in resultado if item["categoria"] == "Habilitação jurídica"]
    assert juridica[0]["status"] == "nao_cadastrado"


def test_declaracao_com_modelo_do_orgao():
    exigidos = {"juridica": [], "fiscal_trabalhista": [], "tecnica": [], "economico_financeira": [],
               "declaracoes": [{"nome": "Declaração de ME/EPP", "modelo_orgao": True, "detalhe": "modelo no Anexo IV"}]}
    resultado = montar(exigidos, [])
    assert resultado[0]["status"] == "modelo_orgao"
    assert resultado[0]["detalhe"] == "modelo no Anexo IV"
    # nunca "nao_cadastrado" — declaração não tem botão de cadastrar
    assert resultado[0]["status"] != "nao_cadastrado"


def test_declaracao_sem_modelo_do_orgao_pede_elaborar_proprio():
    exigidos = {"juridica": [], "fiscal_trabalhista": [], "tecnica": [], "economico_financeira": [],
               "declaracoes": [{"nome": "Declaração de elaboração independente de proposta",
                                "modelo_orgao": False, "detalhe": ""}]}
    resultado = montar(exigidos, [])
    assert resultado[0]["status"] == "modelo_proprio"


def test_documento_sem_vencimento_cruza_como_valido_sem_quebrar():
    """Documento cadastrado como "sem vencimento" (ex.: contrato social) não
    tem data pra subtrair -- tem que virar "válido" direto, sem tentar
    calcular dias_para_vencer (isso quebraria com data_validade=None)."""
    exigidos = {"juridica": ["Contrato social"], "fiscal_trabalhista": [], "tecnica": [],
               "economico_financeira": [], "declaracoes": []}
    usuario = [_doc_sem_validade("Contrato Social e Alterações")]
    resultado = montar(exigidos, usuario)
    assert resultado[0]["status"] == "valido"
    assert resultado[0]["dias_para_vencer"] is None
    assert resultado[0]["data_validade"] is None


def test_declaracao_formato_antigo_string_simples_nao_quebra():
    """Compatibilidade: análise em cache de antes dessa mudança (ou uma
    resposta da IA fora do formato pedido) pode trazer string solta em vez
    de objeto — não pode quebrar, só perde o veredito modelo_orgao."""
    exigidos = {"juridica": [], "fiscal_trabalhista": [], "tecnica": [], "economico_financeira": [],
               "declaracoes": ["Declaração de idoneidade"]}
    resultado = montar(exigidos, [])
    assert resultado[0]["exigido"] == "Declaração de idoneidade"
    assert resultado[0]["status"] == "indefinido"


def test_ainda_cruza_documentos_realmente_equivalentes():
    """A correção não pode virar falso negativo generalizado — siglas e
    nomes reais de certidão continuam batendo com o nome expandido. Confere
    o PAR certo, não só "bateu com algo" -- é exatamente o tipo de bug que
    passaria despercebido só checando "!= nao_cadastrado" (ver achado real
    do edital 136161 abaixo: a CRF batia com a exigência errada e o teste
    antigo não pegava isso)."""
    exigidos = {
        "juridica": [],
        "fiscal_trabalhista": [
            "CND Receita Federal/PGFN",
            "CRF do FGTS",
            "CNDT",
        ],
        "tecnica": [], "economico_financeira": [], "declaracoes": [],
    }
    usuario = [
        _doc("Certidão Negativa de Débitos Relativos aos Tributos Federais e à Dívida Ativa da União"),
        _doc("Certificado de Regularidade do FGTS"),
        _doc("Certidão Negativa de Débitos Trabalhistas"),
    ]
    resultado = montar(exigidos, usuario)
    por_exigido = {item["exigido"]: item for item in resultado}
    assert por_exigido["CND Receita Federal/PGFN"]["nome_cadastrado"] == \
        "Certidão Negativa de Débitos Relativos aos Tributos Federais e à Dívida Ativa da União"
    assert por_exigido["CRF do FGTS"]["nome_cadastrado"] == "Certificado de Regularidade do FGTS"
    assert por_exigido["CNDT"]["nome_cadastrado"] == "Certidão Negativa de Débitos Trabalhistas"


# ---------------------------------------------------------------------------
# Achado real (edital 136161, usuário reportou): a análise por IA mostrava
# "Certificado de Regularidade do FGTS - CRF" como atendendo "Prova de
# regularidade fiscal perante a Fazenda Nacional (Certidão conjunta
# RFB/PGFN)" -- documentos completamente diferentes (FGTS é a Caixa, RFB/
# PGFN é Receita Federal). Causa: token_set_ratio sozinho tem um viés a
# favor de candidatos com nome CURTO -- poucas palavras de ligação em
# comum ("de", "regularidade") já bastavam pra inflar o score acima do
# limiar, mesmo sem nenhuma palavra realmente distintiva em comum. Pior:
# no mesmo edital, uma "Certidão Negativa de Processo - TCU" (documento
# sem NADA a ver) batia com a exigência de CNDT pelo mesmo motivo.
# ---------------------------------------------------------------------------

def test_crf_nao_atende_exigencia_de_fazenda_nacional_rfb_pgfn():
    exigidos = {
        "juridica": [], "tecnica": [], "economico_financeira": [], "declaracoes": [],
        "fiscal_trabalhista": [
            "Prova de regularidade fiscal perante a Fazenda Nacional (Certidão conjunta RFB/PGFN)",
            "Prova de regularidade com o Fundo de Garantia do Tempo de Serviço (FGTS)",
        ],
    }
    usuario = [
        _doc("Certificado de Regularidade do FGTS - CRF", dias_para_vencer=1),
        _doc("CERTIDÃO NEGATIVA DE DÉBITOS RELATIVOS AOS TRIBUTOS FEDERAIS E À DÍVIDA DA UNIÃO", dias_para_vencer=100),
    ]
    resultado = montar(exigidos, usuario)
    por_exigido = {item["exigido"]: item for item in resultado}
    fazenda_nacional = por_exigido["Prova de regularidade fiscal perante a Fazenda Nacional (Certidão conjunta RFB/PGFN)"]
    fgts = por_exigido["Prova de regularidade com o Fundo de Garantia do Tempo de Serviço (FGTS)"]
    assert fazenda_nacional["nome_cadastrado"] != "Certificado de Regularidade do FGTS - CRF"
    assert fazenda_nacional["nome_cadastrado"] == \
        "CERTIDÃO NEGATIVA DE DÉBITOS RELATIVOS AOS TRIBUTOS FEDERAIS E À DÍVIDA DA UNIÃO"
    assert fgts["nome_cadastrado"] == "Certificado de Regularidade do FGTS - CRF"


def test_documento_nao_relacionado_nao_atende_cndt_so_por_palavra_de_ligacao():
    exigidos = {"juridica": [], "tecnica": [], "economico_financeira": [], "declaracoes": [],
               "fiscal_trabalhista": ["Prova de inexistência de débitos inadimplidos perante a Justiça do Trabalho (CNDT)"]}
    usuario = [_doc("Certidão Negativa de Processo - TCU")]
    resultado = montar(exigidos, usuario)
    assert resultado[0]["status"] == "nao_cadastrado"


def test_documento_cadastrado_vencido_ainda_reporta_vencido_quando_o_match_e_correto():
    exigidos = {"juridica": [], "fiscal_trabalhista": ["CNDT"], "tecnica": [], "economico_financeira": [], "declaracoes": []}
    usuario = [_doc("Certidão Negativa de Débitos Trabalhistas", dias_para_vencer=-5)]
    resultado = montar(exigidos, usuario)
    assert resultado[0]["status"] == "vencido"
    assert resultado[0]["dias_para_vencer"] == -5
