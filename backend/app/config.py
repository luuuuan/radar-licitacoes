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
    TOKEN_EXPIRA_HORAS: int = 24 * 7        # teto absoluto da sessão: 7 dias, mesmo com uso contínuo
    # Sessão também expira por inatividade: cada requisição autenticada
    # renova o cookie por mais TOKEN_IDLE_HORAS (ver auth.criar_token), sem
    # nunca passar do teto absoluto acima. Fica sozinho sem usar o site por
    # mais que isso e precisa logar de novo, mesmo dentro dos 7 dias.
    TOKEN_IDLE_HORAS: int = 2
    # URL pública do app (para links de verificação de e-mail). Ex.: https://...onrender.com
    APP_BASE_URL: str = ""
    # Banco de dados
    DATABASE_URL: str = "postgresql+psycopg2://radar:radar@db:5432/radar"

    # Bot protection no cadastro (Cloudflare Turnstile, gratuito). SITE_KEY é
    # pública (vai pro HTML de /cadastro sem problema); SECRET_KEY nunca sai
    # do servidor. Ambas vazias (padrão) = captcha desligado, mesmo padrão de
    # outras integrações opcionais deste app (SMTP, chave Gemini): o cadastro
    # continua funcionando sem, só fica sem essa camada extra até configurar
    # as duas em dash.cloudflare.com -> Turnstile.
    TURNSTILE_SITE_KEY: str = ""
    TURNSTILE_SECRET_KEY: str = ""

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
    LEMBRETE_DOC_DIAS: int = 5       # avisa quando um documento vence em <= X dias

    # achado real: gemini-2.5-flash parou de responder pra contas novas
    # ANTES da data de desligamento anunciada (HTTP 404 "no longer
    # available to new users", a própria API do Gemini recomendou
    # gemini-3.6-flash no lugar) -- promovido a principal; o antigo
    # principal (3.5-flash) vira fallback (1 versão pra trás, mesmo
    # padrão de antes). Gemini desativa modelo antigo sem aviso prévio
    # cumprido à risca -- ao trocar de novo, mover o modelo atual pra
    # IA_MODELO_TEXTO_FALLBACK e o novo pra IA_MODELO_TEXTO, não só
    # apagar o de trás (é o fallback que _gerar() tenta).
    IA_MODELO_TEXTO: str = "gemini-3.6-flash"   # análise de editais (texto)
    IA_MODELO_TEXTO_FALLBACK: str = "gemini-3.5-flash"

    # Último recurso quando os DOIS modelos Gemini acima esgotam com erro
    # de sobrecarga/rede -- provedor diferente (Groq), então uma
    # instabilidade do Google inteiro não derruba a análise. Chave GLOBAL
    # (mesmo espírito do DEEPINFRA_API_KEY abaixo), não do usuário -- o
    # objetivo é o fallback funcionar sozinho, sem pedir configuração
    # extra de quem já cadastrou a chave Gemini pessoal. Vazio = fallback
    # pro Groq desativado (comportamento de antes).
    GROQ_API_KEY: str = ""
    # achado real: llama-3.3-70b-versatile devolveu HTTP 404 "does not
    # exist or you do not have access to it" -- a documentação da Groq
    # ainda lista esse modelo, mas na categoria "Enterprise" (provavelmente
    # exige plano pago, não o tier gratuito). openai/gpt-oss-120b (usado
    # antes deste) funcionava, mas só tem 8000 tokens/min no plano
    # gratuito -- um edital grande sozinho já esgotava isso, e feria a
    # comparação de catálogo em lotes rodando em sequência. groq/compound-
    # mini tinha 70000 tokens/min (confirmado ao vivo via header
    # x-ratelimit-limit-tokens, ~8.75x mais) -- só que a Groq DESCONTINUOU
    # o compound-mini em 21/09/2026 (anunciado em 24/08/2026, ver
    # console.groq.com/docs/deprecations) -- achado real, usuário reportou
    # HTTP 404 "model_not_found" no dia seguinte ao desligamento (edital
    # 135627). Voltou pro openai/gpt-oss-120b (o teto de 8000 tokens/min
    # continua valendo -- ver _GROQ_LIMITE_PROMPT_BYTES/_GROQ_MAX_TOKENS_
    # RESPOSTA em analise_edital.py, reduzidos junto desta troca). Se um
    # dia parar de funcionar (model_not_found ou comportamento estranho),
    # conferir direto no console.groq.com (Playground/Docs > Deprecations)
    # -- a doc pública nem sempre reflete o acesso da conta, e a Groq às
    # vezes desliga modelo com pouco aviso.
    GROQ_MODELO_TEXTO: str = "openai/gpt-oss-120b"

    # Mistral (settings.MISTRAL_API_KEY) — 2º provedor diferente do Gemini
    # na cadeia de fallback de analisar() (ver _gerar em analise_edital.py),
    # tentado ANTES do Groq: mesmo espírito (chave GLOBAL do operador,
    # tier gratuito, API compatível com OpenAI), mas com MUITO mais
    # orçamento por minuto. Achado real, testado ao vivo em 24/09/2026
    # direto contra a conta (não só a doc pública, que às vezes nem bate
    # com o que a conta realmente tem acesso -- ver painel em
    # admin.mistral.ai/plateforme/limits):
    # - mistral-small-latest/mistral-medium-latest: HTTP 429 "Rate limit
    #   exceeded" mesmo numa chamada isolada -- cota efetiva zero nesta
    #   conta, apesar de aparecerem na doc geral da Mistral.
    # - zai-glm-5-2/zai-glm-5-3/glm-5-2 (aparecem no painel de limites,
    #   20000 tokens/min): HTTP 403 "not available in your subscription
    #   tier" -- o painel de limites lista o teto do MODELO, não confirma
    #   que a conta Experiment (grátis) tem acesso a ele.
    # - codestral-2508: funciona (200), 625000 tokens/min -- mas é modelo
    #   de CÓDIGO, risco real de não seguir bem instrução longa em
    #   português sobre habilitação/prazo/lote (nunca foi feito pra isso).
    # - ministral-8b-latest: funciona (200), MESMO orçamento gigante do
    #   codestral (625000 tokens/min, 188 req/min, confirmado via header
    #   x-ratelimit-limit-tokens-minute) mas é modelo de PROPÓSITO GERAL
    #   (instruct), não de código -- escolhido por isso, mesmo não
    #   aparecendo no painel de limites (o painel não é uma lista
    #   exaustiva do que a conta pode chamar). response_format tanto
    #   json_object quanto json_schema testados e funcionando (JSON limpo,
    #   sem cercas de markdown) -- mas _chamar_mistral usa só json_object,
    #   mesmo raciocínio de _chamar_groq (json_schema exigiria converter o
    #   dialeto de schema do Gemini, formato "OBJECT"/"STRING" maiúsculo,
    #   pro JSON Schema padrão -- não vale o risco pra um caminho que já é
    #   fallback de fallback).
    MISTRAL_API_KEY: str = ""
    MISTRAL_MODELO_TEXTO: str = "ministral-8b-latest"

    # IA extra (DeepInfra) — opcional. Ao contrário da chave Gemini (chave
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
    # Orçamento de tempo (segundos) pro OCR inteiro (rasterizar + reconhecer
    # texto). Achado real: nem rasterizar (poppler) nem reconhecer texto
    # (tesseract) tinham QUALQUER limite de tempo -- um PDF grande/denso
    # numa CPU fraca (comum em plano básico de hospedagem) podia rodar por
    # tempo indefinido, prendendo a trava do edital pra sempre (nem o botão
    # manual de tentar de novo funcionava até o servidor reiniciar). Estoura
    # o orçamento -> aproveita o texto já reconhecido até ali, best-effort,
    # mesmo espírito do resto do OCR (nunca é obrigatório dar certo).
    OCR_ORCAMENTO_SEGUNDOS: int = 90

    # Limite maior de páginas de OCR só pra itens_pdf.py (completar descrição
    # de item lendo o documento escaneado) — achado real: a tabela de itens
    # costuma estar bem mais pra frente no documento do que as primeiras 12
    # páginas alcançam, então um edital escaneado nunca conseguia completar
    # nada. Só é seguro usar um limite maior aqui porque itens_pdf roda em
    # segundo plano (ver _rodar_completar_descricao_bg em main.py) — a
    # "Análise por IA" (analise_edital.py) continua com OCR_MAX_PAGINAS (12)
    # porque ainda roda dentro da request HTTP, sensível a timeout.
    OCR_MAX_PAGINAS_ITENS: int = 40

    # OCR de PDF escaneado via modelo de visão (VLM) na DeepInfra, em vez do
    # Tesseract -- lê tabela de verdade (preserva colunas/linhas), enquanto
    # o Tesseract embaralha e mistura a descrição de um item com a do
    # vizinho (achado real: edital 56106, item 24). Tentado ANTES do
    # Tesseract quando o PDF parece escaneado; se falhar (sem saldo na
    # DeepInfra, erro de rede, indisponível), cai pro Tesseract normalmente
    # -- nunca é obrigatório dar certo. Pago por página (chave global do
    # operador, DEEPINFRA_API_KEY), por isso um limite de páginas PRÓPRIO e
    # bem menor que o do Tesseract (que é grátis) -- NÃO reaproveita
    # OCR_MAX_PAGINAS/OCR_MAX_PAGINAS_ITENS, que continuam valendo só pro
    # fallback do Tesseract.
    OCR_VLM_ATIVO: bool = True
    DEEPINFRA_MODELO_OCR: str = "google/gemma-4-31B-it"
    OCR_VLM_MAX_PAGINAS: int = 15

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
