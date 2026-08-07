"""Interface base para conectores de portais de licitação."""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import date


@dataclass
class ItemColetado:
    numero: int | None
    descricao: str
    material_ou_servico: str | None = None
    ncm: str | None = None
    catalogo_codigo: str | None = None
    quantidade: float | None = None
    valor_unitario: float | None = None


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
    data_abertura: date | None = None
    data_encerramento: date | None = None
    link: str | None = None
    categoria_pncp: str | None = None
    itens: list[ItemColetado] = field(default_factory=list)
    raw: dict | None = None


class BaseConnector:
    nome: str = "base"

    def coletar(self) -> list[EditalColetado]:
        """Retorna a lista de editais coletados na execução."""
        raise NotImplementedError
