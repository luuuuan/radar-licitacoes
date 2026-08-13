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
    PNCP_DELAY: float = 0.5
    # Re-tentativas quando o PNCP falha/instabiliza (timeout, 5xx, 429)
    PNCP_TENTATIVAS: int = 3

    # Matching / pontuação
    LIMIAR_FORTE: float = 0.62
    LIMIAR_MEDIO: float = 0.40
    # Acima desse score o item é considerado compatível — usado só pro
    # nível/score AGREGADO do edital (decide "forte"/"médio"/notificar), que
    # continua calculado direto do motor. NÃO decide mais sozinho se um item
    # individual entra em cotação/margem — ver LIMIAR_ITEM_ALTA/
    # LIMIAR_ITEM_SUGESTAO logo abaixo, que fazem esse papel por item.
    LIMIAR_ITEM: float = 0.5
    # Faixas de confiança POR ITEM (não confundir com LIMIAR_ITEM acima, que
    # é agregado). Score >= ALTA: usado automático, sem pedir confirmação
    # (mas o usuário sempre pode trocar — código NCM/CATMAT exato não é
    # garantia de ser o mesmo produto, já teve caso real de código batendo
    # com item sem nada a ver). Entre SUGESTAO e ALTA: mostra como sugestão,
    # não entra em cotação/margem/Inteligência de Preço até o usuário
    # confirmar. Abaixo de SUGESTAO: não mostra nada.
    # Calibrado com o score do RERANKER (Qwen3-Reranker via DeepInfra, ver
    # matching/engine.py) — testes reais mostraram matches genuínos sempre
    # >=0.89 (chegando a 0.9999) e falsos positivos (ex.: mesmo código fiscal
    # amplo, produtos sem relação nenhuma) sempre <=0.014. Recalibração
    # reconfirmada em ago/2026 na troca pro modelo 4B + instrução no prompt
    # (ver DEEPINFRA_MODELO_RERANKER): matches genuínos continuaram >=0.9 nos
    # casos auditados, sem precisar mexer nesses limiares. Ainda é um chute
    # inicial (poucos dados reais até agora) — recalibrar com produção
    # depois, mesmo espírito de quando esses limiares foram criados.
    LIMIAR_ITEM_ALTA: float = 0.85
    LIMIAR_ITEM_SUGESTAO: float = 0.3
    # Achado real: item "GRAMPEADOR" bateu com "Grampo para Grampeador"
    # (score 1.0) em vez do grampeador de verdade que existia no catálogo
    # (score 0.999) — diferença de 0.001, um empate técnico que o reranker
    # claramente não tinha certeza (o nome do produto errado contém a
    # palavra "grampeador", confundindo o modelo). Quando o 2º colocado fica
    # a menos que essa margem do 1º, não confia automático mesmo com score
    # alto — vira sugestão (confiança "média"), pedindo confirmação.
    MARGEM_AMBIGUA_ITEM: float = 0.02
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
    BREVO_FROM_NOME: str = "Minha Licitação"

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

    # IA semântica (Gemini embeddings) — usada só pela Análise por IA do
    # edital hoje (analise_edital.py); o motor de matching usa o reranker
    # da DeepInfra (ver DEEPINFRA_MODELO_RERANKER, abaixo).
    GEMINI_API_KEY: str = ""
    # gemini-embedding-001 será desligado em 14/07/2026 -> gemini-embedding-2
    IA_MODELO_EMBEDDING: str = "gemini-embedding-2"
    # gemini-2.5-flash será desligado em 16/10/2026 -> gemini-3.5-flash
    IA_MODELO_TEXTO: str = "gemini-3.5-flash"   # análise de editais (texto)

    # IA extra (DeepInfra) — opcional. Ao contrário da GEMINI_API_KEY (chave
    # do PRÓPRIO usuário, BYOK), esta é uma chave GLOBAL paga pelo operador
    # do app — vale pra todos os usuários, mesmo quem não configurou uma
    # chave Gemini pessoal.
    DEEPINFRA_API_KEY: str = ""
    DEEPINFRA_MODELO_EMBEDDING: str = "BAAI/bge-m3"
    # Reranker (cross-encoder) usado pelo motor de matching pra julgar a
    # compatibilidade item↔produto — ver matching/engine.py. Substituiu
    # TF-IDF + palavras-chave + embeddings bi-encoder: testado contra a API
    # real, separa com muito mais clareza match genuíno (score ~0.9-1.0) de
    # coincidência textual/código fiscal amplo sem relação real (~0.0-0.01)
    # do que qualquer heurística ou cosseno de embeddings conseguia.
    # 4B (não o 0.6B menor/mais barato) — achado real em auditoria de
    # produção (ago/2026): o 0.6B dava "alta" (automático) pra pares sem
    # relação real quando o catálogo não tinha nada equivalente (remédio
    # batendo com produto de limpeza, fralda com copo, etc). Teste A/B
    # contra a API real (9 casos graves): 0.6B só corrigiu 3/9, 4B corrigiu
    # 9/9 — empatado com o 8B, que custa o dobro sem ganho adicional medido.
    # Ver `MatchingEngine._INSTRUCAO_RERANKER` em matching/engine.py — a
    # instrução no prompt é parte necessária dessa correção, não só o
    # tamanho do modelo (0.6B + instrução ainda deixava passar 6 dos 9).
    DEEPINFRA_MODELO_RERANKER: str = "Qwen/Qwen3-Reranker-4B"
    # Modelo de CHAT (não embedding/reranker) na mesma DeepInfra/mesma chave
    # global — usado só pra completar a descrição de itens lendo o PDF do
    # edital (ver app/itens_pdf.py). Testado contra a API real: DeepSeek-V3-0324
    # tinha latência MUITO inconsistente (de 1s a >180s/timeout, mesmo em
    # prompts pequenos) — Llama-3.3-70B-Instruct-Turbo, testado repetidas
    # vezes com o mesmo edital/itens, ficou sempre entre 17-64s e sempre
    # achou os itens certos. Mais barato também. Ajustar o nome do modelo
    # se a DeepInfra descontinuar/renomear.
    DEEPINFRA_MODELO_CHAT: str = "meta-llama/Llama-3.3-70B-Instruct-Turbo"

    # OCR de PDF escaneado (Tesseract, grátis e local). Pesado: limites apertados.
    # Requer no servidor os pacotes de sistema 'tesseract-ocr', 'tesseract-ocr-por'
    # e 'poppler-utils' (ver render/Dockerfile).
    OCR_ATIVO: bool = True
    OCR_MAX_PAGINAS: int = 12    # só as primeiras N páginas (controla custo de CPU)
    OCR_DPI: int = 200           # resolução do raster (menor = mais rápido)
    OCR_IDIOMA: str = "por"      # português

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
