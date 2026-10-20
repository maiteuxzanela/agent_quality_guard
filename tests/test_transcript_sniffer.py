"""Testes reais e sem mocks para o Módulo de Transcript Sniffing."""

import json
import time
from pathlib import Path

import pytest

from agent_quality_guard.core.transcript_sniffer import (
    SCORE_PREMISE_MAX_LEN,
    build_factual_premise,
    extract_latest_user_request,
    find_active_transcript_path,
    get_git_changes_summary,
)


def test_find_active_transcript_path_from_payload(tmp_path: Path):
    fake_conv = tmp_path / "conv_abc" / ".system_generated" / "logs"
    fake_conv.mkdir(parents=True)
    fake_transcript = fake_conv / "transcript.jsonl"
    fake_transcript.write_text('{"step_index":0,"type":"USER_INPUT","content":"teste"}\n')

    payload = {"transcript_path": str(fake_transcript)}
    found = find_active_transcript_path(payload=payload, base_brain_dir=tmp_path)
    assert found == fake_transcript.resolve()


def test_find_active_transcript_path_from_mtime(tmp_path: Path):
    # Cria duas conversas com mtimes diferentes
    conv1 = tmp_path / "conv_old" / ".system_generated" / "logs"
    conv1.mkdir(parents=True)
    f1 = conv1 / "transcript.jsonl"
    f1.write_text('{"step_index":0}\n')

    time.sleep(0.05)

    conv2 = tmp_path / "conv_new" / ".system_generated" / "logs"
    conv2.mkdir(parents=True)
    f2 = conv2 / "transcript.jsonl"
    f2.write_text('{"step_index":0}\n')

    found = find_active_transcript_path(base_brain_dir=tmp_path)
    assert found == f2.resolve()


def test_extract_latest_user_request_with_tags(tmp_path: Path):
    transcript_file = tmp_path / "transcript.jsonl"
    lines = [
        {"step_index": 0, "type": "USER_INPUT", "content": "<USER_REQUEST>\nComando Antigo\n</USER_REQUEST>"},
        {"step_index": 1, "type": "PLANNER_RESPONSE", "content": "Ok"},
        {
            "step_index": 2,
            "type": "USER_INPUT",
            "content": "<USER_REQUEST>\nImplementar transcript sniffing nos 5 projetos\n</USER_REQUEST>\n<ADDITIONAL_METADATA>\ntime=now\n</ADDITIONAL_METADATA>",
        },
        {"step_index": 3, "type": "PLANNER_RESPONSE", "content": "Trabalhando nisso"},
    ]
    with open(transcript_file, "w", encoding="utf-8") as f:
        for entry in lines:
            f.write(json.dumps(entry) + "\n")

    t0 = time.perf_counter()
    req = extract_latest_user_request(transcript_path=transcript_file)
    lat_ms = (time.perf_counter() - t0) * 1000

    assert req == "Implementar transcript sniffing nos 5 projetos"
    assert lat_ms < 20.0  # Execução ultra-rápida


def test_extract_latest_user_request_without_tags(tmp_path: Path):
    transcript_file = tmp_path / "transcript.jsonl"
    entry = {"step_index": 0, "type": "USER_INPUT", "content": "Refatorar banco de dados PostgreSQL"}
    transcript_file.write_text(json.dumps(entry) + "\n")

    req = extract_latest_user_request(transcript_path=transcript_file)
    assert req == "Refatorar banco de dados PostgreSQL"


def test_build_factual_premise_conformance(tmp_path: Path):
    # Cria repositório simulado simples
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    premise = build_factual_premise(
        workspace=str(repo_dir),
        prompt_override="Refatorar módulo de criptografia e adicionar testes de integração reais",
    )

    assert "Pedido do Prompt: " in premise
    assert " | Resultado Executado: " in premise
    assert "Refatorar módulo de criptografia" in premise
    assert len(premise) <= SCORE_PREMISE_MAX_LEN


def test_build_factual_premise_truncation_safety(tmp_path: Path):
    repo_dir = tmp_path / "repo_huge"
    repo_dir.mkdir()

    huge_prompt = "A" * 3000
    premise = build_factual_premise(
        workspace=str(repo_dir),
        prompt_override=huge_prompt,
    )

    assert len(premise) <= SCORE_PREMISE_MAX_LEN
    assert premise.startswith("Pedido do Prompt: ")
    assert " | Resultado Executado: " in premise


def test_extract_real_session_transcript():
    # Testa extração contra o arquivo real do ambiente se presente
    real_path = find_active_transcript_path()
    if real_path and real_path.is_file():
        req = extract_latest_user_request(transcript_path=real_path)
        assert len(req) > 0
        assert isinstance(req, str)
