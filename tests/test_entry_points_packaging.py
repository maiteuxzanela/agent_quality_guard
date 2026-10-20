"""Integridade dos entry points de empacotamento (TICKET-AQG-02).

Guarda determinística, sem mocks, de que:

1. Os console_scripts declarados no ``pyproject.toml`` resolvem para
   ``module:attr`` importáveis e chamáveis (``cli:main`` e ``mcp_server:main``).
2. Os módulos-alvo executam de verdade em subprocesso real (CLI ``--help``
   e servidor MCP sobre stdio com EOF imediato).
3. O wrapper Node (``bin/agent-quality-guard.js``) e o ``package.json``
   apontam para caminhos existentes do pacote real (não de ``src/``).
4. Os assets JS de package-data (vendor acorn/babel + js_ast_guard) existem
   e estão declarados no ``pyproject.toml``.
5. Não há referências stale de empacotamento para o antigo diretório ``src/``.

Qualquer renomeação/quebra de entry point, remoção de package-data ou
reativação de caminho ``src/`` neste repo derruba estes asserts.
"""

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PKG_DIR = ROOT / "agent_quality_guard"
PYPROJECT = ROOT / "pyproject.toml"
NODE_WRAPPER = ROOT / "bin" / "agent-quality-guard.js"
PACKAGE_JSON = ROOT / "package.json"

EXPECTED_SCRIPTS = {
    "agent-quality-guard": "agent_quality_guard.cli:main",
    "agent-quality-guard-mcp": "agent_quality_guard.mcp_server:main",
}


def _pyproject_section(section: str) -> dict:
    """Parser determinístico e sem dependências da seção ``[section]`` do toml.

    ``tomllib`` só existe em Python >= 3.11 e o CI roda matriz 3.10–3.12,
    por isso o parser linha-a-linha (suficiente para as seções rasas usadas
    aqui: ``[project.scripts]`` e ``[tool.setuptools.*]``).
    """
    text = PYPROJECT.read_text(encoding="utf-8")
    header = f"[{section}]"
    out = {}
    in_section = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            in_section = stripped == header
            continue
        if in_section and "=" in stripped and not stripped.startswith("#"):
            key, _, val = stripped.partition("=")
            # TOML permite chaves e valores entre aspas: agent_quality_guard.memory = ["*.js", ...]
            out[key.strip().strip('"').strip("'")] = val.strip().strip('"').strip("'")
    return out


class TestPyprojectConsoleScripts:

    def test_expected_entry_point_names_and_targets(self):
        """Os dois console_scripts canônicos permanecem declarados e corretos."""
        scripts = _pyproject_section("project.scripts")
        for name, target in EXPECTED_SCRIPTS.items():
            assert scripts.get(name) == target, (
                f"console_script '{name}' mudou: esperado '{target}', "
                f"encontrado '{scripts.get(name)}'"
            )

    @pytest.mark.parametrize("name,target", sorted(EXPECTED_SCRIPTS.items()))
    def test_target_resolves_to_importable_callable(self, name, target):
        """``module:attr`` de cada console_script importa e é chamável."""
        module_name, _, attr = target.partition(":")
        assert attr, f"target '{target}' sem separador module:attr"
        module = __import__(module_name, fromlist=["__name__"])
        fn = getattr(module, attr, None)
        assert callable(fn), f"{target} não resolve para callable (obtido: {fn!r})"

    def test_installed_metadata_matches_pyproject_when_available(self):
        """Se o dist estiver instalado, os console_scripts batem com o pyproject.

        Em CI (``pip install ".[dev]"``) e no .venv local o metadata existe;
        numa execução só-com-pythonpath o teste se abstém (sem mock/stub).
        """
        from importlib import metadata

        try:
            dist = metadata.distribution("agent-quality-guard-mcp")
        except metadata.PackageNotFoundError:
            pytest.skip("dist agent-quality-guard-mcp não instalado no ambiente")

        installed = {
            ep.name: ep.value
            for ep in dist.entry_points
            if ep.group == "console_scripts"
        }
        declared = _pyproject_section("project.scripts")
        for name, target in declared.items():
            assert installed.get(name) == target, (
                f"console_script instalado '{name}' diverge do pyproject: "
                f"{installed.get(name)!r} != {target!r}"
            )


class TestEntryModulesExecuteForReal:

    def test_entry_modules_declare_main_guard(self):
        """``cli.py`` e ``mcp_server.py`` executam direto como script.

        O wrapper Node faz ``spawn(python, [mcp_server.py])``, então o
        ``if __name__ == "__main__"`` é obrigatório nos dois módulos.
        """
        for rel in ("cli.py", "mcp_server.py"):
            source = (PKG_DIR / rel).read_text(encoding="utf-8")
            assert re.search(r'if\s+__name__\s*==\s*["\']__main__["\']', source), (
                f"{rel} sem guard __main__ — execução direta (wrapper Node) viria silenciosa"
            )

    def test_cli_entry_runs_help_in_real_subprocess(self):
        """O módulo-alvo do CLI responde ``--help`` com exit 0 em subprocesso."""
        result = subprocess.run(
            [sys.executable, str(PKG_DIR / "cli.py"), "--help"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
        assert "usage" in result.stdout.lower(), result.stdout[:400]

    def test_mcp_server_entry_starts_and_exits_clean_on_stdin_eof(self):
        """O módulo-alvo do MCP sobe sobre stdio e sai com 0 no EOF (sem hang)."""
        result = subprocess.run(
            [sys.executable, str(PKG_DIR / "mcp_server.py")],
            cwd=str(ROOT),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, (
            f"mcp_server.py exit={result.returncode}; stderr={result.stderr[:400]}"
        )


class TestNodeWrapperAndPackageJson:

    def test_node_wrapper_targets_real_package_path(self):
        """``bin/agent-quality-guard.js`` spawna o pacote real, não o antigo ``src/``."""
        assert NODE_WRAPPER.is_file(), "bin/agent-quality-guard.js ausente"
        source = NODE_WRAPPER.read_text(encoding="utf-8")
        assert 'path.join(rootDir, "agent_quality_guard")' in source, (
            "wrapper Node não referencia o diretório do pacote agent_quality_guard"
        )
        assert "src/mcp_server.py" not in source, (
            "wrapper Node ainda spawna src/mcp_server.py (caminho stale pré-rename)"
        )
        assert (PKG_DIR / "mcp_server.py").is_file(), (
            "alvo agent_quality_guard/mcp_server.py não existe no disco"
        )

    def test_package_json_bin_and_files_paths_exist(self):
        """Os caminhos ``bin`` e ``files`` do package.json existem no repo."""
        manifest = json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))
        for alias, rel in manifest["bin"].items():
            assert (ROOT / rel).is_file(), f"package.json bin['{alias}'] -> {rel} inexistente"
        for rel in manifest["files"]:
            assert (ROOT / rel).exists(), f"package.json files[] entry '{rel}' inexistente"
        assert "src" not in manifest["files"], (
            "package.json ainda empacota o diretório src/ pré-rename"
        )


class TestPackageDataAssets:

    def test_vendor_js_assets_exist_and_are_declared(self):
        """Assets JS de package-data existem no disco e no pyproject."""
        memory_dir = PKG_DIR / "memory"
        for asset in ("js_ast_guard.js",):
            assert (memory_dir / asset).is_file(), f"asset {asset} ausente em memory/"
        for vendor_asset in ("acorn.js", "babel_parser.js"):
            assert (memory_dir / "vendor" / vendor_asset).is_file(), (
                f"asset vendor/{vendor_asset} ausente"
            )

        package_data = _pyproject_section("tool.setuptools.package-data")
        memory_decl = package_data.get("agent_quality_guard.memory", "")
        assert "vendor/*.js" in memory_decl, (
            f"pyproject não declara vendor/*.js em package-data (obtido: {memory_decl!r})"
        )
        assert "*.js" in memory_decl, (
            f"pyproject não declara *.js em package-data (obtido: {memory_decl!r})"
        )

    def test_packages_find_targets_new_package_not_src(self):
        """``packages.find`` aponta para agent_quality_guard*, sem src*."""
        find = _pyproject_section("tool.setuptools.packages.find")
        include = find.get("include", "")
        where = find.get("where", "")
        assert "agent_quality_guard" in include, f"include stale: {include!r}"
        assert not re.search(r"\bsrc\b", include + " " + where), (
            f"empacotamento ainda referencia src/: include={include!r} where={where!r}"
        )
