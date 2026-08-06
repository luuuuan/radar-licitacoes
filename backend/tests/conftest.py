"""
Fixtures globais da suíte de testes.
"""
import pytest

from app.config import settings


@pytest.fixture(autouse=True)
def _sem_chaves_externas_por_padrao(monkeypatch):
    """Zera as chaves de IA por padrão em TODO teste — sem isso, um `.env`
    local com chaves reais (usadas pra testar manualmente contra as APIs de
    verdade, prática comum nesta sessão) faz testes "offline" chamarem rede
    de verdade sem querer (achado real: DEEPINFRA_API_KEY no .env local
    fazia MatchingEngine.usar_ia virar True e testes de service.py sem
    nenhum mock chamarem o reranker de verdade). Testes que precisam de uma
    chave (real ou fake) setam explicitamente com monkeypatch."""
    monkeypatch.setattr(settings, "DEEPINFRA_API_KEY", "")
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "")
