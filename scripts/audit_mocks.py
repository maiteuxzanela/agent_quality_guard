"""Auditoria de presença de mocks/stubs/fakes nos 4 projetos já auditados.

Mesmo molde dos audit_* antigos (Needle 3 Local + Open Jev + cascata
Choice → Noul → Score), com calibração vigente Laya:
  noul threshold = 0.78 | score threshold = 3.40

Diferencial: scrub determinístico léxico (file:line) de padrões de mock
(Python: unittest.mock/MagicMock/@patch/monkeypatch; JS/TS: jest.mock/
sinon/jest.fn/__mocks__; geral: Mock(/def mock/stub). Qualquer ocorrência
conta igual — sem distinguir teste de produção na severidade (metadata
'em_testes' vs 'em_producao' apenas informativa na saída).

Se score < 3.40, dispara choice único sobre DEFECT_OPTIONS (10 categorias
flat) — a categoria esperada aqui é mock_stub_em_producao
(folha: stub_ou_mock_em_producao).
"""

import asyncio
import json
import re
import sys
import time
from pathlib import Path
from typing import Dict, List, Tuple

# Adiciona raiz do projeto JEV
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.schemas import (
    ChoiceRequest,
    DEFECT_DESCRIPTIONS,
    DEFECT_MEMBERS,
    DEFECT_OPTIONS,
    DIAG_CHOICE_INSTRUCTIONS,
    Needle3Query,
    NoulRequest,
    RUBRIC_CATALOG,
    ScoreRequest,
)
from src.decision.engine import OpenJevEngine
from src.memory.local_needle import LocalNeedleEngine

# ─── Alvos (projetos já auditados anteriormente) ──────────────────────────────

TARGETS: List[Dict[str, str]] = [
    {"id": "faturamento", "name": "Faturamento", "path": "/mnt/94CCB337CCB3130A/Faturamento"},
    {"id": "jez_collections", "name": "JEZ Collections", "path": "/mnt/94CCB337CCB3130A/JEZ collections"},
    {"id": "predictera", "name": "Predictera", "path": "/mnt/94CCB337CCB3130A/predictera"},
    {"id": "panela_android", "name": "PanelaAndroid", "path": "/home/maiteuxzanela/PanelaBuild/PanelaAndroid"},
]

# ─── Scrub determinístico de padrões de mock ──────────────────────────────────

SKIP_DIRS = {
    ".git", "node_modules", ".venv", "venv", "dist", "build", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", "coverage", ".next",
    ".gradle", "Pods", ".idea", ".expo", ".cache", "site-packages",
}

CODE_EXTENSIONS = {".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}

MOCK_PATTERNS: List[Tuple[str, str]] = [
    ("python_mock_import", re.compile(r"unittest\.mock|from unittest import mock")),
    ("magic_mock", re.compile(r"\bMagicMock\b|\bAsyncMock\b")),
    ("patch_decorator", re.compile(r"@patch\b|mock\.patch\b")),
    ("monkeypatch", re.compile(r"\bmonkeypatch\b|\bmocker\b")),
    ("jest_mock", re.compile(r"jest\.mock\b|jest\.fn\b|jest\.spyOn\b")),
    ("sinon", re.compile(r"\bsinon\.(mock|stub|spy)\b|\bsinon\b")),
    ("mock_implementation", re.compile(r"mockImplementation|__mocks__")),
    ("def_mock", re.compile(r"\bdef mock_|\bdef mock\b")),
    ("mock_call", re.compile(r"\bMock\(|createMock\b|generateMock\b")),
    ("stub_marker", re.compile(r"\bstub_\w+|\w+_stub\b|\.stub\(")),
]


def _is_test_path(rel: str) -> bool:
    parts = rel.lower().split("/")
    name = parts[-1]
    if any(seg in ("test", "tests", "__tests__", "__mocks__", "spec", "specs") for seg in parts[:-1]):
        return True
    return name.startswith("test_") or name.endswith((".test.js", ".test.ts", ".test.tsx",
                                                        ".test.jsx", ".spec.js", ".spec.ts",
                                                        ".spec.jsx", ".spec.tsx"))


def scrub_mock_occurrences(root: str) -> List[Dict]:
    """Varre o alvo e retorna ocorrências de mock como {file, line, pattern, text, location}."""
    root_path = Path(root)
    occurrences: List[Dict] = []
    if not root_path.is_dir():
        return occurrences

    for p in sorted(root_path.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in CODE_EXTENSIONS:
            continue
        rel_parts = p.relative_to(root_path).parts
        if any(part in SKIP_DIRS for part in rel_parts):
            continue
        try:
            content = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        rel = p.relative_to(root_path).as_posix()
        location = "em_testes" if _is_test_path(rel) else "em_producao"
        # Diretório __mocks__/ é mock pela Estrutura (JS module mocks), mesmo sem
        # chamada explícita na linha — registra 1 ocorrência por arquivo usando
        # a PRIMEIRA LINHA REAL do arquivo (nunca texto fabricado/simulado).
        if "__mocks__" in rel_parts:
            first_line = ""
            for raw_line in content.splitlines():
                if raw_line.strip():
                    first_line = raw_line.strip()[:200]
                    break
            occurrences.append({
                "file": rel,
                "line": 1,
                "pattern": "mock_implementation",
                "text": first_line if first_line else "[arquivo vazio em __mocks__/]",
                "location": location,
            })
        for lineno, line in enumerate(content.splitlines(), 1):
            for pattern_id, regex in MOCK_PATTERNS:
                if regex.search(line):
                    occurrences.append({
                        "file": rel,
                        "line": lineno,
                        "pattern": pattern_id,
                        "text": line.strip()[:200],
                        "location": location,
                    })
                    break  # 1 ocorrência por linha (evita contagem inflada)
    return occurrences


def summarize_occurrences(occurrences: List[Dict]) -> Dict:
    by_pattern: Dict[str, int] = {}
    by_location: Dict[str, int] = {}
    files = set()
    for occ in occurrences:
        by_pattern[occ["pattern"]] = by_pattern.get(occ["pattern"], 0) + 1
        by_location[occ["location"]] = by_location.get(occ["location"], 0) + 1
        files.add(occ["file"])
    return {
        "total": len(occurrences),
        "files_affected": len(files),
        "by_pattern": dict(sorted(by_pattern.items(), key=lambda kv: -kv[1])),
        "by_location": dict(sorted(by_location.items(), key=lambda kv: -kv[1])),
    }


def scrub_summary_text(summary: Dict, occurrences: List[Dict], limit: int = 40) -> str:
    """Relatório textual compacto do scrub para injetar na premissa do Score."""
    if summary["total"] == 0:
        return "Deterministic mock scrub: ZERO occurrences found in code files."
    lines = [
        f"Deterministic mock scrub: {summary['total']} occurrences "
        f"across {summary['files_affected']} files.",
        f"By location (metadata only — severity is equal): {summary['by_location']}",
        f"By pattern: {summary['by_pattern']}",
        "Occurrences (file:line):",
    ]
    for occ in occurrences[:limit]:
        lines.append(f"  - {occ['file']}:{occ['line']} [{occ['pattern']}] {occ['text']}")
    if len(occurrences) > limit:
        lines.append(f"  ... +{len(occurrences) - limit} more occurrences")
    return "\n".join(lines)


# ─── Cascata JEV (Laya: noul 0.78 / score 3.40) ──────────────────────────────

NOUL_THRESHOLD = 0.78
SCORE_THRESHOLD = 3.40

CHOICE_OPTIONS = ["sem_mocks", "apenas_em_testes", "mocks_em_producao"]

NUL_HYPOTHESIS = (
    "The detected mocks and test doubles do not mask real system behavior "
    "and the audited codebase relies on genuine implementations."
)

# Rubrica anti-mock: catálogo fechado codigo_producao (fonte única de verdade).
SCORE_RUBRIC = RUBRIC_CATALOG["codigo_producao"]


async def audit_target(engine: OpenJevEngine, needle: LocalNeedleEngine, target: Dict) -> Dict:
    print(f"\n[{target['id']}] Avaliando: {target['name']}")

    # 1. Scrub determinístico
    t0 = time.perf_counter()
    occurrences = scrub_mock_occurrences(target["path"])
    summary = summarize_occurrences(occurrences)
    lat_scrub = (time.perf_counter() - t0) * 1000.0
    print(f"  [Scrub] {summary['total']} ocorrências em {summary['files_affected']} arquivos ({lat_scrub:.2f} ms)")

    # 2. Needle 3 — trechos cirúrgicos de mocks
    t0 = time.perf_counter()
    needle_resp = await needle.search(Needle3Query(
        query="mock MagicMock patch jest stub sinon monkeypatch test double",
        max_tokens=1200,
    ))
    lat_needle = (time.perf_counter() - t0) * 1000.0
    print(f"  [Needle 3] {len(needle_resp.snippets)} trechos ({needle_resp.total_tokens} tokens) em {lat_needle:.2f} ms")

    snippets_txt = "\n---\n".join(
        f"File: {s.file_path} (L{s.start_line}-L{s.end_line}):\n{s.content[:600]}"
        for s in needle_resp.snippets[:3]
    )
    scrub_txt = scrub_summary_text(summary, occurrences)

    # 3. Choice — veredito de localização (metadata de severidade igual)
    choice_resp = await engine.evaluate_choice(ChoiceRequest(
        premise=(
            f"Project: {target['name']}\nDimension: Mock/stub/fake presence audit\n"
            f"{scrub_txt[:800]}"
        ),
        options=CHOICE_OPTIONS,
    ))
    print(f"  [Choice] Veredito: [{choice_resp.selected}] ({choice_resp.top_confidence * 100:.1f}%)")

    # 4. Noul (threshold calibrado Laya 0.78)
    noul_resp = await engine.evaluate_noul(NoulRequest(
        premise=f"Mock scrub evidence:\n{scrub_txt[:1000]}\n\nNeedle context:\n{snippets_txt[:600]}",
        hypothesis=NUL_HYPOTHESIS,
        threshold=NOUL_THRESHOLD,
    ))
    risk_tag = "MOCKS NÃO MASCARAM (OK)" if noul_resp.passed else "RISCO DE MASCARAMENTO"
    print(f"  [Noul] {risk_tag} (P={noul_resp.probability:.4f}, threshold={NOUL_THRESHOLD})")

    # 5. Score (corte calibrado Laya 3.40)
    score_premise = (
        f"Project: {target['name']}\nDimension: Mock presence audit\n"
        f"{scrub_txt}\n\nNeedle context:\n{snippets_txt[:800]}"
    )
    score_resp = await engine.evaluate_score(ScoreRequest(
        premise=score_premise[:4000],
        rubric=SCORE_RUBRIC,
        min_score=1.0,
        max_score=5.0,
        threshold=SCORE_THRESHOLD,
    ))
    print(f"  [Score] Nota: {score_resp.score:.2f} / 5.00 (corte {SCORE_THRESHOLD})")
    print(f"          Distribuição: {score_resp.breakdown}")

    # 6. Diagnóstico flat (10 categorias) se reprovado
    diagnosis = None
    if score_resp.passed is False:
        scrub_head = (
            f"SCRUB DETERMINÍSTICO: {summary['total']} ocorrências de mock em "
            f"{summary['files_affected']} arquivos (em_testes="
            f"{summary['by_location'].get('em_testes', 0)}, em_producao="
            f"{summary['by_location'].get('em_producao', 0)})."
        )
        if summary["total"] == 0:
            scrub_head += (
                " ATENÇÃO: scrub encontrou ZERO mocks — a reprovação NÃO decorre "
                "de mocks; avalie outros defeitos (cobertura de testes, qualidade, contrato)."
            )
        diag_premise = (
            f"Code quality audit score {score_resp.score:.2f}/5.0 "
            f"(reprovado, corte={SCORE_THRESHOLD}). {scrub_head} "
            f"Rubric applied: anti-mock quality. Distribution: {score_resp.breakdown}."
        )
        diag_resp = await engine.evaluate_choice(ChoiceRequest(
            premise=diag_premise,
            options=DEFECT_OPTIONS,
            instructions=DIAG_CHOICE_INSTRUCTIONS,
            criteria=DEFECT_DESCRIPTIONS,
        ))
        diagnosis = {
            "selected": diag_resp.selected,
            "confidence": diag_resp.top_confidence,
            "description": DEFECT_DESCRIPTIONS.get(diag_resp.selected, ""),
            "candidate_defects": DEFECT_MEMBERS.get(diag_resp.selected, []),
        }
        print(f"  [Diagnóstico] {diag_resp.selected} ({diag_resp.top_confidence * 100:.1f}%)")

    return {
        "project": target["name"],
        "path": target["path"],
        "scrub": summary,
        "occurrences": occurrences,
        "needle_tokens": needle_resp.total_tokens,
        "needle_latency_ms": round(lat_needle, 2),
        "choice_selected": choice_resp.selected,
        "choice_confidence": choice_resp.top_confidence,
        "choice_latency_ms": round(choice_resp.latency_ms, 2),
        "noul_passed": noul_resp.passed,
        "noul_probability": noul_resp.probability,
        "noul_threshold": NOUL_THRESHOLD,
        "noul_latency_ms": round(noul_resp.latency_ms, 2),
        "score": score_resp.score,
        "score_passed": score_resp.passed,
        "score_threshold": SCORE_THRESHOLD,
        "score_breakdown": score_resp.breakdown,
        "score_latency_ms": round(score_resp.latency_ms, 2),
        "diagnosis": diagnosis,
    }


async def _safe_audit_target(engine: OpenJevEngine, needle: LocalNeedleEngine, target: Dict) -> Dict:
    """Executa audit_target com fail-fast interno por alvo: uma exceção
    (GPU, path, inferência) não derruba a auditoria dos demais projetos.
    O erro é registrado factually no JSON — nunca silenciado nem simulado.
    """
    try:
        return await audit_target(engine, needle, target)
    except Exception as exc:
        print(f"  [ERRO] {target['name']}: {type(exc).__name__}: {exc}")
        return {
            "project": target["name"],
            "path": target["path"],
            "error": f"{type(exc).__name__}: {exc}",
        }


async def run_mocks_audit():
    print("=" * 80)
    print("AUDITORIA DE PRESENÇA DE MOCKS — 4 PROJETOS (molde Needle + Open Jev Laya)")
    print(f"Thresholds: noul >= {NOUL_THRESHOLD} | score >= {SCORE_THRESHOLD}")
    print("=" * 80)

    start_total = time.perf_counter()

    # 1. Inicializa motores locais (Laya — sem DeBERTa legado, sem mock)
    print("\n[1/3] Inicializando Motores Locais...")
    engine = OpenJevEngine()
    assert engine.is_laya, "Engine padrão DEVE ser Laya (calibração 0.78/3.40)."
    engine.load_model()
    print(f"  Engine pronta: model={engine.model_id_or_path} is_laya={engine.is_laya}")
    print("-" * 80)

    # 2. Loop de auditoria
    print("\n[2/3] Executando Scrub + Needle + Cascata JEV...")
    results = []
    for idx, target in enumerate(TARGETS, 1):
        root = Path(target["path"])
        if not root.is_dir():
            print(f"\n[{idx}/4] {target['name']}: ALVO INEXISTENTE ({target['path']}) — pulando")
            results.append({"project": target["name"], "path": target["path"], "error": "target_missing"})
            continue
        needle = LocalNeedleEngine(workspace_root=target["path"])
        result = await _safe_audit_target(engine, needle, target)
        result["index"] = idx
        results.append(result)

    total_time = (time.perf_counter() - start_total) * 1000.0

    # 3. Grava JSON consolidado
    print("\n[3/3] Gravando resultados...")
    out_file = Path("/home/maiteuxzanela/projeto JEV/mocks_audit_results.json")
    out_file.write_text(json.dumps({
        "engine": "laya",
        "noul_threshold": NOUL_THRESHOLD,
        "score_threshold": SCORE_THRESHOLD,
        "choice_options": CHOICE_OPTIONS,
        "total_time_ms": round(total_time, 2),
        "projects": results,
    }, indent=2, ensure_ascii=False), encoding="utf-8")

    print("\n" + "=" * 80)
    print("RESUMO")
    for r in results:
        if "error" in r:
            print(f"  {r['project']}: ERRO ({r['error']})")
        else:
            print(
                f"  {r['project']}: mocks={r['scrub']['total']} "
                f"arquivos={r['scrub']['files_affected']} "
                f"score={r['score']:.2f} "
                f"({'APROVADO' if r['score_passed'] else 'REPROVADO'} "
                f"corte={SCORE_THRESHOLD}) "
                f"choice={r['choice_selected']}"
            )
    print("-" * 80)
    print(f"AUDITORIA CONCLUÍDA EM {total_time:.2f} ms ({total_time / 1000.0:.2f} s)")
    print(f"Resultados gravados em: {out_file}")
    print("=" * 80)


if __name__ == "__main__":
    asyncio.run(run_mocks_audit())
