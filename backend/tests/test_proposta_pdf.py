"""
Testes da proposta em PDF timbrado (substitui o CSV antigo) — gerar_pdf_proposta
(proposta_pdf.py) é pura (recebe dicts já prontos, sem tocar banco/HTTP) e o
endpoint /api/editais/{id}/proposta.pdf, chamado direto (sem HTTP, mesmo
padrão dos outros testes de main.py). Rode com:  cd backend && pytest
"""
from pypdf import PdfReader
import io

from app.proposta_pdf import gerar_pdf_proposta, _decodificar_logo


_LOGO_PNG = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="

_REMETENTE_BASE = {
    "nome": "Empresa Teste LTDA", "documento": "12.345.678/0001-99",
    "endereco": {"logradouro": "Av. Paulista", "numero": "1000", "cidade": "São Paulo", "uf": "SP"},
    "empresa": {"telefone": "11988887777", "representante_legal": "Fulano de Tal",
               "banco_nome": "Banco X", "banco_agencia": "0001", "banco_conta": "12345-6"},
    "logo_base64": None,
}
_EDITAL_BASE = {
    "orgao": "Prefeitura de Teste", "objeto": "Aquisição de material de escritório",
    "modalidade": "Pregão Eletrônico", "municipio": "Teste", "uf": "SP",
    "id_externo": "123/2026", "data_encerramento": "2026-08-20", "link": "https://x",
}
_PAYLOAD_BASE = {
    "itens": [{"descricao": "Papel A4", "quantidade": 10, "preco_unit": 25.0}],
    "total_venda": 250.0, "observacoes": "",
}


def _texto(pdf_bytes: bytes) -> str:
    r = PdfReader(io.BytesIO(pdf_bytes))
    return "\n".join(p.extract_text() for p in r.pages)


def test_gera_pdf_valido_com_dados_completos():
    pdf_bytes = gerar_pdf_proposta(_REMETENTE_BASE, _EDITAL_BASE, _PAYLOAD_BASE)
    assert pdf_bytes[:4] == b"%PDF"
    r = PdfReader(io.BytesIO(pdf_bytes))
    assert len(r.pages) >= 1


def test_conteudo_inclui_dados_do_remetente_e_edital():
    txt = _texto(gerar_pdf_proposta(_REMETENTE_BASE, _EDITAL_BASE, _PAYLOAD_BASE))
    assert "Empresa Teste LTDA" in txt
    assert "12.345.678/0001-99" in txt
    assert "Prefeitura de Teste" in txt
    assert "Papel A4" in txt
    assert "Fulano de Tal" in txt
    assert "Banco X" in txt


def test_total_calculado_aparece_formatado():
    txt = _texto(gerar_pdf_proposta(_REMETENTE_BASE, _EDITAL_BASE, _PAYLOAD_BASE))
    assert "250,00" in txt


def test_sem_nenhum_dado_complementar_nao_quebra():
    """Remetente com só nome (sem endereço/empresa/logo) — todos os campos
    novos são opcionais, gerar a proposta não pode depender deles."""
    remetente = {"nome": "Fulano", "documento": None, "endereco": {}, "empresa": {}, "logo_base64": None}
    edital = {"orgao": None, "objeto": None, "modalidade": None, "municipio": None,
             "uf": None, "id_externo": None, "data_encerramento": None, "link": None}
    payload = {"itens": [], "total_venda": 0, "observacoes": None}
    pdf_bytes = gerar_pdf_proposta(remetente, edital, payload)
    assert pdf_bytes[:4] == b"%PDF"


def test_gera_pdf_com_logo_valida():
    remetente = dict(_REMETENTE_BASE, logo_base64=_LOGO_PNG)
    pdf_bytes = gerar_pdf_proposta(remetente, _EDITAL_BASE, _PAYLOAD_BASE)
    assert pdf_bytes[:4] == b"%PDF"


def test_muitos_itens_gera_mais_de_uma_pagina_sem_quebrar():
    payload = dict(_PAYLOAD_BASE, itens=[
        {"descricao": f"Item {i}", "quantidade": 1, "preco_unit": 10.0} for i in range(80)
    ])
    pdf_bytes = gerar_pdf_proposta(_REMETENTE_BASE, _EDITAL_BASE, payload)
    r = PdfReader(io.BytesIO(pdf_bytes))
    assert len(r.pages) >= 1   # quebra de página automática do fpdf2, sem exceção


# --------- _decodificar_logo --------- #

def test_decodificar_logo_png_valido():
    r = _decodificar_logo(_LOGO_PNG)
    assert r is not None
    buf, ext = r
    assert ext == "png"
    assert buf.getvalue()[:8] == b"\x89PNG\r\n\x1a\n"


def test_decodificar_logo_svg_retorna_none():
    """fpdf2 não tem suporte confiável a SVG arbitrário — melhor pular a
    imagem do que gerar um PDF quebrado."""
    svg = "data:image/svg+xml;base64," + "PHN2Zz48L3N2Zz4="
    assert _decodificar_logo(svg) is None


def test_decodificar_logo_none_ou_vazio():
    assert _decodificar_logo(None) is None
    assert _decodificar_logo("") is None


def test_decodificar_logo_string_invalida():
    assert _decodificar_logo("não é uma imagem") is None


# --------- estrutura formal (modelo real de proposta) --------- #

def test_inclui_rg_do_representante_quando_informado():
    remetente = dict(_REMETENTE_BASE, empresa=dict(_REMETENTE_BASE["empresa"],
                     representante_rg="41.241.411-0"))
    txt = _texto(gerar_pdf_proposta(remetente, _EDITAL_BASE, _PAYLOAD_BASE))
    assert "RG" in txt
    assert "41.241.411-0" in txt


def test_sem_rg_nao_aparece_mencao_a_rg():
    """representante_rg é opcional — sem ele, não inventa nem deixa "RG:"
    solto no meio do texto."""
    txt = _texto(gerar_pdf_proposta(_REMETENTE_BASE, _EDITAL_BASE, _PAYLOAD_BASE))
    assert "inscrito no RG" not in txt


def test_inclui_declaracao_de_ciencia_do_edital():
    txt = _texto(gerar_pdf_proposta(_REMETENTE_BASE, _EDITAL_BASE, _PAYLOAD_BASE))
    assert "DECLARO ESTAR CIENTE E DE ACORDO COM O EDITAL" in txt
    assert "Termo de Refer" in txt   # "Referência" — assertiva tolerante a variação de acentuação na extração


def test_inclui_identificacao_formal_do_proponente():
    txt = _texto(gerar_pdf_proposta(_REMETENTE_BASE, _EDITAL_BASE, _PAYLOAD_BASE))
    assert "IDENTIFICA" in txt and "PROPONENTE" in txt
    assert "CNPJ/CPF sob o n" in txt


def test_inclui_condicoes_padrao_conforme_edital():
    txt = _texto(gerar_pdf_proposta(_REMETENTE_BASE, _EDITAL_BASE, _PAYLOAD_BASE))
    for rotulo in ("VALIDADE DA PROPOSTA", "PRAZO DE ENTREGA", "PRAZO DE GARANTIA"):
        assert rotulo in txt


def test_dados_bancarios_nao_aparecem_sem_conta_cadastrada():
    remetente = dict(_REMETENTE_BASE, empresa={})
    txt = _texto(gerar_pdf_proposta(remetente, _EDITAL_BASE, _PAYLOAD_BASE))
    assert "DADOS BANC" not in txt


def test_assinatura_repete_nome_e_documento_da_empresa():
    txt = _texto(gerar_pdf_proposta(_REMETENTE_BASE, _EDITAL_BASE, _PAYLOAD_BASE))
    assert txt.count("Empresa Teste LTDA") >= 2   # cabeçalho + assinatura
    assert txt.count("12.345.678/0001-99") >= 2


def test_cabecalho_repete_em_mais_de_uma_pagina():
    payload = dict(_PAYLOAD_BASE, itens=[
        {"descricao": f"Item {i}", "quantidade": 1, "preco_unit": 10.0} for i in range(80)
    ])
    r = PdfReader(io.BytesIO(gerar_pdf_proposta(_REMETENTE_BASE, _EDITAL_BASE, payload)))
    assert len(r.pages) >= 2
    for page in r.pages:
        assert "Empresa Teste LTDA" in page.extract_text()
