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
echo "Done! Restart Claude Code, then try:"
echo "  Use log_ingest with file_path=\"/path/to/your.log\""
