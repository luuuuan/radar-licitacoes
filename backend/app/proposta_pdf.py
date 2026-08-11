"""
Gera a proposta comercial em PDF timbrado (logo + dados da empresa no
cabeçalho, repetidos em toda página) pra anexar na licitação — substitui o
CSV antigo, que era só uma planilha de trabalho interna, sem nada que
identificasse o proponente. Estrutura seguida de perto a partir de um
modelo real de proposta aceita em pregão eletrônico (identificação formal
do proponente, representante legal, declaração de ciência do edital,
dados bancários, assinatura).
"""
from __future__ import annotations
import base64
import datetime
import io
import re

from fpdf import FPDF
from fpdf.fonts import FontFace

_COR_ACCENT = (37, 99, 235)
_COR_MUTED = (91, 103, 112)
_COR_TXT = (20, 25, 30)
_COR_LINHA = (216, 222, 230)

_MESES = ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho",
         "agosto", "setembro", "outubro", "novembro", "dezembro"]

_DECLARACAO = (
    "Declaramos que estamos de pleno acordo com todas as condições estabelecidas no Edital e "
    "seus Anexos, bem como aceitamos todas as obrigações e responsabilidades especificadas no "
    "Termo de Referência. Declaramos que nos preços cotados estão incluídas todas as despesas "
    "que, direta ou indiretamente, fazem parte do presente objeto, tais como gastos com suporte "
    "técnico e administrativo, impostos, seguros, taxas, ou quaisquer outros que possam incidir "
    "sobre os gastos da empresa, sem quaisquer acréscimos em virtude de expectativa inflacionária "
    "e deduzidos os descontos eventualmente concedidos."
)


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
    if tipo in ("svg+xml", "svg"):
        return None
    try:
        return io.BytesIO(base64.b64decode(b64)), tipo
    except Exception:
        return None


class _PropostaPDF(FPDF):
    """Cabeçalho (logo + nome) e rodapé (telefone/e-mail/endereço) repetidos
    em TODA página — mesmo padrão do modelo real que essa proposta segue,
    onde a marca aparece de novo se a tabela de itens estoura pra 2ª página."""

    def __init__(self, remetente: dict):
        super().__init__(format="A4")
        self._remetente = remetente
        self.set_auto_page_break(auto=True, margin=22)

    def _marca_dagua(self):
        """Logo grande, bem clara e girada atrás do conteúdo, repetida em
        toda página — mesmo efeito do modelo real que essa proposta segue.
        Silenciosa em qualquer falha (imagem grande demais, formato
        inesperado etc.): marca d'água é só estética, nunca pode quebrar a
        geração do PDF nem sujar a área de conteúdo se der errado."""
        logo = _decodificar_logo(self._remetente.get("logo_base64"))
        if not logo:
            return
        buf, _ext = logo
        try:
            # forçar w=h=lado distorcia qualquer logo que não fosse quadrada
            # (a maioria não é) — keep_aspect_ratio mantém a proporção real
            # da imagem dentro da caixa, sem esticar/achatar.
            lado = 110
            cx, cy = self.w / 2, self.h / 2
            with self.local_context(fill_opacity=0.07, stroke_opacity=0.07):
                with self.rotation(20, x=cx, y=cy):
                    self.image(buf, x=cx - lado / 2, y=cy - lado / 2, w=lado, h=lado,
                              keep_aspect_ratio=True)
        except Exception:
            pass

    def header(self):
        self._marca_dagua()
        remetente = self._remetente
        logo = _decodificar_logo(remetente.get("logo_base64"))
        x_texto = 15
        if logo:
            buf, _ext = logo
            try:
                self.image(buf, x=15, y=12, w=22, h=22)
                x_texto = 40
            except Exception:
                x_texto = 15
        self.set_xy(x_texto, 14)
        self.set_font("Helvetica", "B", 13)
        self.set_text_color(*_COR_ACCENT)
        self.cell(0, 6, remetente.get("nome") or "", new_x="LMARGIN", new_y="NEXT")
        if remetente.get("documento"):
            self.set_x(x_texto)
            self.set_font("Helvetica", "", 8.5)
            self.set_text_color(*_COR_MUTED)
            self.cell(0, 5, f"CNPJ/CPF: {remetente['documento']}", new_x="LMARGIN", new_y="NEXT")
        self.set_y(max(self.get_y(), 14 + 22) + 3)
        self.set_draw_color(*_COR_LINHA)
        self.line(15, self.get_y(), 195, self.get_y())
        self.ln(5)
        self.set_text_color(*_COR_TXT)

    def footer(self):
        emp = self._remetente.get("empresa") or {}
        end = self._remetente.get("endereco") or {}
        partes = [p for p in [
            f"Tel: {emp['telefone']}" if emp.get("telefone") else None,
            ", ".join(v for v in [end.get("logradouro"), end.get("numero"), end.get("bairro")] if v) or None,
            " - ".join(v for v in [end.get("cidade"), end.get("uf")] if v) or None,
            end.get("cep"),
        ] if p]
        if not partes:
            return
        self.set_y(-15)
        self.set_font("Helvetica", "", 7.5)
        self.set_text_color(*_COR_MUTED)
        self.cell(0, 4, "  |  ".join(partes), align="C")


def gerar_pdf_proposta(remetente: dict, edital_info: dict, payload: dict) -> bytes:
    """remetente: {nome, documento, endereco: {...}, empresa: {...}, logo_base64}
    edital_info: {orgao, objeto, modalidade, municipio, uf, id_externo,
                  data_encerramento, link}
    payload: mesmo dict de _proposta_payload (main.py) — itens, totais,
    observações."""
    pdf = _PropostaPDF(remetente)
    pdf.add_page()
    pdf.set_margins(15, 15, 15)
    emp = remetente.get("empresa") or {}
    end = remetente.get("endereco") or {}

    # ---- destinatário / identificação do edital ----
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 6, edital_info.get("orgao") or "Órgão não informado", new_x="LMARGIN", new_y="NEXT")
    linha_edital = " Nº: ".join(v for v in [edital_info.get("modalidade"), edital_info.get("id_externo")] if v)
    if linha_edital:
        pdf.set_font("Helvetica", "", 10)
        pdf.cell(0, 6, linha_edital, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)

    # ---- identificação do proponente ----
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(0, 6, "IDENTIFICAÇÃO DO PROPONENTE:", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9.5)
    endereco_txt = ", ".join(v for v in [
        end.get("logradouro"), end.get("numero"), end.get("bairro"),
    ] if v)
    cidade_txt = " - ".join(v for v in [end.get("cidade"), end.get("uf")] if v)
    partes_id = [remetente.get("nome") or ""]
    if remetente.get("documento"):
        partes_id.append(f"inscrita no CNPJ/CPF sob o nº {remetente['documento']}")
    if endereco_txt or cidade_txt:
        sede = ", ".join(v for v in [endereco_txt, cidade_txt, end.get("cep")] if v)
        partes_id.append(f"com sede em {sede}")
    pdf.multi_cell(0, 5, ", ".join(partes_id) + ".")
    pdf.ln(1)

    if emp.get("representante_legal"):
        pdf.set_font("Helvetica", "B", 9.5)
        pdf.write(5, "REPRESENTANTE LEGAL: ")
        pdf.set_font("Helvetica", "", 9.5)
        rg = f", inscrito no RG nº {emp['representante_rg']}" if emp.get("representante_rg") else ""
        pdf.write(5, f"{emp['representante_legal']}{rg}.")
        pdf.ln(7)

    pdf.ln(2)
    pdf.set_font("Helvetica", "", 9.5)
    pdf.multi_cell(0, 5, "Proposta de preços, conforme Termo de Referência do Edital em epígrafe, "
                        "nas seguintes condições:")
    pdf.ln(2)

    # ---- tabela de itens ----
    itens = payload.get("itens") or []
    linhas = [["Descrição", "Qtd.", "Valor unit.", "Valor total"]]
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

    pdf.ln(2)
    pdf.set_font("Helvetica", "B", 10.5)
    pdf.cell(0, 7, f"VALOR TOTAL: {_fmt_moeda(payload.get('total_venda') or 0)}", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    # ---- condições padrão ----
    pdf.set_font("Helvetica", "", 9.5)
    for rotulo in ("VALIDADE DA PROPOSTA COMERCIAL", "PRAZO DE ENTREGA", "PRAZO DE GARANTIA"):
        pdf.set_font("Helvetica", "B", 9.5)
        pdf.write(5, f"{rotulo}: ")
        pdf.set_font("Helvetica", "", 9.5)
        pdf.write(5, "conforme condições do edital.")
        pdf.ln(6)

    dados_banco = [v for v in [
        f"Banco {emp['banco_nome']}" if emp.get("banco_nome") else None,
        f"conta {emp['banco_conta']}" if emp.get("banco_conta") else None,
        f"agência {emp['banco_agencia']}" if emp.get("banco_agencia") else None,
    ] if v]
    if dados_banco:
        pdf.set_font("Helvetica", "B", 9.5)
        pdf.write(5, "DADOS BANCÁRIOS: ")
        pdf.set_font("Helvetica", "", 9.5)
        titular = f" ({remetente['nome']})" if remetente.get("nome") else ""
        pdf.write(5, " --- ".join(dados_banco) + titular + ".")
        pdf.ln(8)

    observacoes = payload.get("observacoes")
    if observacoes:
        pdf.set_font("Helvetica", "B", 9.5)
        pdf.cell(0, 5, "Observações", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 9)
        pdf.set_text_color(*_COR_MUTED)
        pdf.multi_cell(0, 5, observacoes)
        pdf.set_text_color(*_COR_TXT)
        pdf.ln(4)

    # ---- declaração ----
    pdf.set_font("Helvetica", "B", 9.5)
    pdf.cell(0, 6, "DECLARO ESTAR CIENTE E DE ACORDO COM O EDITAL E SEUS ANEXOS.",
             new_x="LMARGIN", new_y="NEXT")
    pdf.ln(1)
    pdf.set_font("Helvetica", "", 8.5)
    pdf.multi_cell(0, 4.6, _DECLARACAO)
    pdf.ln(6)

    # ---- local/data + assinatura ----
    hoje = datetime.date.today()
    cidade_data = ", ".join(v for v in [end.get("cidade"), end.get("uf")] if v)
    linha_data = f"{cidade_data + ', ' if cidade_data else ''}{hoje.day} de {_MESES[hoje.month-1]} de {hoje.year}."
    pdf.set_font("Helvetica", "", 9.5)
    pdf.cell(0, 6, linha_data, align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(14)
    pdf.set_font("Helvetica", "", 9.5)
    pdf.cell(0, 5, "_" * 45, align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "B", 9.5)
    pdf.cell(0, 5, remetente.get("nome") or "", align="C", new_x="LMARGIN", new_y="NEXT")
    if remetente.get("documento"):
        pdf.set_font("Helvetica", "", 8.5)
        pdf.cell(0, 5, f"CNPJ/CPF nº {remetente['documento']}", align="C", new_x="LMARGIN", new_y="NEXT")
    if emp.get("representante_legal"):
        pdf.set_font("Helvetica", "", 8.5)
        rg = f" - RG: {emp['representante_rg']}" if emp.get("representante_rg") else ""
        pdf.cell(0, 5, f"{emp['representante_legal']}{rg}", align="C", new_x="LMARGIN", new_y="NEXT")

    return bytes(pdf.output())
