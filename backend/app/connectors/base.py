"""Interface base para conectores de portais de licitação."""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import date, datetime


@dataclass
class ItemColetado:
    numero: int | None
    descricao: str
    material_ou_servico: str | None = None
    ncm: str | None = None
    catalogo_codigo: str | None = None
    quantidade: float | None = None
    valor_unitario: float | None = None
    unidade_medida: str | None = None


@dataclass
class EditalColetado:
    fonte: str
    id_externo: str
    orgao: str | None = None
    cnpj_orgao: str | None = None
    objeto: str | None = None
    modalidade: str | None = None
    uf: str | None = None
    municipio: str | None = None
    valor_estimado: float | None = None
    data_publicacao: date | None = None
    # datetime (não date): pedido do usuário -- data de início/fim de
    # recebimento de propostas precisa mostrar a HORA (ex.: "09h00") e o
    # status "recebendo proposta"/"encerrado" precisa respeitar a hora, não
    # só o dia (um edital que fecha às 09h de hoje já está encerrado, não
    # "encerra hoje" o dia inteiro). O PNCP já manda a hora no campo raw
    # (dataAberturaProposta/dataEncerramentoProposta) -- só nunca foi
    # preservada (ver _parse_data_hora em connectors/pncp.py).
    data_abertura: datetime | None = None
    data_encerramento: datetime | None = None
    link: str | None = None
    plataforma: str | None = None
    link_sistema_origem: str | None = None
    categoria_pncp: str | None = None
    itens: list[ItemColetado] = field(default_factory=list)
    raw: dict | None = None


class BaseConnector:
    nome: str = "base"

    def coletar(self) -> list[EditalColetado]:
        """Retorna a lista de editais coletados na execução."""
        raise NotImplementedError
