"""Testes de scripts/audit_mocks.py — scrub determinístico com arquivos REAIS em tmp_path.

Sem mocks (AGENTS.md §5.2): cria arquivos de verdade no disco e exercita
as funções puras/semi-puras do script de auditoria de mocks.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from audit_mocks import (  # noqa: E402
    _is_test_path,
    scrub_mock_occurrences,
    scrub_summary_text,
    summarize_occurrences,
)


def test_scrub_detecta_padroes_python(tmp_path):
    (tmp_path / "app.py").write_text(
        "from unittest.mock import MagicMock, patch\n"
        "import unittest.mock\n"
        "@patch('os.remove')\n"
        "def run(x):\n"
        "    m = MagicMock()\n"
        "    return m\n"
    )
    occ = scrub_mock_occurrences(str(tmp_path))
    patterns = {o["pattern"] for o in occ}
    assert "python_mock_import" in patterns
    assert "magic_mock" in patterns
    assert "patch_decorator" in patterns
    assert all(o["file"] == "app.py" for o in occ)
    assert all(isinstance(o["line"], int) and o["line"] >= 1 for o in occ)


def test_scrub_detecta_padroes_javascript(tmp_path):
    (tmp_path / "setup.js").write_text(
        "jest.mock('./api');\n"
        "const s = sinon.stub();\n"
        "jest.fn(() => 1);\n"
    )
    mocks_dir = tmp_path / "__mocks__"
    mocks_dir.mkdir()
    (mocks_dir / "fs.js").write_text("module.exports = { stat: () => {} };\n")
    occ = scrub_mock_occurrences(str(tmp_path))
    patterns = {o["pattern"] for o in occ}
    assert "jest_mock" in patterns
    assert "sinon" in patterns
    path_occ = [o for o in occ if o["pattern"] == "mock_implementation" and "__mocks__" in o["file"]]
    assert path_occ, "Arquivo em __mocks__/ deve gerar ocorrência estrutural"
    # text deve ser CONTEÚDO REAL do arquivo, nunca string fabricada
    assert path_occ[0]["text"] == "module.exports = { stat: () => {} };"


def test_scrub_zero_ocorrencias(tmp_path):
    (tmp_path / "clean.py").write_text("def add(a, b):\n    return a + b\n")
    occ = scrub_mock_occurrences(str(tmp_path))
    assert occ == []
    summary = summarize_occurrences(occ)
    assert summary["total"] == 0
    text = scrub_summary_text(summary, occ)
    assert "ZERO" in text


def test_scrub_exclui_node_modules_e_venv(tmp_path):
    nm = tmp_path / "node_modules" / "pkg"
    nm.mkdir(parents=True)
    (nm / "index.js").write_text("jest.mock('x');\n")
    venv = tmp_path / ".venv" / "lib"
    venv.mkdir(parents=True)
    (venv / "mod.py").write_text("from unittest.mock import MagicMock\n")
    (tmp_path / "real.py").write_text("from unittest.mock import patch\n")
    occ = scrub_mock_occurrences(str(tmp_path))
    files = {o["file"] for o in occ}
    assert files == {"real.py"}


def test_scrub_localizacao_metadata(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("from unittest.mock import MagicMock\n")
    (tmp_path / "src.py").write_text("m = MagicMock()\n")
    occ = scrub_mock_occurrences(str(tmp_path))
    by_file = {o["file"]: o["location"] for o in occ}
    assert by_file["tests/test_x.py"] == "em_testes"
    assert by_file["src.py"] == "em_producao"
    summary = summarize_occurrences(occ)
    assert summary["by_location"] == {"em_producao": 1, "em_testes": 1}
    assert summary["files_affected"] == 2


def test_is_test_path_variacoes():
    assert _is_test_path("tests/test_a.py")
    assert _is_test_path("src/__tests__/a.test.js")
    assert _is_test_path("App.test.js")
    assert _is_test_path("functions/__mocks__/x.js")
    assert not _is_test_path("src/main.py")
    assert not _is_test_path("jestSetup.js")


def test_summary_text_trunca_ocorrencias(tmp_path):
    lines = "\n".join(f"x{i} = MagicMock()  # {i}" for i in range(50))
    (tmp_path / "many.py").write_text(lines + "\n")
    occ = scrub_mock_occurrences(str(tmp_path))
    summary = summarize_occurrences(occ)
    assert summary["total"] == 50
    text = scrub_summary_text(summary, occ, limit=10)
    assert "+40 more" in text


# ─── Fail-fast + robustez por alvo (engine real, sem mock) ────────────────────

def test_audit_target_fail_fast_engine_nao_pronta(tmp_path):
    """Engine real SEM load deve lançar OpenJevNotReadyError (fail-fast preservado)."""
    import pytest
    from src.decision.engine import OpenJevEngine
    from src.memory.local_needle import LocalNeedleEngine
    from src.core.schemas import OpenJevNotReadyError
    from audit_mocks import audit_target

    engine = OpenJevEngine(device="cpu")  # sem load_model(); device explícito
    #   ignora IPC ambiente (daemon ativo) — estado real de não-pronto
    needle = LocalNeedleEngine(workspace_root=str(tmp_path))
    target = {"id": "t", "name": "Alvo", "path": str(tmp_path)}

    with pytest.raises(OpenJevNotReadyError):
        # asyncio.run porque o teste é síncrono; executa a coroutine real até o fail-fast.
        import asyncio
        asyncio.run(audit_target(engine, needle, target))


def test_safe_audit_target_registra_erro_sem_derrubar(tmp_path):
    """_safe_audit_target captura a exceção e registra error factually."""
    import asyncio
    from src.decision.engine import OpenJevEngine
    from src.memory.local_needle import LocalNeedleEngine
    from audit_mocks import _safe_audit_target

    engine = OpenJevEngine(device="cpu")  # sem load; device explícito ignora
    #   IPC ambiente (daemon ativo) — vai falhar de verdade
    needle = LocalNeedleEngine(workspace_root=str(tmp_path))
    target = {"id": "t", "name": "AlvoTeste", "path": str(tmp_path)}

    result = asyncio.run(_safe_audit_target(engine, needle, target))
    assert result["project"] == "AlvoTeste"
    assert "error" in result
    assert "OpenJevNotReadyError" in result["error"]
