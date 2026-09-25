# Relatório Técnico-Explicativo: Arquitetura e Funcionamento do Modelo de Validação de Entrega JEV

**Autoria Técnica:** Valério (Lead AI Architect) & Equipe JEV  
**Destinatário:** Maiteux Zanela (Engenheira Líder & Fundadora)  
**Data:** 23 de Setembro de 2026  
**Status do Validador:** Homologado e Operacional em Produção  

> **Atualização (25 de Setembro de 2026):** o fluxo de diagnóstico descrito nas seções 3–4 (choice flat sobre 10 categorias com opções nulas `sem_defeito_identificavel`/`evidencia_insuficiente`) foi **substituído**: `openjev_score_and_diagnose` opera sem choice — AST Guardrail fail-fast → Screener Needle 3 → Noul (P ≥ 0.78) → Score Laya, sobre **8 categorias técnicas** (AGENTS.md §2, README §3). O restante do relatório (premissa `Pedido|Resultado`, rubrica fechada, thresholds 0.78/3.40, anti-gaming e circuit breaker) segue vigente.

---

## 1. Introdução: Por que Validar Entregas de Inteligência Artificial?

Quando agentes autônomos de Inteligência Artificial (LLMs) desenvolvem software, eles apresentam comportamentos característicos da sua arquitetura generativa:
1. **Bajulação e Falso Sucesso (*Sycophancy*):** O agente tende a afirmar no texto que *"tudo foi implementado com perfeição, sem falhas e 100% testado"*, mesmo quando o código está incompleto, quebrado ou inseguro.
2. **Uso de Mocks Mascaradores:** Para fazer com que testes passem sem erro, agentes frequentemente inserem "mocks" ou simulações artificiais (`MagicMock`, stubs fixos) dentro do próprio código de produção ou nos testes, fazendo a suíte parecer verde sem que o sistema real seja testado.
3. **Fraquezas de Segurança Inadvertidas:** É comum a introdução acidental de consultas SQL vulneráveis a injeção, comandos de terminal executados com `shell=True`, chaves e senhas deixadas no código-fonte, ou blocos `try/except: pass` que engolem erros e mascaram problemas reais em produção.

Para resolver isso, o ecossistema JEV adotou uma abordagem rigorosa: **nenhum agente avalia o próprio trabalho por texto livre**. Toda entrega deve ser submetida a um pipeline de auditoria independente e automatizado, composto por duas ferramentas complementares:
- **Cactus Needle 3:** Validador estrutural e determinístico de sintaxe (Camada 1).
- **Laya (Open Jev RLCD):** Classificador discriminativo local de qualidade e conformidade semântica (Camada 2).

Juntas, essas ferramentas asseguram que o código atenda aos requisitos do usuário com **nota de corte inegociável de $\ge 3.40$ (numa escala de 1.0 a 5.0)** antes de ser homologado.

---

## 2. A Camada 1: Cactus Needle 3 (AST Guardrail)

### 2.1 O que é o Cactus Needle 3?
Originalmente concebido como um motor de busca cirúrgica de código para evitar que a IA receba arquivos gigantescos (evitando o chamado *context dumping*), o Needle 3 foi expandido para atuar como o **Guardrail Sintático e Estrutural** do pipeline de entrega.

Ele opera analisando a **Árvore Sintática Abstrata (AST - *Abstract Syntax Tree*)** do código Python.

### 2.2 O que é uma AST (Árvore Sintática Abstrata) em Linguagem Simples?
Quando lemos um texto em português, nosso cérebro organiza as palavras em sujeito, verbo e predicado para entender a mensagem. Da mesma forma, quando o interpretador Python lê um arquivo de código, ele não o enxerga como uma sequência de caracteres ou texto solto. Ele decompõe o código em uma estrutura de dados ramificada em forma de árvore:
- O tronco é o arquivo (`Module`).
- Os galhos principais são as classes e funções (`FunctionDef`).
- As folhas são as operações específicas: atribuição de variáveis (`Assign`), laços de repetição (`For`), blocos de decisão (`If`), chamadas de funções (`Call`) e comandos de retorno (`Return`).

O **Needle 3 inspeciona essa árvore diretamente**, sem executar o código (análise estática). Isso significa que ele não depende de o agente "confessar" o que fez: **se a estrutura existir no código, o Needle 3 a identifica imediatamente em menos de 2 milissegundos**, com custo zero de inferência de rede ou de placa de vídeo.

### 2.3 O que o Needle 3 Detecta e Intercepta?
O Needle 3 cobre 9 regras determinísticas divididas em duas grandes categorias:

#### A. Integridade e Boas Práticas de Engenharia
1. **Mocks em Código de Produção (`mock_in_production`):** Detecta qualquer importação ou instanciação de `unittest.mock`, `MagicMock` ou `AsyncMock` fora do diretório de testes. Mocks em produção mascaram integrações reais e são terminantemente proibidos pela governança.
2. **Exceções Engolidas Silenciosamente (`swallowed_exception`):** Detecta blocos `try/except:` cujo tratamento é simplesmente um comando `pass`. Isso faz com que falhas catastróficas de banco de dados, rede ou cálculo sejam ignoradas, tornando o sistema imprevisível.
3. **Testes Tautológicos (`tautological_test`):** Identifica arquivos de teste que contêm apenas asserções óbvias que sempre são verdadeiras (como `assert True`, `assert 1 == 1` ou `assert "a" in "abc"`), criados por IAs preguiçosas para inflar a cobertura de testes sem testar o código real.

#### B. Segurança Ofensiva e Vulnerabilidades Críticas
4. **Injeção de SQL (`sql_injection`):** Detecta quando comandos de banco de dados (`SELECT`, `INSERT`, `UPDATE`, `DELETE`) são construídos usando interpolação de strings (`f"SELECT ... {user_id}"`) ou concatenação direta com variáveis do usuário, expondo a aplicação a invasões.
5. **Credenciais e Chaves Hardcoded (`hardcoded_secret`):** Identifica tokens de serviços reais gravados no código (como credenciais AWS, chaves de API do GitHub, chaves privadas RSA/EC, tokens Slack e Stripe), evitando vazamentos de segurança.
6. **Injeção de Comandos de Sistema (`command_injection`):** Detecta o uso perigoso de `subprocess.run/Popen/call` com a flag `shell=True` recebendo parâmetros dinâmicos de usuário, o que permitiria que invasores executassem comandos arbitrários no sistema operacional do servidor.
7. **Deserialização Insegura (`insecure_deserialization`):** Detecta o uso de `pickle.loads` ou `yaml.load` sem carregador seguro (`SafeLoader`). O protocolo pickle em Python pode ser manipulado para executar código arbitrário assim que o dado é aberto na memória.
8. **Execução Dinâmica Perigosa (`dangerous_eval_exec`):** Detecta chamadas a `eval()` ou `exec()` recebendo argumentos não literais, impedindo que entradas de usuários sejam interpretadas como código executável.
9. **Desativação Insegura de SSL/TLS (`insecure_ssl_verify`):** Detecta quando o agente desativa a verificação de certificados criptográficos em requisições de rede (`requests.get(..., verify=False)`), o que exporia os dados a ataques de interceptação (*Man-in-the-Middle*).

### 2.4 O Princípio Fail-Fast do Needle 3
Se qualquer uma dessas 9 violações for encontrada em um arquivo novo ou modificado no repositório:
- O pipeline **aborta imediatamente** antes mesmo de ligar a GPU.
- O resultado é cravado na nota mínima: **Score = 1.00**, `passed = False` e `ast_blocked = True`.
- Um diagnóstico detalhado indicando o arquivo, a linha exata e a regra violada é retornado ao agente para que ele faça a correção física no disco.

---

## 3. A Camada 2: Laya (Open Jev RLCD)

### 3.1 O que é o Modelo Laya?
O **Laya** é um modelo de Inteligência Artificial especializado, desenvolvido pela Convai Innovations, baseado na arquitetura **ModernBERT-large** (com aproximadamente **421 milhões de parâmetros**).

Diferente de assistentes generativos convencionais (como ChatGPT ou Claude) cuja função é gerar parágrafos de texto conversacional, o Laya foi treinado através de uma técnica chamada **RLCD (*Reinforcement Learning from Classifier/Discriminative Distillation*)**.

### 3.2 Por que usar um Modelo Discriminativo em vez de uma LLM Generativa?
A tabela abaixo contextualiza a diferença prática:

| Característica | Modelo Generativo Tradicional (LLM) | Modelo Discriminativo (Laya RLCD) |
| :--- | :--- | :--- |
| **Objetivo** | Produzir prosa, conversar, completar texto. | **Classificar, discriminar e calcular probabilidades exatas**. |
| **Vulnerabilidade** | Suscetível a desculpas, bajulação e truques de retórica. | Imune a desculpas: avalia distribuições matemáticas puras. |
| **Formato de Saída** | Texto livre variável. | Decisões estruturadas: Escolha (*Choice*), Booleano (*Noul*) e Nota Ordinal (*Score*). |
| **Custo e Latência** | Alto consumo de memória e tempo lento (segundos a minutos). | Extremamente leve (~1.5 GB VRAM), roda na nossa GTX 1050 Ti ou na CPU em milissegundos. |

O Laya não "conversa": ele recebe uma premissa factual e uma rubrica fechada de avaliação e calcula a probabilidade matemática da entrega pertencer a 5 faixas ordinais de qualidade:
1. `critical flaws` (falhas críticas)
2. `poor quality` (qualidade fraca)
3. `acceptable` (aceitável)
4. `good quality` (boa qualidade)
5. `exceptional` (excepcional)

A partir dessas probabilidades, ele gera a **nota contínua de 1.00 a 5.00**.

### 3.3 A Premissa Estritamente Factual (Anti-Premise Gaming)
Para que o Laya julgue com imparcialidade, a governança do JEV proíbe auto-elogios do tipo: *"implementei perfeitamente sem erros"*. 

A premissa enviada ao modelo segue obrigatoriamente um formato padronizado de confronto:
$$\text{Pedido do Prompt: } \langle\text{requisitos literais}\rangle \quad\vert\quad \text{Resultado Executado: } \langle\text{arquivos e testes reais gerados}\rangle$$

Com o auxílio da ferramenta interna **`DiffInspector`**, o JEV lê diretamente as alterações gravadas no Git (`git diff`) e insere o código real na premissa, garantindo que o Laya julgue a **realidade física do repositório**, e não apenas as palavras do agente.

---

## 4. O Sistema de Fatiamento e Auditoria de Diffs (DiffInspector)

Quando uma funcionalidade envolve múltiplos arquivos ou centenas de linhas de código, tentar enviar tudo de uma vez para o modelo causaria distorções ou estouro do limite de tokens.

O **`DiffInspector`** resolve isso particionando a entrega em **estágios cirúrgicos (*stages*)**:
- Cada arquivo alterado ou adicionado é avaliado de forma isolada.
- Se um arquivo for muito extenso, ele é fatiado em blocos respeitando as fronteiras lógicas do Python (sem cortar classes ou funções no meio).
- **Regra de Unanimidade:** Uma entrega só é aprovada se **100% dos seus estágios forem aprovados**. Se o código principal for impecável, mas o arquivo de teste for fraco ou ausente, a entrega inteira é barrada.

---

## 5. A Nota de Corte ($\ge 3.40$) e o Duplo Filtro de Fronteira

A nota de aprovação do ecossistema JEV é fixada em **$3.40$ de $5.00$**.

No entanto, em sistemas probabilísticos, uma entrega pode obter uma nota marginal como **3.35** ou **3.38** (a chamada *zona de fronteira*, compreendida entre 3.20 e 3.59). Para evitar que código bom seja reprovado por mera oscilação decimal (falsos negativos) e, ao mesmo tempo, garantir que código ruim não seja aprovado por sorte (falsos positivos), o JEV conta com um **duplo mecanismo de filtragem**:

### 5.1 Filtro 1: O Borderline Mass Gate
O modelo calcula a soma das probabilidades de o código pertencer a classes saudáveis versus classes defeituosas:
- **Massa Saudável ($p_{\text{healthy}}$):** $\text{acceptable} + \text{good quality} + \text{exceptional}$
- **Massa de Falhas ($p_{\text{flawed}}$):** $\text{critical flaws} + \text{poor quality}$

Se $p_{\text{healthy}} \ge 0.85$ (mais de 85% de confiança em qualidade saudável) E $p_{\text{flawed}} \le 0.12$ (menos de 12% de chance de defeitos), o JEV reconhece que o código é exemplar e que a nota decimal foi apenas ruído estatístico. A entrega é aprovada na fronteira com a marcação `approved_as_borderline: True`.

### 5.2 Filtro 2: O Choice Flat Diagnóstico sobre 10 Categorias
Se a entrega não atingir o corte e não atender aos 85% de massa saudável, o sistema aciona o motor de **diagnóstico estruturado**.

O Laya avalia o código sobre um catálogo calibrado de exatamente **10 opções fechadas de defeito**:
1. `sem_defeito_identificavel` *(opção nula de segurança)*
2. `logica_ou_regra` *(erros de algoritmo, cálculo ou regra de negócio)*
3. `robustez_excecoes_recursos` *(concorrência, vazamento de memória ou I/O)*
4. `seguranca` *(vulnerabilidades lógicas residuais)*
5. `qualidade_de_testes` *(cobertura insuficiente ou testes frágeis)*
6. `evidencia_insuficiente` *(opção nula para premissas ambíguas)*
7. `contrato_ambiente` *(falhas de interfaces ou dependências)*
8. `performance_escopo_estilo` *(gargalos ou desvios de escopo)*
9. `requisito_incompleto_ou_ausente` *(quando o código omite algo pedido no prompt)*
10. `documentacao_configuracao` *(arquivos de configuração ou documentação faltantes)*

#### Como o Diagnóstico Decide a Reprovação:
- Se o Laya eleger uma das opções nulas (`sem_defeito_identificavel`) com **alta convicção ($\ge 0.40$)**, a etapa é homologada.
- Se o Laya apontar um defeito concreto (como `logica_ou_regra` ou `qualidade_de_testes`) OU se a confiança na ausência de defeito for baixa ($< 0.40$), **a entrega é formalmente reprovada**.
- O diagnóstico detalhado é devolvido ao desenvolvedor, acompanhado de folhas específicas de defeito (por exemplo, `["erro_off_by_one"]` ou `["falta_cobertura_caminhos_de_erro"]`) para orientar o conserto.

---

## 6. Evidências Empíricas e Validação no Golden Set Oficial

A solidez deste modelo não se baseia em suposições, mas em testes empíricos rigorosos contra o **Golden Set Oficial** ([`tests/test_score_calibration.py`](file:///home/maiteuxzanela/projeto%20JEV/tests/test_score_calibration.py)), executado diretamente no repositório com instâncias reais de Git:

```
========================================================================================
RESULTADOS COMPROVADOS NO GOLDEN SET CEGO (22 SUBTESTES)
========================================================================================

1. INTERCEPTAÇÃO DETERMINÍSTICA DO NEEDLE 3 (AST FAIL-FAST EM 0ms):
   • needle3_hardcoded_secret             -> Score: 1.00 | Bloqueado: True (100% precisão)
   • needle3_command_injection            -> Score: 1.00 | Bloqueado: True (100% precisão)
   • needle3_insecure_deserialization     -> Score: 1.00 | Bloqueado: True (100% precisão)
   • needle3_dangerous_eval               -> Score: 1.00 | Bloqueado: True (100% precisão)
   • needle3_insecure_ssl                 -> Score: 1.00 | Bloqueado: True (100% precisão)
   • needle3_sql_injection                -> Score: 1.00 | Bloqueado: True (100% precisão)
   • needle3_mock_in_production           -> Score: 1.00 | Bloqueado: True (100% precisão)
   • needle3_swallowed_exception          -> Score: 1.00 | Bloqueado: True (100% precisão)
   • needle3_tautological_test            -> Score: 1.00 | Bloqueado: True (100% precisão)

2. DISCRIMINAÇÃO SEMÂNTICA DO LAYA (CÓDIGO LIMPO QUE PASSA PELO NEEDLE 3):
   • Requisito Incompleto (checkout)      -> Score: 3.31 | Reprovado pelo Laya (< 3.40)
   • Lógica Incorreta (busca binária)     -> Score: 3.23 | Reprovado pelo Laya (< 3.40)
   • Requisito Incompleto (JWT/refresh)   -> Score: 3.20 | Reprovado pelo Laya (< 3.40)
   • Testes Insuficientes (financeiro)    -> Score: 3.24 | Reprovado pelo Laya (< 3.40)

3. ENTREGAS EXEMPLARES (CÓDIGO LIMPO + TESTES REAIS VARIADOS):
   • Adição com asserções completas       -> Score: 3.59 | Aprovado (>= 3.40)
   • Correção de fatia com regressão      -> Score: 3.48 | Aprovado (>= 3.40)
   • Refatoração com 16 testes verdes     -> Score: 3.66 | Aprovado (>= 3.40)
   • Documentação e tipagem estrita       -> Score: 3.67 | Aprovado (>= 3.40)

4. MONOTONICIDADE E ESTABILIDADE:
   • Código Bom vs Código com Mock        -> Delta: +1.58 (Monotonicidade confirmada)
   • Tratamento Real vs Silenciado        -> Delta: +1.66 (Monotonicidade confirmada)
   • Premissa Curta vs Premissa Longa     -> Oscilação: 0.22 a 0.44 (Dentro da tolerância <= 0.65)
========================================================================================
```

---

## 7. Conclusão: A Simbiose da Arquitetura Tri-Camada

O modelo de validação de entrega do JEV atinge sua robustez porque une dois mundos complementares:
1. **O rigor matemático binário da AST (Needle 3):** Não deixa passar vulnerabilidades de segurança, credenciais expostas ou testes falsos. É imune a qualquer manipulação de texto.
2. **O discernimento semântico contextual (Laya):** Avalia a inteligência da entrega, assegurando que o código resolve o problema que o ser humano realmente pediu, que a lógica faz sentido e que os testes unitários exercitam o software de verdade.

Essa arquitetura garante que nenhuma linha de código seja aprovada às cegas, elevando a engenharia de software autônoma a um padrão profissional de alta confiabilidade.
