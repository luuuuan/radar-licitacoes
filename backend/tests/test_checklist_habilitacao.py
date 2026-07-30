"""
Testes do cruzamento fuzzy de documentos exigidos x documentos cadastrados
(sem banco, sem HTTP). Rode com:  cd backend && pytest
"""
from datetime import date, timedelta

from app.checklist_habilitacao import montar


def _doc(nome, dias_para_vencer=365):
    return {"id": 1, "nome": nome, "data_validade": date.today() + timedelta(days=dias_para_vencer), "ativo": True}


def test_nao_cruza_declaracao_com_certidao_nao_relacionada():
    """Caso real: 4 declarações (não emprego de trabalho degradante, reserva
    de vagas PCD, ME/EPP, elaboração independente de proposta) e um
    "credenciamento no Sicaf" batiam com uma CND cadastrada sem ter nada a
    ver — e como essa CND estava vencida, isso vazava um "vencido" falso
    pros itens errados."""
    exigidos = {
        "juridica": ["Credenciamento prévio ativo no Sistema de Cadastramento Unificado de Fornecedores - Sicaf"],
        "fiscal_trabalhista": [],
        "tecnica": [],
        "economico_financeira": [],
        "declaracoes": [
            "Declaração de que não possui empregados executando trabalho degradante ou forçado",
            "Declaração de que cumpre as exigências de reserva de cargos para pessoa com deficiência",
            "Declaração de que cumpre os requisitos estabelecidos no artigo 3º da Lei Complementar nº 123 de 2006",
        ],
    }
    # a CND cadastrada está VENCIDA — se cruzar por engano com as
    # declarações acima, cada uma vazaria um "vencido" falso.
    usuario = [_doc("CERTIDÃO NEGATIVA DE DÉBITOS RELATIVOS AOS TRIBUTOS FEDERAIS E À DÍVIDA ATIVA DA UNIÃO", dias_para_vencer=-24)]
    resultado = montar(exigidos, usuario)
    assert all(item["status"] == "nao_cadastrado" for item in resultado)
    assert not any(item["status"] == "vencido" for item in resultado)


def test_ainda_cruza_documentos_realmente_equivalentes():
    """A correção não pode virar falso negativo generalizado — siglas e
    nomes reais de certidão continuam batendo com o nome expandido."""
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
    assert all(item["status"] != "nao_cadastrado" for item in resultado)


def test_documento_cadastrado_vencido_ainda_reporta_vencido_quando_o_match_e_correto():
    exigidos = {"juridica": [], "fiscal_trabalhista": ["CNDT"], "tecnica": [], "economico_financeira": [], "declaracoes": []}
    usuario = [_doc("Certidão Negativa de Débitos Trabalhistas", dias_para_vencer=-5)]
    resultado = montar(exigidos, usuario)
    assert resultado[0]["status"] == "vencido"
    assert resultado[0]["dias_para_vencer"] == -5
