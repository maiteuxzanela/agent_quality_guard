"""Experimento borderline: A/B de rúbrica com âncora + língua + banda.

Rodada única (sem mocks, engine Laya real):
  Passo 0  — gate de tokens em head_max_len=192 (tokenizer + build_sequence)
  Passo 1–2 — braços A (catálogo), B (A+âncoras EN), B2 (labels descritivos)
  3.1      — golden set (12 pares) sob A: taxa de borderline th=3.40
  3.2      — A/B língua premissa PT vs EN (8 casos pos/neg)
  3.3      — varredura offline borderline_band 0.35→0.50 (breakdowns + golden)

Saída: benchmarks/borderline_ab_report.json
"""

from __future__ import annotations

import asyncio
import glob
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.core.schemas import RUBRIC_CATALOG, ScoreRequest  # noqa: E402
from src.decision.laya_adapter import compute_score_uncertainty  # noqa: E402

THRESHOLD = 3.40
HEAD_MAX_LEN = 192
MAX_LEN = 512
OUT_PATH = ROOT / "benchmarks" / "borderline_ab_report.json"

# ─── Braços (Passo 1 — imutáveis nesta rodada) ────────────────────────────────

LABELS_DRY = [
    "critical flaws",
    "poor quality",
    "acceptable",
    "good quality",
    "exceptional",
]

LABELS_B2 = [
    "critical flaws: fails basic requirements, critical errors",
    "poor: partial requirements, significant quality issues",
    "acceptable: core requirements met, minor imperfections",
    "good: high quality, robust, best practices",
    "exceptional: comprehensive, strictly exceeds rubric",
]

ANCHOR_GLOSSARY = (
    "\n\nScale anchors (1.0-5.0): "
    "1 critical flaws fails basic requirements with critical errors; "
    "2 poor partially meets requirements with significant quality issues; "
    "3 acceptable meets core requirements with minor imperfections; "
    "4 good high quality, robust, follows best practices; "
    "5 exceptional comprehensive and strictly exceeds the rubric."
)

ARM_A = RUBRIC_CATALOG["entrega"]
ARM_B = ARM_A + ANCHOR_GLOSSARY
ARM_B2 = ARM_A  # rubrica idêntica; muda só criteria_labels

ARMS = {
    "A": {"rubric": ARM_A, "criteria": LABELS_DRY},
    "B": {"rubric": ARM_B, "criteria": LABELS_DRY},
    "B2": {"rubric": ARM_B2, "criteria": LABELS_B2},
}

# ─── Golden set (importa dos testes — mesma fonte, sem duplicar lógica) ──────

from tests.test_score_calibration import (  # noqa: E402
    NEGATIVE_CASES,
    ORDER_PAIRS,
    POSITIVE_CASES,
    STABILITY_PAIRS,
)

# Premissas EN fixas (tradução literal dos 8 pos/neg — Passo 3.2)
EN_CASES = {
    "funcao_add_com_teste_real": (
        "Prompt Request: implement add(a, b). | Executed Result: "
        "created src/add.py with def add(a: int, b: int) -> int: return a + b; "
        "tests/test_add.py with assert add(1, 2) == 3, assert add(-1, 1) == 0, "
        "assert add(0, 0) == 0 and assert add(2, 1) == 3 (commutativity); "
        "error path covered: pytest.raises(TypeError) for add('1', 2); "
        "pytest 5/5 passed; mypy src/add.py clean (typed signature verified); "
        "no mocks."
    ),
    "bugfix_off_by_one_com_regressao": (
        "Prompt Request: fix off-by-one bug that dropped the last slice element. | "
        "Executed Result: changed range(n) to range(n + 1) in src/slice_helper.py; "
        "regression tests tests/test_slice.py cover n=0, n=1, n=5 and n=100 with "
        "full slice equality (out == expected, includes last element) and concrete "
        "case out[-1] == xs[-1]; error path covered: negative n raises ValueError; "
        "pytest 4/4 passed; full suite 13/13 passed; "
        "2-file diff; no mocks; no new dependencies; helper docstring updated."
    ),
    "refatoracao_sem_duplicacao_com_testes_verdes": (
        "Prompt Request: remove duplication between two handlers. | Executed Result: "
        "extracted common function _validate in src/validators.py; old call sites removed; "
        "new unit tests tests/test_validators.py covering _validate "
        "(success, invalid input and boundaries) plus integration test asserting "
        "both handlers delegate to _validate; error path covered; "
        "existing suite kept green; "
        "pytest 16/16 passed (12 old + 4 new); no stub or mock introduced."
    ),
    "documentacao_e_tipagem_com_cobertura": (
        "Prompt Request: document payments API and type interfaces. | Executed Result: "
        "docstrings with examples added in src/payments.py; complete types with "
        "mypy clean; 4 contract tests passing; no hardcoded data."
    ),
    "mock_em_producao_com_retorno_fake": (
        "Prompt Request: deliver payment module. | Executed Result: "
        "src/pagamento.py uses unittest.mock.MagicMock() in production code; "
        "function cobrar() returns hardcoded 42 (synthetic fallback); no real test "
        "exercises the integration; network call replaced by fixed stub."
    ),
    "testes_tautologicos_assert_true": (
        "Prompt Request: deliver test suite. | Executed Result: "
        "tests/test_app.py contains only assert True; assert 1 == 1; assert 'a' in 'abc'; "
        "zero import of production module; zero error-path coverage; "
        "suites always pass without exercising real code."
    ),
    "excecoes_engolidas_sem_tratamento": (
        "Prompt Request: deliver database client. | Executed Result: "
        "src/db.py uses bare except: pass at every I/O point; connection failures become "
        "silence; no log and no retry; no failure test; connection never "
        "closed in finally."
    ),
    "injecao_sql_sem_sanitizacao": (
        "Prompt Request: deliver user search. | Executed Result: "
        "src/users.py builds query via f-string: f\"SELECT * FROM users WHERE id={user_id}\"; "
        "user input goes straight in without parameterization; no quote-rejection "
        "test; SQL injection vector exposed."
    ),
}


def _all_pt_cases():
    cases = []
    for name, p in POSITIVE_CASES:
        cases.append((name, "pos", p))
    for name, p in NEGATIVE_CASES:
        cases.append((name, "neg", p))
    for name, good, bad in ORDER_PAIRS:
        cases.append((name + "__good", "order_good", good))
        cases.append((name + "__bad", "order_bad", bad))
    return cases


# ─── Passo 0: gate de tokens ─────────────────────────────────────────────────

def token_gate() -> dict:
    from laya.common import build_sequence, render_options

    agent_tok = None
    cfg = {"head_max_len": HEAD_MAX_LEN, "max_len": MAX_LEN}
    try:
        from laya import Agent
        from src.decision.laya_adapter import LAYA_CHECKPOINTS

        c = LAYA_CHECKPOINTS["english"]
        agent = Agent(c["repo"], device="cpu", subfolder=c["subfolder"])
        agent_tok = agent.tok
        cfg = {
            "head_max_len": agent.cfg.get("head_max_len", HEAD_MAX_LEN),
            "max_len": agent.cfg.get("max_len", MAX_LEN),
        }
    except Exception as e:
        return {"error": f"tokenizer load failed: {e}", "passed": False}

    sample_premise = POSITIVE_CASES[0][1]
    results = {}
    for arm, spec in ARMS.items():
        q = {"t": "score", "ins": spec["rubric"], "crit": spec["criteria"]}
        opts = render_options(q)
        try:
            seq, markers = build_sequence(
                agent_tok,
                {"delivery": sample_premise},
                q,
                max_len=cfg["max_len"],
                head_max_len=cfg["head_max_len"],
            )
            ins_ids = agent_tok(
                "score question: %s" % q["ins"],
                add_special_tokens=False,
            )["input_ids"]
            opt_tokens = []
            for o in opts:
                opt_tokens.append(
                    1
                    + len(
                        agent_tok(
                            " " + o.replace(agent_tok.mask_token, " "),
                            add_special_tokens=False,
                        )["input_ids"][:48]
                    )
                )
            opt_budget = cfg["head_max_len"] - sum(opt_tokens)
            results[arm] = {
                "n_options": len(opts),
                "options_tokens": opt_tokens,
                "opt_budget_for_instructions": opt_budget,
                "instructions_full_tokens": len(ins_ids),
                "instructions_fit": len(ins_ids) <= max(8, opt_budget),
                "markers_ok": len(markers) == len(opts),
                "seq_len": len(seq),
                "passed": len(markers) == len(opts) and opt_budget >= 16,
                "ins_truncated": len(ins_ids) > max(8, opt_budget),
            }
        except Exception as e:
            results[arm] = {"error": str(e), "passed": False}
    return {
        "head_max_len": cfg["head_max_len"],
        "max_len": cfg["max_len"],
        "arms": results,
        "all_passed": all(r.get("passed") for r in results.values()),
    }


# ─── Inferência por braço (Passo 2) ──────────────────────────────────────────

def _ensure_engine():
    import src.mcp_server as mcp
    if mcp._engine_instance is None:
        from src.decision.engine import OpenJevEngine
        eng = OpenJevEngine(model_id_or_path="convaiinnovations/laya", allow_cpu_fallback=True)
        eng.load_model()
        mcp._engine_instance = eng
    return mcp._engine_instance


async def score_arm(engine, arm: str, premise: str, threshold: float = THRESHOLD) -> dict:
    spec = ARMS[arm]
    adapter = engine.laya_adapter
    agent = adapter._agent
    question = {
        "quality": {
            "type": "score",
            "instructions": spec["rubric"],
            "criteria": spec["criteria"],
        }
    }
    res = await asyncio.to_thread(
        lambda: agent.predict({"delivery": premise}, question)
    )
    ans = res["answers"]["quality"]
    raw_ordinal = float(ans["score"])
    score = round(1.0 + raw_ordinal, 2)
    probs_list = [float(p) for p in ans.get("probabilities", {}).values()]
    # labels canônicos p/ breakdown (ordem é a mesma do ordinal 0–4)
    keys = ["critical flaws", "poor quality", "acceptable", "good quality", "exceptional"]
    breakdown = {k: round(p, 4) for k, p in zip(keys, probs_list)}
    p_at, p_below, margin, borderline = compute_score_uncertainty(
        dict(enumerate(probs_list)), threshold
    )
    return {
        "score": score,
        "passed": score >= threshold,
        "breakdown": breakdown,
        "p_at": p_at,
        "p_below": p_below,
        "score_margin": margin,
        "borderline": borderline,
        "acceptable_mass": breakdown["acceptable"],
        "confidence": ans.get("confidence"),
    }


async def run_ab(engine) -> dict:
    out = {}
    for name, kind, premise in _all_pt_cases():
        out[name] = {"kind": kind, "premise_len": len(premise), "arms": {}}
        for arm in ARMS:
            out[name]["arms"][arm] = await score_arm(engine, arm, premise)
    return out


async def run_lang_ab(engine) -> dict:
    """3.2: PT vs EN nos 8 pos/neg, braços A e B."""
    out = {}
    pt_map = dict(POSITIVE_CASES + NEGATIVE_CASES)
    for name, en_premise in EN_CASES.items():
        pt_premise = pt_map[name]
        out[name] = {"arms": {}}
        for arm in ("A", "B"):
            r_pt = await score_arm(engine, arm, pt_premise)
            r_en = await score_arm(engine, arm, en_premise)
            out[name]["arms"][arm] = {
                "pt": r_pt,
                "en": r_en,
                "delta_acceptable_en_minus_pt": round(
                    r_en["acceptable_mass"] - r_pt["acceptable_mass"], 4
                ),
                "delta_borderline": r_en["borderline"] != r_pt["borderline"],
            }
    return out


# ─── 3.1 — métricas golden sob cada braço ────────────────────────────────────

def golden_metrics(ab_results: dict) -> dict:
    metrics = {}
    for arm in ARMS:
        pos_scores, neg_scores = [], []
        borderline_n, total = 0, 0
        acceptable_acc, p_below_acc = [], []
        order_ok = 0
        order_total = 0
        stability_max = 0.0
        false_neg, false_pos = [], []
        for name, data in ab_results.items():
            r = data["arms"][arm]
            kind = data["kind"]
            total += 1
            if r["borderline"]:
                borderline_n += 1
            acceptable_acc.append(r["acceptable_mass"])
            p_below_acc.append(r["p_below"])
            if kind == "pos":
                pos_scores.append(r["score"])
                if r["score"] < THRESHOLD:
                    false_neg.append(name)
            elif kind == "neg":
                neg_scores.append(r["score"])
                if r["score"] >= THRESHOLD:
                    false_pos.append(name)
        for name, good, bad in ORDER_PAIRS:
            key_g, key_b = name + "__good", name + "__bad"
            if key_g in ab_results and key_b in ab_results:
                order_total += 1
                if ab_results[key_g]["arms"][arm]["score"] > ab_results[key_b]["arms"][arm]["score"]:
                    order_ok += 1
        metrics[arm] = {
            "borderline_rate": round(borderline_n / total, 4) if total else None,
            "borderline_n": borderline_n,
            "total_cases": total,
            "acceptable_mean": round(sum(acceptable_acc) / len(acceptable_acc), 4)
            if acceptable_acc
            else None,
            "p_below_mean": round(sum(p_below_acc) / len(p_below_acc), 4)
            if p_below_acc
            else None,
            "pos_mean": round(sum(pos_scores) / len(pos_scores), 4) if pos_scores else None,
            "neg_mean": round(sum(neg_scores) / len(neg_scores), 4) if neg_scores else None,
            "false_negatives": false_neg,
            "false_positives": false_pos,
            "order_ok": f"{order_ok}/{order_total}",
        }
    return metrics


def lang_metrics(lang_results: dict) -> dict:
    out = {}
    for arm in ("A", "B"):
        d_en, d_pts = [], []
        bl_diff = 0
        for name, data in lang_results.items():
            pair = data["arms"][arm]
            d_en.append(pair["delta_acceptable_en_minus_pt"])
            d_pts.append(abs(pair["pt"]["score"] - pair["en"]["score"]))
            if pair["delta_borderline"]:
                bl_diff += 1
        out[arm] = {
            "mean_delta_acceptable_en_minus_pt": round(sum(d_en) / len(d_en), 4),
            "mean_abs_score_delta": round(sum(d_pts) / len(d_pts), 4),
            "borderline_flips": bl_diff,
            "n": len(d_en),
        }
    return out


# ─── 3.3 — banda offline ─────────────────────────────────────────────────────

def _extract_breakdowns() -> list:
    found = []

    def walk(o, source):
        if isinstance(o, dict):
            bd = None
            if isinstance(o.get("score_breakdown"), dict):
                bd = o["score_breakdown"]
            elif isinstance(o.get("breakdown"), dict) and "acceptable" in o["breakdown"]:
                bd = o["breakdown"]
            if bd is not None and set(
                ["critical flaws", "poor quality", "acceptable", "good quality", "exceptional"]
            ) <= set(bd):
                ordinal_probs = [
                    bd["critical flaws"],
                    bd["poor quality"],
                    bd["acceptable"],
                    bd["good quality"],
                    bd["exceptional"],
                ]
                found.append((source, ordinal_probs))
            for v in o.values():
                walk(v, source)
        elif isinstance(o, list):
            for v in o:
                walk(v, source)

    for f in sorted(glob.glob(str(ROOT / "**" / "*.json"), recursive=True)):
        if ".venv" in f or "node_modules" in f or "borderline_ab_report" in f:
            continue
        try:
            walk(json.load(open(f)), str(Path(f).relative_to(ROOT)))
        except Exception:
            continue
    return found


def band_sweep(extra_golden_probs: list = None) -> dict:
    """3.3: taxa de borderline por banda em [0.35, 0.50]."""
    hist = [(s, p) for s, p in _extract_breakdowns()]
    golden_norm = [
        (f"golden[{i}]", list(p)) for i, p in enumerate(extra_golden_probs or [])
    ]
    datasets = {"historical": hist, "golden_ab": golden_norm}
    bands = [0.35, 0.375, 0.40, 0.425, 0.45, 0.475, 0.50]
    report = {}
    for ds_name, items in datasets.items():
        if not items:
            report[ds_name] = {"n": 0}
            continue
        rows = []
        for band in bands:
            n_bl = 0
            for item in items:
                probs = item[-1]
                _, _, _, bl = compute_score_uncertainty(
                    dict(enumerate(probs)), THRESHOLD, borderline_band=band
                )
                if bl:
                    n_bl += 1
            rows.append(
                {
                    "band": band,
                    "borderline_n": n_bl,
                    "borderline_rate": round(n_bl / len(items), 4),
                }
            )
        report[ds_name] = {"n": len(items), "by_band": rows}
    return report


def main():
    t0 = time.time()
    print("=== Passo 0: token gate ===")
    gate = token_gate()
    print(json.dumps(gate, indent=2, ensure_ascii=False))
    if not gate.get("all_passed"):
        print("GATE FALHOU — encerrando sem inferência (âncora precisa encurtar).")
        report = {
            "meta": {"threshold": THRESHOLD, "ts": time.time()},
            "step0_token_gate": gate,
            "status": "gate_failed",
        }
        OUT_PATH.write_text(json.dumps(report, indent=2, ensure_ascii=False))
        print(f"Wrote {OUT_PATH}")
        sys.exit(2)

    print("=== Carregando engine Laya real ===")
    engine = _ensure_engine()

    async def _run_all():
        print("=== Passos 1–2: A/B/B2 no golden ===")
        ab = await run_ab(engine)
        golden_m = golden_metrics(ab)
        print(json.dumps(golden_m, indent=2))
        print("=== 3.1 borderline golden ===")
        print(json.dumps({a: golden_m[a]["borderline_rate"] for a in ARMS}, indent=2))
        print("=== 3.2 língua PT/EN ===")
        lang = await run_lang_ab(engine)
        lang_m = lang_metrics(lang)
        print(json.dumps(lang_m, indent=2))
        return ab, golden_m, lang, lang_m

    ab, golden_m, lang, lang_m = asyncio.run(_run_all())

    # golden probs p/ banda (sob A, e também B/B2)
    golden_probs = []
    for name, data in ab.items():
        for arm in ARMS:
            bd = data["arms"][arm]["breakdown"]
            golden_probs.append(
                [bd["critical flaws"], bd["poor quality"], bd["acceptable"], bd["good quality"], bd["exceptional"]]
            )

    print("=== 3.3 banda offline ===")
    bands = band_sweep(golden_probs)
    print(json.dumps(bands, indent=2))

    report = {
        "meta": {
            "threshold": THRESHOLD,
            "head_max_len": gate.get("head_max_len"),
            "arms": {
                k: {
                    "rubric_chars": len(v["rubric"]),
                    "rubric_tail": v["rubric"][-80:],
                    "criteria": v["criteria"],
                }
                for k, v in ARMS.items()
            },
            "elapsed_s": round(time.time() - t0, 2),
            "ts": time.time(),
        },
        "step0_token_gate": gate,
        "step2_case_results": ab,
        "step31_golden_metrics": golden_m,
        "step32_lang_ab": {"per_case": lang, "metrics": lang_m},
        "step33_band_sweep": bands,
        "status": "ok",
    }
    OUT_PATH.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"OK em {report['meta']['elapsed_s']}s → {OUT_PATH}")


if __name__ == "__main__":
    main()
