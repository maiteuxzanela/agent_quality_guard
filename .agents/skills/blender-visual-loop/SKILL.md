---
name: blender-visual-loop
description: >-
  Use as the default gate for any delivery that includes a Blender 3D visual
  artifact (render PNG, .blend scene, or bpy script): the Laya visual evaluation
  must reach approved before needle_verify_delivery or /jev-fecha close the
  delivery. Also use when iterating on Blender 3D scenes with visual
  self-confirmation: the LLM orchestrator writes the bpy script, the tool renders
  4 views headless, extracts deterministic bpy facts + CLIP evidence, and runs a
  calibrated chained noul gate (0.78) with focused rewrite instructions on first
  rejection.
---

# blender-visual-loop

## Visão Geral

Ciclo 100% local e gratuito (R$ 0,00) de **autorrevisão visual** para modelagem 3D:

1. A **LLM orquestradora escreve o script bpy** (o Laya é um modelo de decisão
   texto-only — ele classifica, não gera código).
2. A ferramenta executa o script no **Blender headless** com harness fixo
   (4 vistas: perspectiva, frontal, lateral, topo — CYCLES CPU 640×480).
3. Extrai **fatos determinísticos via bpy** (dimensões, posições, nós de
   material, energias de luz, cobertura de quadro, luminância) + **evidência
   CLIP** (pares positivo/negativo, descrição do pedido e fotos de referência).
4. Avalia a **cadeia de noul encadeada** (fail-fast, threshold FIXO 0.78):
   `proporcoes_objeto → posicao_objeto → proporcoes_partes → posicao_partes
   → cores → luzes → texturas → composicao (opt-in) → pedido_objeto (opt-in)
   → referencia_fotos (opt-in)`.
5. Todos passam → `approved`. Primeira reprovação → `rejected` com
   `rewrite_instruction` cirúrgico (corrigir SÓ aquele critério).

Anti premise-gaming: toda premissa é montada por template determinístico de
medições — nunca por redação livre. Circuit breaker: 5 reprovações consecutivas
→ `emergency_release` (governança needle_verify_delivery).

## Fluxo Padrão de Entrega (Obrigatório)

A **avaliação visual pelo Laya é o gate padrão** de qualquer entrega visual 3D
no DeepSeek Harness — render, cena `.blend` ou script bpy. Não é opcional nem
condicionada a pedido explícito:

1. Spec estruturada + script bpy → rodar o loop com `--session` persistente.
2. Repetir o ciclo até `approved` (ou `emergency_release` na 5ª reprovação
   consecutiva — aí a entrega sai com o laudo registrado).
3. Só então fechar a entrega: `needle_verify_delivery` (código) → `/jev-fecha`.
4. O `report_attempt_N.json` da sessão é a evidência visual anexada à entrega.

Anti-pattern: entregar artefato visual 3D sem relatório do loop (`approved` ou
laudo de emergência) = entrega fora do padrão, reprovável.

## Como Executar

```bash
"/home/maiteuxzanela/projeto JEV/.venv/bin/python" \
  "/home/maiteuxzanela/deepseek harness/tools/blender_visual_loop.py" \
  --pedido "mesa com tampo xadrez vermelho e uma perna" \
  --spec "/home/maiteuxzanela/deepseek harness/tests/fixtures/spec.json" \
  --script caminho/do/script_iteracao.py \
  --session /tmp/opencode/minha_sessao
```

- `--session` DEVE ser reusado entre iterações da mesma peça (mantém
  `history.json`, tentativas e evidências).
- `--evaluate-all` avalia todos os critérios mesmo após reprovação (diagnóstico).
- Exit codes: `0` approved, `2` rejected, `3` error, `4` emergency_release.

## Spec Estruturada (JSON)

```json
{
  "main_object": "NomeDoObjeto",
  "dimensions": [2.0, 1.0, 0.5],
  "on_floor": true,
  "floor_z": 0.0,
  "color": "red",
  "texture": "checker",
  "lighting": "bright",
  "composition": false,
  "description": "a table with a red and white checkered top",
  "description_contrast": "a wooden bench, a chair or any unrelated object",
  "reference_images": ["/caminho/foto1.png", "/caminho/foto2.png"],
  "parts": [
    {"name": "Perna", "dimensions": [0.2, 0.2, 1.0], "position": [0.8, 0.0, 0.5]}
  ]
}
```

- Campos ausentes → critério correspondente é pulado (consta em `skipped`).
- `description` ativa `pedido_objeto` (par CLIP texto-imagem: p(descrição
  pedida) vs p(objeto alheio)); `description_contrast` fornece o negativo do
  par (fallback determinístico quando ausente).
- `reference_images` ativa `referencia_fotos` (cosseno CLIP image-image da vista
  vs fotos; bandas calibradas: matching ≥ 0.60, unrelated ≤ 0.55 — alterar exige
  revalidação no golden set, skill `calibracao-score`).
- `composition: true` exige câmera própria no script (a câmera do harness é
  infraestrutura fixa de medição, não julgável).
- `position` das partes é **relativa ao centro do objeto principal**.

## Ciclo da Orquestradora

1. Escrever o script bpy → `--script` (sessão nova ou existente).
2. Ler o JSON: se `rejected`, aplicar APENAS `first_failure.rewrite_instruction`
   e reescrever o script preservando o que já passou.
3. Repetir até `approved` (ou `emergency_release` na 5ª reprovação consecutiva).
4. Com `approved` em mãos, seguir o fechamento padrão (`needle_verify_delivery`
   + `/jev-fecha`) citando o `report_attempt_N.json` como evidência visual.

## Referências

- Fixtures de exemplo: `/home/maiteuxzanela/deepseek harness/tests/fixtures/`
- Testes (reais, sem mocks): `projeto JEV/tests/test_blender_visual_loop.py`
- Calibração dos templates: `deepseek harness/spikes/noul/` (sondas 2–4, matriz 12/12)
