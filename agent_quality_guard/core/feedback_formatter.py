"""Módulo de Formatação de Feedback Estruturado com Diretiva Imperativa de Auto-Correção.

Gera payloads de retorno 'decision: continue' com telemetria detalhada de defeitos
(10 categorias técnicas do Screener Needle 3 + Noul, descrições, blocos do git e
prescrições de ação), forçando o agente a refatorar o código no disco em vez de
pedir desculpas em texto livre.
"""

import logging
from typing import Any, Dict, List, Optional

from agent_quality_guard.core.schemas import DEFECT_DESCRIPTIONS, DEFECT_MEMBERS

logger = logging.getLogger("feedback_formatter")

MANDATORY_ACTION_DIRECTIVE = (
    "🚨 DIRETIVA MANDATÓRIA DE AUTO-CORREÇÃO (NÃO ENCERRE O TURNO):\n"
    "Sua entrega técnica NÃO foi homologada pela governança do projeto.\n"
    "1. NÃO responda à usuária pedindo desculpas ou prometendo correções futuras em texto.\n"
    "2. É OBRIGATÓRIO abrir os arquivos listados e aplicar refatorações reais no disco.\n"
    "3. O mecanismo de avaliação inspeciona alterações físicas no git e só homologará com nota >= 3.40."
)

# Prescrição genérica quando não há categoria (reprovação mono / diagnosis ausente)
GENERIC_PRESCRIPTION = "Aplique refatorações reais de código e testes no disco."

# Prescrições técnicas de ação cirúrgica por categoria de defeito (10 categorias)
DEFECT_ACTION_PRESCRIPTIONS: Dict[str, str] = {
    "logica_ou_regra": "Refine condicionais lógicas, operadores de comparação e consistência de estado do domínio.",
    "robustez_excecoes_recursos": "Adicione tratamento defensivo de exceções com blocos try/finally e proteção a nulos.",
    "seguranca": "Aplique parametrização de consultas, leitura de credenciais via variáveis de ambiente e sanitização de entradas.",
    "qualidade_de_testes": "Implemente testes automatizados de integração cobrindo dependências reais e caminhos de exceção.",
    "contrato_ambiente": "Harmonize assinaturas de funções, anotações de tipo estritas e schemas Pydantic.",
    "performance_escopo_estilo": "Otimize processamento algorítmico e assegure conformidade estrita ao escopo solicitado.",
    "requisito_incompleto_ou_ausente": "Inspecione os requisitos do prompt e implemente todos os comportamentos esperados no disco.",
    "documentacao_configuracao": "Atualize documentação técnica, arquivos de configuração e variáveis de ambiente pertinentes.",
    "tipagem_ausente": "Anote tipos de parâmetros e retorno em toda função pública de produção (mypy estrito).",
    "docstring_ausente": "Escreva docstrings em funções e classes públicas descrevendo contrato, efeitos e exemplos de uso.",
}


def get_defect_prescription(selected_defect: Optional[str]) -> str:
    """Retorna prescrição de ação técnica recomendada para a categoria de defeito com fallback defensivo."""
    try:
        key = str(selected_defect or "").strip().lower()
        return DEFECT_ACTION_PRESCRIPTIONS.get(key, GENERIC_PRESCRIPTION)
    except Exception as e:
        logger.debug(f"Erro ao obter prescrição para {selected_defect}: {e}")
        return GENERIC_PRESCRIPTION


def format_ast_rejection_reason(persona_name: str, violations: List[Dict[str, Any]]) -> str:
    """Formata mensagem de bloqueio determinístico do Needle 3 AST Guardrail."""
    try:
        clean_persona = str(persona_name or "Guardião da Qualidade").strip()
        safe_violations = violations if isinstance(violations, list) else []

        lines: List[str] = [
            f"🚨 [{clean_persona} — Bloqueio Determinístico de AST / Needle 3]:",
            "A entrega foi bloqueada por violações determinísticas de conformidade no código:\n",
        ]
        for v in safe_violations[:6]:
            if not isinstance(v, dict):
                continue
            rule_id = str(v.get("rule_id", "regra_ast"))
            file_path = str(v.get("file_path", "desconhecido"))
            line_no = v.get("line_number", 0)
            message = str(v.get("message", ""))
            lines.append(f"- [{rule_id}] {file_path}:{line_no} — {message}")

        if len(safe_violations) > 6:
            lines.append(f"... e mais {len(safe_violations) - 6} violação(ões) detectada(s).")

        lines.append("\n" + MANDATORY_ACTION_DIRECTIVE)
        return "\n".join(lines)
    except Exception as e:
        logger.error(f"Erro ao formatar mensagem de rejeição de AST: {e}", exc_info=True)
        return f"🚨 [{persona_name}]: Violações de AST detectadas. Corrija o código no disco.\n\n{MANDATORY_ACTION_DIRECTIVE}"


def format_score_rejection_reason(
    persona_name: str,
    res: Dict[str, Any],
    max_attempts: int = 5,
) -> str:
    """Formata mensagem de rejeição de Score com telemetria rica e diretiva de ação."""
    try:
        clean_persona = str(persona_name or "Guardião da Qualidade").strip()
        safe_res = res if isinstance(res, dict) else {}

        raw_score = safe_res.get("score")
        has_score = raw_score is not None
        score = float(raw_score) if has_score else 0.0

        threshold = float(safe_res.get("threshold", 3.40))
        attempts = int(safe_res.get("attempts", 1))

        diag = safe_res.get("diagnosis")
        diag_dict = diag if isinstance(diag, dict) else {}

        selected = str(diag_dict.get("selected", "") or "")
        confidence = float(diag_dict.get("confidence", 0.0))
        description = str(diag_dict.get("description", DEFECT_DESCRIPTIONS.get(selected, "")))
        candidate_defects = diag_dict.get("candidate_defects") or DEFECT_MEMBERS.get(selected, [])

        stages = safe_res.get("stages")
        stages_list = stages if isinstance(stages, list) else []
        failed_stages = [st for st in stages_list if isinstance(st, dict) and not st.get("passed")]

        is_no_change = safe_res.get("error") == "no_change_since_last_rejection"

        lines: List[str] = []
        lines.append(f"🚨 [{clean_persona} — Guardião da Qualidade] (Tentativa {attempts}/{max_attempts}):")

        if is_no_change:
            lines.append(
                "🚫 NENHUMA ALTERAÇÃO DETECTADA: O repositório não sofreu modificações físicas no disco "
                "desde a última reprovação. O motor recusa reavaliações repetidas sem trabalho técnico real.\n"
            )
        elif has_score and score < threshold:
            lines.append(f"A entrega obteve nota de conformidade {score:.2f} < {threshold:.2f} na Camada 3 (Laya RLCD):\n")
        elif has_score:
            lines.append(
                f"A entrega possui blocos reprovados na Camada 3 (Score global {score:.2f}, corte {threshold:.2f}):\n"
            )

        # Telemetria dos blocos reprovados
        if failed_stages:
            lines.append("📌 Blocos que exigem refatoração no disco:")
            for st in failed_stages[:5]:
                fp = str(st.get("file_path", ""))
                part = st.get("part_index", 1)
                total = st.get("total_parts", 1)
                st_score = float(st.get("score", 0.0))
                lines.append(f"  • {fp} (Bloco {part}/{total}): Score {st_score:.2f} < {threshold:.2f}")
            if len(failed_stages) > 5:
                lines.append(f"  • ... e mais {len(failed_stages) - 5} bloco(s) pendente(s).")
            lines.append("")

        # Diagnóstico da categoria de defeito (apenas se disponível)
        if diag_dict:
            lines.append(f"🔍 Diagnóstico Search Guard: '{selected}' (Confiança: {confidence:.1%}) — {description}")
            if candidate_defects:
                lines.append(f"🎯 Focos prioritários de investigação: {', '.join(candidate_defects[:4])}")
            prescription = DEFECT_ACTION_PRESCRIPTIONS.get(selected) or GENERIC_PRESCRIPTION
            lines.append(f"💡 Ação recomendada: {prescription}")

        # Alerta de proximidade do Circuit Breaker
        if attempts == max_attempts - 1:
            lines.append(
                "\n⚠️ ATENÇÃO: Esta é a penúltima tentativa antes do desbloqueio da Saída de Emergência. "
                "Se a próxima tentativa não atingir nota >= 3.40, você deverá emitir Laudo de Inviabilidade Técnica."
            )

        lines.append("\n" + MANDATORY_ACTION_DIRECTIVE)
        return "\n".join(lines)
    except Exception as e:
        logger.error(f"Erro ao formatar mensagem de rejeição de score: {e}", exc_info=True)
        return (
            f"🚨 [{persona_name}]: A entrega técnica não atingiu a conformidade exigida.\n\n"
            f"{MANDATORY_ACTION_DIRECTIVE}"
        )


def format_infrastructure_error_reason(persona_name: str, error_msg: str) -> str:
    """Formata erro de infraestrutura IPC/systemd."""
    try:
        clean_persona = str(persona_name or "Guardião da Qualidade").strip()
        clean_error = str(error_msg or "Falha de comunicação não especificada").strip()
        return (
            f"🚨 [{clean_persona} — Alerta de Infraestrutura]:\n"
            f"A avaliação de conformidade não pôde ser concluída devido a falha no daemon Open JEV:\n\n"
            f"• Erro: {clean_error}\n\n"
            "Ação de recuperação:\n"
            "1. Execute `openjev_ctl.sh status` ou `systemctl --user status openjev.service`.\n"
            "2. Reinicie o daemon se necessário: `openjev_ctl.sh restart`.\n"
            "3. O turno não pode ser encerrado sem homologação de entrega."
        )
    except Exception as e:
        logger.error(f"Erro ao formatar mensagem de infraestrutura: {e}", exc_info=True)
        return f"🚨 [{persona_name}]: Falha de infraestrutura IPC. Verifique o serviço openjev."


def format_circuit_breaker_guidance(persona_name: str, attempts: int, max_attempts: int = 5) -> str:
    """Formata a instrução final quando o Circuit Breaker é acionado."""
    try:
        clean_persona = str(persona_name or "Guardião da Qualidade").strip()
        safe_attempts = int(attempts) if attempts is not None else 5
        safe_max_attempts = int(max_attempts) if max_attempts is not None else 5
        return (
            f"🚨 [{clean_persona} — Saída de Emergência Desbloqueada] (Tentativa {safe_attempts}/{safe_max_attempts}):\n"
            f"O limite de {safe_max_attempts} tentativas consecutivas de auto-correção foi atingido sem que o código "
            "atingisse a nota de conformidade >= 3.40.\n\n"
            "O agente está expressamente AUTORIZADO a emitir o LAUDO DE INVIABILIDADE TÉCNICA, contendo:\n"
            "1. Diagnóstico preciso da causa-raiz do bloqueio.\n"
            "2. Histórico das alterações e testes tentados no disco.\n"
            "3. Alternativas arquiteturais e caminhos viáveis para a usuária.\n"
        )
    except Exception as e:
        logger.error(f"Erro ao formatar instrução de circuit breaker: {e}", exc_info=True)
        return f"🚨 [{persona_name}]: Limite de {max_attempts} tentativas atingido. Emita Laudo de Inviabilidade Técnica."
