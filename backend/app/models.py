"""
Modelos de dados (SQLAlchemy).

Entidades principais:
- Produto: catálogo do que a empresa vende (descrição, códigos, palavras-chave).
- Edital: uma contratação/licitação coletada de um portal (ex.: PNCP).
- ItemEdital: cada item solicitado dentro de um edital.
- Match: vínculo entre um Edital e o catálogo, com pontuação e nível.
- RegraExclusao: termos/categorias que o usuário quer ignorar.
"""
from datetime import datetime, date
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
    # verificação de e-mail
    email_verificado: Mapped[bool] = mapped_column(Boolean, default=False)
    token_verificacao: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # integrações próprias do usuário (cifradas/preferências)
    gemini_key_cifrada: Mapped[str | None] = mapped_column(Text, nullable=True)
    telegram_chat_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    notif_email: Mapped[bool] = mapped_column(Boolean, default=True)
    notif_telegram: Mapped[bool] = mapped_column(Boolean, default=False)
    ativo: Mapped[bool] = mapped_column(Boolean, default=True)
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


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
    # Preços (para cálculo de margem)
    preco_custo: Mapped[float | None] = mapped_column(Float, nullable=True)   # quanto você paga
    preco_venda: Mapped[float | None] = mapped_column(Float, nullable=True)   # seu preço de venda
    # Fornecedor
    fornecedor_nome: Mapped[str | None] = mapped_column(String(160), nullable=True)
    fornecedor_contato: Mapped[str | None] = mapped_column(String(160), nullable=True)
    fornecedor_site: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ativo: Mapped[bool] = mapped_column(Boolean, default=True)
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


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
    data_abertura: Mapped[date | None] = mapped_column(Date, nullable=True)
    data_encerramento: Mapped[date | None] = mapped_column(Date, nullable=True)  # fim recebimento propostas
    link: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    analise_ia: Mapped[str | None] = mapped_column(Text, nullable=True)        # JSON da análise (cache)
    analise_em: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    coletado_em: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    itens: Mapped[list["ItemEdital"]] = relationship(
        back_populates="edital", cascade="all, delete-orphan"
    )
    match: Mapped["Match | None"] = relationship(
        back_populates="edital", cascade="all, delete-orphan", uselist=False
    )


class ItemEdital(Base):
    __tablename__ = "itens_edital"

    id: Mapped[int] = mapped_column(primary_key=True)
    edital_id: Mapped[int] = mapped_column(ForeignKey("editais.id"))
    numero: Mapped[int | None] = mapped_column(Integer, nullable=True)
    descricao: Mapped[str] = mapped_column(Text)
    material_ou_servico: Mapped[str | None] = mapped_column(String(20), nullable=True)
    ncm: Mapped[str | None] = mapped_column(String(20), nullable=True)
    catalogo_codigo: Mapped[str | None] = mapped_column(String(40), nullable=True)  # CATMAT/CATSER
    quantidade: Mapped[float | None] = mapped_column(Float, nullable=True)
    valor_unitario: Mapped[float | None] = mapped_column(Float, nullable=True)

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
    notificado: Mapped[bool] = mapped_column(Boolean, default=False)
    prazo_avisado: Mapped[bool] = mapped_column(Boolean, default=False)  # lembrete de prazo já enviado
    # acompanhamento (pipeline): novo, vou_participar, proposta_enviada, ganho, perdido, descartado
    status: Mapped[str] = mapped_column(String(20), default="novo")
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    edital: Mapped["Edital"] = relationship(back_populates="match")


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
    fonte: Mapped[str] = mapped_column(String(40))
    iniciado_em: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
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
    data_validade: Mapped[date] = mapped_column(Date)
    observacao: Mapped[str | None] = mapped_column(Text, nullable=True)
    ativo: Mapped[bool] = mapped_column(Boolean, default=True)
    # para não avisar o mesmo vencimento repetidamente (guarda a validade já avisada)
    avisado_para: Mapped[date | None] = mapped_column(Date, nullable=True)
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


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
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    atualizado_em: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
