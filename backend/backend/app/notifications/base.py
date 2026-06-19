"""Interface base para canais de notificação."""
from __future__ import annotations


class BaseNotifier:
    nome: str = "base"

    def disponivel(self) -> bool:
        """True se o canal está configurado e pode enviar."""
        return False

    def enviar(self, titulo: str, corpo: str) -> bool:
        raise NotImplementedError
