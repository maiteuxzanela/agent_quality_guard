"""Mede se DIAG_CHOICE_INSTRUCTIONS + DEFECT_DESCRIPTIONS cabem em head_max_len
sem truncamento de instructions nem per-option cut (laya.common.build_sequence).
Roda em loop de ajuste até passar ou esgotar iterações.
"""
from __future__ import annotations

import sys

sys.path.insert(0, ".")

from laya import Agent
from laya.common import build_sequence, render_options
from src.core.schemas import DEFECT_OPTIONS, DEFECT_DESCRIPTIONS, DIAG_CHOICE_INSTRUCTIONS
from src.decision.laya_adapter import LAYA_CHECKPOINTS


def measure(ins: str, options: list[str], descriptions: dict[str, str]) -> dict:
    c = LAYA_CHECKPOINTS["english"]
    agent = Agent(c["repo"], device="cpu", subfolder=c["subfolder"])
    tok = agent.tok
    head_max = int(agent.cfg.get("head_max_len", 192))
    max_len = int(agent.cfg.get("max_len", 512))

    criteria = {opt: descriptions[opt] for opt in options}
    q = {"t": "choice", "ins": ins, "crit": criteria}
    opts = render_options(q)

    head_full = tok(
        "%s question: %s" % (q["t"], ins), add_special_tokens=False
    )["input_ids"]

    opt_ids = []
    per_option_content = []
    for o in opts:
        content = tok(" " + o.replace(tok.mask_token, " "), add_special_tokens=False)["input_ids"]
        per_option_content.append(len(content))
        opt_ids.append([tok.mask_token_id] + content[:48])

    total_opts = sum(len(o) for o in opt_ids)
    opt_budget = head_max - total_opts
    options_cut = opt_budget < 16
    per = None
    if options_cut:
        per = max(4, (head_max - 16) // max(1, len(opt_ids)))
        opt_ids = [o[:per] for o in opt_ids]
        opt_budget = head_max - sum(len(o) for o in opt_ids)

    ins_budget = max(8, opt_budget)
    ins_visible_tokens = min(len(head_full), ins_budget)
    ins_truncated = len(head_full) > ins_budget
    ins_text = tok.decode(head_full[:ins_budget])

    # full sequence check
    premise = "Código avaliado com score 3.30/5.0 (reprovado, corte=3.6, borderline=True). Contexto: teste."
    seq, markers = build_sequence(
        tok, premise, q, max_len=max_len, head_max_len=head_max
    )

    # null guidance must be in visible instructions
    null_ok = (
        "sem_defeito_identificavel" in ins_text
        and "evidencia_insuficiente" in ins_text
    )
    # per-option: after cut (if any), does id:desc still show meaningful desc?
    option_details = []
    for i, o in enumerate(opts):
        content_full = tok(" " + o, add_special_tokens=False)["input_ids"]
        visible_n = (per - 1) if options_cut else len(content_full)
        # also hard 48 already applied before per-cut in build_sequence
        visible_n = min(visible_n, 48)
        text = tok.decode(content_full[:visible_n])
        option_details.append(
            {
                "option": options[i],
                "full_content_tokens": len(content_full),
                "visible_tokens": visible_n,
                "visible_text": text,
                "desc_fully_visible": visible_n >= len(content_full),
            }
        )

    return {
        "head_max": head_max,
        "instructions_tokens": len(head_full),
        "instructions_budget": ins_budget,
        "instructions_truncated": ins_truncated,
        "instructions_visible": ins_text,
        "null_guidance_visible": null_ok,
        "options_sum_tokens": total_opts,
        "opt_budget_before": head_max - total_opts,
        "options_cut": options_cut,
        "per_option_cap": per,
        "content_tokens": per_option_content,
        "all_desc_fully_visible": all(d["desc_fully_visible"] for d in option_details),
        "options": option_details,
        "seq_len": len(seq),
        "markers_ok": len(markers) == len(options),
        "passed": (
            not ins_truncated
            and null_ok
            and not options_cut
            and all(d["desc_fully_visible"] for d in option_details)
            and len(markers) == len(options)
        ),
    }


def main() -> int:
    m = measure(DIAG_CHOICE_INSTRUCTIONS, DEFECT_OPTIONS, DEFECT_DESCRIPTIONS)
    import json

    print(json.dumps(m, indent=2, ensure_ascii=False))
    return 0 if m["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
