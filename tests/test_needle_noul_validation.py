"""Testes reais e sem mocks para a Camada 2: Needle 3 AST + JEV Noul Universal."""

import asyncio
from pathlib import Path
from typing import List

import pytest

from src.core.schemas import ScoreDiagnoseResponse
from src.memory.ast_diff_extractor import (
    AstSymbolSlice,
    extract_workspace_ast_symbols,
    get_git_modified_line_ranges,
)
from src.memory.needle_defect_screener import (
    DEFECT_NOUL_HYPOTHESES,
    DefectSuspicion,
    screen_symbol_defects,
    screen_workspace_symbols,
)


def test_ast_diff_extractor_extracts_real_symbols():
    """Valida que o extrator de símbolos AST localiza símbolos reais do workspace."""
    workspace = Path(__file__).parent.parent.resolve()
    symbols = extract_workspace_ast_symbols(str(workspace))
    assert isinstance(symbols, list)
    # Garante que os símbolos extraídos possuam campos sintáticos íntegros
    for s in symbols:
        assert isinstance(s, AstSymbolSlice)
        assert s.file_path.endswith(".py")
        assert s.start_line >= 1
        assert s.end_line >= s.start_line
        assert s.content.strip() != ""
        assert s.symbol_name != ""


def test_needle_defect_screener_detects_structural_suspicions():
    """Valida a triagem de suspeitas estruturais contra trechos conhecidos de código."""
    clean_code = '''
def soma_segura(a: int, b: int) -> int:
    """Realiza uma soma pura sem I/O ou efeitos colaterais."""
    return a + b
'''
    clean_slice = AstSymbolSlice(
        file_path="src/math_utils.py",
        symbol_name="soma_segura",
        symbol_type="function",
        start_line=1,
        end_line=5,
        content=clean_code.strip(),
    )
    suspicions_clean = screen_symbol_defects(clean_slice)
    assert len(suspicions_clean) == 0, "Código limpo puro deve ter zero suspeitas (Fast-Path)"

    # Código com I/O fora de bloco protegido (gatilho de robustez)
    io_code = '''
def le_arquivo(caminho: str) -> str:
    f = open(caminho, "r")
    f.seek(0)
    data = f.read()
    return data
'''
    io_slice = AstSymbolSlice(
        file_path="src/io_utils.py",
        symbol_name="le_arquivo",
        symbol_type="function",
        start_line=1,
        end_line=7,
        content=io_code.strip(),
    )
    suspicions_io = screen_symbol_defects(io_slice)
    categories_io = {s.category for s in suspicions_io}
    assert "robustez_excecoes_recursos" in categories_io, "Deve identificar risco de vazamento de recurso/open fora de context manager"


def test_score_diagnose_response_approved_with_caveat():
    """Valida que o contrato Pydantic serializa approved_with_caveat corretamente."""
    resp = ScoreDiagnoseResponse(
        score=3.24,
        max_score=5.0,
        normalized_score=0.56,
        passed=True,
        threshold=3.40,
        breakdown={},
        score_latency_ms=15.0,
        attempts=0,
        approved_with_caveat=True,
        message="Entrega aprovada com ressalva: Nenhuma falha estrutural detectada pelo Needle 3.",
    )
    data = resp.model_dump()
    assert data["passed"] is True
    assert data["approved_with_caveat"] is True
    assert "ressalva" in data["message"].lower()

    # Deserialização e validação
    parsed = ScoreDiagnoseResponse.model_validate(data)
    assert parsed.approved_with_caveat is True
    assert parsed.passed is True


def test_ast_diff_extractor_class_methods_and_dedent(tmp_path: Path):
    """Valida que métodos dentro de classes são extraídos cirurgicamente e parseados sem erro de indentação."""
    sample_file = tmp_path / "sample_service.py"
    sample_file.write_text(
        '''
class PaymentService:
    """Serviço de pagamentos."""

    def __init__(self, token: str):
        self.token = token

    def process_transaction(self, amount: float) -> bool:
        if amount <= 0:
            return False
        return True
'''.strip(),
        encoding="utf-8",
    )

    from src.memory.ast_diff_extractor import extract_ast_symbols_from_file

    # Simula alteração apenas nas linhas do método process_transaction (linhas 7-10)
    changed_lines = {7, 8, 9, 10}
    slices = extract_ast_symbols_from_file("sample_service.py", sample_file, changed_lines)

    assert len(slices) == 1
    assert slices[0].symbol_name == "PaymentService.process_transaction"
    assert slices[0].symbol_type == "method"

    # Testa que o screener consegue parsear o método indentado sem IndentationError
    suspicions = screen_symbol_defects(slices[0])
    assert isinstance(suspicions, list)
    # Não deve ter suspeita de contrato por sintaxe quebrada
    syntax_errors = [s for s in suspicions if "Erro de sintaxe AST" in s.trigger_evidence]
    assert len(syntax_errors) == 0

