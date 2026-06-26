"""
Configurações centrais do Radar de Licitações.
Tudo é lido de variáveis de ambiente (arquivo .env). Veja .env.example.
"""
from functools import lru_cache
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Autenticação (HTTP Basic). Se ambos vazios, a API fica aberta (dev local).
    BASIC_AUTH_USER: str = ""
    BASIC_AUTH_PASS: str = ""

    # Segurança multiusuário
    # SECRET_KEY assina os tokens de sessão (JWT). Em produção, defina no Render!
    SECRET_KEY: str = "troque-isto-em-producao-please-32+chars-aleatorios"
    # APP_ENCRYPTION_KEY cifra dados sensíveis (chave Gemini, CPF/CNPJ). Se vazio,
    # é derivada da SECRET_KEY. Defina no Render para algo estável e secreto.
    APP_ENCRYPTION_KEY: str = ""
    TOKEN_EXPIRA_HORAS: int = 24 * 7        # sessão dura 7 dias
    # URL pública do app (para links de verificação de e-mail). Ex.: https://...onrender.com
    APP_BASE_URL: str = ""
    # Banco de dados
    DATABASE_URL: str = "postgresql+psycopg2://radar:radar@db:5432/radar"

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

    # Fonte extra: Portal da Transparência (licitações FEDERAIS). Opcional.
    # Token gratuito em api.portaldatransparencia.gov.br (cadastro gov.br).
    PORTAL_TRANSPARENCIA_TOKEN: str = ""
    PORTAL_TRANSPARENCIA_ATIVO: bool = False
    PNCP_TAMANHO_PAGINA: int = 50
    # Atraso entre requisições para não sobrecarregar o portal (segundos)
    PNCP_DELAY: float = 0.3
    # Re-tentativas quando o PNCP falha/instabiliza (timeout, 5xx, 429)
    PNCP_TENTATIVAS: int = 3

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

    # E-mail via API Brevo (HTTPS — funciona no Render, que bloqueia SMTP).
    # Envia para qualquer destinatário sem precisar verificar domínio.
    BREVO_API_KEY: str = ""
    BREVO_FROM_EMAIL: str = ""   # ex.: "voce@gmail.com" (remetente verificado no Brevo)
    BREVO_FROM_NOME: str = "Radar de Licitações"

    # Notificações por Telegram
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHAT_ID: str = ""
    # Bot compartilhado (multiusuário): username do bot e segredo do webhook
    TELEGRAM_BOT_USERNAME: str = ""   # ex.: "RadarLicitacoesBot" (sem @)
    TELEGRAM_WEBHOOK_SECRET: str = ""  # segredo que protege o endpoint do webhook

    # Só notifica matches deste nível pra cima: "forte" ou "medio"
    NOTIFICAR_NIVEL_MINIMO: str = "forte"

    # Lembretes
    LEMBRETE_PRAZO_DIAS: int = 2     # avisa quando faltam <= X dias p/ encerrar proposta
    LEMBRETE_DOC_DIAS: int = 15      # avisa quando um documento vence em <= X dias

    # IA semântica (Gemini embeddings) — opcional
    GEMINI_API_KEY: str = ""
    IA_MODELO_EMBEDDING: str = "gemini-embedding-001"
    IA_MODELO_TEXTO: str = "gemini-2.5-flash"   # análise de editais (texto)
    # peso da IA no score final (0..1). 0.4 = 60% texto + 40% IA.
    IA_PESO: float = 0.4
    # piso de similaridade: cosseno abaixo disso conta como 0 (evita que a
    # "linha de base" alta dos embeddings infle itens sem relação).
    IA_FLOOR: float = 0.5
    # sinal textual mínimo do edital para valer a pena gastar IA nele.
    # Editais sem nenhuma relação (texto ~0) não chamam a IA -> economiza cota.
    IA_MIN_SINAL: float = 0.12

    # Chave para disparar a coleta via HTTP (endpoint /api/coletar-cron).
    # Se vazia, o endpoint fica desativado.
    CRON_SECRET: str = ""

    @field_validator("SMTP_PORT", mode="before")
    @classmethod
    def _porta_vazia_vira_padrao(cls, v):
        # No GitHub Actions, um secret não definido chega como "" e quebraria o int.
        if v is None or (isinstance(v, str) and v.strip() == ""):
            return 587
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()


def parse_csv_ints(valor: str) -> list[int]:
    return [int(x.strip()) for x in valor.split(",") if x.strip()]


def parse_csv_str(valor: str) -> list[str]:
    return [x.strip().upper() for x in valor.split(",") if x.strip()]
