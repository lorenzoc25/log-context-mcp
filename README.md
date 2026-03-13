# log-context-mcp

**Stop dumping raw logs into your AI agent's context window.**

`cat error.log | claude` on a 5000-line log burns 15,000+ tokens on repeated health checks, INFO spam, and boilerplate — before the agent finds the 3 lines that matter. This MCP server preprocesses logs first, hands the agent a structured ~1000 token summary, and lets it drill into raw lines only when needed.

[![PyPI](https://img.shields.io/pypi/v/log-context-mcp)](https://pypi.org/project/log-context-mcp/)
[![Python](https://img.shields.io/pypi/pyversions/log-context-mcp)](https://pypi.org/project/log-context-mcp/)
[![Tests](https://github.com/lorenzoc25/log-context-mcp/actions/workflows/tests.yml/badge.svg)](https://github.com/lorenzoc25/log-context-mcp/actions/workflows/tests.yml)

**Benchmark:** 2,000-line Apache log → 70 unique lines (**96.5% reduction**). Root cause correctly identified: mod_jk worker instability from a cyclic init/failure pattern.

---

## How it works

```
Raw log (5000 lines)
      │
      ▼
┌─────────────────────────────────────────┐
│  Layer 1 — Deterministic (free)         │
│  • Dedup lines, count occurrences       │
│  • Detect severity (FATAL/ERROR/WARN…)  │
│  • Group stack traces                   │
│  • Strip ANSI, timestamps, noise        │
│  → typically 50–95% reduction           │
└────────────────────┬────────────────────┘
                     │ ~1000 token summary
                     ▼
┌─────────────────────────────────────────┐
│  Layer 2 — Semantic (cheap/optional)    │
│  • Root cause in 1–2 sentences          │
│  • Error classification & timeline      │
│  • Flags lines needing attention        │
└────────────────────┬────────────────────┘
                     │
                     ▼
             Agent sees summary
             + drills into raw lines
               on demand (Layer 3)
```

---

## Install

**Quickest — the setup script handles everything:**

```bash
pip install log-context-mcp
curl -fsSL https://raw.githubusercontent.com/lorenzoc25/log-context-mcp/main/scripts/setup.sh | bash
```

It registers the MCP server, optionally configures semantic analysis, installs the `/analyze-log` skill + `log-analyzer` Haiku agent, and updates `~/.claude/CLAUDE.md` so Claude automatically uses `log_ingest` when it sees a log file.

<details>
<summary>Manual setup</summary>

```bash
# 1. Install
pip install log-context-mcp

# 2. Register MCP server (deterministic only — no API key needed)
claude mcp add log-context -- log-context-mcp

# 3. Enable semantic analysis — pick one backend:
#    Gemini/Gemma (free tier): https://aistudio.google.com/apikey
claude mcp add log-context \
  -e OPENAI_API_KEY=<gemini-key> \
  -e OPENAI_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai \
  -e LOG_CONTEXT_MODEL=gemma-3-27b-it \
  -- log-context-mcp

#    Anthropic
claude mcp add log-context -e ANTHROPIC_API_KEY=<key> -- log-context-mcp

#    OpenAI / Groq / Together / any OpenAI-compatible provider
claude mcp add log-context \
  -e OPENAI_API_KEY=<key> \
  -e OPENAI_BASE_URL=<base-url> \
  -e LOG_CONTEXT_MODEL=<model> \
  -- log-context-mcp

#    Ollama (local)
claude mcp add log-context -e LOG_CONTEXT_MODEL=llama3 -- log-context-mcp

# 4. Install the /analyze-log skill + Haiku agent (Option B — no external API)
mkdir -p ~/.claude/commands ~/.claude/agents
curl -fsSL https://raw.githubusercontent.com/lorenzoc25/log-context-mcp/main/skills/analyze-log.md \
  -o ~/.claude/commands/analyze-log.md
curl -fsSL https://raw.githubusercontent.com/lorenzoc25/log-context-mcp/main/.claude/agents/log-analyzer.md \
  -o ~/.claude/agents/log-analyzer.md

# 5. Auto-trigger: add to ~/.claude/CLAUDE.md
cat >> ~/.claude/CLAUDE.md << 'EOF'

## Log Analysis
When analyzing log files or log output, always use the `log_ingest` MCP tool instead of reading the file directly. Call `log_ingest` with `file_path=` and `enable_semantic=false`, then analyze the preprocessed summary yourself. Use `log_get_lines` to drill into specific patterns.
EOF
```

</details>

---

## Semantic analysis: two approaches

Layer 1 is always free and runs locally. For semantic analysis (root cause, timeline, classification), choose one:

### Option A — External LLM
A dedicated cheap model analyzes the compressed summary. Runs automatically on every `log_ingest` call. The setup script configures this interactively.

| Provider | Cost | Notes |
|---|---|---|
| Google Gemini / Gemma | Free tier | [Get key](https://aistudio.google.com/apikey) — recommended starting point |
| Anthropic Haiku | ~$0.001/call | [Get key](https://console.anthropic.com/settings/keys) |
| OpenAI / Groq / Together | Varies | Any OpenAI-compatible endpoint |
| Ollama | Free | Fully local — `ollama pull llama3` |

> `OPENAI_API_KEY` is used for all OpenAI-compatible providers. Set `OPENAI_BASE_URL` to point to your provider.

### Option B — `/analyze-log` skill
Layer 1 compresses the log, then the skill spins up a dedicated `log-analyzer` sub-agent running on **Haiku** to do semantic analysis. No separate API key needed beyond your existing Claude subscription.

```
/analyze-log /path/to/your.log
```

The `log-analyzer` agent is defined in `.claude/agents/log-analyzer.md` with `model: haiku` — cheap, fast, and isolated from your main conversation context.

Both options produce equivalent output. Option A runs automatically on every `log_ingest`; Option B is explicit and uses Haiku via your subscription.

---

## Usage

Once installed, just talk to Claude normally — it calls `log_ingest` automatically:

```
look at /tmp/error.log
why is my build failing? here's the output: [paste]
debug this crash dump: /var/log/app/crash.log
```

To drill into specific lines after ingestion:
```
show me the ConnectionRefused lines
what's around line 847?
```

### MCP tools

| Tool | Description |
|---|---|
| `log_ingest` | Ingest a log file or text — returns preprocessed summary + optional semantic analysis |
| `log_get_lines` | Fetch raw lines by pattern, severity, or line number range |
| `log_get_analysis` | Get the full semantic analysis as JSON |
| `log_list_sessions` | List all active log sessions |

`log_ingest` parameters: `file_path`, `log_text`, `label` (default: `"default"`), `enable_semantic` (default: `true`).

`log_get_lines` parameters: `pattern` (regex), `severity`, `max_lines` (default: 30), `around_line`, `context_lines` (default: 5).

---

## Environment variables

| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Use Anthropic backend |
| `OPENAI_API_KEY` | Use any OpenAI-compatible provider |
| `OPENAI_BASE_URL` | Override API endpoint (default: `https://api.openai.com/v1`) |
| `LOG_CONTEXT_MODEL` | Override model name |
| `LOG_CONTEXT_BACKEND` | Force backend: `anthropic`, `openai`, or `ollama` |

---

## License

MIT
