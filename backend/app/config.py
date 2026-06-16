"""
Configurações centrais do Radar de Licitações.
Tudo é lido de variáveis de ambiente (arquivo .env). Veja .env.example.
"""
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Autenticação (HTTP Basic). Se ambos vazios, a API fica aberta (dev local).
    BASIC_AUTH_USER: str = ""
    BASIC_AUTH_PASS: str = ""

    # Banco de dados
    DATABASE_URL: str = "postgresql+psycopg2://radar:radar@db:5432/radar"

    # Redis / Celery
    REDIS_URL: str = "redis://redis:6379/0"

    # PNCP (API pública de consultas — Lei 14.133/2021)
    PNCP_BASE_URL: str = "https://pncp.gov.br/api/consulta"
    PNCP_ITENS_BASE_URL: str = "https://pncp.gov.br/api/pncp"
    # Modalidades a monitorar (6=Pregão Eletrônico, 8=Dispensa, 9=Inexigibilidade,
    # 4=Concorrência Eletrônica). Veja tabela de domínio no README.
    PNCP_MODALIDADES: str = "6,8,9,4"
    # UFs a monitorar (vazio = todas). Ex.: "PR,SP,RJ,MG,BA"
    PNCP_UFS: str = ""
    # Quantos dias à frente buscar editais com proposta em aberto
    PNCP_HORIZONTE_DIAS: int = 30
    PNCP_TAMANHO_PAGINA: int = 50
    # Atraso entre requisições para não sobrecarregar o portal (segundos)
    PNCP_DELAY: float = 0.3

    # Matching / pontuação
    LIMIAR_FORTE: float = 0.62
    LIMIAR_MEDIO: float = 0.40
    # Acima desse score textual o item é considerado compatível
    LIMIAR_ITEM: float = 0.35
    # Exige cobertura mínima de itens para classificar como "forte", MAS só
    # para matches fuzzy/textuais — um casamento por código exato (NCM/CATMAT)
    # continua forte mesmo sendo 1 item. 0 = desliga. 0.05 = 5% dos itens.
    FRACAO_MINIMA_FORTE: float = 0.05

    # Notificações por e-mail (SMTP)
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM: str = ""
    NOTIFICAR_EMAIL: str = ""  # destinatário dos alertas

    # Notificações por Telegram
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHAT_ID: str = ""

    # Só notifica matches deste nível pra cima: "forte" ou "medio"
    NOTIFICAR_NIVEL_MINIMO: str = "forte"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()


def parse_csv_ints(valor: str) -> list[int]:
    return [int(x.strip()) for x in valor.split(",") if x.strip()]


def parse_csv_str(valor: str) -> list[str]:
    return [x.strip().upper() for x in valor.split(",") if x.strip()]
