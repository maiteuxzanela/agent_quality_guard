"""Testes de integração para o Circuit Breaker e Trava Anti-Premise Gaming do MCP Server.

Sem mocks de engine — usa o engine Laya real via singleton do mcp_server (AGENTS.md §5.2).
Repositórios git reais criados com subprocess para produzir git_state autêntico.

Estratégia de threshold para controlar outcome sem mock:
  - threshold=5.0 → força reprovação real para testar o ciclo anti-gaming
  - threshold=1.0 → força aprovação real para testar reset de histórico
"""

import subprocess
import tempfile
import unittest
from pathlib import Path

import src.mcp_server as _mcp_module
from src.mcp_server import openjev_score_and_diagnose, _score_history, _get_workspace_git_signature


# ─── Engine real ──────────────────────────────────────────────────────────────

def _ensure_engine_loaded():
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
    """Cria repositório git real com commit inicial."""
    tmp = tempfile.mkdtemp(prefix="jev_cb_")
    subprocess.run(["git", "init", tmp], check=True, capture_output=True)
    subprocess.run(["git", "-C", tmp, "config", "user.email", "test@jev.local"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", tmp, "config", "user.name", "JEV Test"],
                   check=True, capture_output=True)
    (Path(tmp) / "README.md").write_text("# CB test repo\n")
    subprocess.run(["git", "-C", tmp, "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", tmp, "commit", "-m", "init"], check=True, capture_output=True)
    return tmp


class TestCircuitBreakerAndAntiGaming(unittest.IsolatedAsyncioTestCase):

    @classmethod
    def setUpClass(cls):
        _ensure_engine_loaded()

    def setUp(self):
        _score_history.clear()
        self.ws = _make_git_repo()

    def tearDown(self):
        _score_history.clear()

    async def test_anti_premise_gaming_blocks_quando_git_inalterado(self):
        """Após reprovação real, nova chamada sem commit deve ser recusada."""
        # 1ª chamada: threshold=5.0 garante reprovação real pelo Laya
        res1 = await openjev_score_and_diagnose(
            premise="Pedido do Prompt: entregar módulo. | Resultado Executado: código sem testes, sem docs, sem tratamento de erros.",
            workspace=self.ws,
            threshold=5.0,
        )
        self.assertFalse(res1["passed"])
        self.assertNotIn("error", res1)

        # 2ª chamada: mesmo git_state → deve ser bloqueada
        res2 = await openjev_score_and_diagnose(
            premise="Pedido do Prompt: reavaliar entrega. | Resultado Executado: tentativa de re-avaliar sem alterar o repositório.",
            workspace=self.ws,
            threshold=5.0,
        )
        self.assertIn("error", res2)
        self.assertIn("AVALIAÇÃO RECUSADA", res2["error"])
        self.assertFalse(res2["emergency_exit_unlocked"])

    async def test_circuit_breaker_desbloqueia_na_tentativa_5(self):
        """Saída de emergência deve ser ativada após MAX_ATTEMPTS=5 tentativas sem mudança."""
        from src.mcp_server import MAX_ATTEMPTS

        self.assertEqual(MAX_ATTEMPTS, 5)
        git_state_real = _get_workspace_git_signature(self.ws)

        # Pré-carrega histórico com 4 tentativas já realizadas (5-1)
        _score_history[self.ws] = {
            "passed": False,
            "attempts": MAX_ATTEMPTS - 1,
            "git_state": git_state_real,
            "last_score": 3.20,
        }

        # 5ª chamada sem mudança: incrementa para 5 e destrava a Saída de Emergência
        # (Circuit Breaker Universal do working tree: limite atingido => desbloqueio
        # imediato, mesmo sem alteração no disco).
        res5 = await openjev_score_and_diagnose(
            premise="Pedido do Prompt: refatorar módulo. | Resultado Executado: tentativa 5 sem refatoração.",
            workspace=self.ws,
            threshold=5.0,
        )
        self.assertNotIn("error", res5)
        self.assertEqual(_score_history[self.ws]["attempts"], MAX_ATTEMPTS)
        self.assertTrue(res5.get("emergency_exit_unlocked"))
        self.assertIn("SAÍDA DE EMERGÊNCIA DESBLOQUEADA", res5.get("message", ""))

        # 6ª chamada: desbloqueia o circuit breaker
        res_cb = await openjev_score_and_diagnose(
            premise="Pedido do Prompt: acionar circuit breaker. | Resultado Executado: chamada que deve acionar o circuit breaker.",
            workspace=self.ws,
            threshold=5.0,
        )
        self.assertTrue(res_cb.get("emergency_exit_unlocked"))
        self.assertIn("SAÍDA DE EMERGÊNCIA DESBLOQUEADA", res_cb.get("message", ""))
        self.assertEqual(res_cb.get("max_attempts"), 5)

    async def test_aprovacao_real_reseta_historico(self):
        """Aprovação real (threshold=1.0) deve zerar tentativas no histórico."""
        # Pré-carrega histórico com reprovações anteriores
        old_git = "estado_antigo_diferente:clean"
        _score_history[self.ws] = {
            "passed": False,
            "attempts": 3,
            "git_state": old_git,
            "last_score": 2.80,
        }

        # threshold=1.0 garante aprovação — git_state atual é diferente do antigo
        res = await openjev_score_and_diagnose(
            premise="Pedido do Prompt: implementar add(a, b). | Resultado Executado: função add(a, b): return a + b. Teste: assert add(1, 2) == 3.",
            workspace=self.ws,
            threshold=1.0,
        )
        self.assertTrue(res["passed"])
        self.assertGreaterEqual(res["score"], 1.0)

        # Histórico deve ser resetado
        self.assertTrue(_score_history[self.ws]["passed"])
        self.assertEqual(_score_history[self.ws]["attempts"], 0)

    async def test_isolamento_entre_workspaces(self):
        """Estado de tentativas em um workspace não contamina outro workspace."""
        ws2 = _make_git_repo()

        # ws1 tem histórico de reprovação
        git_state_ws1 = _get_workspace_git_signature(self.ws)
        _score_history[self.ws] = {
            "passed": False,
            "attempts": 5,
            "git_state": git_state_ws1,
            "last_score": 3.0,
        }

        # ws2 não tem histórico — threshold=1.0 deve passar normalmente
        res_ws2 = await openjev_score_and_diagnose(
            premise="Pedido do Prompt: entregar código. | Resultado Executado: código limpo com testes.",
            workspace=ws2,
            threshold=1.0,
        )
        self.assertTrue(res_ws2["passed"])
        self.assertNotIn("error", res_ws2)

        # ws1 mantém tentativas inalteradas
        self.assertEqual(_score_history[self.ws]["attempts"], 5)

    async def test_novo_commit_desbloqueia_avaliacao(self):
        """Novo commit real muda git_state e deve permitir nova avaliação."""
        # 1ª chamada: reprova
        res1 = await openjev_score_and_diagnose(
            premise="Pedido do Prompt: implementar módulo. | Resultado Executado: código sem implementação.",
            workspace=self.ws,
            threshold=5.0,
        )
        self.assertFalse(res1["passed"])

        # Cria novo commit real
        (Path(self.ws) / "fix.py").write_text("def fix(): return 42\n")
        subprocess.run(["git", "-C", self.ws, "add", "."], check=True, capture_output=True)
        subprocess.run(["git", "-C", self.ws, "commit", "-m", "fix: implementacao real"],
                       check=True, capture_output=True)

        # Nova avaliação após commit deve ser aceita (não bloqueada)
        res2 = await openjev_score_and_diagnose(
            premise="Pedido do Prompt: corrigir defeito. | Resultado Executado: código com fix implementado.",
            workspace=self.ws,
            threshold=1.0,
        )
        self.assertNotIn("error", res2)
        self.assertTrue(res2["passed"])

    def test_git_signature_retorna_string_com_separador(self):
        """Assinatura Git deve conter ':' separando hash do status."""
        sig = _get_workspace_git_signature(self.ws)
        self.assertIsInstance(sig, str)
        self.assertIn(":", sig)
        self.assertGreater(len(sig), 3)
        head, digest = sig.split(":", 1)
        self.assertEqual(len(head), 40, f"HEAD sha1 inválido: {head!r}")
        self.assertEqual(len(digest), 64, f"sha256 digest inválido: {digest!r}")
        self.assertNotEqual(digest, "0" * 64)

    def test_assinatura_muda_ao_reeditar_arquivo_ja_sujo(self):
        """Reeditar um arquivo já modificado DEVE mudar a assinatura.

        Regressão do bug: `git status --porcelain` não muda se o mesmo
        path já estava ` M` — bloqueava reavaliação após refatoração real.
        """
        readme = Path(self.ws) / "README.md"
        readme.write_text("# v1 — primeira edição real\n")
        sig1 = _get_workspace_git_signature(self.ws)
        self.assertIn(":", sig1)
        self.assertTrue(sig1.split(":", 1)[1], "digest da assinatura não pode ser vazio")

        # Mesmo path, já sujo — segunda edição com conteúdo diferente
        readme.write_text("# v2 — segunda edição real no mesmo arquivo sujo\n")
        sig2 = _get_workspace_git_signature(self.ws)

        self.assertNotEqual(
            sig1,
            sig2,
            "Reeditar arquivo já sujo deve alterar a assinatura (conteúdo, não porcelain).",
        )

        # Terceira edição no mesmo arquivo já sujo
        readme.write_text("# v3 — terceira edição real\n")
        sig3 = _get_workspace_git_signature(self.ws)
        self.assertNotEqual(sig2, sig3)
        self.assertNotEqual(sig1, sig3)

    def test_untracked_vazio_nao_destrava_assinatura(self):
        """Arquivo untracked vazio (artificial) não pode alterar a assinatura."""
        sig_before = _get_workspace_git_signature(self.ws)
        self.assertIn(":", sig_before)
        self.assertEqual(len(sig_before.split(":", 1)[1]), 64)
        (Path(self.ws) / "artificial_empty.txt").write_text("")
        sig_after = _get_workspace_git_signature(self.ws)
        self.assertEqual(
            sig_before,
            sig_after,
            "Untracked vazio não deve contar para destravar reavaliação.",
        )
        # remover o vazio e confirmar estabilidade (não é equalidade trivial por falha de git)
        (Path(self.ws) / "artificial_empty.txt").unlink()
        sig_clean_again = _get_workspace_git_signature(self.ws)
        self.assertEqual(sig_before, sig_clean_again)

    def test_untracked_com_conteudo_real_altera_assinatura(self):
        """Arquivo novo untracked com conteúdo real altera a assinatura."""
        sig_before = _get_workspace_git_signature(self.ws)
        (Path(self.ws) / "real_new_module.py").write_text(
            "def helper(x: int) -> int:\n    return x + 1\n"
        )
        sig_after = _get_workspace_git_signature(self.ws)
        self.assertNotEqual(sig_before, sig_after)
        self.assertIn(":", sig_after)

    def test_mudanca_de_conteudo_sem_commit_muda_assinatura(self):
        """Mudança staged+unstaged de conteúdo (sem novo commit) deve contar."""
        # deixa sujo
        (Path(self.ws) / "app.py").write_text("VALUE = 1\n")
        subprocess.run(["git", "-C", self.ws, "add", "app.py"], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", self.ws, "commit", "-m", "add app"],
            check=True, capture_output=True,
        )
        sig_clean = _get_workspace_git_signature(self.ws)

        # edição unstaged no mesmo arquivo
        (Path(self.ws) / "app.py").write_text("VALUE = 2  # editado sem commit\n")
        sig_dirty = _get_workspace_git_signature(self.ws)
        self.assertNotEqual(sig_clean, sig_dirty)

        # segunda edição unstaged no MESMO arquivo já sujo
        (Path(self.ws) / "app.py").write_text("VALUE = 3  # segunda edição sem commit\n")
        sig_dirty2 = _get_workspace_git_signature(self.ws)
        self.assertNotEqual(sig_dirty, sig_dirty2)

    async def test_reavaliacao_destrava_apos_edicao_arquivo_ja_sujo(self):
        """Reavaliar após editar o MESMO arquivo já sujo deve ser aceita."""
        (Path(self.ws) / "lib.py").write_text("def a():\n    return 1\n")
        subprocess.run(["git", "-C", self.ws, "add", "lib.py"], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", self.ws, "commit", "-m", "lib"],
            check=True, capture_output=True,
        )

        # reprova real
        res1 = await openjev_score_and_diagnose(
            premise="Pedido do Prompt: completar módulo. | Resultado Executado: código incompleto sem tratamento.",
            workspace=self.ws,
            threshold=5.0,
        )
        self.assertFalse(res1["passed"])

        # sem mudança → bloqueada
        res2 = await openjev_score_and_diagnose(
            premise="Pedido do Prompt: reavaliar. | Resultado Executado: repetir sem mudar nada.",
            workspace=self.ws,
            threshold=5.0,
        )
        self.assertIn("error", res2)

        # edição real no MESMO arquivo já sujo da primeira rodada
        (Path(self.ws) / "lib.py").write_text(
            "def a():\n    return 1  # refatoração real no arquivo sujo\n"
        )
        res3 = await openjev_score_and_diagnose(
            premise="Pedido do Prompt: refatorar. | Resultado Executado: refatoração real no mesmo arquivo após reprovação.",
            workspace=self.ws,
            threshold=1.0,
        )
        self.assertNotIn(
            "error",
            res3,
            "Edição em arquivo já sujo deve destravar a reavaliação.",
        )
        self.assertTrue(res3["passed"])


class TestSignatureNonGit(unittest.TestCase):
    """Caminho não-git de `_get_workspace_git_signature` — asserções explícitas.

    Política documentada no docstring: dir sem `.git` (ou repo sem commits)
    retorna assinatura estável com prefixo `no_head:` SENSÍVEL A CONTEÚDO;
    path inexistente retorna `""` (fallback de exceção).
    """

    def _make_nogit_dir(self) -> str:
        return tempfile.mkdtemp(prefix="jev_nogit_")

    def test_nao_git_retorna_prefixo_no_head(self):
        """Dir existente sem .git → assinatura 'no_head:<sha256>' (não silenciosa)."""
        d = self._make_nogit_dir()
        sig = _get_workspace_git_signature(d)
        self.assertIsInstance(sig, str)
        self.assertTrue(sig, "assinatura não-git não pode ser vazia para dir existente")
        self.assertTrue(sig.startswith("no_head:"), f"prefixo esperado, obtido: {sig!r}")
        head, digest = sig.split(":", 1)
        self.assertEqual(head, "no_head")
        self.assertEqual(len(digest), 64, f"sha256 digest inválido: {digest!r}")

    def test_nao_git_assinatura_estavel_sem_mudanca(self):
        """Sem edição no disco, duas chamadas idênticas devem produzir a mesma assinatura."""
        d = self._make_nogit_dir()
        (Path(d) / "mod.py").write_text("VALUE = 1\n")
        sig1 = _get_workspace_git_signature(d)
        sig2 = _get_workspace_git_signature(d)
        self.assertEqual(sig1, sig2)
        self.assertTrue(sig1.startswith("no_head:"))

    def test_nao_git_edicao_real_muda_assinatura(self):
        """Editar arquivo com conteúdo real no disco DEVE alterar a assinatura.

        Regressão: assinatura constante em dir não-git bloqueava reavaliação
        legítima até a Saída de Emergência (tentativa 5).
        """
        d = self._make_nogit_dir()
        f = Path(d) / "mod.py"
        f.write_text("VALUE = 1\n")
        sig1 = _get_workspace_git_signature(d)

        f.write_text("VALUE = 2  # refatoração real\n")
        sig2 = _get_workspace_git_signature(d)

        self.assertNotEqual(
            sig1,
            sig2,
            "Edição real em workspace não-git deve mudar a assinatura (conteúdo).",
        )
        self.assertTrue(sig2.startswith("no_head:"))

    def test_nao_git_arquivo_vazio_nao_altera_assinatura(self):
        """Arquivo vazio (artificial) não conta no hash não-git."""
        d = self._make_nogit_dir()
        (Path(d) / "real.py").write_text("X = 1\n")
        sig_before = _get_workspace_git_signature(d)
        (Path(d) / "artificial_empty.txt").write_text("")
        sig_after = _get_workspace_git_signature(d)
        self.assertEqual(sig_before, sig_after)

    def test_nao_git_sujeira_de_cache_nao_destrava(self):
        """Dirs artificiais (cache de testes/ferramentas) não alteram a assinatura.

        Sem o skip, rodar pytest num workspace não-git criaria
        `.pytest_cache/` e destravaria o anti-gaming sem refatoração real.
        """
        d = self._make_nogit_dir()
        (Path(d) / "real.py").write_text("X = 1\n")
        sig_before = _get_workspace_git_signature(d)

        cache = Path(d) / ".pytest_cache" / "v"
        cache.mkdir(parents=True)
        (cache / "cache").write_text("lastfailed\n{}\n")
        pyc = Path(d) / "__pycache__"
        pyc.mkdir()
        (pyc / "real.cpython-312.pyc").write_bytes(b"\x00\x01\x02fakebytecode")

        sig_after = _get_workspace_git_signature(d)
        self.assertEqual(
            sig_before,
            sig_after,
            "Sujeira de cache não deve alterar a assinatura não-git.",
        )

        # refatoração real ainda destrava
        (Path(d) / "real.py").write_text("X = 2  # refatoracao real\n")
        sig_refactored = _get_workspace_git_signature(d)
        self.assertNotEqual(sig_before, sig_refactored)

    def test_path_inexistente_retorna_vazio(self):
        """Path inexistente → `''` (fallback documentado; nunca bloqueia anti-gaming)."""
        missing = str(Path(tempfile.gettempdir()) / "jev_definitely_missing_dir_xyz")
        sig = _get_workspace_git_signature(missing)
        self.assertEqual(sig, "")


class TestAgentsMdEspelhado(unittest.TestCase):
    """AGENTS.md global (Antigravity + opencode) deve espelhar limiares Laya."""

    GEMINI = Path.home() / ".gemini/config/AGENTS.md"
    OPENCODE = Path.home() / ".config/opencode/AGENTS.md"

    def _assert_laya_thresholds(self, path: Path, label: str):
        if not path.is_file():
            self.skipTest(f"{label} não encontrado ou fora do escopo do sandbox: {path}")
        text = path.read_text(encoding="utf-8")
        # limiares ativos
        self.assertIn("0.78", text, f"{label}: noul 0.78 ausente")
        self.assertIn("3.40", text, f"{label}: score 3.40 ausente")
        # regras ativas antigas proibidas (fora da nota histórica DeBERTa)
        active = [
            ln for ln in text.splitlines()
            if ("0.85" in ln or "3.75" in ln) and "DeBERTa" not in ln and "era DeBERTa" not in ln
        ]
        # permite apenas linhas que mencionam explicitamente a substituição histórica
        for ln in active:
            self.assertTrue(
                "Substituem" in ln or "0.85 / 3.75" in ln or "3.75 do DeBERTa" in ln,
                f"{label}: regra ativa com limiar antigo: {ln!r}",
            )
        self.assertIn("openjev_score_and_diagnose", text, f"{label}: tool diagnose ausente")
        self.assertIn("needle3_search", text, f"{label}: tool needle ausente")

    def test_gemini_agents_espelha_laya(self):
        self._assert_laya_thresholds(self.GEMINI, "Antigravity AGENTS.md")
        if not self.GEMINI.is_file():
            self.skipTest("Antigravity AGENTS.md não acessível no sandbox")
        text = self.GEMINI.read_text(encoding="utf-8")
        self.assertIn("Antigravity", text)
        self.assertIn("mcp_config.json", text)

    def test_opencode_agents_espelha_laya(self):
        self._assert_laya_thresholds(self.OPENCODE, "opencode AGENTS.md")
        if not self.OPENCODE.is_file():
            self.skipTest("opencode AGENTS.md não acessível no sandbox")
        text = self.OPENCODE.read_text(encoding="utf-8")
        self.assertIn("opencode", text)
        self.assertIn("opencode.jsonc", text)

    def _assert_protocolo_duvida(self, path: Path, label: str):
        if not path.is_file():
            self.skipTest(f"{label} não encontrado ou fora do escopo do sandbox: {path}")
        text = path.read_text(encoding="utf-8")
        self.assertIn(
            "Protocolo de Dúvida — Consulta Formal via Open Jev",
            text,
            f"{label}: título Protocolo de Dúvida ausente",
        )
        self.assertIn("### Incentivo forte", text, f"{label}: incentivo ausente")
        self.assertIn("Qual **skill**", text, f"{label}: linha skill ausente")
        self.assertIn(
            "Qual **arquivo ou diretório**", text, f"{label}: linha arquivo/dir ausente"
        )
        self.assertIn("openjev_consulta", text, f"{label}: consulta ausente")
        self.assertIn("/jev-fecha", text, f"{label}: jev-fecha ausente")
        self.assertNotIn(
            "### Roteamento Formal de Subagentes",
            text,
            f"{label}: prosa antiga de roteamento ainda presente",
        )
        self.assertNotIn(
            "### Roteamento de Ferramentas e Busca",
            text,
            f"{label}: prosa antiga de ferramentas ainda presente",
        )
        self.assertIn(
            "arquivo/diretório ou skill", text, f"{label}: §1.1 não cobre skill/dir"
        )

    def test_gemini_protocolo_duvida(self):
        self._assert_protocolo_duvida(self.GEMINI, "Antigravity AGENTS.md")

    def test_opencode_protocolo_duvida(self):
        self._assert_protocolo_duvida(self.OPENCODE, "opencode AGENTS.md")

    def test_opencode_relatorio_resumido_validacao(self):
        """Garante que o AGENTS.md global contém a instrução de relatório resumido."""
        if not self.OPENCODE.is_file():
            self.skipTest("opencode AGENTS.md não acessível no sandbox")
        text = self.OPENCODE.read_text(encoding="utf-8")
        self.assertIn("Relatório Resumido de Validação", text)
        self.assertIn("**Status:**", text)
        self.assertIn("**Tentativas:**", text)
        self.assertIn("**Notas:**", text)
        self.assertNotIn("**Confronto:**", text)

    def test_skill_creator_aponta_protocolo(self):
        skill = (
            Path.home()
            / ".config"
            / "opencode"
            / "skills"
            / "skills-opencode-creator"
            / "SKILL.md"
        )
        self.assertTrue(skill.is_file(), f"skill ausente: {skill}")
        self.assertIn(
            "Protocolo de Dúvida",
            skill.read_text(encoding="utf-8"),
            "skills-opencode-creator sem ponteiro p/ Protocolo de Dúvida",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
