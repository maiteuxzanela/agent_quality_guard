"""Contratos de dados Pydantic estritos para as 3 camadas do sistema JEV:
1. Memória & Busca (Needle 3)
2. Decisão & Roteamento (Open Jev: Noul, Choice, Score)
3. Execução & Orquestração
"""

from typing import Dict, List, Optional
from pydantic import BaseModel, Field, model_validator


# ==============================================================================
# Exceções Tipadas Estritas (Fail-Fast Policy - Sem Mocks/Simulações)
# ==============================================================================

class OpenJevError(Exception):
    """Exceção base para falhas do subsistema Open Jev."""
    pass


class OpenJevNotReadyError(OpenJevError):
    """Lançada quando a engine/modelo não foi inicializada ou os pesos não estão carregados."""
    pass


class HardwareUnavailableError(OpenJevError):
    """Lançada quando a GPU/CPU requisitada não atende aos requisitos ou está inacessível."""
    pass


class InferenceError(OpenJevError):
    """Lançada quando a inferência do tensor falha durante a execução matemática."""
    pass


class Needle3Error(Exception):
    """Exceção base para falhas no motor de recuperação Needle 3."""
    pass


class Needle3NotAvailableError(Needle3Error):
    """Lançada quando o serviço ou API Cactus Needle 3 não está alcançável."""
    pass


# ==============================================================================
# Schemas Open Jev: Primitiva Noul (Avaliação Booleana / Guardrail)
# ==============================================================================

class NoulRequest(BaseModel):
    """Requisição para avaliação de condição lógica booleana."""
    premise: str = Field(
        ...,
        description="Contexto fático, estado atual ou comando proposto para execução."
    )
    hypothesis: str = Field(
        ...,
        description="Condição de segurança, regra de negócio ou predicado a validar (deve ser verdadeiro/falso)."
    )
    threshold: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="Limiar de probabilidade mínima para considerar a condição verdadeira."
    )


class NoulResponse(BaseModel):
    """Resposta tipada da primitiva Noul."""
    passed: bool = Field(
        ...,
        description="Indica se a probabilidade calculada foi maior ou igual ao threshold estipulado."
    )
    probability: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Probabilidade calibrada P(hipótese é verdadeira dado o contexto)."
    )
    confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Métrica de certeza da decisão (|probabilidade - 0.5| * 2)."
    )
    threshold: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Limiar de corte utilizado nesta avaliação."
    )
    latency_ms: float = Field(
        ...,
        ge=0.0,
        description="Tempo de latência da inferência em milissegundos."
    )


# ==============================================================================
# Schemas Open Jev: Primitiva Choice (Roteamento Categórico de Especialistas)
# ==============================================================================

class ChoiceRequest(BaseModel):
    """Requisição para seleção de alternativa em espaço discreto de opções."""
    premise: str = Field(
        ...,
        description="Descrição da tarefa, objetivo ou contexto a ser categorizado."
    )
    options: List[str] = Field(
        ...,
        min_length=2,
        description="Lista de categorias ou subagentes disponíveis (ex.: backend, security, frontend, docs)."
    )
    temperature: float = Field(
        default=1.0,
        gt=0.0,
        le=5.0,
        description="Temperatura para calibrar a distribuição softmax das probabilidades."
    )

    @model_validator(mode="after")
    def validate_unique_options(self):
        if len(self.options) != len(set(self.options)):
            raise ValueError("As opções de escolha devem ser mutuamente exclusivas e sem duplicatas.")
        return self


class ChoiceResponse(BaseModel):
    """Resposta tipada da primitiva Choice."""
    selected: str = Field(
        ...,
        description="Opção que obteve a maior probabilidade discriminativa."
    )
    probabilities: Dict[str, float] = Field(
        ...,
        description="Distribuição completa de probabilidades sobre todas as opções fornecidas (soma ~ 1.0)."
    )
    top_confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Probabilidade da opção vencedora."
    )
    latency_ms: float = Field(
        ...,
        ge=0.0,
        description="Tempo de inferência em milissegundos."
    )


# ==============================================================================
# Schemas Open Jev: Primitiva Score (Avaliação e Calibração Ordinal de Entrega)
# ==============================================================================

class ScoreRequest(BaseModel):
    """Requisição para avaliação de qualidade e conformidade contra uma rubrica."""
    premise: str = Field(
        ...,
        description="O artefato produzido, diff de código, resposta ou plano a ser avaliado."
    )
    rubric: str = Field(
        default="Avalie a resposta técnica de 1.0 a 5.0 considerando rigor analítico, clareza didática, fidelidade ao código real do repositório e atendimento aos comandos do usuário.",
        description="Critérios de conformidade e rubrica de qualidade esperada."
    )
    min_score: float = Field(
        default=1.0,
        description="Nota mínima da escala (ex.: 1.0)."
    )
    max_score: float = Field(
        default=5.0,
        description="Nota máxima da escala (ex.: 5.0)."
    )
    threshold: Optional[float] = Field(
        default=3.75,
        description="Nota mínima para aprovação (se fornecido, retorna se foi aprovado)."
    )

    @model_validator(mode="after")
    def validate_scale(self):
        if self.min_score >= self.max_score:
            raise ValueError("min_score deve ser estritamente menor que max_score.")
        if self.threshold is not None:
            if not (self.min_score <= self.threshold <= self.max_score):
                raise ValueError("threshold deve estar contido no intervalo [min_score, max_score].")
        return self


class ScoreResponse(BaseModel):
    """Resposta tipada da primitiva Score."""
    score: float = Field(
        ...,
        description="Nota contínua interpolada calculada pelo modelo (ex.: 4.2 em escala de 1 a 5)."
    )
    normalized_score: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Nota normalizada proporcionalmente no intervalo [0.0, 1.0]."
    )
    passed: Optional[bool] = Field(
        default=None,
        description="Indica se a nota obtida atingiu ou superou o threshold configurado."
    )
    breakdown: Dict[str, float] = Field(
        default_factory=dict,
        description="Distribuição de pesos por nível ou dimensão da rubrica."
    )
    latency_ms: float = Field(
        ...,
        ge=0.0,
        description="Tempo de latência da inferência em milissegundos."
    )


# ==============================================================================
# Schemas Cactus Needle 3: Busca & Recuperação Cirúrgica Relacional
# ==============================================================================

class Needle3Query(BaseModel):
    """Requisição de busca cirúrgica para o Cactus Needle 3."""
    query: str = Field(
        ...,
        description="Termo ou pergunta técnica de busca."
    )
    scope: Optional[List[str]] = Field(
        default=None,
        description="Filtro opcional de diretórios, pacotes ou arquivos-alvo."
    )
    max_tokens: int = Field(
        default=1500,
        gt=0,
        le=4096,
        description="Limite máximo estrito de tokens para o recorte retornado."
    )
    relational_depth: int = Field(
        default=2,
        ge=0,
        le=5,
        description="Profundidade do grafo de relações (chamadores, implementações, imports, referências de tipo)."
    )
    preserve_relational_integrity: bool = Field(
        default=True,
        description="Se verdadeiro, garante que dependências relacionais do recorte sejam incluídas sem truncamento arbitrário."
    )


class Needle3Snippet(BaseModel):
    """Fragmento cirúrgico recuperado pelo motor Needle 3."""
    file_path: str = Field(..., description="Caminho relativo ou absoluto do arquivo de origem.")
    start_line: int = Field(..., ge=1, description="Linha inicial do trecho.")
    end_line: int = Field(..., ge=1, description="Linha final do trecho.")
    content: str = Field(..., description="Conteúdo exato do fragmento de código ou documentação.")
    relevance_score: float = Field(..., ge=0.0, le=1.0, description="Score de relevância semântica calculada pelo Needle 3.")
    relations: List[str] = Field(
        default_factory=list,
        description="Relações detectadas no grafo de AST (ex: 'caller: auth_handler', 'implements: BaseRepository')."
    )


class Needle3Response(BaseModel):
    """Resposta agregada da busca cirúrgica do Needle 3."""
    snippets: List[Needle3Snippet] = Field(..., description="Lista de trechos cirúrgicos recuperados.")
    total_tokens: int = Field(..., ge=0, description="Total de tokens consumidos pelos trechos.")
    query_latency_ms: float = Field(..., ge=0.0, description="Latência da busca no Cactus Compute.")
