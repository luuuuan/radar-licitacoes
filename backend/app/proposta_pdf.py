"""
Gera a proposta comercial em PDF timbrado (logo + dados da empresa no
cabeçalho) pra anexar na licitação — substitui o CSV antigo, que era só uma
planilha de trabalho interna, sem nada que identificasse o proponente.
"""
from __future__ import annotations
import base64
import io
import re

from fpdf import FPDF
from fpdf.fonts import FontFace

_COR_ACCENT = (37, 99, 235)     # --accent do app (tema claro)
_COR_MUTED = (91, 103, 112)
_COR_LINHA = (216, 222, 230)


def _fmt_moeda(v: float) -> str:
    txt = f"{v:,.2f}"
    return "R$ " + txt.replace(",", "_").replace(".", ",").replace("_", ".")


def _decodificar_logo(data_uri: str | None) -> tuple[io.BytesIO, str] | None:
    """data URI ('data:image/png;base64,...') -> (bytes, extensão). None se
    vazio, malformado, ou SVG (fpdf2 não tem suporte confiável a SVG
    arbitrário — nesse caso a proposta sai só com o nome da empresa)."""
    if not data_uri or not data_uri.startswith("data:image/"):
        return None
    m = re.match(r"data:image/(\w+);base64,(.+)$", data_uri, re.DOTALL)
    if not m:
        return None
    tipo, b64 = m.group(1).lower(), m.group(2)
    if tipo == "svg+xml" or tipo == "svg":
        return None
    try:
        return io.BytesIO(base64.b64decode(b64)), tipo
    except Exception:
        return None


def gerar_pdf_proposta(remetente: dict, edital_info: dict, payload: dict) -> bytes:
    """remetente: {nome, documento, endereco: {...}, empresa: {...}, logo_base64}
    edital_info: {orgao, objeto, modalidade, municipio, uf, id_externo,
                  data_encerramento, link}
    payload: mesmo dict de _proposta_payload (main.py) — itens, totais,
    observações."""
    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.add_page()
    pdf.set_margins(15, 15, 15)

    # ---- cabeçalho / timbre ----
    logo = _decodificar_logo(remetente.get("logo_base64"))
    y_inicial = pdf.get_y()
    x_texto = 15
    if logo:
        buf, _ext = logo
        try:
            pdf.image(buf, x=15, y=y_inicial, w=26, h=26)
            x_texto = 45
        except Exception:
            x_texto = 15   # imagem corrompida/formato não suportado -> segue sem ela
    pdf.set_xy(x_texto, y_inicial)
    pdf.set_font("Helvetica", "B", 14)
    pdf.set_text_color(*_COR_ACCENT)
    pdf.cell(0, 7, remetente.get("nome") or "", new_x="LMARGIN", new_y="NEXT")
    pdf.set_x(x_texto)
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(*_COR_MUTED)
    doc = remetente.get("documento")
    if doc:
        pdf.cell(0, 5, f"CNPJ/CPF: {doc}", new_x="LMARGIN", new_y="NEXT")
        pdf.set_x(x_texto)
    end = remetente.get("endereco") or {}
    linha_end = ", ".join(v for v in [
        end.get("logradouro"), end.get("numero"), end.get("bairro"),
    ] if v)
    linha_cid = " - ".join(v for v in [end.get("cidade"), end.get("uf")] if v)
    if linha_end or linha_cid or end.get("cep"):
        partes = [p for p in [linha_end, linha_cid, end.get("cep")] if p]
        pdf.cell(0, 5, " | ".join(partes), new_x="LMARGIN", new_y="NEXT")
        pdf.set_x(x_texto)
    emp = remetente.get("empresa") or {}
    if emp.get("telefone"):
        pdf.cell(0, 5, f"Tel: {emp['telefone']}", new_x="LMARGIN", new_y="NEXT")

    pdf.set_y(max(pdf.get_y(), y_inicial + 26) + 4)
    pdf.set_draw_color(*_COR_LINHA)
    pdf.line(15, pdf.get_y(), 195, pdf.get_y())
    pdf.ln(6)

    # ---- título ----
    pdf.set_text_color(20, 25, 30)
    pdf.set_font("Helvetica", "B", 15)
    pdf.cell(0, 8, "Proposta Comercial", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)

    # ---- dados do edital ----
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(0, 6, edital_info.get("orgao") or "Órgão não informado", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9.5)
    pdf.set_text_color(*_COR_MUTED)
    pdf.multi_cell(0, 5, edital_info.get("objeto") or "")
    linha1 = " | ".join(v for v in [
        edital_info.get("modalidade"),
        " / ".join(v for v in [edital_info.get("municipio"), edital_info.get("uf")] if v) or None,
    ] if v)
    if linha1:
        pdf.cell(0, 5, linha1, new_x="LMARGIN", new_y="NEXT")
    linha2 = " | ".join(v for v in [
        f"Processo/controle: {edital_info['id_externo']}" if edital_info.get("id_externo") else None,
        f"Encerramento: {edital_info['data_encerramento']}" if edital_info.get("data_encerramento") else None,
    ] if v)
    if linha2:
        pdf.cell(0, 5, linha2, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    # ---- tabela de itens ----
    pdf.set_text_color(20, 25, 30)
    itens = payload.get("itens") or []
    linhas = [["Descrição", "Qtd.", "Preço unit.", "Total"]]
    for it in itens:
        qtd = it.get("quantidade") or 0
        preco = it.get("preco_unit") or 0
        linhas.append([
            str(it.get("descricao") or ""), f"{qtd:g}",
            _fmt_moeda(preco), _fmt_moeda(preco * qtd),
        ])
    with pdf.table(linhas, col_widths=(46, 12, 21, 21), text_align=("LEFT", "CENTER", "RIGHT", "RIGHT"),
                   headings_style=FontFace(emphasis="BOLD", fill_color=(240, 242, 245)),
                   line_height=6, padding=1.5):
        pass

    pdf.ln(3)
    pdf.set_font("Helvetica", "B", 10.5)
    pdf.cell(0, 7, f"Total geral da proposta: {_fmt_moeda(payload.get('total_venda') or 0)}",
             new_x="LMARGIN", new_y="NEXT", align="R")

    observacoes = payload.get("observacoes")
    if observacoes:
        pdf.ln(4)
        pdf.set_font("Helvetica", "B", 9.5)
        pdf.cell(0, 5, "Observações", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 9)
        pdf.set_text_color(*_COR_MUTED)
        pdf.multi_cell(0, 5, observacoes)

    # ---- rodapé: assinatura + dados bancários ----
    pdf.ln(10)
    pdf.set_draw_color(*_COR_LINHA)
    pdf.line(15, pdf.get_y(), 195, pdf.get_y())
    pdf.ln(6)
    pdf.set_text_color(20, 25, 30)
    if emp.get("representante_legal"):
        pdf.set_font("Helvetica", "", 9.5)
        pdf.cell(0, 5, "_" * 45, new_x="LMARGIN", new_y="NEXT")
        pdf.cell(0, 5, emp["representante_legal"], new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(*_COR_MUTED)
        pdf.set_font("Helvetica", "", 8.5)
        pdf.cell(0, 5, "Representante legal", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(4)
    dados_banco = [v for v in [
        f"Banco: {emp['banco_nome']}" if emp.get("banco_nome") else None,
        f"Agência: {emp['banco_agencia']}" if emp.get("banco_agencia") else None,
        f"Conta: {emp['banco_conta']}" if emp.get("banco_conta") else None,
    ] if v]
    if dados_banco:
        pdf.set_font("Helvetica", "B", 8.5)
        pdf.set_text_color(*_COR_MUTED)
        pdf.cell(0, 5, "Dados bancários para pagamento", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 8.5)
        pdf.cell(0, 5, "  |  ".join(dados_banco), new_x="LMARGIN", new_y="NEXT")

    return bytes(pdf.output())
