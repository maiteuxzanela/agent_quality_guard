"""Testes do Blender Visual Loop (ciclo render → CLIP → noul encadeado).

SEM mocks (AGENTS.md §5.2): a lógica de decisão vive em funções PURAS
(nearest_color_name, luminance_bucket, validate_spec, build_clip_pairs,
build_criteria_chain, count_consecutive_rejections) testadas com dados fixos;
o contrato ponta-a-ponta usa Blender REAL, Laya REAL (IPC/pesos do disco) e
CLIP REAL — sem stubs, sem unittest.mock.

Fixtures de cena: /home/maiteuxzanela/deepseek harness/tests/fixtures/.
"""

import importlib.util
import json
from pathlib import Path

import pytest

TOOL_PATH = Path(
    "/home/maiteuxzanela/deepseek harness/tools/blender_visual_loop.py"
).resolve()
FIXTURES = Path(
    "/home/maiteuxzanela/deepseek harness/tests/fixtures"
).resolve()

_spec = importlib.util.spec_from_file_location("blender_visual_loop", TOOL_PATH)
bvl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bvl)


# ─── Funções puras ───────────────────────────────────────────────────────────

class TestFuncoesPuras:
    def test_nearest_color_name(self):
        assert bvl.nearest_color_name([0.9, 0.05, 0.05]) == "red"
        assert bvl.nearest_color_name([0.05, 0.15, 0.9]) == "blue"
        assert bvl.nearest_color_name([0.9, 0.9, 0.95]) == "white"

    def test_luminance_bucket(self):
        assert bvl.luminance_bucket(20.0) == "extremely low"
        assert bvl.luminance_bucket(100.0) == "moderate"
        assert bvl.luminance_bucket(200.0) == "strong"
        assert bvl.luminance_bucket(60.0) == "moderate"
        assert bvl.luminance_bucket(139.9) == "moderate"
        assert bvl.luminance_bucket(140.0) == "strong"

    def test_validate_spec_valida(self):
        assert bvl.validate_spec(
            {"dimensions": [2, 1, 0.5], "color": "red", "parts": [{"name": "Perna"}]}
        ) == []

    def test_validate_spec_dimensoes_invalidas(self):
        erros = bvl.validate_spec({"dimensions": [2, 1]})
        assert any("dimensions" in e for e in erros)

    def test_validate_spec_parte_sem_nome(self):
        erros = bvl.validate_spec({"parts": [{"dimensions": [1, 1, 1]}]})
        assert any("name" in e for e in erros)

    def test_build_clip_pairs_composition_opt_in(self):
        sem_comp = bvl.build_clip_pairs({"color": "red", "texture": "checker"})
        assert "framing" not in sem_comp
        assert "color" in sem_comp and "texture" in sem_comp and "bright" in sem_comp
        com_comp = bvl.build_clip_pairs({"composition": True})
        assert "framing" in com_comp

    def test_count_consecutive_rejections(self):
        assert bvl.count_consecutive_rejections([]) == 0
        rej = [{"status": "rejected"}] * 3
        assert bvl.count_consecutive_rejections(rej) == 3
        misto = [
            {"status": "rejected"},
            {"status": "approved"},
            {"status": "rejected"},
            {"status": "rejected"},
        ]
        assert bvl.count_consecutive_rejections(misto) == 2
        erro_no_fim = [{"status": "rejected"}, {"status": "error"}]
        assert bvl.count_consecutive_rejections(erro_no_fim) == 0

    def test_count_consecutive_rejections_historico_invalido_levanta_type_error(self):
        with pytest.raises(TypeError) as excinfo:
            bvl.count_consecutive_rejections(None)  # type: ignore[arg-type]
        assert "NoneType" in str(excinfo.value)

    def test_nearest_color_name_rgb_vazio_levanta_value_error(self):
        with pytest.raises(ValueError) as excinfo:
            bvl.nearest_color_name([])
        assert "unpack" in str(excinfo.value)

    def test_load_history_json_corrompido_levanta_json_decode_error(self, tmp_path):
        (tmp_path / "history.json").write_text('{"iteracao": ', encoding="utf-8")
        with pytest.raises(json.JSONDecodeError) as excinfo:
            bvl.load_history(tmp_path)
        assert excinfo.value.doc is not None

    def test_load_history_diretorio_inexistente_retorna_vazio(self, tmp_path):
        assert bvl.load_history(tmp_path / "nao_existe") == []


# ─── Geração do script combinado ─────────────────────────────────────────────

class TestScriptGerado:
    def test_tokens_substituidos_e_python_valido(self, tmp_path):
        script = 'import bpy\nbpy.ops.wm.read_factory_settings(use_empty=True)\n'
        runnable = bvl.build_runnable_script(
            script, tmp_path / "facts.json", {"main_object": "Cubo"}
        )
        for token in (
            "__FACTS_PATH_REPR__",
            "__SAMPLES__",
            "__RES_X__",
            "__RES_Y__",
            "__VIEWS_JSON__",
            "__MAIN_OBJECT_REPR__",
            "__COMPOSITION__",
        ):
            assert token not in runnable, f"token {token} não substituído"
        compile(runnable, "runnable.py", "exec")

    def test_composition_false_gera_flag(self, tmp_path):
        runnable = bvl.build_runnable_script(
            "x = 1\n", tmp_path / "f.json", {"composition": False}
        )
        assert "_prefer_script_cam = False" in runnable
        compile(runnable, "runnable.py", "exec")


# ─── Cadeia de critérios ─────────────────────────────────────────────────────

def _facts_base() -> dict:
    return {
        "objects": [
            {
                "name": "CuboPedido",
                "type": "MESH",
                "location": [0.0, 0.0, 0.25],
                "dimensions": [2.0, 1.0, 0.5],
                "materials": [
                    {
                        "name": "M",
                        "nodes": ["BSDF_PRINCIPLED", "TEX_CHECKER"],
                        "checker_color1": [0.9, 0.05, 0.05],
                    }
                ],
            },
            {
                "name": "Perna",
                "type": "MESH",
                "location": [0.8, 0.0, 0.75],
                "dimensions": [0.2, 0.2, 1.0],
                "materials": [{"name": "P", "nodes": ["BSDF_PRINCIPLED"]}],
            },
        ],
        "lights": [{"name": "Sol", "type": "SUN", "energy": 4.0, "location": [5, -5, 8]}],
        "main_object": "CuboPedido",
        "has_script_camera": False,
        "coverage": {"CuboPedido": 0.08},
        "luminance": {"perspectiva": 165.0},
        "views": [],
    }


def _spec_full() -> dict:
    return {
        "main_object": "CuboPedido",
        "dimensions": [2.0, 1.0, 0.5],
        "on_floor": True,
        "color": "red",
        "texture": "checker",
        "lighting": "bright",
        "parts": [{"name": "Perna", "dimensions": [0.2, 0.2, 1.0], "position": [0.8, 0.0, 0.5]}],
    }


def _evidence() -> dict:
    return {"color": 0.95, "texture": 0.95, "bright": 0.9, "framing": 0.9}


class TestCadeiaDeCriterios:
    def test_ordem_da_cadeia_calibrada(self, tmp_path):
        chain, skipped = bvl.build_criteria_chain(
            _spec_full(), _facts_base(), _evidence(), tmp_path
        )
        ids = [c.id for c in chain]
        assert ids == [
            "proporcoes_objeto",
            "posicao_objeto",
            "proporcoes_partes:Perna",
            "posicao_partes:Perna",
            "cores",
            "luzes",
            "texturas",
        ]
        assert skipped == []

    def test_parte_ausente_e_pulada(self, tmp_path):
        spec = _spec_full()
        chain, skipped = bvl.build_criteria_chain(
            spec, _facts_base(), _evidence(), tmp_path
        )
        spec["parts"] = [{"name": "Inexistente", "dimensions": [1, 1, 1]}]
        chain2, skipped2 = bvl.build_criteria_chain(
            spec, _facts_base(), _evidence(), tmp_path
        )
        assert not any("Inexistente" in c.id for c in chain2)
        assert any("Inexistente" in s["criterion"] for s in skipped2)

    def test_composicao_opt_in_sem_camera_do_script(self, tmp_path):
        spec = dict(_spec_full(), composition=True)
        chain, skipped = bvl.build_criteria_chain(
            spec, _facts_base(), _evidence(), tmp_path
        )
        assert not any(c.id == "composicao" for c in chain)
        assert any(s["criterion"] == "composicao" for s in skipped)

    def test_composicao_opt_in_com_camera_do_script(self, tmp_path):
        spec = dict(_spec_full(), composition=True)
        facts = _facts_base()
        facts["has_script_camera"] = True
        chain, skipped = bvl.build_criteria_chain(spec, facts, _evidence(), tmp_path)
        assert any(c.id == "composicao" for c in chain)
        assert skipped == []

    def test_composicao_pulada_quando_falta_evidencia_clip(self, tmp_path):
        spec = dict(_spec_full(), composition=True)
        facts = _facts_base()
        facts["has_script_camera"] = True
        sem_framing = {"color": 0.95, "texture": 0.95, "bright": 0.9}
        chain, skipped = bvl.build_criteria_chain(spec, facts, sem_framing, tmp_path)
        assert not any(c.id == "composicao" for c in chain)
        assert any(
            s["criterion"] == "composicao" and "evid" in s["reason"] for s in skipped
        )

    def test_sem_spec_de_cor_critério_pulado_ou_ausente(self, tmp_path):
        spec = _spec_full()
        del spec["color"]
        chain, _ = bvl.build_criteria_chain(spec, _facts_base(), _evidence(), tmp_path)
        assert not any(c.id == "cores" for c in chain)


# ─── Integração real (Blender + Laya + CLIP — sem mocks) ────────────────────

@pytest.fixture(scope="module")
def engine():
    from src.decision.engine import OpenJevEngine

    eng = OpenJevEngine()
    eng.load_model()
    return eng


@pytest.fixture(scope="module")
def clip():
    return bvl.ClipEvidence()


@pytest.fixture(scope="module")
def spec() -> dict:
    return json.loads((FIXTURES / "spec.json").read_text(encoding="utf-8"))


async def _evaluate(pedido, spec, script_name, session_dir, engine, clip):
    return await bvl.evaluate_iteration(
        pedido=pedido,
        spec=spec,
        script_text=(FIXTURES / script_name).read_text(encoding="utf-8"),
        session_dir=session_dir,
        engine=engine,
        clip=clip,
    )


@pytest.mark.skip(
    reason=(
        "TICKET-HARNESS-47: o ciclo visual de integração usa o motor Laya RLCD "
        "(pesos/GPU), desativado; os testes de funções puras permanecem ativos."
    )
)
class TestIntegracaoReal:
    @pytest.mark.asyncio
    async def test_cenario_correto_e_aprovado(self, tmp_path, engine, clip, spec):
        report = await _evaluate(
            "mesa xadrez", spec, "script_good.py", tmp_path / "ok", engine, clip
        )
        assert report["status"] == "approved"
        assert report["first_failure"] is None
        assert len(report["evaluated"]) == 7
        assert all(e["passed"] for e in report["evaluated"])
        assert report["threshold"] == 0.78
        assert all(v["exists"] for v in report["views"])

    @pytest.mark.asyncio
    async def test_defeito_reprova_no_criterio_correto_fail_fast(
        self, tmp_path, engine, clip, spec
    ):
        report = await _evaluate(
            "mesa xadrez", spec, "script_bad.py", tmp_path / "bad", engine, clip
        )
        assert report["status"] == "rejected"
        assert report["first_failure"]["criterion"] == "proporcoes_objeto"
        assert report["first_failure"]["probability"] < 0.78
        assert report["first_failure"]["rewrite_instruction"]
        # fail-fast: parou no primeiro critério reprovado
        assert len(report["evaluated"]) == 1

    @pytest.mark.asyncio
    async def test_script_com_excecao_vira_status_error(
        self, tmp_path, engine, clip, spec
    ):
        report = await _evaluate(
            "cena quebrada", spec, "script_error.py", tmp_path / "err", engine, clip
        )
        assert report["status"] == "error"
        assert report["stage"] == "blender_execution"

    @pytest.mark.asyncio
    async def test_sessao_reusada_com_script_falho_nao_aprova_com_facts_velhos(
        self, tmp_path, engine, clip, spec
    ):
        """Sessão reusada: facts.json/PNGs da tentativa anterior nunca aprovam a atual."""
        session = tmp_path / "reusada"
        ok = await _evaluate(
            "mesa xadrez", spec, "script_good.py", session, engine, clip
        )
        assert ok["status"] == "approved"
        assert (session / "facts.json").exists()

        err = await _evaluate(
            "cena quebrada", spec, "script_error.py", session, engine, clip
        )
        assert err["status"] == "error"
        assert err["stage"] == "blender_execution"
        assert err["attempt"] == 2
        # a reprovação/erro da tentativa 2 entrou no histórico da sessão
        history = json.loads((session / "history.json").read_text(encoding="utf-8"))
        assert history[-1] == {"attempt": 2, "status": "error"}

    @pytest.mark.asyncio
    async def test_cinco_rejeicoes_consecutivas_destravam_saida_de_emergencia(
        self, tmp_path, engine, clip, spec
    ):
        session = tmp_path / "breaker"
        session.mkdir()
        # Estado real de histórico: 4 reprovações consecutivas anteriores.
        (session / "history.json").write_text(
            json.dumps([{"attempt": i, "status": "rejected"} for i in range(1, 5)]),
            encoding="utf-8",
        )
        report = await _evaluate(
            "mesa xadrez", spec, "script_bad.py", session, engine, clip
        )
        assert report["status"] == "emergency_release"
        assert report["attempt"] == 5
        assert report["consecutive_rejections"] == 5
        assert "SAÍDA DE EMERGÊNCIA" in report["message"]
