# Cofre de documentos de habilitação (v1) — desenho para revisão

## O que já existe (achado antes de desenhar qualquer coisa nova)

Boa parte do que foi pedido **já está implementada**. Existe hoje:

- Modelo `Documento` (`backend/app/models.py:292`) com `usuario_id`, `nome`,
  `orgao_emissor`, `data_validade`, `link`, `observacao`, `ativo`,
  `texto_extraido`, `avisado_para` (e-mail), `avisado_para_telegram`,
  `criado_em`.
- CRUD completo em `backend/app/main.py`:
  - `GET /api/documentos` — lista, filtrado por `usuario_id == user.id`.
  - `POST /api/documentos` — cria (upload opcional, só extrai texto).
  - `PUT /api/documentos/{doc_id}` — edita.
  - `DELETE /api/documentos/{doc_id}` — remove.
- Alertas de vencimento já rodando (e-mail + Telegram) via
  `lembretes.verificar_todos()`.
- Widget "Documentos a vencer" já existe no dashboard.
- Esses documentos já alimentam a verificação por IA de habilitação dentro
  da análise de edital (compara `texto_extraido` contra as exigências).

**O que falta**, comparado ao que foi pedido:

1. **O arquivo em si não é guardado.** Hoje o upload só extrai texto
   (`texto_extraido`) e descarta os bytes — não existe forma de baixar o
   certificado de volta depois.
2. **A validade é sempre digitada à mão.** Não há extração automática por
   IA — é esse o único trabalho que a IA deve fazer no v1 (nada de análise
   de aptidão ou cruzamento com exigências de edital — isso é v2).

## Decisão de desenho: estender o `Documento` existente, não criar um novo

Criar um modelo `DocumentoHabilitacao` separado duplicaria isolamento,
alertas e a integração que a análise por IA do edital já usa. A proposta é
estender o `Documento` que já existe.

### Model — 3 colunas novas em `Documento`

| Coluna | Tipo | Descrição |
|---|---|---|
| `arquivo_cifrado` | `Text` | Bytes do arquivo em base64, cifrados com `auth.cifrar` (Fernet) — mesmo padrão já usado hoje pra CNPJ/endereço em `Usuario` |
| `arquivo_nome` | `String` | Nome original do arquivo, pro `Content-Disposition` do download |
| `arquivo_tipo` | `String` | Content-type (`application/pdf`, `image/jpeg`, etc.) |

A cifra é defesa em profundidade pros dados em repouso (caso o banco
vaze) — **não** é o mecanismo de isolamento entre usuários. Isolamento é
feito na camada de API, descrito abaixo.

### Isolamento por usuário — o ponto que você pediu pra revisar com cuidado

Reaproveita o helper que já existe hoje e já está correto:

```python
def _documento_do_usuario(db, doc_id, user) -> Documento:
    d = db.get(Documento, doc_id)
    if not d or d.usuario_id != user.id:
        raise HTTPException(404, "Documento não encontrado")
    return d
```

Esse helper já é usado em `PUT`/`DELETE` hoje — devolve 404 (não 403, pra
não confirmar que o ID existe) sempre que `doc_id` não pertence ao usuário
logado. O endpoint novo de download usa **o mesmo helper**, então herda a
garantia sem lógica nova pra auditar:

```python
@app.get("/api/documentos/{doc_id}/arquivo")
def baixar_arquivo_documento(doc_id: int, user=Depends(_auth.get_current_user),
                             db=Depends(get_session)):
    d = _documento_do_usuario(db, doc_id, user)   # 404 se não for dono
    if not d.arquivo_cifrado:
        raise HTTPException(404, "Documento não tem arquivo salvo")
    conteudo = base64.b64decode(_auth.decifrar(d.arquivo_cifrado))
    return Response(content=conteudo, media_type=d.arquivo_tipo,
                    headers={"Content-Disposition": f'attachment; filename="{d.arquivo_nome}"'})
```

A listagem (`GET /api/documentos`) já filtra por
`Documento.usuario_id == user.id` na query — usuário B nunca vê o
documento de A na lista pra sequer tentar adivinhar o ID.

Resumo da garantia: **todo** endpoint que toca um documento específico
(`GET .../arquivo`, `PUT`, `DELETE`) passa pelo mesmo ponto único
(`_documento_do_usuario`) antes de fazer qualquer coisa — não tem caminho
alternativo que pule esse check.

### Extração de validade por IA

Nova função em `backend/app/analise_edital.py`, reaproveitando `_gerar`/
`_parse_json` que já existem (mesma chave Gemini do próprio usuário, sem
fallback global — igual toda outra chamada de IA no app):

```python
def extrair_validade_documento(texto: str, api_key: str | None) -> date | None:
    # prompt pede só {"data_validade": "AAAA-MM-DD" | null}
    # v1: só isso. Sem análise de aptidão, sem cruzamento com edital.
```

Chamada dentro de `POST /api/documentos` quando `data_validade` não vier
preenchida no formulário. Se a IA não conseguir (sem chave configurada,
sem data identificável no texto, etc.), a resposta é **422** pedindo pra
digitar a validade manualmente — sem mexer no schema: `data_validade`
continua `NOT NULL`, sem efeito colateral nos alertas/dashboard que já
dependem dela sempre existir.

### Mudança de comportamento a confirmar

Hoje `arquivo` é **opcional** no upload (serve só pra extrair texto de
comparação). Pra virar um cofre de verdade, a proposta é tornar `arquivo`
**obrigatório** na criação — sem arquivo não há o que guardar nem baixar
depois. Documentos já cadastrados sem arquivo continuam existindo
normalmente (só não têm botão de download até serem reeditados com
upload).

## Resultado: `[ok]` ou comentário abaixo desta linha
