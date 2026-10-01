"""Testes reais e sem mocks para a Camada 2: Needle 3 AST + JEV Noul Universal."""

import asyncio
from pathlib import Path
from typing import List

import pytest

from src.core.schemas import NeedleVerifyDeliveryResponse
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
    """Realiza uma soma pura sem I/O ou efeitos colaterais.

    Exemplo:
        >>> soma_segura(2, 3)
        5
    """
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


def _make_slice(code: str, file_path: str, name: str) -> AstSymbolSlice:
    return AstSymbolSlice(
        file_path=file_path,
        symbol_name=name,
        symbol_type="function",
        start_line=1,
        end_line=code.count("\n") + 1,
        content=code.strip(),
    )


def test_screener_tipagem_ausente_em_producao():
    """Categoria de análise: função de src/ sem anotações => tipagem_ausente."""
    slc = _make_slice(
        'def calcular(total, taxa):\n    return total * (1 + taxa)\n',
        "src/calculos.py",
        "calcular",
    )
    cats = {s.category for s in screen_symbol_defects(slc)}
    assert "tipagem_ausente" in cats
    susp = next(s for s in screen_symbol_defects(slc) if s.category == "tipagem_ausente")
    assert "taxa" in susp.trigger_evidence or "retorno" in susp.trigger_evidence


def test_screener_tipagem_muda_em_arquivo_de_teste():
    """Gatilhos de análise não valem em código de teste (tests/ fica mudo)."""
    slc = _make_slice(
        'def test_calcular():\n    assert calcular(1, 0.1)\n',
        "tests/test_calculos.py",
        "test_calcular",
    )
    assert screen_symbol_defects(slc) == []


def test_screener_docstring_ausente_em_producao():
    """Categoria de análise: função/classe de src/ sem docstring => docstring_ausente."""
    slc = _make_slice(
        'def aplicar(x: int) -> int:\n    return x + 1\n',
        "src/operacoes.py",
        "aplicar",
    )
    cats = {s.category for s in screen_symbol_defects(slc)}
    assert "docstring_ausente" in cats

    class_slc = _make_slice(
        'class Servico:\n    def rodar(self) -> None:\n        pass\n',
        "src/servico.py",
        "Servico",
    )
    cats_class = {s.category for s in screen_symbol_defects(class_slc)}
    assert "docstring_ausente" in cats_class


def test_screener_tipagem_e_docstring_mudos_em_codigo_exemplar():
    """Função tipada com docstring e exemplo em src/ => zero suspeitas (Fast-Path preservado)."""
    slc = _make_slice(
        'def somar(a: int, b: int) -> int:\n'
        '    """Soma dois inteiros.\n\n'
        '    Exemplo:\n'
        '        >>> somar(1, 2)\n'
        '        3\n'
        '    """\n'
        '    return a + b\n',
        "src/mat.py",
        "somar",
    )
    assert screen_symbol_defects(slc) == []


def test_screener_dunder_isento_de_analise():
    """Métodos dunder são isentos dos gatilhos de análise (convenção própria)."""
    slc = _make_slice(
        'class Ponto:\n    """Ponto 2D."""\n\n    def __init__(self, x, y):\n        self.x = x\n        self.y = y\n',
        "src/ponto.py",
        "Ponto",
    )
    cats = {s.category for s in screen_symbol_defects(slc)}
    assert "tipagem_ausente" not in cats
    assert "docstring_ausente" not in cats


def test_defect_hypotheses_cobrem_as_10_categorias():
    """DEFECT_NOUL_HYPOTHESES cobre exatamente DEFECT_OPTIONS (10 categorias)."""
    from src.core.schemas import DEFECT_OPTIONS
    assert set(DEFECT_NOUL_HYPOTHESES) == set(DEFECT_OPTIONS)
    assert all(h.strip() for h in DEFECT_NOUL_HYPOTHESES.values())


def test_needle_verify_delivery_response_approved():
    """Valida que o contrato Pydantic serializa NeedleVerifyDeliveryResponse corretamente."""
    resp = NeedleVerifyDeliveryResponse(
        passed=True,
        status="APPROVED",
        attempt_number=1,
        pytest_success=True,
        pytest_chunks=[],
        blocking_ast_violations=[],
        structural_ast_warnings=[],
        total_files_scanned=5,
        total_lines_scanned=250,
        message="Entrega aprovada deterministicamente.",
    )
    data = resp.model_dump()
    assert data["passed"] is True
    assert data["status"] == "APPROVED"

    parsed = NeedleVerifyDeliveryResponse.model_validate(data)
    assert parsed.passed is True
    assert parsed.status == "APPROVED"


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


# ─── Gatilhos Grupo A (set/2026): 13 novas detecções nas 10 categorias ────────


def _cats(code: str, path: str = "src/app.py", name: str = "f") -> set:
    return {s.category for s in screen_symbol_defects(_make_slice(code, path, name))}


def test_a1_arquivo_de_teste_sem_caminho_de_erro():
    """A1: módulo de teste sem pytest.raises/assertRaises => 1 suspeita no representante."""
    slices = [_make_slice("def test_a():\n    assert 1 == 1\n", "tests/test_a.py", "test_a")]
    result = screen_workspace_symbols(slices)
    assert any(
        s.category == "qualidade_de_testes" for sus in result.values() for s in sus
    )

    clean = [
        _make_slice(
            "import pytest\n\ndef test_a():\n    with pytest.raises(ValueError):\n        f()\n",
            "tests/test_b.py",
            "test_a",
        )
    ]
    assert screen_workspace_symbols(clean) == {}


def test_a2_docstring_sem_exemplo():
    """A2: docstring de função PÚBLICA de módulo sem '>>>'/'Exemplo:' => docstring_ausente.

    Calibração 09/2026: métodos e helpers privados ficam isentos (sem uso exibível).
    """
    bad = 'def add(a: int, b: int) -> int:\n    """Soma dois inteiros."""\n    return a + b\n'
    good = (
        'def add(a: int, b: int) -> int:\n'
        '    """Soma dois inteiros.\n\n    Exemplo:\n        >>> add(1, 2)\n        3\n    """\n'
        '    return a + b\n'
    )
    privado = 'def _helper(x: int) -> int:\n    """Sem uso exibível."""\n    return x\n'
    metodo = 'class C:\n    """Classe C."""\n\n    def process(self, x: int) -> int:\n        """Método interno."""\n        return x\n'
    metodo_avulso = '    def process(self, x: int) -> int:\n        """Método interno."""\n        return x\n'
    assert "docstring_ausente" in _cats(bad, name="add")
    assert "docstring_ausente" not in _cats(good, name="add")
    assert "docstring_ausente" not in _cats(privado, name="_helper")
    assert "docstring_ausente" not in _cats(metodo, name="C")
    assert "docstring_ausente" not in _cats(metodo_avulso, name="process")


def test_a3_recurso_aberto_sem_fechamento():
    """A3: `f = open()` sem close/with no símbolo => robustez (mesmo dentro de try)."""
    bad = (
        "def f() -> str:\n"
        "    try:\n"
        "        fh = open('x.txt')\n"
        "        return fh.read()\n"
        "    except OSError:\n"
        "        return ''\n"
    )
    good = "def f() -> str:\n    with open('x.txt') as fh:\n        return fh.read()\n"
    susp = screen_symbol_defects(_make_slice(bad, "src/io.py", "f"))
    assert any(
        s.category == "robustez_excecoes_recursos" and "sem .close()" in s.trigger_evidence
        for s in susp
    )
    assert not any(
        "sem .close()" in s.trigger_evidence
        for s in screen_symbol_defects(_make_slice(good, "src/io.py", "f"))
    )


def test_a4_excecao_engolida_por_controle_de_fluxo():
    """A4: handler de corpo único `return`/`continue` => robustez."""
    bad = "def f(x: int) -> int:\n    try:\n        return g(x)\n    except OSError:\n        return\n"
    good = (
        "def f(x: int) -> int:\n"
        "    try:\n"
        "        return g(x)\n"
        "    except OSError as exc:\n"
        "        logger.warning(exc)\n"
        "        raise\n"
    )
    assert "robustez_excecoes_recursos" in _cats(bad, name="f")
    assert "robustez_excecoes_recursos" not in _cats(good, name="f")


def test_a5_argumento_padrao_mutavel():
    """A5: default `[]`/`{}` => logica_ou_regra; default None => limpo."""
    assert "logica_ou_regra" in _cats("def f(items=[]):\n    return items\n")
    assert "logica_ou_regra" not in _cats("def f(items=None):\n    return items or []\n")


def test_a6_declaracao_global_de_estado():
    """A6: `global` em produção => logica_ou_regra."""
    assert "logica_ou_regra" in _cats("def f() -> None:\n    global counter\n    counter += 1\n")
    assert "logica_ou_regra" not in _cats("def f() -> int:\n    return counter\n")


def test_a7_concatenacao_de_string_em_laco():
    """A7: `s += ...` em laço com variável inferida como str => performance."""
    bad = "def f(xs: list) -> str:\n    out = ''\n    for x in xs:\n        out += str(x)\n    return out\n"
    good = "def f(xs: list) -> int:\n    total = 0\n    for x in xs:\n        total += x\n    return total\n"
    assert "performance_escopo_estilo" in _cats(bad, name="f")
    assert "performance_escopo_estilo" not in _cats(good, name="f")


def test_a8_acesso_direto_a_os_environ():
    """A8: os.environ[...] direto => documentacao_configuracao; .get => limpo."""
    bad = "import os\n\ndef f() -> str:\n    return os.environ['HOME']\n"
    good = "import os\n\ndef f() -> str:\n    return os.environ.get('HOME', '')\n"
    assert "documentacao_configuracao" in _cats(bad, name="f")
    assert "documentacao_configuracao" not in _cats(good, name="f")


def test_a9_caminho_ou_url_hardcoded():
    """A9: string com URL/caminho absoluto fora de docstring => documentacao_configuracao."""
    bad = 'def f() -> str:\n    endpoint = "https://api.exemplo.com/v1"\n    return endpoint\n'
    good = 'def f() -> str:\n    """Consulta.\n\n    Exemplo:\n        >>> get("https://x")\n    """\n    return "ok"\n'
    assert "documentacao_configuracao" in _cats(bad, name="f")
    assert "documentacao_configuracao" not in _cats(good, name="f")


def test_a10_stub_com_corpo_vazio():
    """A10: corpo só `...`/`pass` em função de módulo => requisito; método/abstractmethod => isento."""
    bad = "def pendente() -> None:\n    ...\n"
    good = (
        "from abc import abstractmethod\n\n"
        "class C:\n"
        "    @abstractmethod\n"
        "    def m(self) -> None:\n"
        "        ...\n"
        "\n"
        "    def close(self) -> None:\n"
        "        pass\n"
    )
    assert "requisito_incompleto_ou_ausente" in _cats(bad, name="pendente")
    assert "requisito_incompleto_ou_ausente" not in _cats(good, name="C")


def test_a11_parametro_str_direto_em_open():
    """A11: parâmetro str repassado a open() => seguranca; caminho literal => limpo."""
    bad = "def f(path: str) -> str:\n    return open(path).read()\n"
    good = "def f() -> str:\n    return open('/tmp/x').read()\n"
    assert "seguranca" in _cats(bad, name="f")
    assert "seguranca" not in _cats(good, name="f")


def test_a12_teste_de_classe_sem_assercao():
    """A12: método test_* em classe sem assert/assert* => qualidade_de_testes."""
    bad = "class TestX:\n    def test_ok(self):\n        helper()\n"
    good = "class TestX:\n    def test_ok(self):\n        assert value == 1\n"
    assert "qualidade_de_testes" in _cats(bad, path="tests/test_x.py", name="TestX")
    assert "qualidade_de_testes" not in _cats(good, path="tests/test_x.py", name="TestX")


def test_a13_print_em_producao_fora_do_cli():
    """A13: print em produção => performance (lint); dentro de __main__ => isento."""
    bad = "def f() -> None:\n    print('debug')\n"
    good = "if __name__ == '__main__':\n    print('cli')\n    main()\n"
    assert "performance_escopo_estilo" in _cats(bad, name="f")
    assert "performance_escopo_estilo" not in _cats(good, name="app")



def test_noul_request_rejects_out_of_range_threshold():
    """Caminho de erro real: threshold fora de [0, 1] é recusado pelo schema (fail-fast)."""
    from pydantic import ValidationError

    from src.core.schemas import NoulRequest

    with pytest.raises(ValidationError):
        NoulRequest(premise="Premissa", hypothesis="Hipótese", threshold=1.5)
