# log-context-mcp

**Token-efficient log preprocessing for LLM coding agents.**

An MCP server that sits between raw logs and the LLM context window. Instead of dumping thousands of log lines into the context (burning tokens on noise), the coding agent calls `log_ingest` and gets back a structured, deduplicated summary in ~500-2000 tokens. It can then drill down into specific patterns on demand.

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
┌─────────────┐     ┌──────────────────┐     ┌─────────────────┐
│  Raw Log     │────▶│  Layer 1:        │────▶│  Layer 2:       │
│  (5000 lines)│     │  Deterministic   │     │  Semantic       │
│              │     │  - Dedup         │     │  - Haiku LLM    │
│              │     │  - Severity      │     │  - Classify     │
│              │     │  - Stack traces  │     │  - Root cause   │
│              │     │  - Noise removal │     │  - Timeline     │
│              │     │  (FREE)          │     │  (CHEAP)        │
└─────────────┘     └──────────────────┘     └────────┬────────┘
                                                       │
                                                       ▼
                                             ┌─────────────────┐
                                             │  Summary         │
                                             │  (~1000 tokens)  │
                                             │                  │
                                             │  + drill-down    │
                                             │    on demand     │
                                             └─────────────────┘
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
- Calls Claude Haiku (~$0.001 per analysis)
- Classifies ambiguous errors into categories
- Extracts root cause in 1-2 sentences
- Builds timeline of state changes
- Flags items needing human attention
- Optional — server works without it

**Layer 3 (Drill-down, on demand):**
- Agent requests specific lines by pattern, severity, or line number
- Only requested lines enter the main context window
- Supports regex filtering and context windows around specific lines

## Setup

### Prerequisites

- Python 3.11+
- Claude Code installed

### Install

```bash
cd log-context-mcp
pip install -r requirements.txt
```

### Register with Claude Code

```bash
claude mcp add log-context -- python /path/to/log-context-mcp/server.py
```

### (Optional) Enable Semantic Analysis

Set your Anthropic API key for Layer 2:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

Without this, the server runs in deterministic-only mode (Layer 1 only), which is still very useful.

## Usage

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
- `enable_semantic`: Whether to run Haiku analysis (default: true)

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

### `log_get_analysis` — Get raw semantic analysis

```
Show me the full semantic analysis JSON from log_get_analysis
```

### `log_list_sessions` — See active sessions

```
What logs have I ingested? Use log_list_sessions
```

## Example

Given a 3000-line Node.js application log, the agent gets back:

```
## Log Preprocessing Summary
- Total lines: 3,247
- Unique lines: 142 (95.6% reduction)
- Noise removed: 89
- Time range: 2026-03-07T10:03:12 → 2026-03-07T10:07:45

### Severity Breakdown
- ERROR: 16
- WARNING: 3
- INFO: 2,831
- DEBUG: 308

### Stack Traces (2 found)
**Trace 1** (line 1847):
  at DBClient.connect (src/db/client.ts:142)
  at ConnectionPool.acquire (src/db/pool.ts:89)
  ... (12 frames omitted)
  Error: connect ECONNREFUSED 127.0.0.1:5432

### Semantic Analysis
**Primary Issue**: Database connection failure causing cascading service degradation
**Root Cause**: PostgreSQL on port 5432 is unreachable, likely down or misconfigured
**Error Signatures**:
- `ECONNREFUSED 127.0.0.1:5432` [connection_error] in db/client.ts (×14)
- `TimeoutError: query timeout` [timeout] in api/handler.ts (×2)
**Timeline**:
- [10:03:12] First connection refused error
- [10:03:15] Connection pool exhausted
- [10:03:18] API handler timeout errors begin
- [10:07:45] Last log entry (service likely crashed)
```

That's ~400 tokens instead of ~12,000. The agent then calls `log_get_lines(pattern="ECONNREFUSED", max_lines=5)` for the specific raw lines it needs.

## Design Decisions

**Why MCP and not a CLI tool?**
MCP integrates directly into the agent's tool loop. The agent can decide when to drill down without the user manually piping things around.

**Why Haiku for Layer 2?**
It's ~50x cheaper than Opus/Sonnet. For log classification, you don't need deep reasoning — you need pattern recognition and categorization, which Haiku handles well.

**Why keep raw lines in memory?**
The drill-down tool needs access to the original log. Storing in memory avoids re-reading from disk and keeps the server stateless from the filesystem perspective.

**Why not just use grep?**
Grep finds lines but doesn't understand them. This tool tells the agent "there are 14 connection errors to port 5432 starting at 10:03, the root cause is the database being down" — that's semantic understanding, not string matching.

## License

MIT
