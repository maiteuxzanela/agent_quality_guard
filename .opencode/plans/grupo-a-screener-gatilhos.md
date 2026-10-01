# Plano — Grupo A: gatilhos do Screener (Camada 2)

**Aprovado pela usuária:** "Aprovar e executar". Bloqueio: plan mode ativo (edit negado) — executar assim que liberado.

## Objetivo
13 novos gatilhos no `src/memory/needle_defect_screener.py` mapeados às **10 categorias existentes** (zero impacto em `DEFECT_OPTIONS`/calibração do choice), + enriquecimento dos fixtures positivos do golden, para subir as notas dos testes positivos via loop de auto-correção.

## Passo 1 — Gatilhos (needle_defect_screener.py)

| # | Detecção (anti-FP) | Categoria |
|---|---|---|
| A1 | Arquivo de teste sem `pytest.raises`/`assertRaises` em nenhum símbolo — 1 suspeita por arquivo, em `screen_workspace_symbols` (representante = menor `start_line`; não duplicar se já houver suspeita) | `qualidade_de_testes` |
| A2 | Docstring de **função** (não-dunder, não-teste) sem `>>>`/`Exemplo:` (re `_DOC_EXAMPLE_RE`); classes NÃO (exceções nunca têm doctest) | `docstring_ausente` (análise) |
| A3 | `f = open(...)` com `f` nunca `.close()`/vinculado a `with` — coletar `_open_assigned` (visit_Assign/AnnAssign), `_closed_names` (visit_Call `close`), `_with_names` (visit_With); checar pós-visit | `robustez_excecoes_recursos` |
| A4 | Handler com corpo de 1 instrução = `return` sem valor OU `continue` (sem log/re-raise — corpo tem 1 stmt) | `robustez_excecoes_recursos` |
| A5 | Padrão mutável (`List/Dict/Set/Comprehensions`) em `defaults`/`kw_defaults` — qualquer arquivo | `logica_ou_regra` |
| A6 | `visit_Global` fora de teste | `logica_ou_regra` |
| A7 | `visit_AugAssign`: `+=` com `Constant(str)`/`JoinedStr` com `_loop_depth > 0` | `performance_escopo_estilo` |
| A8 | `visit_Subscript`: `value.attr == "environ"` fora de teste | `documentacao_configuracao` |
| A9 | `visit_Constant` str casando `_PATH_URL_RE`, **não** docstring (marcar ids em `visit_Expr`), fora de teste | `documentacao_configuracao` |
| A10 | Corpo de `def` = só `...`/`pass` (após remover docstring; corpo só-docstring também) — fora de teste/dunder, exceto decorators `abstractmethod/property/setter/getter/overload` | `requisito_incompleto_ou_ausente` |
| A11 | `open(<Name>)` onde Name é parâmetro com anotação contendo `str` (`ast.unparse` contains) — `_str_params` salvo/restaurado em visit_FunctionDef | `seguranca` |
| A12 | Método `test_*` em classe (`_class_depth`) sem `Assert` e sem call `assert*` (`_has_assertions`) — **só em arquivo de teste** | `qualidade_de_testes` |
| A13 | `print`/`breakpoint`/`set_trace` fora de teste e fora de bloco `if __name__ == "__main__"` (`_in_main_guard` em visit_If com Compare `__name__`) | `performance_escopo_estilo` (lint) |

Infra: `import re` no topo + `_PATH_URL_RE`/`_DOC_EXAMPLE_RE`/`_TODO_RE`; estado novo em `__init__`; `_class_depth` em visit_ClassDef; dedup existente (1 suspeita/categoria/símbolo) absorve colisões.

## Passo 2 — Testes
`tests/test_needle_noul_validation.py`: par positivo+negativo por gatilho (AST real, sem mock).

## Passo 3 — Fire-map de pré-voo
Rodar Screener sobre: (a) fixtures golden (CASE_FILES/ORDER/STABILITY/BLIND/NEG), (b) diff real do repo (arquivos `M`).
Esperado golden: A1 em add×3+documentacao+refatoracao; A2 em add(blind/calib)+refatoracao; zero em order_good/sub_banda/stability_mock.
Qualquer outro disparo → decidir com a usuária antes de prosseguir.

## Passo 4 — Enriquecimento de fixtures + premissas
- Atingidos por A1: 1 teste `pytest.raises` real (ex.: `add(1,"x")`→TypeError; `_validate("x",...)`→TypeError).
- Atingidos por A2: exemplo `>>>` nas docstrings.
- Atualizar `Resultado Executado` das premissas afetadas.
- **Sentinela sub_banda:** A2 dispara em `take_last` → adicionar doctest e RE-MEDIR; se ≥3.20, alertar usuária (não mascarar).
- Se A1 mudar diagnosis de `neg_semantico` → checar asserções de `TestGoldenSetLayaSemantico` (fixtures negativos NÃO podem virar positivos).
- Se A1 disparar em arquivos de teste do repo no diff do repo (homologação): adicionar testes de erro GENUÍNOS onde o SUT realmente levanta; sem fixture artificial.

## Passo 5 — Golden harness
Reexecutar `/tmp/opencode/golden_reeval.py` → `golden_band2.json`; diff vs `golden_band.json`; positivos ≥3.40 ou banda sem suspeita; sentinela <3.20; negativos inalterados.

## Passo 6 — Suíte completa em lotes (32 arquivos).

## Passo 7 — Homologação (premissa factual ≤1500 chars) + relatório (Status/Tentativas/Notas) + commit pendente (pergunta à usuária).

## Riscos
- `qualidade_de_testes` é dura: A1 confirmada reprova até score ≥3.40 → fixtures reforçados no passo 4.
- Banda exige zero suspeitas: qualquer disparo novo em arquivo de teste do repo bloqueia homologação na banda.
- +1 inferência Noul por suspeita (latência).
- Não duplicar regras do guard AST (SQL f-string, eval, secrets, verify=False, except:pass, tautologia top-level).
