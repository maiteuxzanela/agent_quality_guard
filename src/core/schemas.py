"""Contratos de dados Pydantic estritos para as 3 camadas do sistema JEV:
1. Memória & Busca (Needle 3)
2. Decisão & Roteamento (Open Jev: Noul, Choice, Score)
3. Execução & Orquestração
"""

from typing import Any, Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field, model_validator


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
        default=0.78,
        ge=0.0,
        le=1.0,
        description="Limiar de probabilidade mínima para considerar a condição verdadeira (guardrail calibrado 0.78)."
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
    instructions: Optional[str] = Field(
        default=None,
        description=(
            "Instrução específica do choice. Se None, usa o padrão de roteamento "
            "('Which specialist agent should handle this task?')."
        ),
    )
    criteria: Optional[Dict[str, str]] = Field(
        default=None,
        description=(
            "Mapa opção -> descrição descritiva servida ao modelo como critérios. "
            "Se None, usa o próprio nome da opção como critério (comportamento legado)."
        ),
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
# Schemas Open Jev: Consulta Direta (Indício Textual — sem booleano de aprovação)
# ==============================================================================

class ConsultaRequest(BaseModel):
    """Requisição de consulta direta (ex.: 'esse script funciona?').

    A resposta é um INDÍCIO, não um veredito. Reformular a premissa para
    arrancar uma resposta pré-definida é premise gaming e está proibido
    pela governança (AGENTS.md).
    """
    model_config = ConfigDict(extra="forbid")

    premise: str = Field(
        ...,
        min_length=1,
        description=(
            "Evidência objetiva: trecho do script, schema do banco, amostra de dados "
            "ou estado real observado."
        ),
    )
    question: str = Field(
        ...,
        min_length=1,
        description="Pergunta direta a ser respondida como indício textual.",
    )


class ConsultaResponse(BaseModel):
    """Resposta tipada da consulta direta: indício textual SEM campo `passed`.

    `extra='forbid'` garante contrato rígido — nunca haverá booleano de
    aprovação que convide o agente a praticar premise gaming.
    """
    model_config = ConfigDict(extra="forbid")

    indicio: str = Field(
        ...,
        min_length=1,
        description=(
            "Indício textual derivado da probabilidade (ex.: 'Indício forte a favor...', "
            "'Indício fraco contra...'). Não é veredito nem aprovação."
        ),
    )
    probability: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Probabilidade calibrada da questão ser verdadeira dado o contexto.",
    )
    confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Confiança da avaliação (|probabilidade - 0.5| * 2).",
    )
    latency_ms: float = Field(
        ...,
        ge=0.0,
        description="Tempo de latência da inferência em milissegundos.",
    )


# ==============================================================================
# Schemas Open Jev: Primitiva Score (Avaliação e Calibração Ordinal de Entrega)
# ==============================================================================

# Catálogo FECHADO de rubricas — única fonte de verdade (anti rubric gaming).
# O MCP resolve por `task_type`; qualquer `rubric` livre do cliente é ignorada.
RUBRIC_CATALOG: Dict[str, str] = {
    "entrega": (
        "Avalie a entrega técnica de 1.0 a 5.0 considerando fidelidade aos requisitos, "
        "correção lógica, integridade do código sem mocks em produção, tratamento de exceções "
        "e cobertura de testes reais automatizados."
    ),
    "auditoria": (
        "Avalie a aderência da evidência determinística fornecida de 1.0 a 5.0 considerando "
        "completude da evidência verificável (saídas de comando, arquivos, testes executados), "
        "rigor analítico, ausência de afirmação sem respaldo factual e fidelidade ao código real do repositório."
    ),
    "codigo_producao": (
        "Avalie o código para produção de 1.0 a 5.0 considerando ausência de mocks, stubs ou "
        "artefatos de teste no caminho de produção, tratamento de exceções e fechamento de recursos, "
        "sanitização de entrada, ausência de credenciais hardcoded e cobertura de testes reais."
    ),
}

# Constantes do Critério Probabilístico de Aprovação na Fronteira (Borderline Mass Gate)
BORDERLINE_HEALTHY_MIN_MASS: float = 0.85
BORDERLINE_FLAWED_MAX_MASS: float = 0.12



class ScoreRequest(BaseModel):
    """Requisição para avaliação de qualidade e conformidade contra uma rubrica."""
    premise: str = Field(
        ...,
        description="O artefato produzido, diff de código, resposta ou plano a ser avaliado."
    )
    rubric: str = Field(
        default=RUBRIC_CATALOG["entrega"],
        description="Critérios de conformidade e rubrica de qualidade esperada (via catálogo fechado no MCP)."
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
        default=3.40,
        description="Nota mínima para aprovação (calibrado Laya 3.40; se fornecido, retorna se foi aprovado)."
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
    score_margin: Optional[float] = Field(
        default=None,
        description="Margem de probabilidade em relação ao threshold: p_acima - p_abaixo em [-1.0, 1.0]."
    )
    borderline: Optional[bool] = Field(
        default=None,
        description="True quando ≥35% da massa probabilística está de cada lado do threshold (nota na fronteira)."
    )
    device: Optional[str] = Field(
        default=None,
        description=(
            "Dispositivo efetivo da inferência (cuda:0/cpu). fp16 GPU × fp32 CPU "
            "podem deslocar score/borderline perto do corte — observabilidade P3.4."
        ),
    )
    dtype: Optional[str] = Field(
        default=None,
        description="dtype efetivo (float16/float32) — Pascal sem bfloat16; migração em OOM muda o numérico."
    )


# ==============================================================================
# Schemas Open Jev: Score + Diagnóstico (Diagnóstico Flat — Choice único com 10 categorias)
# ==============================================================================
# ATENÇÃO de calibração (medido em golden set, ago/2026): o head do Laya só
# discrimina bem com <= 10 opções (bucket de temperature "6-10"). Com >= 11
# opções o modelo colapsa na opção-savor (precisão 1/4) — não reverter para
# listas finas de defeitos sem revalidar tests/test_diagnosis_calibration.py.
# Ordem: nulas em 1º e 6º (não na cauda) — head_max_len=192 não trunca
# instructions nem descrições (gate: scripts/measure_diag_choice_tokens.py).
DEFECT_OPTIONS: List[str] = [
    "sem_defeito_identificavel",
    "logica_ou_regra",
    "robustez_excecoes_recursos",
    "seguranca",
    "qualidade_de_testes",
    "evidencia_insuficiente",
    "contrato_ambiente",
    "performance_escopo_estilo",
    "requisito_incompleto_ou_ausente",
    "documentacao_configuracao",
]

# Instrução do choice de diagnóstico — distinta do texto de roteamento de subagentes.
# Serve DEFECT_DESCRIPTIONS como critérios via ChoiceRequest.criteria.
# Wordings neutras ("Classify…") mediram 2/4 no golden de defeitos (ago/2026);
# a formulação com pressuposto de falha calibrada 3/4 — manter até revalidar.
# TEXT CURTO DE PROPÓSITO: o álibi das nulas deve caber nas ~35 tok que sobram
# em head_max_len=192 após as 10 opções — não alongar sem re-medir tokens.
DIAG_CHOICE_INSTRUCTIONS = (
    "Audit code artifact for defects. If compliant, select sem_defeito_identificavel. "
    "Weak evidence: evidencia_insuficiente. Use descriptions."
)

# Descrições de cada categoria para feedback legível ao agente E como critérios
# do choice (ChoiceRequest.criteria). Textos EN calibrados no golden set —
# alterações devem revalidar tests/test_diagnosis_calibration.py.
# TEXTOS CURTOS OBRIGATÓRIOS: render "id: desc" + [MASK] deve somar ≤ 176 tok
# em head_max_len=192 (sem per-option cut) e deixar ≥ 35 tok para instructions.
DEFECT_DESCRIPTIONS: Dict[str, str] = {
    "sem_defeito_identificavel": "no defect; borderline noise",
    "logica_ou_regra": "wrong result, off-by-one rule",
    "robustez_excecoes_recursos": "bare except, null, race, leak",
    "seguranca": "injection, secrets, missing authz",
    "qualidade_de_testes": "missing tests, no error path",
    "evidencia_insuficiente": "cannot classify: no evidence",
    "contrato_ambiente": "signature or schema mismatch",
    "performance_escopo_estilo": "bottleneck, scope drift, lint",
    "requisito_incompleto_ou_ausente": "missing requested feature",
    "documentacao_configuracao": "docs or config wrong",
}

# Folhas (defeitos finos) candidatas de cada categoria — servem de dica para o
# agente refinar a auto-correção após o choice de categoria.
DEFECT_MEMBERS: Dict[str, List[str]] = {
    "logica_ou_regra": [
        "erro_off_by_one",
        "regra_de_negocio_incompleta",
        "estado_mutavel_inconsistente",
        "inversao_de_predicado",
    ],
    "robustez_excecoes_recursos": [
        "falha_de_tratamento_de_excecoes",
        "falta_tratamento_nulo_ou_vazio",
        "estouro_de_limites_numericos",
        "condicao_de_corrida_deadlock",
        "vazamento_de_recursos",
    ],
    "seguranca": [
        "injecao_e_sanitizacao",
        "credenciais_hardcoded",
        "autorizacao_e_autenticacao_frouxa",
    ],
    "qualidade_de_testes": [
        "ausencia_de_testes_unitarios",
        "teste_tautologico_ou_falso_positivo",
        "falta_cobertura_caminhos_de_erro",
        "testes_nao_reprodutiveis_flaky",
    ],
    "contrato_ambiente": [
        "dependencia_ou_import_inexistente",
        "incompatibilidade_de_assinatura",
        "violacao_de_schema_de_dados",
        "quebra_de_retrocompatibilidade",
        "incompatibilidade_de_ambiente_ou_runtime",
    ],
    "performance_escopo_estilo": [
        "gargalo_de_performance_ou_complexidade",
        "alucinacao_de_efeitos_colaterais_externos",
        "desvio_de_escopo_ou_overengineering",
        "inconsistencia_estilistica_ou_linting_estrito",
    ],
    "requisito_incompleto_ou_ausente": [
        "funcionalidade_do_pedido_ausente",
        "requisito_do_prompt_ignorado",
        "caso_de_borda_do_pedido_faltando",
    ],
    "documentacao_configuracao": [
        "documentacao_ausente_ou_incorreta",
        "configuracao_ou_governanca_incorreta",
    ],
    "sem_defeito_identificavel": [],
    "evidencia_insuficiente": [],
}


class DiagnosisResult(BaseModel):
    """Resultado do diagnóstico de defeito via choice único sobre 10 categorias flat."""
    selected: str = Field(
        ...,
        description="Categoria de defeito selecionada pelo Laya dentre as 10 opções da taxonomia flat.",
    )
    confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Probabilidade da opção vencedora (confiança do Laya na seleção).",
    )
    probabilities: Dict[str, float] = Field(
        default_factory=dict,
        description="Distribuição completa de probabilidades sobre todas as 10 opções.",
    )
    description: str = Field(
        ...,
        description="Descrição legível da categoria identificada para feedback ao agente.",
    )
    candidate_defects: List[str] = Field(
        default_factory=list,
        description=(
            "Folhas (defeitos finos) candidatas da categoria selecionada "
            "(DEFECT_MEMBERS) — dica para o agente refinar a auto-correção."
        ),
    )
    diagnosis_latency_ms: float = Field(
        ...,
        ge=0.0,
        description="Latência da chamada de choice em milissegundos.",
    )


class DiffStageResult(BaseModel):
    """Resultado da avaliação de uma etapa/chunk individual de diff de código."""
    stage_id: str = Field(..., description="Identificador único da etapa de diff.")
    file_path: str = Field(..., description="Caminho do arquivo correspondente.")
    part_index: int = Field(default=1, description="Índice da parte/bloco do arquivo.")
    total_parts: int = Field(default=1, description="Total de blocos gerados para o arquivo.")
    score: float = Field(..., description="Nota do Laya para a etapa (1.0 a 5.0).")
    passed: bool = Field(..., description="Se a etapa foi aprovada individualmente.")
    borderline: bool = Field(default=False, description="Se a etapa caiu na fronteira probabilística.")
    cached: bool = Field(default=False, description="Se foi reaproveitada de validação anterior sem nova inferência.")
    diagnosis: Optional["DiagnosisResult"] = Field(default=None, description="Diagnóstico de defeito se a etapa foi reprovada.")


class ScoreDiagnoseResponse(BaseModel):
    """Resposta unificada: Score + Diagnóstico flat (quando score < threshold).

    Se passed=True sem borderline, 'diagnosis' é None.
    Se passed=True com approved_as_borderline=True, 'diagnosis' traz a categoria
    nula eleita (sem_defeito_identificavel | evidencia_insuficiente).
    Se passed=False, 'diagnosis' contém o defeito identificado pelo choice único.
    """
    # --- Campos do Score (sempre presentes) ---
    score: float = Field(..., description="Nota contínua calculada pelo Laya (escala 1.0–5.0).")
    max_score: float = Field(default=5.0, description="Nota máxima da escala.")
    normalized_score: float = Field(..., ge=0.0, le=1.0, description="Nota normalizada [0.0, 1.0].")
    passed: bool = Field(
        ...,
        description=(
            "True se score >= threshold OU (borderline e diagnosis ∈ categorias nulas "
            "sem_defeito_identificavel | evidencia_insuficiente)."
        ),
    )
    threshold: float = Field(..., description="Nota de corte configurada.")
    breakdown: Dict[str, float] = Field(default_factory=dict, description="Pesos por nível de qualidade.")
    score_latency_ms: float = Field(..., ge=0.0, description="Latência da inferência de score em ms.")
    score_margin: Optional[float] = Field(
        default=None,
        description="Margem de probabilidade em relação ao threshold: p_acima - p_abaixo em [-1.0, 1.0].",
    )
    borderline: Optional[bool] = Field(
        default=None,
        description="True quando ≥35% da massa probabilística está de cada lado do threshold.",
    )
    task_type: Optional[str] = Field(
        default=None,
        description="Chave do catálogo fechado de rubricas usada (entrega | auditoria | codigo_producao).",
    )
    rubric_source: Optional[str] = Field(
        default=None,
        description=(
            "Origem da rubrica aplicada, sempre 'catalog:<task_type>' — a rubrica livre "
            "é ignorada pelo servidor (rubric gaming proibido)."
        ),
    )

    # --- Diagnóstico ---
    diagnosis: Optional["DiagnosisResult"] = Field(
        default=None,
        description=(
            "Defeito identificado pelo choice flat. Presente quando score < threshold "
            "(inclusive em borderline — o choice sempre roda nesse caminho)."
        ),
    )
    diagnosis_suppressed: Optional[str] = Field(
        default=None,
        description=(
            "DEPRECATED — a supressão em borderline foi removida: o choice sempre roda "
            "quando score < threshold. Campo mantido apenas por compatibilidade."
        ),
    )
    approved_as_borderline: bool = Field(
        default=False,
        description=(
            "True quando score < threshold, borderline=true e o choice elegeu "
            "categoria nula (sem_defeito_identificavel | evidencia_insuficiente) "
            "com confidence >= NULL_CHOICE_MIN_CONFIDENCE (0.40) — entrega aprovada "
            "como ruído de fronteira; divulgar score_margin (§4.7). Nula fraca não aprova."
        ),
    )

    # --- Avaliação Multi-Etapas (Diffs Reais) ---
    stages: List[DiffStageResult] = Field(
        default_factory=list,
        description="Lista detalhada das etapas de diff avaliadas.",
    )
    total_stages: int = Field(default=1, description="Total de etapas geradas a partir da diff do repositório.")
    passed_stages: int = Field(default=1, description="Quantidade de etapas aprovadas.")
    failed_stages: int = Field(default=0, description="Quantidade de etapas reprovadas.")

    # --- Controle Anti-Gaming ---
    attempts: int = Field(default=1, description="Tentativas consecutivas sem aprovação.")
    max_attempts: int = Field(default=5, description="Máximo antes da saída de emergência (MAX_ATTEMPTS).")
    emergency_exit_unlocked: bool = Field(default=False, description="True após MAX_ATTEMPTS tentativas sem refatoração.")

    # --- Needle 3 AST Guardrail (Fail-Fast Determinístico) ---
    ast_blocked: bool = Field(default=False, description="True se a entrega foi reprovada deterministamente pelo Needle 3 AST Guardrail.")
    ast_violations: Optional[List[Dict[str, Any]]] = Field(default=None, description="Violações determinísticas de AST detectadas pelo Needle 3.")


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
        default=1000,
        gt=0,
        le=4096,
        description="Limite máximo estrito de tokens para o recorte retornado (canônico 1000; faixa AGENTS 500–1500)."
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
    token_savings_pct: float = Field(
        default=0.0,
        ge=0.0,
        le=100.0,
        description="Percentual real de tokens economizados vs. indexar todos os blocos candidatos.",
    )


# ==============================================================================
# Schemas Subagentes Especialistas: Invocação com Contexto Isolado
# ==============================================================================

class SpawnSubagentRequest(BaseModel):
    """Requisição para despacho e execução de subagente especialista em contexto limpo."""
    persona: str = Field(
        ...,
        description="Identificador da persona especialista (ex: 'lumi', 'robin', 'kael', 'ariel', 'alex') ou caminho do arquivo."
    )
    task: str = Field(
        ...,
        description="Instrução cirúrgica e detalhada da tarefa técnica a ser executada pelo subagente."
    )
    workspace: Optional[str] = Field(
        default=None,
        description="Diretório absoluto da raiz do repositório alvo. Se omitido, assume o workspace corrente."
    )
    auto_validate: bool = Field(
        default=True,
        description="Se True, submete a entrega do subagente à auditoria fail-fast do Needle AST e ao Score JEV."
    )


class SpawnSubagentResponse(BaseModel):
    """Resposta do subagente especialista após execução em janela de contexto isolada."""
    success: bool = Field(..., description="Indica se o subagente completou a tarefa sem erros não tratados.")
    persona: str = Field(..., description="Nome da persona executada.")
    workspace: str = Field(..., description="Caminho do repositório onde a tarefa foi executada.")
    summary: str = Field(..., description="Relatório resumido e factual da entrega produzida pelo subagente.")
    files_modified: List[str] = Field(default_factory=list, description="Lista de arquivos modificados ou criados no repositório.")
    ast_blocked: bool = Field(default=False, description="Indica se a entrega foi interceptada pelo Needle AST Guardrail.")
    ast_violations: Optional[List[Dict[str, Any]]] = Field(default=None, description="Violações detectadas pelo Needle AST se ast_blocked for True.")
    score: Optional[float] = Field(default=None, description="Nota de conformidade atribuída pelo Open Jev Score (1.00 a 5.00).")
    passed: Optional[bool] = Field(default=None, description="Status de aprovação no quality gate JEV (score >= 3.40).")
    execution_time_ms: float = Field(..., ge=0.0, description="Tempo total de execução do subagente em milissegundos.")
    context_tokens_used: int = Field(default=0, ge=0, description="Estimativa de tokens de contexto consumidos na janela isolada.")

