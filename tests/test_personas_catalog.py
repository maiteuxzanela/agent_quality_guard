"""Testes reais do catálogo de personas e subagentes do Projeto JEV (sem mocks)."""

from __future__ import annotations

import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PERSONAS_DIR = REPO_ROOT / "personas"

EXPECTED_PERSONA_FILES = {
    "README.md",
    "00_valerio_lead_architect.md",
    "01_kael_ml_hardware.md",
    "02_maya_ast_memory.md",
    "03_tulio_mcp_api.md",
    "04_clara_qa_governance.md",
    "05_otavio_audit_bridge.md",
}

EXPECTED_SPECIALISTS = {
    "kael_ml",
    "maya_ast",
    "tulio_mcp",
    "clara_qa",
    "otavio_bridge",
}


class TestPersonasCatalog(unittest.TestCase):
    """Garante a integridade dos arquivos de personas e aderência aos contratos."""

    def test_personas_directory_exists(self) -> None:
        self.assertTrue(PERSONAS_DIR.is_dir(), f"Diretório não encontrado: {PERSONAS_DIR}")

    def test_all_expected_persona_files_exist(self) -> None:
        found_files = {p.name for p in PERSONAS_DIR.glob("*.md")}
        missing = EXPECTED_PERSONA_FILES - found_files
        self.assertFalse(missing, f"Arquivos de persona ausentes: {missing}")

    def test_roster_size_within_laya_calibration_limit(self) -> None:
        """O roster de subagentes especialistas deve ter <= 10 opções para o Laya."""
        self.assertLessEqual(
            len(EXPECTED_SPECIALISTS),
            10,
            "Roster de especialistas excede 10 opções (viola calibração de temperature do Laya).",
        )
        self.assertGreaterEqual(len(EXPECTED_SPECIALISTS), 2)

    def test_orchestrator_persona_content(self) -> None:
        valerio = PERSONAS_DIR / "00_valerio_lead_architect.md"
        content = valerio.read_text(encoding="utf-8")
        self.assertIn("Valério", content)
        self.assertIn("Maiteux Zanela", content)
        self.assertIn("openjev_choice", content)
        self.assertIn("openjev_score_and_diagnose", content)

    def test_specialists_personas_content(self) -> None:
        for fname in EXPECTED_PERSONA_FILES - {"README.md", "00_valerio_lead_architect.md"}:
            path = PERSONAS_DIR / fname
            text = path.read_text(encoding="utf-8")
            self.assertIn("Valério", text, f"{fname} deve referenciar o Orquestrador Valério")
            self.assertIn("Diretrizes e Guardrails", text, f"{fname} deve conter guardrails")

    def test_specialists_have_delivery_contract(self) -> None:
        """Toda persona de especialista declara o Contrato de Entrega do spawn (set/2026)."""
        specialists = EXPECTED_PERSONA_FILES - {"README.md", "00_valerio_lead_architect.md"}
        for fname in specialists:
            text = (PERSONAS_DIR / fname).read_text(encoding="utf-8")
            self.assertIn(
                "Contrato de Entrega", text,
                f"{fname} deve definir a seção Contrato de Entrega (retorno ao orquestrador)",
            )
            self.assertIn(
                "openjev_spawn_subagent", text,
                f"{fname} deve situar o contrato no fluxo de spawn",
            )

    def test_valerio_persona_retires_mandatory_routing(self) -> None:
        """Governança set/2026: persona do Valério sem delegação obrigatória (Nível 2)."""
        text = (PERSONAS_DIR / "00_valerio_lead_architect.md").read_text(encoding="utf-8")
        self.assertIn("roteamento obrigatório está suspenso", text)
        self.assertNotIn("Delegação Obrigatória", text)
        self.assertNotIn("NÃO edita código de produção diretamente", text)

    def test_readme_excludes_orchestrator_from_subagent_roster(self) -> None:
        """Valério não é subagente: não aparece na matriz despachável do README."""
        text = (PERSONAS_DIR / "README.md").read_text(encoding="utf-8")
        self.assertIn("não deve ser despachado", text)
        self.assertNotIn("| **Valério** |", text)

    def test_agents_md_references_team_matrix(self) -> None:
        agents_file = REPO_ROOT / "AGENTS.md"
        text = agents_file.read_text(encoding="utf-8")
        self.assertIn("Matriz da Equipe e Roteamento", text)
        self.assertIn("Valério", text)
        self.assertIn("@Kael", text)
        self.assertIn("@Maya", text)
        self.assertIn("@Túlio", text)
        self.assertIn("@Clara", text)
        self.assertIn("@Otávio", text)

    def test_agents_md_references_relatorio_resumido(self) -> None:
        agents_file = REPO_ROOT / "AGENTS.md"
        text = agents_file.read_text(encoding="utf-8")
        self.assertIn("Relatório Resumido de Validação de Entrega", text)
        self.assertIn("**Status:**", text)
        self.assertIn("**Tentativas:**", text)
        self.assertIn("**Notas:**", text)

    def test_ecosystem_agents_md_despacho_sob_demanda(self) -> None:
        """Governança vigente: roteamento obrigatório suspenso, despacho sob demanda.

        A exigência anterior de "Três Níveis de Execução" (delegação obrigatória +
        nova_persona_especialista) foi aposentada: contradiz o AGENTS.md §3.1/§4
        atuais, que suspendem o roteamento compulsório em favor da autonomia do
        orquestrador com despacho via openjev_spawn_subagent sob demanda.
        """
        workspaces = [
            Path("/home/maiteuxzanela/projeto JEV"),
            Path("/mnt/94CCB337CCB3130A/JEZ collections"),
            Path("/home/maiteuxzanela/PanelaBuild"),
            Path("/mnt/94CCB337CCB3130A/Faturamento"),
        ]
        for ws in workspaces:
            agents_md = ws / "AGENTS.md"
            self.assertTrue(agents_md.is_file(), f"AGENTS.md não encontrado em {ws}")

        agents_md = REPO_ROOT / "AGENTS.md"
        content = agents_md.read_text(encoding="utf-8")
        self.assertIn(
            "roteamento obrigatório está suspenso",
            content,
            "AGENTS.md não formaliza a suspensão do roteamento obrigatório (§3.1)",
        )
        self.assertIn(
            "openjev_spawn_subagent",
            content,
            "AGENTS.md não formaliza o despacho sob demanda via openjev_spawn_subagent",
        )
        self.assertIn(
            "autonomia plena",
            content,
            "AGENTS.md não formaliza a autonomia plena do orquestrador",
        )



if __name__ == "__main__":
    unittest.main()
