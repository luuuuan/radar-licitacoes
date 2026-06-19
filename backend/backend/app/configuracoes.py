"""
Configurações editáveis pelo painel, guardadas no banco (tabela configuracoes).
Quando não há valor no banco, cai no valor da variável de ambiente (settings).
"""
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .models import Configuracao

# chaves suportadas e o fallback (de onde vem o valor se o painel não definiu)
_FALLBACK = {
    "PNCP_UFS": lambda: settings.PNCP_UFS,
    "PNCP_MODALIDADES": lambda: settings.PNCP_MODALIDADES,
    "PNCP_HORIZONTE_DIAS": lambda: str(settings.PNCP_HORIZONTE_DIAS),
    "IA_ATIVA": lambda: "0",  # IA semântica ligada/desligada pelo painel
}


def obter(db: Session, chave: str) -> str:
    row = db.get(Configuracao, chave)
    if row is not None and row.valor != "":
        return row.valor
    fb = _FALLBACK.get(chave)
    return fb() if fb else ""


def definir(db: Session, chave: str, valor: str) -> None:
    row = db.get(Configuracao, chave)
    if row is None:
        row = Configuracao(chave=chave, valor=valor or "")
        db.add(row)
    else:
        row.valor = valor or ""
    db.commit()


def todas(db: Session) -> dict:
    return {chave: obter(db, chave) for chave in _FALLBACK}
