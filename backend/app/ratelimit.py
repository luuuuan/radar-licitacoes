"""
Rate limit em memória para as rotas de autenticação (login, cadastro,
esqueci-senha, redefinir-senha) -- a única barreira contra força bruta/spam
nelas, já que rodam sem sessão (get_current_user não se aplica: o usuário
ainda não provou quem é). Guarda os contadores no processo: o deploy (Render
plano free) sobe uma única instância/worker, então isso já é suficiente e
evita depender de Redis só para isso -- um restart zera os contadores, o
que é aceitável aqui (é só desencorajar automação, não billing).

Cada chamada de `checar()` conta como uma tentativa nova e levanta 429 se a
chave já bateu o limite dentro da janela. Duas chaves por ação (IP e o alvo
específico -- e-mail) pegam ataques diferentes: só por IP pega força bruta
distribuída entre muitos e-mails a partir de uma máquina; só por e-mail pega
credential stuffing rotacionando IP contra uma conta só.
"""
from __future__ import annotations
import threading
import time

from fastapi import HTTPException, Request

_lock = threading.Lock()
# chave -> lista de timestamps (monotonic, em segundos) das tentativas
# ainda dentro da janela em que foram registradas
_tentativas: dict[str, list[float]] = {}

# segurança contra a própria estrutura crescer sem limite (e-mail no corpo
# da requisição é texto livre controlado por quem chama -- um atacante
# mandando um e-mail novo a cada tentativa criaria uma chave nova por
# tentativa). Ao passar disso, uma varredura oportunista descarta chaves
# há muito inativas antes de seguir.
_MAX_CHAVES = 10_000
_INATIVA_APOS_SEG = 3600


def ip_cliente(request: Request) -> str:
    # Render fica atrás de proxy (e a maioria das instalações, atrás de
    # Cloudflare também): o IP real do visitante vem em X-Forwarded-For,
    # não em request.client -- sem isso, todo mundo cairia na mesma chave
    # (o IP do proxy) e o limite por IP nunca faria sentido.
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "desconhecido"


def checar(chave: str, limite: int, janela_seg: int) -> None:
    """Levanta HTTPException 429 se `chave` já bateu `limite` tentativas
    dentro dos últimos `janela_seg` segundos; senão registra mais uma."""
    agora = time.monotonic()
    with _lock:
        if len(_tentativas) > _MAX_CHAVES:
            for k in list(_tentativas):
                ultima = _tentativas[k][-1] if _tentativas[k] else 0
                if agora - ultima > _INATIVA_APOS_SEG:
                    del _tentativas[k]
        vivas = [t for t in _tentativas.get(chave, []) if agora - t < janela_seg]
        if len(vivas) >= limite:
            espera = int(janela_seg - (agora - vivas[0])) + 1
            raise HTTPException(429, f"Muitas tentativas. Tente de novo em {espera}s.",
                                headers={"Retry-After": str(espera)})
        vivas.append(agora)
        _tentativas[chave] = vivas


def limpar(chave: str) -> None:
    """Zera o contador de uma chave (ex.: login que deu certo) -- assim
    quem só errou a senha uma vez antes de acertar não fica penalizado."""
    with _lock:
        _tentativas.pop(chave, None)
