# 🚀 Guia de Deploy — Supabase + Render + GitHub Actions

Arquitetura final (custo **R$ 0** dentro dos limites gratuitos):

```
┌─────────────────┐     ┌──────────────────────┐     ┌─────────────────┐
│  Render (web)   │     │  GitHub Actions      │     │   Supabase      │
│  dashboard/API  │────▶│  coleta diária 06h   │────▶│  Postgres +     │
│  (free tier)    │     │  (cron gratuito)     │     │  pgvector       │
└─────────────────┘     └──────────────────────┘     └─────────────────┘
        │                                                     ▲
        └─────────────────────────────────────────────────────┘
              (os dois leem/escrevem no mesmo banco)
```

A coleta roda no GitHub Actions (separada do site), então **não importa se o
dashboard "dorme"** no free tier do Render — a coleta acontece de qualquer jeito.

---

## Parte 1 — Banco no Supabase

1. Crie uma conta em **supabase.com** → **New project**. Defina uma senha forte
   para o banco (guarde, você vai precisar).
2. (Opcional, para busca semântica futura) **Database → Extensions** → procure
   `vector` → **Enable**. Não é obrigatório agora; o matching atual funciona sem.
3. Clique em **Connect** (topo) → aba **Session pooler** (recomendado para apps
   sempre ligados / ORMs). Copie a URI, algo como:
   ```
   postgresql://postgres.abcdefgh:[SUA-SENHA]@aws-0-sa-east-1.pooler.supabase.com:5432/postgres
   ```
4. **Adapte para o formato do projeto** (2 ajustes):
   - troque `postgresql://` por `postgresql+psycopg2://`
   - acrescente `?sslmode=require` no final
   - troque `[SUA-SENHA]` pela senha real

   Resultado (esta é a sua `DATABASE_URL`):
   ```
   postgresql+psycopg2://postgres.abcdefgh:SUASENHA@aws-0-sa-east-1.pooler.supabase.com:5432/postgres?sslmode=require
   ```

> As tabelas são criadas sozinhas na primeira vez que o app sobe. Não precisa
> rodar migration à mão.

---

## Parte 2 — Código no GitHub

1. Crie um repositório novo no GitHub (pode ser privado).
2. Suba o projeto:
   ```bash
   cd radar-licitacoes
   git init
   git add .
   git commit -m "Radar de Licitacoes"
   git branch -M main
   git remote add origin https://github.com/SEU-USUARIO/radar-licitacoes.git
   git push -u origin main
   ```

---

## Parte 3 — Dashboard no Render

1. Em **render.com** → **New** → **Blueprint**.
2. Conecte o repositório do GitHub. O Render lê o arquivo **`render.yaml`** e já
   monta o web service sozinho.
3. Antes de finalizar (ou depois, em **Environment** do serviço), preencha:
   - **`DATABASE_URL`** → a string do Supabase da Parte 1.
   - (se for usar alertas) `SMTP_*`, `NOTIFICAR_EMAIL`, `TELEGRAM_*`.
   - `PNCP_UFS` já vem como `PR,SP` — ajuste para os seus estados.
4. **Create / Deploy**. O primeiro build leva 1–3 min. Quando ficar verde, abra
   a URL (algo como `https://radar-licitacoes.onrender.com`) → é o dashboard.

> Free tier: o serviço dorme após 15 min sem acesso; a primeira abertura depois
> disso demora ~30s pra "acordar". Normal. Se quiser sem cold start, troque o
> `plan: free` por `plan: starter` no `render.yaml` (US$7/mês).

---

## Parte 4 — Cadastrar o catálogo

Mais fácil: abra o dashboard → aba **Catálogo** → cadastre seus produtos usando
o buscador de **CATMAT** integrado.

Em lote (alternativa): com a `DATABASE_URL` do Supabase exportada na sua máquina,
rode localmente:
```bash
cd backend
export DATABASE_URL="postgresql+psycopg2://...supabase...?sslmode=require"
pip install -r requirements.txt
python -m scripts.seed_catalogo scripts/catalogo_exemplo.csv
```

---

## Parte 5 — Agendar a coleta diária (grátis, GitHub Actions)

O workflow já está no projeto em `.github/workflows/coleta-diaria.yml`. Falta só
dar os segredos a ele:

1. No GitHub, vá em **Settings → Secrets and variables → Actions → New repository secret**.
2. Crie o secret **`DATABASE_URL`** com a mesma string do Supabase.
3. (Se for usar alertas) crie também `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`,
   `SMTP_PASSWORD`, `SMTP_FROM`, `NOTIFICAR_EMAIL`, `TELEGRAM_BOT_TOKEN`,
   `TELEGRAM_CHAT_ID`.
4. Pronto. Roda todo dia às **06:00 (Brasília)**. Para testar agora, vá na aba
   **Actions → Coleta diária de licitações → Run workflow**.

> Detalhes do GitHub Actions: execuções agendadas podem atrasar alguns minutos
> em horários de pico, e o GitHub **pausa** agendamentos após 60 dias sem
> atividade no repositório (basta um push ou re-ativar na aba Actions).

---

## Parte 6 (opcional) — Coleta via Render em vez de GitHub Actions

Se preferir tudo no Render, adicione ao `render.yaml` um serviço de cron
(**é um serviço pago**, ~US$7/mês):

```yaml
  - type: cron
    name: radar-coleta
    runtime: docker
    dockerfilePath: ./backend/Dockerfile
    dockerContext: ./backend
    schedule: "0 9 * * *"        # 09:00 UTC = 06:00 Brasília
    dockerCommand: python -m scripts.primeira_coleta
    envVars:
      - key: DATABASE_URL
        sync: false
      # ...mesmas variáveis do web service
```

---

## Resumo do que vai onde

| Peça | Onde | Custo |
|------|------|-------|
| Banco (Postgres + pgvector) | Supabase | Grátis |
| Dashboard / API | Render (web, free) | Grátis* |
| Coleta diária (cron) | GitHub Actions | Grátis |
| Notificações | Telegram (grátis) / e-mail SMTP | Grátis |

\* O free do Render dorme após 15 min. Para uso pessoal, sem problema.

---

## Por que não tudo no Vercel?

O Vercel é serverless: funções curtas (60s no Hobby, ~300s com Fluid Compute) e
sem processo sempre ligado. A coleta varre o PNCP por modalidade/UF e busca os
itens de cada edital — passa fácil desse tempo. Por isso a coleta vive no GitHub
Actions (cron sem limite prático de tempo) e o dashboard no Render. O Supabase
cuida do estado (catálogo, editais, matches, regras, logs), que é tudo que
precisa ser guardado.
