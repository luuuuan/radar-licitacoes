"""
Montagem das mensagens de notificação (e-mail em HTML e Telegram em HTML).

Cada "item" de edital é um dict:
    {objeto, orgao, municipio, uf, link, abertura, encerramento, extra}
Nem todos os campos precisam estar presentes.
"""
from __future__ import annotations
import html


# ----------------------------- helpers ----------------------------- #
def _esc(t) -> str:
    return html.escape(str(t or ""))


def _linha_local(it) -> str:
    m, uf = it.get("municipio") or "", it.get("uf") or ""
    if m and uf:
        return f"{m}/{uf}"
    return m or uf or ""


def item_edital(ed, nivel: str | None = None) -> dict:
    """Constrói o dict padrão de um edital a partir do modelo Edital, usado
    tanto pelo aviso de novas oportunidades quanto pelos lembretes de prazo/
    abertura — mantém os e-mails com as mesmas informações (valor, local,
    datas) em vez de cada chamador montar um subconjunto diferente."""
    return {
        "objeto": ed.objeto, "orgao": ed.orgao,
        "municipio": ed.municipio, "uf": ed.uf, "link": ed.link,
        "valor_estimado": ed.valor_estimado,
        "abertura": str(ed.data_abertura) if ed.data_abertura else "",
        "encerramento": str(ed.data_encerramento) if ed.data_encerramento else "",
        "nivel": nivel,
    }


# ----------------------------- E-MAIL (HTML) ----------------------------- #
# Mesma identidade visual usada no dashboard e nos e-mails de conta (cabeçalho
# escuro + azul de destaque) — antes esta função usava roxo, uma cor que não
# aparece em nenhum outro lugar do produto.
_ESCURO = "#1a2129"
_AZUL = "#2563eb"
_COR_NIVEL = {"forte": "#16a34a", "medio": "#ca8a04"}
_BG_NIVEL = {"forte": "rgba(22,163,74,.1)", "medio": "rgba(202,138,4,.1)"}
_NOME_NIVEL = {"forte": "Alta compatibilidade", "medio": "Média compatibilidade"}


def _formatar_valor(v) -> str | None:
    if not v:
        return None
    try:
        return f"R$ {float(v):,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")
    except (TypeError, ValueError):
        return None


def _pill_nivel(nivel: str | None) -> str:
    if nivel not in _COR_NIVEL:
        return ""
    cor, bg, nome = _COR_NIVEL[nivel], _BG_NIVEL[nivel], _NOME_NIVEL[nivel]
    return (f'<span style="display:inline-block;padding:2px 9px;border-radius:20px;'
            f'font-size:11px;font-weight:700;text-transform:uppercase;'
            f'color:{cor};background:{bg};margin-bottom:8px">{nome}</span><br>')


def email_html(titulo: str, intro: str, itens: list[dict],
               rotulo_data: str = "") -> str:
    """Monta um e-mail HTML com uma lista de editais em cartões."""
    cartoes = []
    for it in itens:
        local = _linha_local(it)
        linhas = []
        valor = _formatar_valor(it.get("valor_estimado"))
        if valor:
            linhas.append(f"<b>Valor estimado:</b> {_esc(valor)}")
        if it.get("abertura"):
            linhas.append(f"<b>Abertura:</b> {_esc(it['abertura'])}")
        if it.get("encerramento"):
            linhas.append(f"<b>Encerra em:</b> {_esc(it['encerramento'])}")
        if local:
            linhas.append(f"<b>Local:</b> {_esc(local)}")
        if it.get("extra"):
            linhas.append(_esc(it["extra"]))
        meta = "<br>".join(linhas)
        botao = ""
        if it.get("link"):
            botao = (
                f'<a href="{_esc(it["link"])}" '
                f'style="display:inline-block;margin-top:10px;padding:8px 16px;'
                f'background:{_AZUL};color:#fff;text-decoration:none;border-radius:6px;'
                f'font-size:14px">Abrir edital</a>'
            )
        cartoes.append(f"""
        <div style="border:1px solid #e5e7eb;border-radius:10px;padding:16px;margin:12px 0;background:#fff">
          {_pill_nivel(it.get('nivel'))}
          <div style="font-weight:600;color:#111;font-size:15px;margin-bottom:4px">{_esc(it.get('orgao') or 'Edital')}</div>
          <div style="color:#374151;font-size:14px;margin-bottom:8px">{_esc((it.get('objeto') or '')[:400])}</div>
          <div style="color:#6b7280;font-size:13px;line-height:1.6">{meta}</div>
          {botao}
        </div>""")

    corpo_cartoes = "".join(cartoes) if cartoes else \
        '<p style="color:#6b7280">Nenhum edital nesta categoria hoje.</p>'

    return f"""<!DOCTYPE html><html><body style="margin:0;background:#f4f6f9;padding:20px;font-family:Arial,Helvetica,sans-serif">
  <div style="max-width:600px;margin:0 auto">
    <div style="background:{_ESCURO};padding:18px 20px;border-radius:10px 10px 0 0">
      <div style="color:#8b98a5;font-size:11px;font-weight:600;letter-spacing:.4px;text-transform:uppercase;margin-bottom:4px">Radar de Licitações</div>
      <div style="color:#fff;font-size:18px;font-weight:700">{_esc(titulo)}</div>
    </div>
    <div style="background:#fff;padding:16px 20px;border-radius:0 0 10px 10px;border:1px solid #e5e7eb;border-top:none">
      <p style="color:#374151;font-size:14px;margin:4px 0 8px">{_esc(intro)}</p>
      {corpo_cartoes}
      <p style="color:#9ca3af;font-size:12px;margin-top:20px;border-top:1px solid #eee;padding-top:12px">
        Radar de Licitações · você recebe este aviso porque ativou as notificações no seu perfil.
      </p>
    </div>
  </div>
</body></html>"""


def email_texto(intro: str, itens: list[dict]) -> str:
    """Versão texto puro do e-mail (fallback para clientes sem HTML)."""
    partes = [intro, ""]
    for it in itens:
        partes.append(f"• {it.get('orgao') or 'Edital'}")
        if it.get("objeto"):
            partes.append(f"  {(it['objeto'] or '')[:300]}")
        if it.get("abertura"):
            partes.append(f"  Abertura: {it['abertura']}")
        if it.get("encerramento"):
            partes.append(f"  Encerra em: {it['encerramento']}")
        local = _linha_local(it)
        if local:
            partes.append(f"  Local: {local}")
        if it.get("link"):
            partes.append(f"  {it['link']}")
        partes.append("")
    return "\n".join(partes)


# ----------------------------- TELEGRAM (HTML) ----------------------------- #
def telegram_item(titulo: str, it: dict) -> tuple[str, str, str | None]:
    """Monta uma mensagem de Telegram para UM edital.
    Retorna (titulo_negrito, corpo, link_para_botao)."""
    linhas = []
    if it.get("orgao"):
        linhas.append(f"<b>{_esc(it['orgao'])}</b>")
    if it.get("objeto"):
        linhas.append(_esc((it["objeto"] or "")[:400]))
    linhas.append("")
    valor = _formatar_valor(it.get("valor_estimado"))
    if valor:
        linhas.append(f"<b>Valor estimado:</b> {_esc(valor)}")
    if it.get("abertura"):
        linhas.append(f"<b>Abertura:</b> {_esc(it['abertura'])}")
    if it.get("encerramento"):
        linhas.append(f"<b>Encerra em:</b> {_esc(it['encerramento'])}")
    local = _linha_local(it)
    if local:
        linhas.append(f"<b>Local:</b> {_esc(local)}")
    if it.get("extra"):
        linhas.append(_esc(it["extra"]))
    return f"<b>{_esc(titulo)}</b>", "\n".join(linhas), it.get("link")


def telegram_resumo(titulo: str, intro: str, itens: list[dict]) -> str:
    """Monta uma mensagem única de Telegram com vários editais (com links)."""
    partes = [f"<b>{_esc(titulo)}</b>", "", _esc(intro), ""]
    for it in itens:
        orgao = _esc(it.get("orgao") or "Edital")
        obj = _esc((it.get("objeto") or "")[:140])
        linha = f"• <b>{orgao}</b> — {obj}"
        if it.get("link"):
            linha += f'\n  <a href="{_esc(it["link"])}">Abrir edital</a>'
        partes.append(linha)
    return "\n".join(partes)
