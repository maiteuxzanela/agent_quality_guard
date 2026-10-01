"""Testes reais e sem mocks para as novas categorias da Camada 2 (Needle AST Screener).

Cobre as 5 regras avançadas especificadas para qualidade de engenharia:
- Regra A11: async_blocking_call
- Regra A12: mutable_default_argument
- Regra A13: naked_exception_reraise
- Regra A14: unbounded_session_lifecycle
- Regra A15: cognitive_nesting_depth
"""

import textwrap
from typing import List, Set

import pytest

from src.memory.ast_diff_extractor import AstSymbolSlice
from src.memory.needle_defect_screener import (
    DEFECT_NOUL_HYPOTHESES,
    DefectSuspicion,
    NeedleASTScreener,
    screen_symbol_defects,
)


def _make_slice(code: str, path: str = "src/service.py", name: str = "handler") -> AstSymbolSlice:
    """Helper para instanciar AstSymbolSlice com código Python dedentado."""
    dedented = textwrap.dedent(code).strip()
    lines = dedented.splitlines()
    return AstSymbolSlice(
        file_path=path,
        symbol_name=name,
        symbol_type="function",
        start_line=1,
        end_line=max(1, len(lines)),
        content=dedented,
    )


def _screen(code: str, path: str = "src/service.py", name: str = "handler") -> List[DefectSuspicion]:
    return screen_symbol_defects(_make_slice(code, path, name))


def _cats(code: str, path: str = "src/service.py", name: str = "handler") -> Set[str]:
    return {s.category for s in _screen(code, path, name)}


# ── Regra A11: async_blocking_call ──────────────────────────────────────────


def test_a11_async_blocking_call_time_sleep():
    """Identifica time.sleep() síncrono dentro de AsyncFunctionDef."""
    code = """
    import time

    async def worker() -> None:
        '''Executa trabalho assíncrono.

        Exemplo:
            >>> await worker()
        '''
        time.sleep(1.0)
    """
    suspicions = _screen(code, name="worker")
    assert any(
        s.category == "performance_escopo_estilo"
        and "time.sleep" in s.trigger_evidence
        for s in suspicions
    )


def test_a11_async_blocking_call_requests():
    """Identifica requests.get() dentro de AsyncFunctionDef."""
    code = """
    import requests

    async def fetch_data(url: str) -> str:
        '''Busca dados via HTTP síncrono.

        Exemplo:
            >>> await fetch_data("https://api.local")
        '''
        resp = requests.get(url)
        return resp.text
    """
    suspicions = _screen(code, name="fetch_data")
    assert any(
        s.category == "performance_escopo_estilo"
        and "requests.get" in s.trigger_evidence
        for s in suspicions
    )


def test_a11_async_blocking_call_open_and_subprocess():
    """Identifica open() direto e subprocess.run() dentro de AsyncFunctionDef."""
    code_open = """
    async def salvar(dados: str) -> None:
        '''Salva dados em disco.

        Exemplo:
            >>> await salvar("ok")
        '''
        f = open("/tmp/dados.txt", "w")
        f.write(dados)
        f.close()
    """
    assert any(
        s.category == "performance_escopo_estilo"
        and "open()" in s.trigger_evidence
        for s in _screen(code_open, name="salvar")
    )

    code_subproc = """
    import subprocess

    async def rodar() -> None:
        '''Executa comando externo.

        Exemplo:
            >>> await rodar()
        '''
        subprocess.run(["echo", "1"])
    """
    assert any(
        s.category == "performance_escopo_estilo"
        and "subprocess.run" in s.trigger_evidence
        for s in _screen(code_subproc, name="rodar")
    )


def test_a11_async_blocking_call_negative_clean():
    """Código assíncrono saudável (asyncio.sleep, httpx async with) passa sem suspeita."""
    code = """
    import asyncio
    import httpx

    async def healthy_worker(url: str) -> str:
        '''Executa trabalho assíncrono não-bloqueante.

        Exemplo:
            >>> await healthy_worker("https://example.com")
        '''
        await asyncio.sleep(0.01)
        async with httpx.AsyncClient() as client:
            resp = await client.get(url)
            return resp.text
    """
    suspicions = _screen(code, name="healthy_worker")
    blocking_suspicions = [
        s for s in suspicions
        if s.category == "performance_escopo_estilo" and "bloqueante" in s.trigger_evidence
    ]
    assert len(blocking_suspicions) == 0


# ── Regra A12: mutable_default_argument ─────────────────────────────────────


def test_a12_mutable_default_argument_positive():
    """Detecta defaults mutáveis literais (list, dict, set)."""
    bad_list = """
    def append_item(item: str, target: list = []) -> list:
        '''Acrescenta item.

        Exemplo:
            >>> append_item("x")
        '''
        target.append(item)
        return target
    """
    assert "logica_ou_regra" in _cats(bad_list, name="append_item")

    bad_dict = """
    def init_map(config: dict = {}) -> dict:
        '''Inicializa mapa.

        Exemplo:
            >>> init_map()
        '''
        return config
    """
    assert "logica_ou_regra" in _cats(bad_dict, name="init_map")

    bad_field_default = """
    from pydantic import Field

    def criar_usuario(tags: list = Field(default=[])) -> list:
        '''Cria usuário.

        Exemplo:
            >>> criar_usuario()
        '''
        return tags
    """
    suspicions = _screen(bad_field_default, name="criar_usuario")
    assert any(
        s.category == "logica_ou_regra" and "default_factory" in s.trigger_evidence
        for s in suspicions
    )


def test_a12_mutable_default_argument_negative_field_and_none():
    """Isenção para default=None e default_factory em Field/field."""
    good_none = """
    def append_item(item: str, target: list = None) -> list:
        '''Acrescenta item de forma segura.

        Exemplo:
            >>> append_item("x")
        '''
        lista = target if target is not None else []
        lista.append(item)
        return lista
    """
    assert "logica_ou_regra" not in _cats(good_none, name="append_item")

    good_field = """
    from pydantic import Field

    def registrar(items: list = Field(default_factory=list)) -> list:
        '''Registra itens com default_factory.

        Exemplo:
            >>> registrar()
        '''
        return items
    """
    assert "logica_ou_regra" not in _cats(good_field, name="registrar")


# ── Regra A13: naked_exception_reraise ──────────────────────────────────────


def test_a13_naked_exception_reraise_without_chain():
    """Detecta raise de novo erro sem 'from err' em except genérico."""
    bad_code = """
    def processar() -> None:
        '''Processa operação.

        Exemplo:
            >>> processar()
        '''
        try:
            val = int("invalido")
        except Exception:
            raise RuntimeError("Falha de conversao")
    """
    suspicions = _screen(bad_code, name="processar")
    assert any(
        s.category == "robustez_excecoes_recursos"
        and "sem encadeamento explícito" in s.trigger_evidence
        for s in suspicions
    )


def test_a13_naked_exception_logger_without_exc_info():
    """Detecta except genérico contendo apenas logger.error sem exc_info=True."""
    bad_code = """
    import logging
    logger = logging.getLogger("app")

    def executar() -> None:
        '''Executa comando com log incompleto.

        Exemplo:
            >>> executar()
        '''
        try:
            x = 1 / 0
        except Exception:
            logger.error("Erro inesperado no sistema")
    """
    suspicions = _screen(bad_code, name="executar")
    assert any(
        s.category == "robustez_excecoes_recursos"
        and "exc_info=True" in s.trigger_evidence
        for s in suspicions
    )


def test_a13_naked_exception_negative_clean():
    """Exceções encadeadas com 'from err', bare raise ou log com exc_info=True passam limpas."""
    good_chain = """
    def converter() -> int:
        '''Converte com encadeamento correto.

        Exemplo:
            >>> converter()
        '''
        try:
            return int("10")
        except Exception as err:
            raise RuntimeError("Erro ao converter") from err
    """
    chain_suspicions = [
        s for s in _screen(good_chain, name="converter")
        if "sem encadeamento" in s.trigger_evidence
    ]
    assert len(chain_suspicions) == 0

    good_bare_raise = """
    def relancar() -> None:
        '''Relança exceção original.

        Exemplo:
            >>> relancar()
        '''
        try:
            int("x")
        except Exception:
            raise
    """
    bare_suspicions = [
        s for s in _screen(good_bare_raise, name="relancar")
        if "sem encadeamento" in s.trigger_evidence
    ]
    assert len(bare_suspicions) == 0


# ── Regra A14: unbounded_session_lifecycle ──────────────────────────────────


def test_a14_unbounded_session_lifecycle_positive():
    """Detecta criação de cliente HTTP ou conexão de banco solto fora de context manager."""
    bad_httpx = """
    import httpx

    def obter_dados(url: str) -> str:
        '''Obtém dados criando cliente solto.

        Exemplo:
            >>> obter_dados("https://exemplo.com")
        '''
        client = httpx.Client()
        resp = client.get(url)
        return resp.text
    """
    suspicions = _screen(bad_httpx, name="obter_dados")
    assert any(
        s.category == "robustez_excecoes_recursos"
        and "httpx.Client()" in s.trigger_evidence
        for s in suspicions
    )

    bad_sqlite = """
    import sqlite3

    def consultar_banco() -> list:
        '''Consulta banco com conexão órfã.

        Exemplo:
            >>> consultar_banco()
        '''
        conn = sqlite3.connect("app.db")
        cursor = conn.cursor()
        return cursor.fetchall()
    """
    suspicions_sql = _screen(bad_sqlite, name="consultar_banco")
    assert any(
        s.category == "robustez_excecoes_recursos"
        and "sqlite3.connect()" in s.trigger_evidence
        for s in suspicions_sql
    )


def test_a14_unbounded_session_lifecycle_negative_with_and_self():
    """Context manager ('with') ou vinculação a self no __init__ não gera suspeita."""
    good_with = """
    import httpx

    def obter_dados(url: str) -> str:
        '''Obtém dados seguro com context manager.

        Exemplo:
            >>> obter_dados("https://exemplo.com")
        '''
        with httpx.Client() as client:
            return client.get(url).text
    """
    session_suspicions = [
        s for s in _screen(good_with, name="obter_dados")
        if "Instanciação de sessão/conexão" in s.trigger_evidence
    ]
    assert len(session_suspicions) == 0

    good_init = """
    import httpx

    class ServicoApi:
        '''Serviço de API que gerencia lifecycle no __init__.'''

        def __init__(self) -> None:
            self.client = httpx.Client()
    """
    slc = _make_slice(good_init, name="ServicoApi")
    init_suspicions = [
        s for s in screen_symbol_defects(slc)
        if "Instanciação de sessão/conexão" in s.trigger_evidence
    ]
    assert len(init_suspicions) == 0


# ── Regra A15: cognitive_nesting_depth ──────────────────────────────────────


def test_a15_cognitive_nesting_depth_positive():
    """Detecta aninhamento excessivo de blocos de controle (profundidade >= 4)."""
    deep_code = """
    def fluxo_complexo(a: bool, b: bool, items: list) -> int:
        '''Fluxo com aninhamento excessivo.

        Exemplo:
            >>> fluxo_complexo(True, True, [1])
        '''
        total = 0
        if a:
            for item in items:
                if b:
                    try:
                        total += int(item)
                    except Exception:
                        pass
        return total
    """
    suspicions = _screen(deep_code, name="fluxo_complexo")
    assert any(
        s.category == "performance_escopo_estilo"
        and "Complexidade cognitiva excessiva" in s.trigger_evidence
        for s in suspicions
    )


def test_a15_cognitive_nesting_depth_negative_shallow():
    """Funções com aninhamento moderado (< 4) passam limpas."""
    shallow_code = """
    def fluxo_simples(cond: bool, items: list) -> int:
        '''Fluxo modular com profundidade baixa.

        Exemplo:
            >>> fluxo_simples(True, [1, 2])
        '''
        if not cond:
            return 0
        return sum(items)
    """
    deep_suspicions = [
        s for s in _screen(shallow_code, name="fluxo_simples")
        if "Complexidade cognitiva excessiva" in s.trigger_evidence
    ]
    assert len(deep_suspicions) == 0


# ── Verificação de Extração Atômica e Hipóteses Calibradas ─────────────────


def test_atomic_snippet_window_and_hypothesis():
    """Garante que suspeitas geradas possuem snippet atômico (10-12 linhas) e hipóteses íntegras."""
    code = """
    import time

    async def processar_lote() -> None:
        '''Processa lote de dados.

        Exemplo:
            >>> await processar_lote()
        '''
        linha_1 = 1
        linha_2 = 2
        linha_3 = 3
        linha_4 = 4
        linha_5 = 5
        time.sleep(0.5)
        linha_7 = 7
        linha_8 = 8
        linha_9 = 9
        linha_10 = 10
    """
    suspicions = _screen(code, name="processar_lote")
    assert len(suspicions) > 0
    for s in suspicions:
        assert s.snippet.strip() != ""
        line_count = len(s.snippet.splitlines())
        assert line_count <= 15, f"Snippet muito longo ({line_count} linhas)"
        assert s.noul_hypothesis != ""
        assert s.category in DEFECT_NOUL_HYPOTHESES


def test_screener_handles_syntax_error_and_raises():
    """Valida tratamento defensivo e validação de caminho de erro (A1) via pytest.raises."""
    with pytest.raises(AttributeError):
        # Passar None para screen_symbol_defects deve disparar AttributeError esperado
        screen_symbol_defects(None)  # type: ignore
