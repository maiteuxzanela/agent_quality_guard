"""Fase 1 — Probe isolado: threshold de score 3.60 + aprovação secundária.

Não altera defaults do motor (LAYA_SCORE_THRESHOLD_DEFAULT / MCP continuam 3.40).
Apenas invoca openjev_score_and_diagnose(..., threshold=3.60) nos 8 pares
pos/neg do golden set (tests/test_score_calibration) com git real e engine Laya.

    Critério de sucesso (usuária):
      - Todo POSITIVO aprova: primário (score >= 3.60) OU secundário
        (borderline=true AND diagnosis.selected ∈ NULL_DEFECT_OPTIONS
         AND confidence >= NULL_CHOICE_MIN_CONFIDENCE=0.40).
      - Todo NEGATIVO reprova: (não-borderline) OU (borderline AND choice
        com defeito concreto) OU (borderline AND nula com conf < 0.40).
        FP secundário (neg aprovado como borderline nula forte) zera o
        experimento.

Saída: benchmarks/threshold_360_secondary_probe.json
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.test_score_calibration import (  # noqa: E402
    NEGATIVE_CASES,
    POSITIVE_CASES,
)

THRESHOLD = 3.60
OUT_PATH = ROOT / "benchmarks" / "threshold_360_secondary_probe.json"


def _ensure_engine_loaded():
    import src.mcp_server as _mcp_module

    if _mcp_module._engine_instance is None:
        from src.decision.engine import OpenJevEngine

        engine = OpenJevEngine(
            model_id_or_path="convaiinnovations/laya",
            allow_cpu_fallback=True,
        )
        engine.load_model()
        _mcp_module._engine_instance = engine
    return _mcp_module._engine_instance


def _make_git_repo() -> str:
    tmp = tempfile.mkdtemp(prefix="jev_probe360_")
    subprocess.run(["git", "init", tmp], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", tmp, "config", "user.email", "probe@jev.local"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", tmp, "config", "user.name", "JEV Probe"],
        check=True,
        capture_output=True,
    )
    (Path(tmp) / "README.md").write_text("# threshold 3.60 secondary probe\n")
    subprocess.run(["git", "-C", tmp, "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", tmp, "commit", "-m", "init"],
        check=True,
        capture_output=True,
    )
    return tmp


async def _eval_case(ws: str, premise: str) -> dict:
    from src.mcp_server import NULL_DEFECT_OPTIONS, _score_history, openjev_score_and_diagnose

    _score_history.clear()
    result = await openjev_score_and_diagnose(
        premise=premise,
        workspace=ws,
        threshold=THRESHOLD,
        task_type="entrega",
    )
    if "error" in result and result.get("premise_rejected"):
        raise RuntimeError(f"premise rejected: {result['error']}")

    diagnosis = result.get("diagnosis") or {}
    selected = diagnosis.get("selected")
    return {
        "score": result.get("score"),
        "passed": result.get("passed"),
        "threshold": result.get("threshold", THRESHOLD),
        "borderline": result.get("borderline"),
        "score_margin": result.get("score_margin"),
        "approved_as_borderline": result.get("approved_as_borderline", False),
        "diagnosis_selected": selected,
        "diagnosis_confidence": diagnosis.get("confidence"),
        "is_null_choice": selected in NULL_DEFECT_OPTIONS if selected else False,
        "attempts": result.get("attempts"),
        "rubric_source": result.get("rubric_source"),
    }


def _classify(kind: str, r: dict) -> dict:
    """Avalia critério da usuária para um caso."""
    score = r.get("score")
    passed = bool(r.get("passed"))
    borderline = bool(r.get("borderline"))
    is_null = bool(r.get("is_null_choice"))
    approved_bl = bool(r.get("approved_as_borderline"))

    primary_pass = score is not None and score >= THRESHOLD
    secondary_pass = (
        (not primary_pass)
        and borderline
        and is_null
        and approved_bl
        and passed
    )

    if kind == "pos":
        ok = passed and (primary_pass or secondary_pass)
        via = "primary" if primary_pass else ("secondary_null" if secondary_pass else "none")
        failure = None if ok else (
            f"FALSO NEGATIVO: score={score} borderline={borderline} "
            f"selected={r.get('diagnosis_selected')} passed={passed}"
        )
    else:
        # Negativo: precisa reprovar. Caminho aceito: não-borderline OU choice concreta.
        ok = not passed
        if ok:
            if not borderline:
                via = "reject_not_borderline"
            elif not is_null:
                via = "reject_concrete_choice"
            else:
                via = "reject_other"
        else:
            via = "FALSE_POSITIVE"
            failure = (
                f"FALSO POSITIVO SECUNDÁRIO: score={score} borderline={borderline} "
                f"selected={r.get('diagnosis_selected')} approved_as_borderline={approved_bl}"
            )
        if ok:
            failure = None

    return {
        "kind": kind,
        "ok": ok,
        "via": via,
        "failure": failure,
        **r,
    }


async def main() -> int:
    t0 = time.time()
    print("=== Carregando engine Laya real ===")
    _ensure_engine_loaded()
    ws = _make_git_repo()
    print(f"workspace git: {ws}")

    cases: list[tuple[str, str, str]] = []
    for name, premise in POSITIVE_CASES:
        cases.append(("pos", name, premise))
    for name, premise in NEGATIVE_CASES:
        cases.append(("neg", name, premise))

    results = []
    for kind, name, premise in cases:
        print(f"--- {kind.upper()} {name} @ threshold={THRESHOLD} ---")
        raw = await _eval_case(ws, premise)
        entry = _classify(kind, raw)
        entry["name"] = name
        results.append(entry)
        status = "OK" if entry["ok"] else "FAIL"
        print(
            f"  {status} score={entry['score']} passed={entry['passed']} "
            f"borderline={entry['borderline']} selected={entry['diagnosis_selected']} "
            f"via={entry['via']} margin={entry['score_margin']}"
        )

    pos = [r for r in results if r["kind"] == "pos"]
    neg = [r for r in results if r["kind"] == "neg"]
    pos_ok = [r for r in pos if r["ok"]]
    neg_ok = [r for r in neg if r["ok"]]
    false_neg = [r for r in pos if not r["ok"]]
    false_pos = [r for r in neg if not r["ok"]]

    primary_pos = [r for r in pos if r["via"] == "primary"]
    secondary_pos = [r for r in pos if r["via"] == "secondary_null"]
    primary_reject_neg = [
        r for r in neg if r.get("score") is not None and r["score"] >= THRESHOLD
    ]

    metrics = {
        "threshold": THRESHOLD,
        "n_pos": len(pos),
        "n_neg": len(neg),
        "pos_pass": len(pos_ok),
        "neg_reject": len(neg_ok),
        "false_negatives": [r["name"] for r in false_neg],
        "false_positives": [r["name"] for r in false_pos],
        "primary_pass_pos": [r["name"] for r in primary_pos],
        "secondary_null_pass_pos": [r["name"] for r in secondary_pos],
        "neg_score_ge_threshold": [r["name"] for r in primary_reject_neg],
        "neg_borderline_null_approved": [
            r["name"]
            for r in neg
            if r.get("approved_as_borderline") and r.get("is_null_choice")
        ],
        "success": len(false_neg) == 0 and len(false_pos) == 0,
        "elapsed_s": round(time.time() - t0, 2),
    }

    report = {
        "meta": {
            "experiment": "phase1_threshold_360_secondary",
            "threshold": THRESHOLD,
            "default_engine_threshold_unchanged": 3.40,
            "criterion": (
                "all positives pass (primary score>=3.60 OR secondary borderline+null "
                "choice with confidence>=0.40); all negatives fail (not borderline OR "
                "concrete choice OR null with confidence<0.40)"
            ),
            "ts": time.time(),
            "elapsed_s": metrics["elapsed_s"],
        },
        "metrics": metrics,
        "cases": results,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(f"Wrote {OUT_PATH}")
    print(json.dumps(metrics, indent=2, ensure_ascii=False))
    return 0 if metrics["success"] else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
