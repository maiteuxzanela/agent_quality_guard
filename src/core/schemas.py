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
# Schemas Open Jev: Score + Diagnose (Avaliação com Árvore de Defeitos)
# ==============================================================================

# Taxonomia Nível 1 — Macro-Categorias ortogonais de defeito
DEFECT_MACRO_CATEGORIES = [
    "falha_funcional_logica",
    "robustez_e_limites",
    "integridade_e_seguranca",
    "qualidade_de_testes",
    "contrato_e_acoplamento",
]

# Taxonomia Nível 2 — Micro-diagnósticos por macro-categoria
DEFECT_MICRO_OPTIONS: Dict[str, List[str]] = {
    "falha_funcional_logica": [
        "erro_off_by_one",
        "regra_de_negocio_incompleta",
        "estado_mutavel_inconsistente",
        "inversao_de_predicado",
    ],
    "robustez_e_limites": [
        "falta_tratamento_nulo_ou_vazio",
        "estouro_de_limites_numericos",
        "falha_de_tratamento_de_excecoes",
        "condicao_de_corrida_deadlock",
    ],
    "integridade_e_seguranca": [
        "injecao_e_sanitizacao",
        "vazamento_de_recursos",
        "credenciais_hardcoded",
        "autorizacao_e_autenticacao_frouxa",
    ],
    "qualidade_de_testes": [
        "ausencia_de_testes_unitarios",
        "teste_tautologico_ou_falso_positivo",
        "falta_cobertura_caminhos_de_erro",
        "testes_nao_reprodutiveis_flaky",
    ],
    "contrato_e_acoplamento": [
        "dependencia_ou_import_inexistente",
        "incompatibilidade_de_assinatura",
        "violacao_de_schema_de_dados",
        "quebra_de_retrocompatibilidade",
    ],
}

# Descrições humanas de cada micro-diagnóstico para feedback rico
DEFECT_MICRO_DESCRIPTIONS: Dict[str, str] = {
    # falha_funcional_logica
    "erro_off_by_one": "Índices de loops, fatiamento ou intervalos desviados por um elemento (< em vez de <=).",
    "regra_de_negocio_incompleta": "O fluxo geral foi implementado, mas uma cláusula específica da especificação foi omitida.",
    "estado_mutavel_inconsistente": "Efeitos colaterais indesejados, mutação acidental de parâmetros ou variáveis compartilhadas.",
    "inversao_de_predicado": "Operadores booleanos invertidos (and/or, ! mal posicionado ou lógica de curto-circuito errada).",
    # robustez_e_limites
    "falta_tratamento_nulo_ou_vazio": "Código lança exceções com coleções vazias ou valores nulos (NullPointerError, AttributeError).",
    "estouro_de_limites_numericos": "Divisão por zero, overflow de inteiros, perda de precisão em ponto flutuante ou datas inválidas.",
    "falha_de_tratamento_de_excecoes": "Exceções silenciadas (swallowed) ou ausência de try/catch em operações de I/O e rede.",
    "condicao_de_corrida_deadlock": "Falta de sincronização em concorrência, locks travados ou async/await mal utilizado.",
    # integridade_e_seguranca
    "injecao_e_sanitizacao": "Concatenação direta de parâmetros em SQL, shell ou HTML sem sanitização (CWE-89, CWE-79).",
    "vazamento_de_recursos": "Conexões de banco, file descriptors ou sockets não fechados após uso (with/try-finally ausente).",
    "credenciais_hardcoded": "Chaves de API, senhas, tokens ou URLs sensíveis inseridos diretamente no código-fonte.",
    "autorizacao_e_autenticacao_frouxa": "Falta de validação de escopo, permissões ou tokens antes de executar ações restritas.",
    # qualidade_de_testes
    "ausencia_de_testes_unitarios": "Nenhum arquivo ou função de teste foi fornecido na entrega.",
    "teste_tautologico_ou_falso_positivo": "Testes que sempre passam por mocks excessivos ou asserções triviais (assert True).",
    "falta_cobertura_caminhos_de_erro": "Testes cobrem apenas o caminho feliz (happy path), sem verificar falhas esperadas.",
    "testes_nao_reprodutiveis_flaky": "Testes dependem de ordem de execução, sleeps fixos ou estado externo não controlado.",
    # contrato_e_acoplamento
    "dependencia_ou_import_inexistente": "O código importa pacotes ou funções alucinadas/não declaradas no ambiente do projeto.",
    "incompatibilidade_de_assinatura": "Quantidade, nome ou tipo de argumentos e retornos divergem da interface esperada.",
    "violacao_de_schema_de_dados": "Objeto de saída não segue o formato exigido (campos faltantes ou tipos errados em JSON/dicts).",
    "quebra_de_retrocompatibilidade": "Alterações que quebram contratos públicos existentes sem migração documentada.",
}


class DiagnosisResult(BaseModel):
    """Resultado completo do diagnóstico em dois níveis após reprovação no Score."""
    macro_category: str = Field(
        ...,
        description="Macro-categoria de defeito selecionada pelo Laya (Nível 1 da árvore de diagnóstico).",
    )
    macro_confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Confiança do Laya na seleção da macro-categoria (probabilidade da opção vencedora).",
    )
    macro_probabilities: Dict[str, float] = Field(
        default_factory=dict,
        description="Distribuição completa de probabilidades sobre as 5 macro-categorias.",
    )
    micro_diagnosis: str = Field(
        ...,
        description="Micro-diagnóstico específico dentro da macro-categoria (Nível 2 da árvore).",
    )
    micro_confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Confiança do Laya na seleção do micro-diagnóstico.",
    )
    micro_probabilities: Dict[str, float] = Field(
        default_factory=dict,
        description="Distribuição completa de probabilidades sobre os 4 micro-diagnósticos do nível 2.",
    )
    description: str = Field(
        ...,
        description="Descrição legível do micro-diagnóstico identificado para feedback ao agente.",
    )
    diagnosis_latency_ms: float = Field(
        ...,
        ge=0.0,
        description="Latência total das duas chamadas choice de diagnóstico em milissegundos.",
    )


class ScoreDiagnoseResponse(BaseModel):
    """Resposta unificada da validação Score + Diagnóstico em Árvore (quando score < threshold).

    Se passed=True, o campo 'diagnosis' é None.
    Se passed=False, 'diagnosis' contém o resultado completo L1+L2 da árvore de defeitos.
    """
    # --- Campos do Score (sempre presentes) ---
    score: float = Field(..., description="Nota contínua calculada pelo Laya (escala 1.0–5.0).")
    max_score: float = Field(default=5.0, description="Nota máxima da escala.")
    normalized_score: float = Field(..., ge=0.0, le=1.0, description="Nota normalizada [0.0, 1.0].")
    passed: bool = Field(..., description="True se score >= threshold.")
    threshold: float = Field(..., description="Nota de corte configurada.")
    breakdown: Dict[str, float] = Field(default_factory=dict, description="Pesos por nível de qualidade.")
    score_latency_ms: float = Field(..., ge=0.0, description="Latência da inferência de score em ms.")

    # --- Campos do Diagnóstico (apenas quando passed=False) ---
    diagnosis: Optional["DiagnosisResult"] = Field(
        default=None,
        description="Árvore de diagnóstico L1+L2. Presente apenas quando passed=False.",
    )

    # --- Controle Anti-Gaming (herdado do score) ---
    attempts: int = Field(default=1, description="Número de tentativas consecutivas sem aprovação.")
    max_attempts: int = Field(default=10, description="Máximo de tentativas antes da saída de emergência.")
    emergency_exit_unlocked: bool = Field(default=False, description="True após 10 tentativas sem refatoração.")


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
