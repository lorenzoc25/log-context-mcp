# log-context-mcp

**Token-efficient log preprocessing for LLM coding agents.**

An MCP server that sits between raw logs and the LLM context window. Instead of dumping thousands of log lines into the context (burning tokens on noise), the coding agent calls `log_ingest` and gets back a structured, deduplicated summary in ~500-2000 tokens. It can then drill down into specific patterns on demand.

[![PyPI](https://img.shields.io/pypi/v/log-context-mcp)](https://pypi.org/project/log-context-mcp/)
[![Python](https://img.shields.io/pypi/pyversions/log-context-mcp)](https://pypi.org/project/log-context-mcp/)
[![Tests](https://github.com/lorenzoc25/log-context-mcp/actions/workflows/tests.yml/badge.svg)](https://github.com/lorenzoc25/log-context-mcp/actions/workflows/tests.yml)

## The Problem

When debugging with AI coding agents (Claude Code, Cursor, Copilot), the current workflow is:

```
cat error.log | claude    # 5000 lines → 15,000+ tokens of mostly noise
```

90% of those tokens are repeated INFO lines, health checks, and boilerplate. The agent reads all of it, finds the 3 relevant error lines, and you've burned $0.50 on noise.

## The Solution

```
# Agent calls log_ingest → gets a ~1000 token summary
# Agent calls log_get_lines(pattern="ConnectionRefused") → gets 10 relevant lines
# Total: ~1500 tokens instead of 15,000
```

### Architecture

```
┌──────────────┐     ┌──────────────────┐     ┌─────────────────┐
│  Raw Log     │────▶│  Layer 1:        │────▶│  Layer 2:       │
│  (5000 lines)│     │  Deterministic   │     │  Semantic       │
│              │     │  - Dedup         │     │  - Any LLM      │
│              │     │  - Severity      │     │  - Classify     │
│              │     │  - Stack traces  │     │  - Root cause   │
│              │     │  - Noise removal │     │  - Timeline     │
│              │     │  (FREE)          │     │  (CHEAP)        │
└──────────────┘     └──────────────────┘     └────────┬────────┘
                                                       │
                                                       ▼
                                             ┌──────────────────┐
                                             │  Summary         │
                                             │  (~1000 tokens)  │
                                             │                  │
                                             │  + drill-down    │
                                             │    on demand     │
                                             └──────────────────┘
```

**Layer 1 (Deterministic, zero cost):**
- ANSI color code stripping
- Line deduplication with occurrence counting
- Severity detection (FATAL/ERROR/WARN/INFO/DEBUG)
- Stack trace grouping and summarization
- Blank line and noise removal
- Timestamp extraction and range detection
- Typically achieves 50-70% reduction alone

**Layer 2 (Semantic, cheap):**
- Works with any LLM backend: Anthropic, OpenAI, Gemini, Ollama, or any OpenAI-compatible API
- Classifies errors into categories (timeout, auth_failure, connection_error, etc.)
- Extracts root cause in 1-2 sentences
- Builds timeline of state changes
- Flags items needing human attention
- Optional — server works without it (deterministic-only mode)

**Layer 3 (Drill-down, on demand):**
- Agent requests specific lines by pattern, severity, or line number
- Only requested lines enter the main context window
- Supports regex filtering and context windows around specific lines

## Setup

### Quick setup (recommended)

```bash
# 1. Install
pip install log-context-mcp

# 2. Run the interactive setup script
curl -fsSL https://raw.githubusercontent.com/lorenzoc25/log-context-mcp/main/scripts/setup.sh | bash
```

The setup script walks you through choosing a backend and registers the MCP server with Claude Code automatically.

### Manual setup

```bash
# Install
pip install log-context-mcp

# Register with Claude Code (deterministic-only, no API key needed)
claude mcp add log-context -- log-context-mcp
```

### Enable Semantic Analysis (Layer 2)

Pass your API key as an environment variable when registering. Layer 2 auto-detects the backend from env vars:

Layer 2 uses `OPENAI_API_KEY` as the key variable for **any OpenAI-compatible provider** — not just OpenAI. Gemini, Groq, Together, and LM Studio all use the same variable; only the `OPENAI_BASE_URL` and model change.

**Google Gemini / Gemma (free tier available)**
```bash
# Get a free key at: https://aistudio.google.com/apikey
claude mcp add log-context \
  -e OPENAI_API_KEY=<your-gemini-key> \
  -e OPENAI_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai \
  -e LOG_CONTEXT_MODEL=gemma-3-27b-it \
  -- log-context-mcp
```

**Anthropic (Claude Haiku)**
```bash
claude mcp add log-context \
  -e ANTHROPIC_API_KEY=<your-key> \
  -- log-context-mcp
```

**OpenAI**
```bash
claude mcp add log-context \
  -e OPENAI_API_KEY=<your-openai-key> \
  -- log-context-mcp
```

**Ollama (local, free)**
```bash
ollama pull llama3
claude mcp add log-context \
  -e LOG_CONTEXT_MODEL=llama3 \
  -- log-context-mcp
```

**Groq, Together, LM Studio, or any OpenAI-compatible provider**
```bash
claude mcp add log-context \
  -e OPENAI_API_KEY=<your-key> \
  -e OPENAI_BASE_URL=<provider-base-url> \
  -e LOG_CONTEXT_MODEL=<model-name> \
  -- log-context-mcp
```

Without any API key, the server runs in deterministic-only mode (Layer 1), which still provides significant value.

### Environment variable reference

| Variable | Purpose | Default |
|---|---|---|
| `ANTHROPIC_API_KEY` | Use Anthropic backend | — |
| `OPENAI_API_KEY` | API key for any OpenAI-compatible provider (OpenAI, Gemini, Groq, etc.) | — |
| `OPENAI_BASE_URL` | Base URL for the OpenAI-compatible endpoint | `https://api.openai.com/v1` |
| `LOG_CONTEXT_MODEL` | Override model name | Haiku / gpt-4o-mini / llama3 |
| `LOG_CONTEXT_BACKEND` | Force backend: `anthropic`, `openai`, `ollama` | auto-detect |

### Project Structure

```
log-context-mcp/
├── log_context_mcp/          # Main package
│   ├── __init__.py
│   ├── server.py             # MCP server & tool definitions
│   ├── preprocessor.py       # Layer 1: Deterministic processing
│   └── analyzer.py           # Layer 2: Semantic analysis
├── scripts/
│   └── setup.sh              # Interactive setup script
├── tests/
│   └── test_log_context.py   # Test suite
└── pyproject.toml
```

### Running Tests

```bash
pip install -e ".[dev]"
python3.11 -m pytest tests/ -v
```

All tests run without external API keys.

## Usage

### Automatic triggering (no manual invocation needed)

The setup script adds an instruction to `~/.claude/CLAUDE.md` that tells Claude Code to automatically reach for `log_ingest` whenever it encounters a log file — no `/analyze-log` or explicit "use log_ingest" needed.

To install manually:
```bash
cat >> ~/.claude/CLAUDE.md << 'EOF'

## Log Analysis
When analyzing log files or log output, always use the `log_ingest` MCP tool instead of reading the file directly. Call `log_ingest` with `file_path=` and `enable_semantic=false`, then analyze the preprocessed summary yourself. Use `log_get_lines` to drill into specific patterns.
EOF
```

After this, simply saying "look at /tmp/error.log" is enough — Claude will call `log_ingest` automatically.

### No API key? Use the `/analyze-log` skill

If you're a Claude Code subscriber without a separate API key, install the skill instead of configuring Layer 2. It uses Layer 1 for preprocessing and your existing Claude subscription for semantic analysis — no extra credentials needed.

```bash
# Copy the skill to Claude Code's commands directory
mkdir -p ~/.claude/commands
cp skills/analyze-log.md ~/.claude/commands/analyze-log.md
```

Then in Claude Code:
```
/analyze-log /path/to/your.log
```

The skill calls `log_ingest` with Layer 1 only, then has Claude analyze the condensed output. Since Layer 1 reduces logs by 50-95% before Claude sees them, you get full semantic analysis at a fraction of the token cost.

### MCP tools directly

Once registered, use these tools in Claude Code:

### `log_ingest` — Analyze a log file

```
Analyze the build log at /tmp/build.log using log_ingest
```

Or with raw text:

```
Use log_ingest to analyze this error output: [paste]
```

Parameters:
- `file_path`: Path to log file (preferred for large logs)
- `log_text`: Raw log text (for small snippets)
- `label`: Session name for later reference (default: "default")
- `enable_semantic`: Whether to run semantic analysis (default: true)

### `log_get_lines` — Drill into specific lines

```
Use log_get_lines to show me the ConnectionRefusedError lines
```

Parameters:
- `pattern`: Regex or substring filter
- `severity`: Filter by level (error, warning, etc.)
- `max_lines`: How many lines to return (default: 30)
- `around_line`: Show context around a specific line number
- `context_lines`: How many lines of context (default: 5)

### `log_get_analysis` — Get raw semantic analysis JSON

```
Show me the full semantic analysis JSON from log_get_analysis
```

### `log_list_sessions` — See active sessions

```
What logs have I ingested? Use log_list_sessions
```

## Real-World Benchmark

Tested against the [Apache 2k log](https://github.com/logpai/loghub/blob/master/Apache/Apache_2k.log) from the LogHub dataset:

| Metric | Value |
|---|---|
| Input lines | 2,000 |
| Unique lines after dedup | 70 |
| **Reduction** | **96.5%** |
| Errors identified | 595 |
| Semantic root cause | mod_jk worker instability (Apache–Tomcat connector) |

Layer 2 correctly identified the cyclic failure pattern: `workerEnv.init() ok` → immediately followed by `workerEnv in error state`, repeating across both days — pointing to a version mismatch or misconfiguration rather than transient failures.

## Example Output

Given a 3000-line application log:

```
## Log Preprocessing Summary
- Total lines: 3,247
- Unique lines: 142 (95.6% reduction)
- Time range: 2026-03-07T10:03:12 → 2026-03-07T10:07:45

### Severity Breakdown
- ERROR: 16
- WARNING: 3
- INFO: 2,831

### Stack Traces (2 found)
Trace 1 (line 1847):
  at DBClient.connect (src/db/client.ts:142)
  ... (12 frames omitted)
  Error: connect ECONNREFUSED 127.0.0.1:5432

### Semantic Analysis
**Primary Issue**: Database connection failure causing cascading service degradation
**Root Cause**: PostgreSQL on port 5432 is unreachable, likely down or misconfigured
**Timeline**:
- [10:03:12] First connection refused error
- [10:03:15] Connection pool exhausted
- [10:07:45] Last log entry (service likely crashed)
```

That's ~400 tokens instead of ~12,000.

## Design Decisions

**Why MCP and not a CLI tool?**
MCP integrates directly into the agent's tool loop. The agent can decide when to drill down without the user manually piping things around.

**Why support multiple LLM backends?**
Different users have different API access. Gemini has a free tier; Anthropic is highest quality; Ollama is fully local. Layer 2 should work for everyone.

**Why keep raw lines in memory?**
The drill-down tool needs access to the original log. Storing in memory avoids re-reading from disk and keeps latency low.

**Why not just use grep?**
Grep finds lines but doesn't understand them. This tool tells the agent "there are 14 connection errors to port 5432 starting at 10:03, the root cause is the database being down" — that's semantic understanding, not string matching.

## License

MIT
