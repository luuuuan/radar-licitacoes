# 📡 Radar de Licitações

Sistema que **busca diariamente editais de licitação pública** e identifica
automaticamente aqueles cujos itens batem com os produtos/serviços que sua
empresa vende. Faz correspondência por **código** (NCM, CATMAT/CATSER, EAN) e
por **similaridade de texto** (TF-IDF + cosseno + fuzzy), classifica cada edital
em **forte / médio / fraco**, avisa por **e-mail/Telegram** e mostra tudo num
**dashboard web**.

---

## 🎯 A decisão de arquitetura mais importante

O escopo original pedia raspar ~6 fontes (ComprasNet, BEC, portais estaduais,
municipais, diários oficiais, PNCP). Construir e manter um scraper Selenium para
cada portal é frágil e caro — eles mudam de layout, caem, têm CAPTCHA.

**A Lei 14.133/2021 (Nova Lei de Licitações) obriga todos os órgãos — federais,
estaduais e municipais — a publicar suas contratações no PNCP.** E a API de
consultas do PNCP é **pública, gratuita e em JSON**. Ou seja: um único conector
bem-feito já cobre a maior parte das fontes pedidas, sem scraping frágil.

Por isso o projeto nasce **PNCP-first**: o núcleo (coleta → matching → score →
notificação → dashboard) está pronto e rodando em cima do PNCP. Os portais que
ainda têm fluxo próprio (ex.: BEC/SP legado) entram depois como conectores
adicionais que implementam a mesma interface `BaseConnector` — sem reescrever
nada do resto.

---

## 🚀 Como rodar (Docker)

```bash
cp .env.example .env          # ajuste UFs, e-mail/Telegram se quiser
docker compose up -d --build  # sobe db (pgvector), redis, api, worker, beat
```

Acesse o dashboard em **http://localhost:8000**

### Carregar catálogo inicial e rodar a primeira coleta

```bash
# importa o catálogo de exemplo (ou troque pelo seu CSV)
docker compose exec api python -m scripts.seed_catalogo scripts/catalogo_exemplo.csv

# roda a primeira coleta agora (sem esperar o agendador das 06h)
docker compose exec api python -m scripts.primeira_coleta
```

Depois é só abrir o dashboard, filtrar por **Match forte** e começar a trabalhar.

> A coleta automática roda **todo dia às 06:00** (Celery Beat). Você também pode
> disparar manualmente pelo botão **“Coletar agora”** no dashboard.

---

## 🧩 Como funciona o matching

Para cada item de cada edital, o motor escolhe o melhor sinal disponível:

1. **Código exato** (NCM, CATMAT/CATSER, EAN) → score 1.0. É o sinal mais
   confiável; ignora pontuação/formatação (`4802.55.90` = `48025590`).
2. **Similaridade textual** (TF-IDF + cosseno) entre descrição do item e do
   produto/keywords.
3. **Fuzzy de palavras-chave** (rapidfuzz) para pegar variações de grafia.

O **score do edital** combina o melhor item (70%) com a fração de itens
compatíveis (30%). Limiares (ajustáveis no `.env`):

| Nível | Score |
|-------|-------|
| forte | ≥ 0.62 |
| médio | ≥ 0.40 |
| fraco | < 0.40 |

**Regras de exclusão**: você define termos (ex.: `engenharia`) ou códigos de
categoria do PNCP (ex.: `9` = Serviços de Engenharia) e esses editais são
descartados antes de entrar.

---

## 🔎 Busca de CATMAT/CATSER (integrada)

No dashboard, na aba **Catálogo**, há um buscador que consulta o **catálogo
oficial de dados abertos do Compras.gov.br** (SIASG) e sugere os códigos:

- **CATMAT** (materiais) via `modulo-material/4_consultarItemMaterial`
- **CATSER** (serviços) via `modulo-servico/5_consultarItemServico`

Você digita a descrição (ex.: "papel A4 75g branco"), o sistema traz os itens
do catálogo **ranqueados por relevância**, com a descrição oficial, classe e
unidade de fornecimento. Um clique em **"usar 150823"** preenche o campo CATMAT
do produto. Os códigos vêm direto da API oficial — nada é inventado, e itens
inativos vêm sinalizados.

> Endpoint usado (público, sem autenticação):
> `https://dadosabertos.compras.gov.br/modulo-material/4_consultarItemMaterial?descricaoItem=...`
> Como uma descrição genérica costuma ter vários códigos, confira sempre qual
> variação corresponde ao que você realmente vende.

---

## 📁 Estrutura

```
radar-licitacoes/
├── docker-compose.yml
├── .env.example
└── backend/
    ├── Dockerfile
    ├── requirements.txt
    ├── app/
    │   ├── main.py            # API FastAPI + dashboard
    │   ├── config.py          # configurações (.env)
    │   ├── database.py        # conexão + criação de tabelas (pgvector)
    │   ├── models.py          # Produto, Edital, ItemEdital, Match, RegraExclusao
    │   ├── service.py         # orquestração: coleta→persiste→casa→notifica
    │   ├── tasks.py           # tarefa diária (Celery Beat)
    │   ├── celery_app.py
    │   ├── connectors/
    │   │   ├── base.py        # interface BaseConnector
    │   │   └── pncp.py        # conector do PNCP (pronto)
    │   ├── matching/
    │   │   └── engine.py      # motor de correspondência e pontuação
    │   ├── catalogo/
    │   │   └── catmat.py      # busca de CATMAT/CATSER (Compras.gov.br dados abertos)
    │   └── notifications/     # e-mail (SMTP) + Telegram
    ├── scripts/
    │   ├── seed_catalogo.py   # cadastro inicial via CSV
    │   ├── primeira_coleta.py # primeira coleta manual
    │   └── catalogo_exemplo.csv
    └── static/index.html      # dashboard (HTML+JS, sem build)
```

---

## 🔧 Tabela de modalidades do PNCP (para o `.env`)

| Código | Modalidade |
|--------|------------|
| 4 | Concorrência Eletrônica |
| 6 | Pregão Eletrônico |
| 8 | Dispensa de Licitação |
| 9 | Inexigibilidade |
| 12 | Credenciamento |

Lista completa e schema dos campos no Swagger oficial:
`https://pncp.gov.br/api/consulta/swagger-ui/index.html`

---

## 🗺️ Roadmap (o que ainda dá pra plugar)

O núcleo já entrega os requisitos centrais. Próximos passos, em ordem de retorno:

1. **Busca semântica real (pgvector + embeddings)** — descomente
   `sentence-transformers` no `requirements.txt`, gere o embedding da descrição
   de cada produto/item e use a coluna `vector` para casar sinônimos que o
   TF-IDF não pega (“notebook” ≈ “computador portátil”). A infra (pgvector) já
   está no Compose.
2. **Conectores adicionais** (BEC/SP, portais municipais sem PNCP): criar uma
   classe que herda de `BaseConnector` e registrar em `service.py`. O resto
   (matching, score, dashboard, notificação) funciona sem alteração.
3. **Autenticação** (login e-mail/senha) — hoje o dashboard é aberto na rede
   local; para expor publicamente, adicionar OAuth2/JWT no FastAPI.
4. **Exportar para Excel** além de CSV (a lib `openpyxl` já está incluída).
5. **WhatsApp** — requer a API oficial do WhatsApp Business (Meta) ou um
   provedor (Twilio/360dialog). O canal de Telegram já está pronto e é gratuito;
   recomendo começar por ele.

---

## ⚠️ Notas

- A API pública do PNCP tem limite de uso; o conector já faz paginação e um
  pequeno `delay` entre requisições (`PNCP_DELAY`) para ser educado com o portal.
- Em ambientes sem acesso à internet a coleta não traz dados — confirme que o
  container `api`/`worker` alcança `pncp.gov.br`.
- O motor de matching foi testado com casos reais simulados (material de
  expediente, higiene/limpeza, engenharia) e classifica corretamente forte/fraco
  e aplica as regras de exclusão.
```
