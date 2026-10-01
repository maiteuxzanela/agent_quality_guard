"""HTML Interface simples para consulta visual em linguagem natural via Cactus Needle 3."""

NEEDLE_UI_HTML = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Cactus Needle 3 — Busca Cirúrgica de Código</title>
    <style>
        :root {
            --bg: #0b0f19;
            --surface: #161e2e;
            --surface-border: #233044;
            --accent: #10b981;
            --accent-hover: #059669;
            --accent-glow: rgba(16, 185, 129, 0.2);
            --text: #f1f5f9;
            --text-muted: #94a3b8;
            --code-bg: #0d131f;
            --badge-bg: #1e293b;
            --border-radius: 10px;
        }

        * {
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }

        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            background-color: var(--bg);
            color: var(--text);
            line-height: 1.6;
            padding: 24px;
            max-width: 1200px;
            margin: 0 auto;
        }

        header {
            margin-bottom: 28px;
            border-bottom: 1px solid var(--surface-border);
            padding-bottom: 16px;
            display: flex;
            align-items: center;
            justify-content: space-between;
        }

        .brand {
            display: flex;
            align-items: center;
            gap: 12px;
        }

        .brand-logo {
            width: 38px;
            height: 38px;
            background: linear-gradient(135deg, #10b981, #06b6d4);
            border-radius: 8px;
            display: flex;
            align-items: center;
            justify-content: center;
            font-weight: bold;
            font-size: 20px;
            color: #0b0f19;
            box-shadow: 0 0 15px var(--accent-glow);
        }

        h1 {
            font-size: 1.5rem;
            font-weight: 700;
            letter-spacing: -0.02em;
        }

        .subtitle {
            font-size: 0.875rem;
            color: var(--text-muted);
        }

        .badge-tag {
            background-color: rgba(16, 185, 129, 0.1);
            color: var(--accent);
            border: 1px solid rgba(16, 185, 129, 0.3);
            font-size: 0.75rem;
            padding: 2px 8px;
            border-radius: 12px;
            font-weight: 600;
        }

        .search-card {
            background-color: var(--surface);
            border: 1px solid var(--surface-border);
            border-radius: var(--border-radius);
            padding: 20px;
            margin-bottom: 24px;
            box-shadow: 0 4px 20px rgba(0, 0, 0, 0.3);
        }

        .form-grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 16px;
            margin-bottom: 16px;
        }

        @media (max-width: 768px) {
            .form-grid {
                grid-template-columns: 1fr;
            }
        }

        .form-group {
            display: flex;
            flex-direction: column;
            gap: 6px;
        }

        .form-group.full-width {
            grid-column: 1 / -1;
        }

        label {
            font-size: 0.85rem;
            font-weight: 600;
            color: var(--text-muted);
        }

        input[type="text"], textarea, select {
            background-color: var(--code-bg);
            border: 1px solid var(--surface-border);
            border-radius: 6px;
            color: var(--text);
            padding: 10px 14px;
            font-size: 0.95rem;
            outline: none;
            transition: border-color 0.2s, box-shadow 0.2s;
        }

        input[type="text"]:focus, textarea:focus, select:focus {
            border-color: var(--accent);
            box-shadow: 0 0 0 3px var(--accent-glow);
        }

        textarea {
            resize: vertical;
            min-height: 80px;
        }

        .controls-row {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 16px;
            flex-wrap: wrap;
        }

        .params-inline {
            display: flex;
            gap: 16px;
            align-items: center;
        }

        .param-item {
            display: flex;
            align-items: center;
            gap: 8px;
            font-size: 0.85rem;
            color: var(--text-muted);
        }

        .param-item input[type="number"] {
            width: 80px;
            background-color: var(--code-bg);
            border: 1px solid var(--surface-border);
            color: var(--text);
            padding: 4px 8px;
            border-radius: 4px;
            outline: none;
        }

        .btn-search {
            background: linear-gradient(135deg, #10b981, #059669);
            color: #ffffff;
            border: none;
            border-radius: 6px;
            padding: 10px 24px;
            font-size: 0.95rem;
            font-weight: 600;
            cursor: pointer;
            display: flex;
            align-items: center;
            gap: 8px;
            transition: transform 0.1s, opacity 0.2s;
            box-shadow: 0 2px 10px var(--accent-glow);
        }

        .btn-search:hover {
            opacity: 0.95;
            transform: translateY(-1px);
        }

        .btn-search:active {
            transform: translateY(0);
        }

        .btn-search:disabled {
            opacity: 0.5;
            cursor: not-allowed;
        }

        .stats-bar {
            display: flex;
            gap: 16px;
            margin-bottom: 20px;
            flex-wrap: wrap;
        }

        .stat-card {
            background-color: var(--surface);
            border: 1px solid var(--surface-border);
            border-radius: 8px;
            padding: 12px 16px;
            flex: 1;
            min-width: 160px;
        }

        .stat-label {
            font-size: 0.75rem;
            color: var(--text-muted);
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }

        .stat-value {
            font-size: 1.25rem;
            font-weight: 700;
            color: var(--accent);
            margin-top: 2px;
        }

        .snippets-container {
            display: flex;
            flex-direction: column;
            gap: 16px;
        }

        .snippet-card {
            background-color: var(--surface);
            border: 1px solid var(--surface-border);
            border-radius: var(--border-radius);
            overflow: hidden;
        }

        .snippet-header {
            background-color: rgba(255, 255, 255, 0.02);
            padding: 10px 16px;
            border-bottom: 1px solid var(--surface-border);
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 12px;
            flex-wrap: wrap;
        }

        .snippet-file {
            font-family: monospace;
            font-size: 0.9rem;
            color: #38bdf8;
            font-weight: 600;
            word-break: break-all;
        }

        .snippet-lines {
            color: var(--text-muted);
            font-size: 0.8rem;
            margin-left: 6px;
        }

        .snippet-meta {
            display: flex;
            align-items: center;
            gap: 8px;
        }

        .score-badge {
            background-color: rgba(16, 185, 129, 0.15);
            color: #34d399;
            font-size: 0.75rem;
            padding: 2px 8px;
            border-radius: 4px;
            font-family: monospace;
        }

        .relation-badge {
            background-color: rgba(147, 51, 234, 0.15);
            color: #c084fc;
            font-size: 0.72rem;
            padding: 2px 6px;
            border-radius: 4px;
            font-family: monospace;
        }

        .code-block {
            background-color: var(--code-bg);
            padding: 14px 16px;
            overflow-x: auto;
            font-family: "Fira Code", Consolas, Monaco, "Andale Mono", monospace;
            font-size: 0.85rem;
            line-height: 1.5;
            color: #e2e8f0;
            white-space: pre;
        }

        .spinner {
            border: 3px solid rgba(255, 255, 255, 0.1);
            border-radius: 50%;
            border-top: 3px solid var(--accent);
            width: 20px;
            height: 20px;
            animation: spin 1s linear infinite;
        }

        @keyframes spin {
            0% { transform: rotate(0deg); }
            100% { transform: rotate(360deg); }
        }

        .error-card {
            background-color: rgba(239, 68, 68, 0.1);
            border: 1px solid rgba(239, 68, 68, 0.3);
            color: #fca5a5;
            padding: 16px;
            border-radius: 8px;
            margin-bottom: 20px;
        }

        .empty-state {
            text-align: center;
            padding: 48px 24px;
            color: var(--text-muted);
            background-color: var(--surface);
            border: 1px dashed var(--surface-border);
            border-radius: var(--border-radius);
        }

        .empty-icon {
            font-size: 2rem;
            margin-bottom: 8px;
            opacity: 0.6;
        }
    </style>
</head>
<body>

    <header>
        <div class="brand">
            <div class="brand-logo">N3</div>
            <div>
                <h1>Cactus Needle 3</h1>
                <div class="subtitle">Busca Cirúrgica & Relacional de Código no Disco</div>
            </div>
        </div>
        <span class="badge-tag">Interface Gráfica Local</span>
    </header>

    <main>
        <div class="search-card">
            <form id="search-form">
                <div class="form-grid">
                    <div class="form-group">
                        <label for="workspace-path">Caminho do Repositório (Workspace)</label>
                        <input type="text" id="workspace-path" placeholder="Ex: /home/maiteuxzanela/projeto JEV ou ." value=".">
                    </div>

                    <div class="form-group">
                        <label for="scope-input">Escopo / Filtro de Diretórios (opcional)</label>
                        <input type="text" id="scope-input" placeholder="Ex: src/memory, src/decision (separado por vírgula)">
                    </div>

                    <div class="form-group full-width">
                        <label for="query-input">Pergunta ou Termo em Linguagem Natural</label>
                        <textarea id="query-input" placeholder="Ex: Como funciona a busca de AST no local needle? ou Onde estão definidos os schemas Pydantic do score?" required></textarea>
                    </div>
                </div>

                <div class="controls-row">
                    <div class="params-inline">
                        <div class="param-item">
                            <label for="max-tokens">Max Tokens:</label>
                            <input type="number" id="max-tokens" value="1000" min="100" max="4096">
                        </div>
                        <div class="param-item">
                            <label for="relational-depth">Profundidade AST:</label>
                            <input type="number" id="relational-depth" value="2" min="0" max="5">
                        </div>
                    </div>

                    <button type="submit" id="btn-submit" class="btn-search">
                        <span id="btn-icon">🔍</span>
                        <span id="btn-text">Consultar Código</span>
                    </button>
                </div>
            </form>
        </div>

        <div id="error-container" style="display: none;" class="error-card"></div>

        <div id="stats-container" style="display: none;" class="stats-bar">
            <div class="stat-card">
                <div class="stat-label">Resultados</div>
                <div class="stat-value" id="stat-count">0 trechos</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Tokens Consumidos</div>
                <div class="stat-value" id="stat-tokens">0 tok</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Economia de Tokens</div>
                <div class="stat-value" id="stat-savings">0%</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Latência</div>
                <div class="stat-value" id="stat-latency">0 ms</div>
            </div>
        </div>

        <div id="results-container" class="snippets-container">
            <div class="empty-state">
                <div class="empty-icon">📁</div>
                <p>Escolha o repositório e faça uma pergunta em linguagem natural para buscar trechos cirúrgicos de código via AST Needle 3.</p>
            </div>
        </div>
    </main>

    <script>
        document.getElementById('query-input').addEventListener('keydown', function(e) {
            if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
                e.preventDefault();
                document.getElementById('search-form').requestSubmit();
            }
        });

        document.getElementById('search-form').addEventListener('submit', async function(e) {
            e.preventDefault();

            const workspace = document.getElementById('workspace-path').value.trim() || '.';
            const scopeRaw = document.getElementById('scope-input').value.trim();
            const query = document.getElementById('query-input').value.trim();
            const maxTokens = parseInt(document.getElementById('max-tokens').value, 10) || 1000;
            const depth = parseInt(document.getElementById('relational-depth').value, 10) || 2;

            if (!query) return;

            const btn = document.getElementById('btn-submit');
            const btnText = document.getElementById('btn-text');
            const btnIcon = document.getElementById('btn-icon');
            const errorContainer = document.getElementById('error-container');
            const statsContainer = document.getElementById('stats-container');
            const resultsContainer = document.getElementById('results-container');

            // Reset UI
            errorContainer.style.display = 'none';
            btn.disabled = true;
            btnText.textContent = 'Pesquisando AST...';
            btnIcon.innerHTML = '<div class="spinner"></div>';

            let scope = null;
            if (scopeRaw) {
                scope = scopeRaw.split(',').map(s => s.trim()).filter(Boolean);
            }

            try {
                const response = await fetch('/v1/needle/search', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        query: query,
                        workspace_path: workspace,
                        scope: scope,
                        max_tokens: maxTokens,
                        relational_depth: depth
                    })
                });

                if (!response.ok) {
                    const errData = await response.json().catch(() => ({ detail: 'Erro desconhecido' }));
                    throw new Error(errData.detail || errData.message || `Erro ${response.status}`);
                }

                const data = await response.json();
                renderResults(data);
            } catch (err) {
                errorContainer.textContent = `Falha na consulta Needle 3: ${err.message}`;
                errorContainer.style.display = 'block';
                statsContainer.style.display = 'none';
                resultsContainer.innerHTML = `
                    <div class="empty-state">
                        <div class="empty-icon">⚠️</div>
                        <p>Ocorreu um erro ao realizar a consulta.</p>
                    </div>
                `;
            } finally {
                btn.disabled = false;
                btnText.textContent = 'Consultar Código';
                btnIcon.textContent = '🔍';
            }
        });

        function renderResults(data) {
            const statsContainer = document.getElementById('stats-container');
            const resultsContainer = document.getElementById('results-container');

            const snippets = data.snippets || [];
            
            document.getElementById('stat-count').textContent = `${snippets.length} trechos`;
            document.getElementById('stat-tokens').textContent = `${data.total_tokens || 0} tok`;
            document.getElementById('stat-savings').textContent = `${data.token_savings_pct || 0}%`;
            document.getElementById('stat-latency').textContent = `${data.query_latency_ms || 0} ms`;

            statsContainer.style.display = 'flex';

            if (snippets.length === 0) {
                resultsContainer.innerHTML = `
                    <div class="empty-state">
                        <div class="empty-icon">🔍</div>
                        <p>Nenhum trecho de código foi encontrado para esta consulta no repositório informado.</p>
                    </div>
                `;
                return;
            }

            resultsContainer.innerHTML = snippets.map(snippet => {
                const relationsHtml = (snippet.relations || []).map(r => 
                    `<span class="relation-badge">${escapeHtml(r)}</span>`
                ).join('');

                const scorePct = Math.round((snippet.relevance_score || 0) * 100);

                return `
                    <div class="snippet-card">
                        <div class="snippet-header">
                            <div>
                                <span class="snippet-file">${escapeHtml(snippet.file_path)}</span>
                                <span class="snippet-lines">Linhas ${snippet.start_line} - ${snippet.end_line}</span>
                            </div>
                            <div class="snippet-meta">
                                <span class="score-badge">Relevância: ${scorePct}%</span>
                                ${relationsHtml}
                            </div>
                        </div>
                        <pre class="code-block"><code>${escapeHtml(snippet.content)}</code></pre>
                    </div>
                `;
            }).join('');
        }

        function escapeHtml(text) {
            if (!text) return '';
            return String(text)
                .replace(/&/g, "&amp;")
                .replace(/</g, "&lt;")
                .replace(/>/g, "&gt;")
                .replace(/"/g, "&quot;")
                .replace(/'/g, "&#039;");
        }
    </script>
</body>
</html>
"""
