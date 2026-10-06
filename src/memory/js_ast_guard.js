#!/usr/bin/env node
/**
 * Needle 3 JavaScript / TypeScript AST Guard
 * Análise sintática semântica real para segurança, integridade e higiene de código.
 */

const fs = require('fs');
const path = require('path');

// Carrega parser (@babel/parser preferencial para TS+JSX, ou acorn)
let parseCode = null;
try {
  const babelParser = require(path.join(__dirname, 'vendor', 'babel_parser.js'));
  parseCode = (code) => babelParser.parse(code, {
    sourceType: 'unambiguous',
    allowReturnOutsideFunction: true,
    allowAwaitOutsideFunction: true,
    allowImportExportEverywhere: true,
    errorRecovery: true,
    plugins: [
      'jsx',
      'typescript',
      'classProperties',
      'asyncGenerators',
      'dynamicImport',
      'objectRestSpread',
      'optionalChaining',
      'nullishCoalescingOperator',
      'topLevelAwait'
    ]
  });
} catch (e) {
  try {
    const acorn = require(path.join(__dirname, 'vendor', 'acorn.js'));
    parseCode = (code) => acorn.parse(code, {
      ecmaVersion: 'latest',
      sourceType: 'module',
      locations: true
    });
  } catch (err) {
    console.error(JSON.stringify({ error: `Nenhum parser AST JS disponível: ${err.message}` }));
    process.exit(1);
  }
}

const RULE_SUGGESTIONS = {
  SECURITY_EVAL: "Remova o uso de eval(). Utilize JSON.parse(), despachos por dicionário ou funções puras.",
  SECURITY_EXEC: "Evite o construtor new Function(). Isole a lógica dinâmica em funções declaradas ou analisadores formais.",
  HARDCODED_SECRET: "Mova a chave privada/segredo para variáveis de ambiente (.env) ou adicione '// needle-ignore: HARDCODED_SECRET (<motivo>)' se for chave pública de client-side (ex: Firebase Client API Key).",
  XSS_INNER_HTML: "Substitua por element.textContent ou document.createElement(); se HTML for mandatório, envolva as interpolações com escapeHtml(...) ou DOMPurify.sanitize().",
  JS_PROD_MOCK: "Remova jest.mock(), jest.fn() ou sinon de arquivos sob src/. Mocks pertencem exclusivamente a arquivos de teste em __tests__/ ou tests/.",
  EMPTY_CATCH: "Não engula exceções silenciosamente. Registre com console.warn/logger, re-lance o erro com throw/raise ou trate a condição explicitamente.",
  CODE_SMELL_CONSOLE: "Remova chamadas residuais de console.log() antes de enviar para produção, ou utilize console.warn/error estruturado se for um alerta de runtime legítimo.",
  JEZ_ANTI_EMOJI: "Remova o emoji. A identidade visual da marca JEZ exige tipografia limpa e ícones SVG minimalistas dedicados."
};

const SANITIZERS = new Set([
  'escapeHtml',
  'escape_html',
  'sanitize',
  'sanitizeHtml',
  'DOMPurify',
  'encodeURIComponent',
  'encodeURI',
  'cleanHtml'
]);

function isSanitizerCall(node) {
  if (!node || node.type !== 'CallExpression') return false;
  const callee = node.callee;
  if (callee.type === 'Identifier' && SANITIZERS.has(callee.name)) return true;
  if (callee.type === 'MemberExpression') {
    const prop = callee.property;
    const obj = callee.object;
    if (obj.type === 'Identifier' && SANITIZERS.has(obj.name)) return true;
    if (prop.type === 'Identifier' && SANITIZERS.has(prop.name)) return true;
  }
  return false;
}

function isSafeExpression(node) {
  if (!node) return false;
  if (node.type === 'StringLiteral' || node.type === 'NumericLiteral' || node.type === 'BooleanLiteral' || node.type === 'Literal') {
    return true;
  }
  if (isSanitizerCall(node)) {
    return true;
  }
  if (node.type === 'BinaryExpression' && node.operator === '+') {
    return isSafeExpression(node.left) && isSafeExpression(node.right);
  }
  if (node.type === 'ConditionalExpression') {
    return isSafeExpression(node.consequent) && isSafeExpression(node.alternate);
  }
  return false;
}

function isTemplateLiteralSafe(tplNode) {
  if (!tplNode.expressions || tplNode.expressions.length === 0) {
    return true;
  }
  return tplNode.expressions.every(isSafeExpression);
}

function getLineText(lines, lineNum) {
  if (lineNum >= 1 && lineNum <= lines.length) {
    return lines[lineNum - 1];
  }
  return '';
}

function getFileSuppressedRules(lines) {
  const suppressed = new Set();
  const filePat = /(?:\/\/|\/\*|#)\s*needle-ignore-file:\s*([A-Za-z0-9_,\s]+)/i;
  const allPat = /(?:\/\/|\/\*|#)\s*needle-ignore-all\b/i;

  const header = lines.slice(0, 15);
  for (const line of header) {
    const trimmed = line.trim();
    if (allPat.test(trimmed)) {
      suppressed.add('ALL');
      return suppressed;
    }
    const m = trimmed.match(filePat);
    if (m) {
      const parts = m[1].split(',').map(s => s.trim().toUpperCase());
      for (const p of parts) {
        if (p) suppressed.add(p);
      }
    }
  }
  return suppressed;
}

function isSuppressed(ruleId, lineNum, lines, fileSuppressed) {
  const target = ruleId.toUpperCase();
  if (fileSuppressed && (fileSuppressed.has('ALL') || fileSuppressed.has(target))) {
    return true;
  }
  const inlinePat = /(?:\/\/|\/\*|#)\s*needle-ignore:\s*([A-Za-z0-9_,\s]+)/i;
  const nextLinePat = /(?:\/\/|\/\*|#)\s*needle-ignore-next-line:\s*([A-Za-z0-9_,\s]+)/i;

  // 1. Linha atual
  const currentLine = getLineText(lines, lineNum);
  const mCurr = currentLine.match(inlinePat);
  if (mCurr) {
    const rules = mCurr[1].split(',').map(s => s.trim().toUpperCase());
    if (rules.includes(target) || rules.includes('ALL')) return true;
  }

  // 2. Linha anterior
  if (lineNum > 1) {
    const prevLine = getLineText(lines, lineNum - 1).trim();
    const mNext = prevLine.match(nextLinePat);
    if (mNext) {
      const rules = mNext[1].split(',').map(s => s.trim().toUpperCase());
      if (rules.includes(target) || rules.includes('ALL')) return true;
    }
    if (prevLine.startsWith('//') || prevLine.startsWith('/*') || prevLine.startsWith('#') || prevLine.startsWith('*')) {
      const mInlinePrev = prevLine.match(inlinePat);
      if (mInlinePrev) {
        const rules = mInlinePrev[1].split(',').map(s => s.trim().toUpperCase());
        if (rules.includes(target) || rules.includes('ALL')) return true;
      }
    }
  }

  return false;
}

function walk(node, callback) {
  if (!node || typeof node !== 'object') return;
  callback(node);
  for (const key of Object.keys(node)) {
    if (key === 'loc' || key === 'tokens' || key === 'comments') continue;
    const child = node[key];
    if (Array.isArray(child)) {
      for (const item of child) {
        if (item && typeof item.type === 'string') {
          walk(item, callback);
        }
      }
    } else if (child && typeof child.type === 'string') {
      walk(child, callback);
    }
  }
}

function analyzeFile(filePath, code, options = {}) {
  const lines = code.split('\n');
  const violations = [];
  const suppressed = [];
  const fileSuppressed = getFileSuppressedRules(lines);

  function addViolation(ruleId, category, lineNum, message, snippet) {
    if (isSuppressed(ruleId, lineNum, lines, fileSuppressed)) {
      suppressed.push({ rule_id: ruleId, line_number: lineNum, file_path: filePath });
      return;
    }
    violations.push({
      rule_id: ruleId,
      category: category,
      file_path: filePath,
      line_number: lineNum,
      code_snippet: snippet || getLineText(lines, lineNum).trim().slice(0, 120),
      message: message,
      suggestion: RULE_SUGGESTIONS[ruleId] || null
    });
  }

  // 1. Análise Léxica por Linha (Segredos e Emojis)
  const SECRET_REGEX = /AIzaSy[0-9A-Za-z-_]{33}/;
  const EMOJI_REGEX = /[\uD83C-\uDBFF\uDC00-\uDFFF]/;

  for (let idx = 0; idx < lines.length; idx++) {
    const lineNum = idx + 1;
    const line = lines[idx];

    if (SECRET_REGEX.test(line)) {
      addViolation(
        'HARDCODED_SECRET',
        'security',
        lineNum,
        'Chave de API/segredo hardcoded detectada em arquivo de produção.',
        line.trim().slice(0, 100)
      );
    }

    if (options.checkEmoji && EMOJI_REGEX.test(line)) {
      addViolation(
        'JEZ_ANTI_EMOJI',
        'brand_invariable',
        lineNum,
        'Emoji detectado no código/template em violação à identidade da marca JEZ.',
        line.trim().slice(0, 100)
      );
    }
  }

  // 2. Análise Semântica via AST
  let ast = null;
  try {
    ast = parseCode(code);
  } catch (err) {
    return {
      violations,
      suppressed,
      parse_error: err.message
    };
  }

  walk(ast, (node) => {
    const lineNum = node.loc ? node.loc.start.line : 1;

    // A. innerHTML assignment
    if (node.type === 'AssignmentExpression') {
      const left = node.left;
      if (left && left.type === 'MemberExpression') {
        const prop = left.property;
        const isInnerHtml = (prop && prop.name === 'innerHTML') || (prop && prop.value === 'innerHTML');
        if (isInnerHtml) {
          const right = node.right;
          if (right.type === 'TemplateLiteral') {
            if (!isTemplateLiteralSafe(right)) {
              addViolation(
                'XSS_INNER_HTML',
                'security',
                lineNum,
                'Atribuição insegura de innerHTML: interpolação em template literal sem sanitização via escapeHtml/DOMPurify.',
                getLineText(lines, lineNum).trim().slice(0, 120)
              );
            }
          } else if (right.type === 'BinaryExpression' && right.operator === '+') {
            if (!isSafeExpression(right)) {
              addViolation(
                'XSS_INNER_HTML',
                'security',
                lineNum,
                'Atribuição insegura de innerHTML: concatenação de strings sem sanitização com escapeHtml().',
                getLineText(lines, lineNum).trim().slice(0, 120)
              );
            }
          } else if (right.type === 'Identifier') {
            if (!isSuppressed('XSS_INNER_HTML', lineNum, lines)) {
              addViolation(
                'XSS_INNER_HTML',
                'security',
                lineNum,
                `Atribuição de innerHTML a partir da variável '${right.name}'. Certifique-se de que o conteúdo foi higienizado.`,
                getLineText(lines, lineNum).trim().slice(0, 120)
              );
            }
          }
        }
      }
    }

    // B. eval() call
    if (node.type === 'CallExpression') {
      const callee = node.callee;
      if (callee.type === 'Identifier' && callee.name === 'eval') {
        addViolation(
          'SECURITY_EVAL',
          'security',
          lineNum,
          'Uso proibido de eval() em código de produção.',
          getLineText(lines, lineNum).trim().slice(0, 100)
        );
      }

      // C. Anti-mock em produção
      if (callee.type === 'MemberExpression') {
        const obj = callee.object;
        const prop = callee.property;
        if (obj && obj.name === 'jest' && (prop.name === 'mock' || prop.name === 'fn')) {
          addViolation(
            'JS_PROD_MOCK',
            'anti_mock_producao',
            lineNum,
            `Chamada de jest.${prop.name}() detectada em arquivo de produção.`,
            getLineText(lines, lineNum).trim().slice(0, 100)
          );
        }
        if (obj && obj.name === 'sinon' && prop.name === 'stub') {
          addViolation(
            'JS_PROD_MOCK',
            'anti_mock_producao',
            lineNum,
            'Chamada de sinon.stub() detectada em arquivo de produção.',
            getLineText(lines, lineNum).trim().slice(0, 100)
          );
        }

        // D. console.log em produção
        if (obj && obj.name === 'console' && prop.name === 'log') {
          addViolation(
            'CODE_SMELL_CONSOLE',
            'hygiene',
            lineNum,
            'Chamada de console.log() residual em arquivo de produção.',
            getLineText(lines, lineNum).trim().slice(0, 100)
          );
        }
      }
    }

    // E. new Function()
    if (node.type === 'NewExpression') {
      const callee = node.callee;
      if (callee.type === 'Identifier' && callee.name === 'Function') {
        addViolation(
          'SECURITY_EXEC',
          'security',
          lineNum,
          'Uso proibido de new Function() em código de produção.',
          getLineText(lines, lineNum).trim().slice(0, 100)
        );
      }
    }

    // F. empty catch () {}
    if (node.type === 'CatchClause') {
      const body = node.body;
      if (body && body.type === 'BlockStatement' && (!body.body || body.body.length === 0)) {
        addViolation(
          'EMPTY_CATCH',
          'hygiene',
          lineNum,
          'Bloco catch silencioso engolindo exceções sem log ou tratamento.',
          getLineText(lines, lineNum).trim().slice(0, 100)
        );
      }
    }
  });

  return { violations, suppressed };
}

// CLI Execution (suporta arquivo individual ou múltiplos arquivos)
if (require.main === module) {
  const rawArgs = process.argv.slice(2);
  const checkEmoji = rawArgs.includes('--check-emoji');
  const files = rawArgs.filter(a => !a.startsWith('--'));

  if (files.length === 0) {
    console.error(JSON.stringify({ error: "Nenhum arquivo fornecido para análise." }));
    process.exit(1);
  }

  const allViolations = [];
  const allSuppressed = [];
  let filesScanned = 0;

  for (const filePath of files) {
    try {
      if (!fs.existsSync(filePath)) continue;
      const code = fs.readFileSync(filePath, 'utf-8');
      const res = analyzeFile(filePath, code, { checkEmoji });
      allViolations.push(...res.violations);
      allSuppressed.push(...res.suppressed);
      filesScanned++;
    } catch (e) {
      // needle-ignore: EMPTY_CATCH (ignora arquivos binários ou inacessíveis durante varredura CLI)
      process.stderr.write(`[js_ast_guard] Erro ao ler ${filePath}: ${e.message}\n`);
    }
  }

  // needle-ignore: CODE_SMELL_CONSOLE (saída padrão formatada JSON para o consumidor do subprocesso)
  console.log(JSON.stringify({
    files_scanned: filesScanned,
    violations: allViolations,
    suppressed: allSuppressed
  }, null, 2));
}

module.exports = { analyzeFile, RULE_SUGGESTIONS };
