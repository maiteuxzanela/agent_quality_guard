"""Testes de regressão para os defaults do CLI após migração DeBERTa → Laya.

IMPORTANTE: estes testes inspecionam o parser REAL exposto por
`src.cli.build_parser()` — não replicam o parser no teste (um espelho de
defaults hardcoded no próprio teste passaria mesmo se o CLI voltasse ao
DeBERTa legado; isso era um falso positivo corrigido nesta suíte).
"""

LEGACY_DEBERTA_ID = "com-kotobalabs/open-jev-deberta-v3-large"
EXPECTED_LAYA_ID = "convaiinnovations/laya"


def _parse_real(argv):
    """Parseia argv contra o parser REAL do CLI."""
    from src.cli import build_parser

    return build_parser().parse_args(argv)


# ---------------------------------------------------------------------------
# Testes de defaults (parser real)
# ---------------------------------------------------------------------------

class TestCliModelDefaults:
    """Verifica que todos os subcomandos do CLI usam Laya como padrão."""

    def test_choice_default_model_is_laya(self):
        args = _parse_real(["choice", "tarefa de roteamento", "-o", "backend,frontend"])
        assert args.model == EXPECTED_LAYA_ID, (
            f"choice --model deveria ser '{EXPECTED_LAYA_ID}', mas é '{args.model}'"
        )

    def test_noul_default_model_is_laya(self):
        args = _parse_real(["noul", "ação proposta", "-y", "hipotese"])
        assert args.model == EXPECTED_LAYA_ID, (
            f"noul --model deveria ser '{EXPECTED_LAYA_ID}', mas é '{args.model}'"
        )

    def test_score_default_model_is_laya(self):
        args = _parse_real(["score", "artefato", "-r", "rubrica"])
        assert args.model == EXPECTED_LAYA_ID, (
            f"score --model deveria ser '{EXPECTED_LAYA_ID}', mas é '{args.model}'"
        )

    def test_no_subcommand_uses_legacy_deberta(self):
        """Nenhum subcomando real pode apontar para o modelo DeBERTa legado."""
        parses = [
            ["choice", "x", "-o", "a,b"],
            ["noul", "x", "-y", "h"],
            ["score", "x", "-r", "r"],
        ]
        for argv in parses:
            args = _parse_real(argv)
            assert args.model != LEGACY_DEBERTA_ID, (
                f"Subcomando '{argv[0]}' ainda aponta para o modelo DeBERTa legado!"
            )

    def test_choice_default_temperature_is_1(self):
        args = _parse_real(["choice", "x", "-o", "a,b"])
        assert args.temperature == 1.0


class TestCliThresholdDefaults:
    """Defaults de corte calibrados para o Laya (0.78 / 3.40) no parser real."""

    def test_noul_default_threshold_recalibrado(self):
        args = _parse_real(["noul", "x", "-y", "h"])
        assert args.threshold == 0.78, (
            f"noul --threshold deveria ser 0.78 (Laya), mas é {args.threshold}"
        )

    def test_score_default_threshold_recalibrado(self):
        args = _parse_real(["score", "x", "-r", "r"])
        assert args.threshold == 3.40, (
            f"score --threshold deveria ser 3.40 (Laya), mas é {args.threshold}"
        )

    def test_score_default_scale_1_5(self):
        args = _parse_real(["score", "x", "-r", "r"])
        assert args.min_score == 1.0
        assert args.max_score == 5.0


class TestCliScoreCatalog:
    """score no CLI resolve rubrica do catálogo fechado (rubric livre deprecado)."""

    def test_score_parseia_sem_rubric_obrigatoria(self):
        args = _parse_real(["score", "x"])
        assert args.rubric is None

    def test_score_task_type_default_codigo_producao(self):
        args = _parse_real(["score", "x"])
        assert args.task_type == "codigo_producao"

    def test_score_task_type_aceita_chaves_do_catalogo(self):
        from src.core.schemas import RUBRIC_CATALOG
        for key in RUBRIC_CATALOG:
            args = _parse_real(["score", "x", "--task-type", key])
            assert args.task_type == key


# ---------------------------------------------------------------------------
# Importabilidade
# ---------------------------------------------------------------------------

def test_cli_module_importable():
    """src/cli.py deve importar sem erros e expor main/get_engine/build_parser."""
    import importlib

    mod = importlib.import_module("src.cli")
    assert hasattr(mod, "main"), "src.cli deve expor função 'main'"
    assert hasattr(mod, "get_engine"), "src.cli deve expor função 'get_engine'"
    assert hasattr(mod, "build_parser"), "src.cli deve expor função 'build_parser'"
