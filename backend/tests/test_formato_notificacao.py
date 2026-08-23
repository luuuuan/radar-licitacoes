"""
formato.item_edital() -- link "Abrir edital" das notificações (e-mail/
Telegram). Pedido do usuário: apontar pra página do próprio site, não pro
portal de origem (PNCP) -- ver formato._link_edital(). Rode com:
cd backend && pytest
"""
from app.config import settings
from app.notifications import formato
from app.models import Edital


def _edital(**kwargs):
    base = dict(id=42, fonte="PNCP", id_externo="123", orgao="Orgao Teste",
               objeto="Aquisicao", uf="SP", link="https://pncp.gov.br/app/editais/123")
    base.update(kwargs)
    return Edital(**base)


def test_link_aponta_pro_proprio_site_quando_app_base_url_configurado(monkeypatch):
    monkeypatch.setattr(settings, "APP_BASE_URL", "https://app.minhalicitacao.com")
    ed = _edital()

    item = formato.item_edital(ed)

    assert item["link"] == "https://app.minhalicitacao.com/edital/42"


def test_link_remove_barra_duplicada_quando_app_base_url_termina_com_barra(monkeypatch):
    monkeypatch.setattr(settings, "APP_BASE_URL", "https://app.minhalicitacao.com/")
    ed = _edital()

    item = formato.item_edital(ed)

    assert item["link"] == "https://app.minhalicitacao.com/edital/42"


def test_link_cai_pro_link_do_portal_sem_app_base_url_configurado(monkeypatch):
    monkeypatch.setattr(settings, "APP_BASE_URL", "")
    ed = _edital(link="https://pncp.gov.br/app/editais/123")

    item = formato.item_edital(ed)

    assert item["link"] == "https://pncp.gov.br/app/editais/123"


def test_demais_campos_do_item_continuam_montados_normalmente(monkeypatch):
    monkeypatch.setattr(settings, "APP_BASE_URL", "https://app.minhalicitacao.com")
    ed = _edital(orgao="Prefeitura X", objeto="Compra de papel", municipio="Curitiba", uf="PR")

    item = formato.item_edital(ed, nivel="forte")

    assert item["orgao"] == "Prefeitura X"
    assert item["objeto"] == "Compra de papel"
    assert item["municipio"] == "Curitiba"
    assert item["uf"] == "PR"
    assert item["nivel"] == "forte"
