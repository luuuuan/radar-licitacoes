"""
Modelos de dados (SQLAlchemy).

Entidades principais:
- Produto: catálogo do que a empresa vende (descrição, códigos, palavras-chave).
- Edital: uma contratação/licitação coletada de um portal (ex.: PNCP).
- ItemEdital: cada item solicitado dentro de um edital.
- Match: vínculo entre um Edital e o catálogo, com pontuação e nível.
- RegraExclusao: termos/categorias que o usuário quer ignorar.
"""
from datetime import datetime, date, timezone
from zoneinfo import ZoneInfo


def utcnow() -> datetime:
    """UTC atual, sem timezone (naive) — substitui o datetime.utcnow() depreciado,
    mantendo o mesmo comportamento (compatível com as colunas DateTime existentes)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _brt_now() -> datetime:
    """Horário de Brasília atual, sem timezone (naive) -- mesma convenção de
    Edital.analise_em/analise_vista_em (main.py, datetime.now(BR_TZ).replace
    (tzinfo=None)), NÃO utcnow() acima. Usado só onde o campo é comparado
    diretamente contra um desses dois (ex.: AnaliseIAExtras.atualizado_em) --
    misturar as duas convenções desalinha a comparação por ~3h (mesma classe
    de bug já corrigida em Match.analise_vista_em, commit 00c4dc7)."""
    return datetime.now(ZoneInfo("America/Sao_Paulo")).replace(tzinfo=None)
from sqlalchemy import (
    String, Integer, Float, Text, DateTime, Date, Boolean, ForeignKey, JSON,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Usuario(Base):
    __tablename__ = "usuarios"

    id: Mapped[int] = mapped_column(primary_key=True)
    nome: Mapped[str] = mapped_column(String(160))
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    senha_hash: Mapped[str] = mapped_column(String(255))
    # dados cadastrais (CPF/CNPJ e endereço guardados cifrados)
    doc_cifrado: Mapped[str | None] = mapped_column(Text, nullable=True)       # CPF/CNPJ
    endereco_cifrado: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON cifrado
    # dados complementares pra montar a proposta/documento timbrado — mesmo
    # padrão do endereço: JSON cifrado (telefone, representante_legal,
    # inscricao_estadual, inscricao_municipal, banco_nome, banco_agencia,
    # banco_conta — inclui dado bancário, por isso cifrado).
    dados_empresa_cifrado: Mapped[str | None] = mapped_column(Text, nullable=True)
    # logo da empresa (data URI completo: "data:image/png;base64,...") pra
    # timbrar a proposta exportada — não é sensível (vai impresso no
    # documento mesmo), por isso não cifrado, ao contrário dos campos acima.
    logo_base64: Mapped[str | None] = mapped_column(Text, nullable=True)
    # verificação de e-mail
    email_verificado: Mapped[bool] = mapped_column(Boolean, default=False)
    token_verificacao: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # expira em 24h (ver POST /api/auth/cadastro e /api/auth/reenviar-verificacao)
    token_verificacao_expira: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # redefinição de senha ("esqueci minha senha")
    token_reset_senha: Mapped[str | None] = mapped_column(String(128), nullable=True)
    token_reset_expira: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # integrações próprias do usuário (cifradas/preferências)
    gemini_key_cifrada: Mapped[str | None] = mapped_column(Text, nullable=True)
    telegram_chat_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    telegram_codigo: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    # 2º contato do Telegram (ex.: sócio, outro responsável) -- recebe os
    # MESMOS avisos do 1º, com sua própria vinculação/código (mesmo padrão),
    # mas compartilha a preferência notif_telegram (é "este usuário quer
    # Telegram", não um canal à parte por contato).
    telegram_chat_id_2: Mapped[str | None] = mapped_column(String(64), nullable=True)
    telegram_codigo_2: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    notif_email: Mapped[bool] = mapped_column(Boolean, default=True)
    notif_telegram: Mapped[bool] = mapped_column(Boolean, default=False)
    # aviso de editais fortes que vão ABRIR em breve (X dias antes da abertura)
    avisar_abertura: Mapped[bool] = mapped_column(Boolean, default=True)
    dias_antecedencia: Mapped[int] = mapped_column(Integer, default=2)
    # pedido do usuário: botão "Ler Todos" na central de notificações. Só
    # guarda a DATA (não hora) do último clique -- prazo/abertura/documento
    # somem pelo resto do dia e voltam sozinhos no dia seguinte (comparação
    # simples "!= hoje", sem precisar de job/cron pra "expirar" nada).
    # Análise por IA usa outro mecanismo (Match.analise_vista_em, por
    # edital) -- pedido explícito do usuário pra nunca mais voltar sozinha.
    notificacoes_lidas_em: Mapped[date | None] = mapped_column(Date, nullable=True)
    ativo: Mapped[bool] = mapped_column(Boolean, default=True)
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # checkpoint do recálculo completo (recalcular_todos=True) — permite
    # retomar de onde parou se o processo for interrompido (ex.: deploy no
    # meio de uma rodada longa) em vez de gastar cota de IA reprocessando
    # editais que já foram refeitos. Só vale por um tempo (ver service.py,
    # RECALCULO_CHECKPOINT_VALIDADE): catálogo pode ter mudado desde então.
    recalculo_checkpoint_edital_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    recalculo_checkpoint_coletado_em: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    recalculo_checkpoint_em: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # incrementados a cada criação/edição/exclusão de produto ou documento —
    # usados como chave de invalidação do cache em AnaliseIAExtras (ver
    # main.py:_anexar_verificacao_ia_documentos/_anexar_comparacao_catalogo_ia).
    versao_catalogo: Mapped[int] = mapped_column(Integer, default=0)
    versao_documentos: Mapped[int] = mapped_column(Integer, default=0)


class Fornecedor(Base):
    __tablename__ = "fornecedores"
    id: Mapped[int] = mapped_column(primary_key=True)
    usuario_id: Mapped[int | None] = mapped_column(ForeignKey("usuarios.id"), index=True, nullable=True)
    nome: Mapped[str] = mapped_column(String(160))
    telefone: Mapped[str | None] = mapped_column(String(40), nullable=True)
    whatsapp: Mapped[str | None] = mapped_column(String(40), nullable=True)
    email: Mapped[str | None] = mapped_column(String(160), nullable=True)
    site: Mapped[str | None] = mapped_column(String(255), nullable=True)
    observacao: Mapped[str | None] = mapped_column(Text, nullable=True)
    ativo: Mapped[bool] = mapped_column(Boolean, default=True)
    # fornecedor de confiança do usuário -- pedido: destacar/filtrar os
    # fornecedores que ele já sabe que são bons, sem precisar abrir cada um
    # pra lembrar. Não afeta nada do motor/cálculo, é só organização.
    favorito: Mapped[bool] = mapped_column(Boolean, default=False)
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Produto(Base):
    __tablename__ = "produtos"

    id: Mapped[int] = mapped_column(primary_key=True)
    usuario_id: Mapped[int | None] = mapped_column(ForeignKey("usuarios.id"), index=True, nullable=True)
    descricao: Mapped[str] = mapped_column(Text)
    # Códigos de classificação (qualquer um pode estar vazio)
    ncm: Mapped[str | None] = mapped_column(String(20), nullable=True)
    cest: Mapped[str | None] = mapped_column(String(20), nullable=True)
    ean: Mapped[str | None] = mapped_column(String(20), nullable=True)
    catmat: Mapped[str | None] = mapped_column(String(20), nullable=True)  # material
    catser: Mapped[str | None] = mapped_column(String(20), nullable=True)  # serviço
    # Palavras-chave separadas por vírgula
    palavras_chave: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Usados na planilha de cotação (aba Cotação, na página do edital)
    fabricante: Mapped[str | None] = mapped_column(String(160), nullable=True)
    marca: Mapped[str | None] = mapped_column(String(160), nullable=True)
    modelo: Mapped[str | None] = mapped_column(String(160), nullable=True)
    # Preços (para cálculo de margem)
    preco_custo: Mapped[float | None] = mapped_column(Float, nullable=True)   # quanto você paga
    preco_venda: Mapped[float | None] = mapped_column(Float, nullable=True)   # seu preço de venda
    # Unidade de venda e quantos itens "soltos" cabem nela (ex.: resma = 500 folhas).
    # Serve para comparar com o preço unitário do órgão sem distorcer a margem.
    unidade_venda: Mapped[str | None] = mapped_column(String(20), nullable=True)   # ex.: resma, unidade, caixa
    itens_por_unidade: Mapped[float | None] = mapped_column(Float, nullable=True)  # ex.: 500
    # Link direto pra página do produto no site do fornecedor (quando existe).
    link_produto: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Fornecedor
    fornecedor_nome: Mapped[str | None] = mapped_column(String(160), nullable=True)
    fornecedor_contato: Mapped[str | None] = mapped_column(String(160), nullable=True)
    fornecedor_site: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # vínculo com o cadastro de fornecedores (opcional; quando definido, os dados
    # de contato vêm de lá). Os campos acima ficam como histórico/fallback.
    fornecedor_id: Mapped[int | None] = mapped_column(ForeignKey("fornecedores.id"), nullable=True, index=True)
    ativo: Mapped[bool] = mapped_column(Boolean, default=True)
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Edital(Base):
    __tablename__ = "editais"
    __table_args__ = (UniqueConstraint("fonte", "id_externo", name="uq_fonte_idexterno"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    fonte: Mapped[str] = mapped_column(String(40))          # ex.: "PNCP"
    id_externo: Mapped[str] = mapped_column(String(120))    # numeroControlePNCP
    orgao: Mapped[str | None] = mapped_column(Text, nullable=True)
    cnpj_orgao: Mapped[str | None] = mapped_column(String(20), nullable=True)
    objeto: Mapped[str | None] = mapped_column(Text, nullable=True)
    modalidade: Mapped[str | None] = mapped_column(String(80), nullable=True)
    uf: Mapped[str | None] = mapped_column(String(2), nullable=True)
    municipio: Mapped[str | None] = mapped_column(String(120), nullable=True)
    valor_estimado: Mapped[float | None] = mapped_column(Float, nullable=True)
    data_publicacao: Mapped[date | None] = mapped_column(Date, nullable=True)
    # DateTime (não Date): pedido do usuário -- a UI mostra a hora de
    # início/fim de recebimento de propostas, e o status "recebendo
    # proposta"/"encerrado" (_status_prazo_edital, main.py) respeita a hora,
    # não só o dia. Migração de coluna em database.py (era Date).
    data_abertura: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    data_encerramento: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # fim recebimento propostas
    link: Mapped[str | None] = mapped_column(Text, nullable=True)
    # sistema/plataforma onde a disputa ocorre (ex.: "BLL Compras",
    # "ComprasNet"), derivado do domínio de linkSistemaOrigem (campo da
    # própria API do PNCP) na coleta -- ver _plataforma_de_link em
    # connectors/pncp.py. Ao contrário de dados_orgao.plataforma da Análise
    # por IA (só existe depois que o usuário roda a análise com a própria
    # chave Gemini, e só se o PDF mencionar), este vem de graça pra TODO
    # edital coletado, direto da API estruturada -- é o que alimenta o
    # filtro por plataforma na listagem.
    plataforma: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # URL crua (linkSistemaOrigem da API do PNCP) pra página do PRÓPRIO
    # pregão na plataforma externa -- não é só o domínio (isso é
    # `plataforma`, acima), é o link específico daquele processo, pra abrir
    # direto sem precisar buscar manualmente dentro do site da plataforma.
    link_sistema_origem: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    analise_ia: Mapped[str | None] = mapped_column(Text, nullable=True)        # JSON da análise (cache)
    analise_em: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # cache da lista de arquivos/anexos publicados no PNCP (ver
    # _arquivos_pncp_cache em main.py) -- achado real: a aba Documentos e a
    # Análise por IA buscavam essa lista no PNCP toda vez, cada uma por
    # conta própria, mesmo já tendo sido buscada com sucesso antes. Só é
    # sobrescrita quando a busca funciona de verdade (nunca com uma falha
    # passageira de rede/PNCP) -- ver mesmo raciocínio em
    # _dias_restantes_edital sobre não confundir "falha ao buscar" com
    # "não existe".
    arquivos_pncp: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    arquivos_pncp_em: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # cache do TEXTO já extraído do(s) PDF(s) usado na Análise por IA (até
    # ~80000 chars, o mesmo limite que o prompt já usa -- não o texto bruto
    # bem maior que o completar-descrição às vezes lê, pra não inflar o
    # banco). Achado real: reabrir a aba ou tentar de novo depois de uma
    # falha (ex.: PNCP instável, ou só a chamada de IA que falhou) baixava
    # e extraía o PDF de novo, mesmo já tendo extraído com sucesso antes.
    # Só é gravado quando a extração funciona (mesmo que a chamada de IA
    # em si falhe depois) -- ver _texto_pronto_cache em main.py.
    texto_analise_ia: Mapped[str | None] = mapped_column(Text, nullable=True)
    texto_analise_ia_fonte: Mapped[str | None] = mapped_column(Text, nullable=True)
    texto_analise_ia_em: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    coletado_em: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # já tentou completar a descrição dos itens lendo o PDF (ver
    # app/itens_pdf.py) — só marca quando a tentativa TERMINA com um
    # resultado (status "ok"), mesmo que não tenha achado nada pra
    # completar; assim uma falha real (sem_texto/erro_ia/etc.) continua
    # tentando de novo sozinha na próxima visita à página, mas um "ok"
    # sem melhorias não fica retentando à toa a cada visita.
    itens_completados_em: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # quantos itens a ÚLTIMA tentativa (status "ok") efetivamente
    # atualizou — achado real: "itens_completados_em" setado não significa
    # que algo foi melhorado, só que uma tentativa terminou; sem isso, a
    # tela mostrava "descrições completadas" mesmo quando 0 itens tinham
    # mudado (ou quando a tentativa mais recente falhou, mas um "ok"
    # antigo tinha deixado o timestamp setado).
    itens_completados_qtd: Mapped[int] = mapped_column(Integer, default=0)

    itens: Mapped[list["ItemEdital"]] = relationship(
        back_populates="edital", cascade="all, delete-orphan",
        order_by="ItemEdital.numero",
    )
    match: Mapped["Match | None"] = relationship(
        back_populates="edital", cascade="all, delete-orphan", uselist=False
    )


class ItemEdital(Base):
    __tablename__ = "itens_edital"

    id: Mapped[int] = mapped_column(primary_key=True)
    # index=True: lido em quase toda tela de edital (detalhe, cotação,
    # comparação por IA...) e na busca por item (ver main.py, GET /api/editais)
    # — sem índice, cada uma dessas consultas varria a tabela inteira.
    edital_id: Mapped[int] = mapped_column(ForeignKey("editais.id"), index=True)
    numero: Mapped[int | None] = mapped_column(Integer, nullable=True)
    descricao: Mapped[str] = mapped_column(Text)
    material_ou_servico: Mapped[str | None] = mapped_column(String(20), nullable=True)
    ncm: Mapped[str | None] = mapped_column(String(20), nullable=True)
    catalogo_codigo: Mapped[str | None] = mapped_column(String(40), nullable=True)  # CATMAT/CATSER
    quantidade: Mapped[float | None] = mapped_column(Float, nullable=True)
    valor_unitario: Mapped[float | None] = mapped_column(Float, nullable=True)
    # texto livre do PNCP (ex.: "Embalagem 500 FL", "Unidade") — usado pra
    # saber se valor_unitario já vem na mesma embalagem do produto do
    # catálogo (ver _custo_e_margem em main.py) antes de dividir o custo.
    unidade_medida: Mapped[str | None] = mapped_column(String(60), nullable=True)

    edital: Mapped["Edital"] = relationship(back_populates="itens")


class Match(Base):
    __tablename__ = "matches"
    __table_args__ = (UniqueConstraint("usuario_id", "edital_id", name="uq_match_user_edital"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    usuario_id: Mapped[int | None] = mapped_column(ForeignKey("usuarios.id"), index=True, nullable=True)
    edital_id: Mapped[int] = mapped_column(ForeignKey("editais.id"), index=True)
    score: Mapped[float] = mapped_column(Float)           # 0..1
    nivel: Mapped[str] = mapped_column(String(10))        # fraco | medio | forte
    itens_compativeis: Mapped[int] = mapped_column(Integer, default=0)
    detalhe: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # quais itens casaram
    lido: Mapped[bool] = mapped_column(Boolean, default=False)
    interessante: Mapped[bool] = mapped_column(Boolean, default=False)
    # e-mail (imediato, agrupado) e Telegram (menu interativo) têm cada um seu
    # próprio controle de "já avisei isso" — o Telegram só marca quando o
    # usuário efetivamente pede pra ver aquela categoria no menu, então não
    # pode reusar a mesma flag do e-mail (senão o e-mail marcaria e o item
    # nunca mais apareceria no menu do Telegram, mesmo sem o usuário ter visto).
    notificado: Mapped[bool] = mapped_column(Boolean, default=False)  # alta compatibilidade -> Telegram
    prazo_avisado: Mapped[bool] = mapped_column(Boolean, default=False)  # lembrete de prazo já enviado (e-mail)
    prazo_avisado_telegram: Mapped[bool] = mapped_column(Boolean, default=False)
    abertura_avisada: Mapped[bool] = mapped_column(Boolean, default=False)  # aviso de abertura próxima já enviado (e-mail)
    abertura_avisada_telegram: Mapped[bool] = mapped_column(Boolean, default=False)
    # mesmas 3 flags acima, mas do 2º contato do Telegram (Usuario.telegram_chat_id_2)
    # -- achado real: sem isso, o 1º contato a tocar num botão do menu marcava
    # o item como visto pra CONTA inteira, e o outro contato tocando no mesmo
    # botão depois via a lista vazia (o item nunca chegava até ele de verdade).
    notificado_2: Mapped[bool] = mapped_column(Boolean, default=False)
    prazo_avisado_telegram_2: Mapped[bool] = mapped_column(Boolean, default=False)
    abertura_avisada_telegram_2: Mapped[bool] = mapped_column(Boolean, default=False)
    # acompanhamento (pipeline): novo, vou_participar, proposta_enviada, ganho, perdido, descartado
    status: Mapped[str] = mapped_column(String(20), default="novo")
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # última vez que o usuário navegou entre as abas da página deste edital
    # (itens/cotação/análise/documentos/proposta) -- alimenta o card
    # "Analisados recentemente" do painel Início. None = nunca abriu.
    interagido_em: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # última vez que "status" mudou (qualquer valor, não só "ganho") --
    # alimenta o filtro por mês do card "Editais ganhos" do painel Início.
    # Sem isso não tinha como saber QUANDO o usuário marcou como ganho, só
    # o valor atual do status.
    status_atualizado_em: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # achado real (usuário reportou): a notificação "análise concluída" em
    # /api/notificacoes usava interagido_em (acima) pra saber se o usuário já
    # viu o resultado -- mas interagido_em é atualizado em QUALQUER aba do
    # edital (itens/cotação/documentos/proposta), não só na Análise. Bastava
    # o usuário reabrir o edital por outro motivo qualquer, dias depois, pra
    # apagar a notificação sem nunca ter olhado o resultado nem o sino. Este
    # campo só é atualizado quando a aba "análise" é aberta de verdade (ver
    # registrar_interacao em main.py), então navegar pelas outras abas não
    # dispensa mais a notificação sozinho.
    analise_vista_em: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    edital: Mapped["Edital"] = relationship(back_populates="match")


class CotacaoPreco(Base):
    """Preço de VENDA que o usuário está acompanhando na aba Cotação
    (coluna "Valor unit. (venda)"), por (usuário, edital, item) -- pedido
    do usuário: serve só pra saber até quanto pode ofertar no pregão
    (planilha exportada), não é o valor de verdade da Proposta e não pode
    mudar quando a Proposta muda (nem o contrário). Tabela própria, não
    Match.detalhe: Match é reconstruído a cada POST /api/recalcular por
    _mesclar_confirmacoes_manuais (service.py), que só preserva uma lista
    fixa de campos (produto_id/produto/confirmado_manualmente/motivo/
    confianca) -- um campo novo ali é apagado em silêncio no próximo
    recálculo. Mesmo padrão de AnaliseIAExtras, abaixo."""
    __tablename__ = "cotacoes_precos"
    __table_args__ = (UniqueConstraint("usuario_id", "edital_id", "numero_item",
                                        name="uq_cotacao_preco_user_edital_item"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    usuario_id: Mapped[int] = mapped_column(ForeignKey("usuarios.id"), index=True)
    edital_id: Mapped[int] = mapped_column(ForeignKey("editais.id"), index=True)
    numero_item: Mapped[int] = mapped_column(Integer)
    valor: Mapped[float] = mapped_column(Float)


class AnaliseIAExtras(Base):
    """Cache por (edital, usuário) das duas checagens extras da Análise por
    IA — comparação de catálogo e verificação de documentos de habilitação —
    que são por usuário (não por edital, ao contrário de Edital.analise_ia) e
    caras (cada uma é uma chamada de IA). Reaproveitado enquanto
    Usuario.versao_catalogo/versao_documentos não mudarem desde o último
    cálculo; cada checagem tem seu próprio campo de versão porque uma pode
    ficar desatualizada sem a outra (editar um documento não invalida a
    comparação de catálogo, e vice-versa)."""
    __tablename__ = "analise_ia_extras"
    __table_args__ = (UniqueConstraint("usuario_id", "edital_id", name="uq_analise_extras_user_edital"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    usuario_id: Mapped[int] = mapped_column(ForeignKey("usuarios.id"), index=True)
    edital_id: Mapped[int] = mapped_column(ForeignKey("editais.id"), index=True)
    comparacao_catalogo_ia: Mapped[str | None] = mapped_column(Text, nullable=True)   # JSON
    versao_catalogo_calc: Mapped[int | None] = mapped_column(Integer, nullable=True)
    verificacao_documentos_ia: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON
    versao_documentos_calc: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # _brt_now, não utcnow -- ver docstring de _brt_now: main.py compara este
    # campo direto contra Edital.analise_em (BRT naive) em
    # _query_analise_pendente.
    atualizado_em: Mapped[datetime] = mapped_column(DateTime, default=_brt_now, onupdate=_brt_now)


class RegraExclusao(Base):
    __tablename__ = "regras_exclusao"

    id: Mapped[int] = mapped_column(primary_key=True)
    usuario_id: Mapped[int | None] = mapped_column(ForeignKey("usuarios.id"), index=True, nullable=True)
    # tipo: "termo" (palavra no objeto/item) ou "categoria" (código de categoria PNCP)
    tipo: Mapped[str] = mapped_column(String(20), default="termo")
    valor: Mapped[str] = mapped_column(String(120))
    ativo: Mapped[bool] = mapped_column(Boolean, default=True)


class LogColeta(Base):
    __tablename__ = "logs_coleta"

    id: Mapped[int] = mapped_column(primary_key=True)
    usuario_id: Mapped[int | None] = mapped_column(ForeignKey("usuarios.id"), index=True, nullable=True)
    fonte: Mapped[str] = mapped_column(String(40))
    origem: Mapped[str | None] = mapped_column(String(10), nullable=True)  # "manual" ou "cron"
    iniciado_em: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finalizado_em: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    editais_novos: Mapped[int] = mapped_column(Integer, default=0)
    editais_vistos: Mapped[int] = mapped_column(Integer, default=0)
    matches_fortes: Mapped[int] = mapped_column(Integer, default=0)
    erro: Mapped[str | None] = mapped_column(Text, nullable=True)


class Configuracao(Base):
    """Configurações editáveis pelo painel (UFs, modalidades, etc.).
    Sobrepõem os valores das variáveis de ambiente quando definidas."""
    __tablename__ = "configuracoes"

    chave: Mapped[str] = mapped_column(String(60), primary_key=True)
    valor: Mapped[str] = mapped_column(Text, default="")


class Documento(Base):
    """Documentos de habilitação (certidões, SICAF, etc.) com data de validade,
    para o sistema avisar antes de vencer."""
    __tablename__ = "documentos"

    id: Mapped[int] = mapped_column(primary_key=True)
    usuario_id: Mapped[int | None] = mapped_column(ForeignKey("usuarios.id"), index=True, nullable=True)
    nome: Mapped[str] = mapped_column(String(160))           # ex.: "Certidão Negativa FGTS"
    orgao_emissor: Mapped[str | None] = mapped_column(String(160), nullable=True)
    # None = documento sem vencimento (ex.: contrato social) -- não entra em
    # nenhum aviso de prazo (ver lembretes.py/telegram_menu.py) nem no
    # calendário de compromissos (o filtro por intervalo já exclui NULL).
    data_validade: Mapped[date | None] = mapped_column(Date, nullable=True)
    link: Mapped[str | None] = mapped_column(String(500), nullable=True)  # onde está o documento
    observacao: Mapped[str | None] = mapped_column(Text, nullable=True)
    ativo: Mapped[bool] = mapped_column(Boolean, default=True)
    # texto extraído do PDF/imagem que o usuário anexou ao cadastrar (PDF via
    # pypdf/OCR, imagem via OCR — ver analise_edital.extrair_texto_upload).
    # Usado pela análise por IA do edital para comparar o CONTEÚDO real do
    # documento contra o que está sendo exigido, não só o nome cadastrado.
    texto_extraido: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Arquivo original (o "cofre" propriamente dito), cifrado com Fernet
    # (auth.cifrar) igual ao resto dos dados sensíveis do usuário -- v1
    # guarda os bytes (base64) direto no banco por simplicidade, já que o
    # volume por usuário é pequeno (poucas certidões). Se o volume crescer
    # bastante (muitos usuários/arquivos), o caminho de evolução é migrar
    # pra um storage dedicado (S3-like) com URL assinada temporária, em vez
    # de continuar empurrando o banco.
    arquivo_cifrado: Mapped[str | None] = mapped_column(Text, nullable=True)
    arquivo_nome: Mapped[str | None] = mapped_column(String(255), nullable=True)
    arquivo_tipo: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # para não avisar o mesmo vencimento repetidamente (guarda a validade já avisada)
    avisado_para: Mapped[date | None] = mapped_column(Date, nullable=True)             # e-mail
    avisado_para_telegram: Mapped[date | None] = mapped_column(Date, nullable=True)    # menu do Telegram
    avisado_para_telegram_2: Mapped[date | None] = mapped_column(Date, nullable=True)  # 2º contato do Telegram
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Proposta(Base):
    """Proposta comercial do usuário para um edital: itens com custo e preço,
    para calcular total e margem. Uma proposta por edital."""
    __tablename__ = "propostas"
    __table_args__ = (UniqueConstraint("edital_id", name="uq_proposta_edital"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    usuario_id: Mapped[int | None] = mapped_column(ForeignKey("usuarios.id"), index=True, nullable=True)
    edital_id: Mapped[int] = mapped_column(ForeignKey("editais.id"))
    # itens: [{descricao, quantidade, custo_unit, preco_unit}]
    itens: Mapped[list | None] = mapped_column(JSON, nullable=True)
    observacoes: Mapped[str | None] = mapped_column(Text, nullable=True)
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    atualizado_em: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow)
