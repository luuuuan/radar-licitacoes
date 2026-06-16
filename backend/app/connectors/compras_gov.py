"""
Conector-EXEMPLO do Compras.gov.br (módulo de contratações de dados abertos).

⚠️ STUB / ESQUELETO — NÃO está ativo por padrão e NÃO deve ser tratado como
fonte funcional ainda. Existe para demonstrar como um segundo portal se encaixa
na interface BaseConnector, reaproveitando todo o resto do sistema (matching,
score, dashboard, notificações) sem alterar nada.

Para ativá-lo de verdade, falta:
  1. Confirmar no Swagger o endpoint e os parâmetros do módulo de contratações:
     https://dadosabertos.compras.gov.br/swagger-ui/index.html
  2. Mapear os campos da resposta para EditalColetado (ver _mapear_edital do PNCP).
  3. Registrar a classe em service.processar_coleta (lista de conectores).

Enquanto isso, coletar() devolve lista vazia — então não "mente" trazendo dados.
"""
from __future__ import annotations
import logging

import requests

from .base import BaseConnector, EditalColetado

log = logging.getLogger("conector.compras_gov")


class ComprasGovConnector(BaseConnector):
    nome = "COMPRAS_GOV"

    # Marque como True só depois de mapear endpoint/campos (ver docstring).
    ATIVO = False

    def __init__(self, session: requests.Session | None = None):
        self.base = "https://dadosabertos.compras.gov.br"
        self.http = session or requests.Session()
        self.http.headers.update({"Accept": "application/json",
                                  "User-Agent": "RadarLicitacoes/1.0"})

    def coletar(self) -> list[EditalColetado]:
        if not self.ATIVO:
            log.info("ComprasGovConnector é um stub e está desativado.")
            return []
        # TODO: implementar paginação e _mapear_edital como no PNCPConnector.
        return []
