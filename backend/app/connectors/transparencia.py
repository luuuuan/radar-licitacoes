"""
Conector do Portal da Transparência (licitações do Poder Executivo FEDERAL).

Fonte complementar ao PNCP. Cobre apenas licitações federais e exige um token
gratuito (cabeçalho `chave-api-dados`), obtido com cadastro no gov.br em
https://api.portaldatransparencia.gov.br/ .

É opcional e à prova de falhas: se o token não estiver configurado ou a API
falhar, retorna lista vazia sem interromper a coleta do PNCP.
"""
from __future__ import annotations
import logging
import time
from datetime import date, datetime, timedelta

import requests

from .base import BaseConnector, EditalColetado, ItemColetado
from ..config import settings

log = logging.getLogger("conectores.transparencia")

_BASE = "https://api.portaldatransparencia.gov.br/api-de-dados/licitacoes"


def _parse_data(valor: str | None) -> date | None:
    if not valor:
        return None
    for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(valor[:10], fmt).date()
        except ValueError:
            continue
    return None


class TransparenciaConnector(BaseConnector):
    nome = "transparencia"

    def __init__(self, horizonte: int = 30, max_paginas: int = 10):
        self.token = settings.PORTAL_TRANSPARENCIA_TOKEN
        self.horizonte = horizonte
        self.max_paginas = max_paginas

    def disponivel(self) -> bool:
        return bool(settings.PORTAL_TRANSPARENCIA_ATIVO and self.token)

    def coletar(self) -> list[EditalColetado]:
        if not self.disponivel():
            return []
        hoje = date.today()
        ini = (hoje - timedelta(days=3)).strftime("%d/%m/%Y")
        fim = (hoje + timedelta(days=self.horizonte)).strftime("%d/%m/%Y")
        headers = {"chave-api-dados": self.token, "Accept": "application/json"}

        coletados: list[EditalColetado] = []
        for pagina in range(1, self.max_paginas + 1):
            params = {"dataInicial": ini, "dataFinal": fim, "pagina": pagina}
            try:
                r = requests.get(_BASE, params=params, headers=headers, timeout=30)
            except requests.RequestException as e:
                log.warning("Transparência: falha de rede (pág %s): %s", pagina, e)
                break
            if r.status_code == 401:
                log.warning("Transparência: token inválido ou ausente.")
                break
            if r.status_code != 200:
                log.warning("Transparência: HTTP %s (pág %s)", r.status_code, pagina)
                break
            try:
                lote = r.json()
            except ValueError:
                break
            if not lote:
                break  # acabou
            for reg in lote:
                ed = self._mapear(reg)
                if ed:
                    coletados.append(ed)
            time.sleep(0.3)
        log.info("Transparência: %s licitações federais coletadas.", len(coletados))
        return coletados

    def _mapear(self, reg: dict) -> EditalColetado | None:
        """Converte um registro da API no formato comum. Defensivo: campos podem
        variar, então usa .get em tudo e nunca quebra."""
        try:
            lic = reg.get("licitacao", reg) if isinstance(reg, dict) else {}
            ident = str(reg.get("id") or lic.get("numero") or reg.get("numero") or "")
            if not ident:
                return None
            orgao = (reg.get("unidadeGestora") or {}).get("orgaoVinculado", {}).get("nome") \
                or (reg.get("orgao") or {}).get("nome") or reg.get("nomeOrgao")
            objeto = reg.get("objeto") or lic.get("objeto") or ""
            modalidade = reg.get("modalidadeLicitacao") or reg.get("modalidade") or ""
            valor = reg.get("valor") or reg.get("valorLicitacao")
            try:
                valor = float(str(valor).replace(".", "").replace(",", ".")) if valor else None
            except (ValueError, TypeError):
                valor = None
            return EditalColetado(
                fonte="transparencia",
                id_externo=f"transp-{ident}",
                orgao=orgao,
                objeto=objeto,
                modalidade=str(modalidade),
                uf=reg.get("uf") or None,
                municipio=reg.get("municipio") or None,
                valor_estimado=valor,
                data_publicacao=_parse_data(reg.get("dataPublicacao") or reg.get("dataResultadoCompra")),
                data_abertura=_parse_data(reg.get("dataAbertura")),
                link=reg.get("linkLicitacao") or None,
                itens=[ItemColetado(numero=1, descricao=objeto[:300])] if objeto else [],
                raw=reg,
            )
        except Exception:
            log.exception("Transparência: erro ao mapear registro")
            return None
