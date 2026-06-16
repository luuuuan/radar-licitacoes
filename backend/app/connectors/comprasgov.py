"""
Conector-EXEMPLO para o Compras.gov.br (dados abertos de contratações federais).

⚠️ ESTE É UM ESQUELETO, NÃO UM CONECTOR ATIVO. Ele existe para demonstrar como
adicionar uma nova fonte: basta herdar de BaseConnector, implementar coletar()
devolvendo EditalColetado/ItemColetado, e registrar a classe em service.py
(na lista de conectores). Todo o resto — matching, score, dashboard,
notificações — funciona sem nenhuma alteração.

Por padrão NÃO é usado (PNCP já centraliza federal/estadual/municipal sob a
Lei 14.133). Para ativá-lo de verdade, é preciso mapear os endpoints/campos do
módulo de contratações em https://dadosabertos.compras.gov.br e validar a
resposta — por isso ele está como stub honesto, e não como algo que "funciona"
no papel mas quebra na prática.
"""
from __future__ import annotations
import logging

from .base import BaseConnector, EditalColetado

log = logging.getLogger("conector.comprasgov")


class ComprasGovConnector(BaseConnector):
    nome = "COMPRASGOV"

    def coletar(self) -> list[EditalColetado]:
        # TODO: implementar a chamada ao módulo de contratações do Compras.gov.br
        #       e mapear os campos para EditalColetado/ItemColetado.
        log.info("ComprasGovConnector é um stub; nada coletado.")
        return []
