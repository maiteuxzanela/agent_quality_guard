#!/usr/bin/env python3
"""Valida as 9 skills globais OpenCode/Antigravity e o command jev-fecha.

Fail-fast: exit 1 em qualquer violação. Sem mocks — lê o disco real.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

NAME_RX = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
EXPECTED_SKILLS = frozenset(
    {
        "skills-opencode-creator",
        "needle3-busca",
        "anti-mock-failfast",
        "jev-auditoria-moldura",
        "guard-stop-hooks",
        "calibracao-score",
        "secret-scan-hardcoded",
        "jev-client-bridge",
        "firebase-deploy-pipeline",
    }
)
REQUIRED_FIELDS = ("name", "description")
OPCODE_SKILLS = Path.home() / ".config" / "opencode" / "skills"
GEMINI_SKILLS = Path.home() / ".gemini" / "config" / "skills"
JEV_FECHA = Path_HOME_CMD = Path.home() / ".config" / "opencode" / "command" / "jev-fecha.md"

# Skills intra em <repo>/.agents/skills/<name>/SKILL.md (OpenCode + Antigravity workspace)
INTRA_SKILLS: dict[str, frozenset[str]] = {
    "/mnt/94CCB337CCB3130A/Faturamento": frozenset(
        {"tiss-lote-cli", "kanban-dashboard-sync"}
    ),
    "/home/maiteuxzanela/PanelaBuild": frozenset({"panela-data-pipeline"}),
    "/mnt/94CCB337CCB3130A/JEZ collections": frozenset(
        {"firestore-rules-hardening"}
    ),
    "/mnt/94CCB337CCB3130A/predictera": frozenset(
        {
            "previsao-causal-ev",
            "download-csv-ligas",
            "atualizar-rodada",  # canonical .agents only after dedupe
        }
    ),
}
# Dedupe: atualizar-rodada must NOT exist under .opencode/skills
PRED_ROOT = "/mnt/94CCB337CCB3130A/predictera"


def parse_frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---"):
        raise ValueError("missing opening frontmatter")
    end = text.find("\n---", 3)
    if end < 0:
        raise ValueError("unclosed frontmatter")
    fields: dict[str, str] = {}
    key: str | None = None
    for line in text[3:end].splitlines():
        if line[:1] in (" ", "\t"):
            if key is not None:
                fields[key] = (fields[key] + " " + line.strip()).strip()
            continue
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        key = k.strip()
        fields[key] = v.strip()
        if v.strip() in (">-", ">", "|", "|-"):
            fields[key] = ""
    return fields


def validate_skill(path: Path, errors: list[str]) -> None:
    dir_name = path.parent.name
    try:
        text = path.read_text(encoding="utf-8")
        fm = parse_frontmatter(text)
    except (OSError, ValueError) as exc:
        errors.append(f"{dir_name}: frontmatter error: {exc}")
        return
    for field in REQUIRED_FIELDS:
        if not fm.get(field):
            errors.append(f"{dir_name}: missing field {field}")
    name = fm.get("name", "")
    desc = " ".join(fm.get("description", "").split())
    if name != dir_name:
        errors.append(f"{dir_name}: name {name!r} != directory")
    if name and not NAME_RX.match(name):
        errors.append(f"{dir_name}: name fails regex")
    if not 1 <= len(desc) <= 1024:
        errors.append(f"{dir_name}: description length {len(desc)}")


def main() -> int:
    errors: list[str] = []
    if not OPCODE_SKILLS.is_dir():
        print(f"missing {OPCODE_SKILLS}", file=sys.stderr)
        return 1
    found = {p.parent.name for p in OPCODE_SKILLS.glob("*/SKILL.md")}
    missing = EXPECTED_SKILLS - found
    extra = found - EXPECTED_SKILLS
    if missing:
        errors.append(f"missing skills: {sorted(missing)}")
    if extra:
        errors.append(f"unexpected skills: {sorted(extra)}")
    for skill in sorted(EXPECTED_SKILLS & found):
        validate_skill(OPCODE_SKILLS / skill / "SKILL.md", errors)

    if not GEMINI_SKILLS.is_dir():
        errors.append(f"missing Antigravity dir {GEMINI_SKILLS}")
    else:
        for skill in sorted(EXPECTED_SKILLS):
            link = GEMINI_SKILLS / skill
            if not link.is_symlink() and not link.is_dir():
                errors.append(f"antigravity missing: {skill}")
            elif link.is_symlink() and not link.resolve().exists():
                errors.append(f"broken symlink: {skill}")

    if not JEV_FECHA.is_file():
        errors.append("missing command jev-fecha.md")
    else:
        body = JEV_FECHA.read_text(encoding="utf-8")
        if "threshold`: 3.40" not in body and "threshold: 3.40" not in body:
            errors.append("jev-fecha: threshold is not 3.40")
        if re.search(r"score ≥ 3\.60", body) or re.search(r"Aprovado \(≥ 3\.60\)", body):
            errors.append("jev-fecha: still uses 3.60 as pass cutoff")
        if "Confronto:" in body:
            errors.append("jev-fecha: still contains verbose Confronto field")
        if (OPCODE_SKILLS.parent / "skills" / "jev-entrega").exists():
            errors.append("jev-entrega must not exist (command owns the flow)")

    # Intra-project .agents/skills
    for root_s, expected in INTRA_SKILLS.items():
        root = Path(root_s)
        agents = root / ".agents" / "skills"
        if not agents.is_dir():
            errors.append(f"missing {agents}")
            continue
        found_intra = {p.parent.name for p in agents.glob("*/SKILL.md")}
        for skill in sorted(expected - found_intra):
            errors.append(f"{root.name}: missing intra skill {skill}")
        for skill in sorted(expected & found_intra):
            validate_skill(agents / skill / "SKILL.md", errors)

    pred_opencode_dup = Path(PRED_ROOT) / ".opencode" / "skills" / "atualizar-rodada"
    if pred_opencode_dup.exists():
        errors.append(
            "predictera: duplicate atualizar-rodada under .opencode/skills (dedupe)"
        )

    # Global names must not collide with any intra name
    all_intra_names: set[str] = set()
    for s in INTRA_SKILLS.values():
        all_intra_names |= set(s)
    collide = EXPECTED_SKILLS & all_intra_names
    if collide:
        errors.append(f"name collision global/intra: {sorted(collide)}")

    if errors:
        for e in errors:
            print(f"FAIL {e}", file=sys.stderr)
        print(f"{len(errors)} failure(s)", file=sys.stderr)
        return 1
    n_intra = sum(len(v) for v in INTRA_SKILLS.values())
    print(
        f"OK {len(EXPECTED_SKILLS)} global + {n_intra} intra "
        f"+ antigravity mirrors + jev-fecha 3.40 + no pred dup"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
