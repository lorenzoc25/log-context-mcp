#!/usr/bin/env bash
# setup.sh — Configure log-context-mcp as a Claude Code MCP server

set -e

echo "=== log-context-mcp setup ==="
echo

# Check dependencies
if ! command -v claude &>/dev/null; then
  echo "Error: 'claude' CLI not found. Install Claude Code first."
  exit 1
fi

if ! command -v uvx &>/dev/null && ! command -v log-context-mcp &>/dev/null; then
  echo "Installing log-context-mcp via pip..."
  pip install log-context-mcp
fi

# Choose backend
echo "Choose LLM backend for semantic analysis (Layer 2):"
echo "  1) Google Gemini / Gemma  (free tier available)"
echo "  2) Anthropic Claude Haiku (requires API credits)"
echo "  3) OpenAI / compatible    (requires API key)"
echo "  4) Ollama (local, free)"
echo "  5) Skip — deterministic only"
echo
read -r -p "Enter choice [1-5]: " BACKEND_CHOICE

MCP_ENV_ARGS=""

case "$BACKEND_CHOICE" in
  1)
    echo
    echo "Get a free Gemini API key at: https://aistudio.google.com/apikey"
    echo "(stored as OPENAI_API_KEY — used for any OpenAI-compatible provider)"
    read -r -p "Gemini API key: " GEMINI_KEY
    echo
    echo "Available models: gemini-2.0-flash, gemma-3-27b-it, gemma-3-12b-it"
    read -r -p "Model [gemma-3-27b-it]: " MODEL
    MODEL="${MODEL:-gemma-3-27b-it}"
    MCP_ENV_ARGS="-e OPENAI_API_KEY=${GEMINI_KEY} -e OPENAI_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai -e LOG_CONTEXT_MODEL=${MODEL}"
    ;;
  2)
    echo
    echo "Get an API key at: https://console.anthropic.com/settings/keys"
    read -r -p "Anthropic API key: " ANTHROPIC_KEY
    read -r -p "Model [claude-haiku-4-5-20251001]: " MODEL
    MODEL="${MODEL:-claude-haiku-4-5-20251001}"
    MCP_ENV_ARGS="-e ANTHROPIC_API_KEY=${ANTHROPIC_KEY} -e LOG_CONTEXT_MODEL=${MODEL}"
    ;;
  3)
    echo
    echo "(OPENAI_API_KEY works for any OpenAI-compatible provider: OpenAI, Groq, Together, etc.)"
    read -r -p "API key: " OPENAI_KEY
    read -r -p "Base URL [https://api.openai.com/v1]: " BASE_URL
    BASE_URL="${BASE_URL:-https://api.openai.com/v1}"
    read -r -p "Model [gpt-4o-mini]: " MODEL
    MODEL="${MODEL:-gpt-4o-mini}"
    MCP_ENV_ARGS="-e OPENAI_API_KEY=${OPENAI_KEY} -e OPENAI_BASE_URL=${BASE_URL} -e LOG_CONTEXT_MODEL=${MODEL}"
    ;;
  4)
    if ! curl -sf http://localhost:11434/api/version &>/dev/null; then
      echo "Error: Ollama not running. Start it with: ollama serve"
      exit 1
    fi
    read -r -p "Model [llama3]: " MODEL
    MODEL="${MODEL:-llama3}"
    echo "Pulling model (this may take a while)..."
    ollama pull "$MODEL"
    MCP_ENV_ARGS="-e OPENAI_BASE_URL=http://localhost:11434/v1 -e LOG_CONTEXT_MODEL=${MODEL}"
    ;;
  5)
    echo "Skipping LLM backend — deterministic analysis only."
    ;;
  *)
    echo "Invalid choice."
    exit 1
    ;;
esac

# Remove existing server if present
claude mcp remove log-context 2>/dev/null || true

# Determine command
if command -v uvx &>/dev/null; then
  CMD="uvx log-context-mcp"
else
  CMD="log-context-mcp"
fi

# Register MCP server
# shellcheck disable=SC2086
claude mcp add log-context $MCP_ENV_ARGS -- $CMD

echo
# --- CLAUDE.md auto-trigger ---
echo "Add log analysis instructions to ~/.claude/CLAUDE.md?"
echo "  This makes Claude automatically use log_ingest whenever it sees a log file"
echo "  — no need to say 'use log_ingest' manually."
echo
read -r -p "Update ~/.claude/CLAUDE.md? [Y/n]: " CLAUDE_MD_CHOICE
CLAUDE_MD_CHOICE="${CLAUDE_MD_CHOICE:-Y}"

if [[ "$CLAUDE_MD_CHOICE" =~ ^[Yy] ]]; then
  GLOBAL_MD="$HOME/.claude/CLAUDE.md"
  LOG_INSTRUCTION="$(cat <<'INSTRUCTION'

## Log Analysis

When you need to analyze log files or log output, **always use the `log_ingest` MCP tool** instead of reading the file directly. This applies to any `.log` file, build output, crash dumps, or error traces. Call `log_ingest` with `file_path=` and `enable_semantic=false`, then analyze the preprocessed summary yourself. Use `log_get_lines` to drill into specific patterns.
INSTRUCTION
)"
  if [ -f "$GLOBAL_MD" ]; then
    if ! grep -q "log_ingest" "$GLOBAL_MD"; then
      echo "$LOG_INSTRUCTION" >> "$GLOBAL_MD"
      echo "  ✓ Updated ~/.claude/CLAUDE.md"
    else
      echo "  ~/.claude/CLAUDE.md already has log analysis instructions — skipped"
    fi
  else
    mkdir -p "$HOME/.claude"
    printf "# Global Claude Code Instructions%s" "$LOG_INSTRUCTION" > "$GLOBAL_MD"
    echo "  ✓ Created ~/.claude/CLAUDE.md"
  fi
else
  echo "  Skipped."
fi

echo
# --- /analyze-log skill ---
echo "Install the /analyze-log skill?"
echo "  Lets Claude Code subscribers run semantic analysis using their existing"
echo "  subscription — no separate API key needed."
echo
read -r -p "Install /analyze-log skill? [Y/n]: " SKILL_CHOICE
SKILL_CHOICE="${SKILL_CHOICE:-Y}"

if [[ "$SKILL_CHOICE" =~ ^[Yy] ]]; then
  SKILL_DIR="$HOME/.claude/commands"
  AGENT_DIR="$HOME/.claude/agents"
  mkdir -p "$SKILL_DIR" "$AGENT_DIR"
  REPO_ROOT="$(dirname "$0")/.."

  # Install skill
  if [ -f "$REPO_ROOT/skills/analyze-log.md" ]; then
    cp "$REPO_ROOT/skills/analyze-log.md" "$SKILL_DIR/analyze-log.md"
  else
    curl -fsSL "https://raw.githubusercontent.com/lorenzoc25/log-context-mcp/main/skills/analyze-log.md" \
      -o "$SKILL_DIR/analyze-log.md"
  fi
  echo "  ✓ Skill installed: /analyze-log"

  # Install log-analyzer agent (runs on Haiku)
  if [ -f "$REPO_ROOT/.claude/agents/log-analyzer.md" ]; then
    cp "$REPO_ROOT/.claude/agents/log-analyzer.md" "$AGENT_DIR/log-analyzer.md"
  else
    curl -fsSL "https://raw.githubusercontent.com/lorenzoc25/log-context-mcp/main/.claude/agents/log-analyzer.md" \
      -o "$AGENT_DIR/log-analyzer.md"
  fi
  echo "  ✓ Agent installed: log-analyzer (Haiku)"
else
  echo "  Skipped."
fi

echo
echo "Done! Restart Claude Code, then try:"
echo "  'look at /path/to/your.log'   — Claude will call log_ingest automatically"
echo "  /analyze-log /path/to/your.log — explicit skill invocation"
