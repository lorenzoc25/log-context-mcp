"""
Layer 2: Semantic log analyzer using a cheap LLM (Haiku).

Takes the preprocessed output and asks a fast, inexpensive model to:
- Classify ambiguous lines
- Extract error signatures and root causes
- Build a timeline of state changes
- Produce a structured summary for the main coding agent

This layer is optional — the MCP server works without it (pure deterministic mode)
but produces much better summaries with it enabled.
"""

import json
import os
import sys
from dataclasses import dataclass
from typing import Optional

from log_context_mcp.preprocessor import PreprocessorResult, Severity

# Will use httpx for async API calls
try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False


MAX_TOKENS = 1024

ANALYSIS_SYSTEM_PROMPT = """\
You are a log analysis assistant. You receive preprocessed log data and produce a structured JSON summary.

Your job:
1. Identify the PRIMARY error or issue from the log data
2. Classify each unique error into a category (crash, timeout, connection_error, auth_failure, config_error, resource_exhaustion, dependency_failure, build_error, test_failure, unknown)
3. Identify the probable root cause in 1-2 sentences
4. Extract a timeline of significant state changes
5. Flag any lines that need human attention

Respond ONLY with valid JSON, no markdown fences, no preamble. Schema:

{
  "primary_issue": "string — one-sentence summary of the main problem",
  "error_signatures": [
    {
      "pattern": "string — the error signature or key phrase",
      "category": "string — one of the categories above",
      "count": number,
      "first_seen_timestamp": "string or null",
      "affected_component": "string — file, service, or module name"
    }
  ],
  "root_cause": "string — probable root cause in 1-2 sentences",
  "timeline": [
    {
      "timestamp": "string or null",
      "event": "string — what happened"
    }
  ],
  "attention_needed": ["string — lines or patterns that need human review"],
  "noise_assessment": "string — brief note on how much of the log is noise vs signal"
}
"""


class _AnthropicBackend:
    """Backend for Anthropic's native API."""

    def __init__(self, api_key: str, model: str):
        self.api_key = api_key
        self.model = model

    async def call(self, system_prompt: str, user_prompt: str) -> str:
        """Make a request to Anthropic API and return the text response."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                },
                json={
                    "model": self.model,
                    "max_tokens": MAX_TOKENS,
                    "system": system_prompt,
                    "messages": [
                        {"role": "user", "content": user_prompt}
                    ],
                },
            )
            response.raise_for_status()
            data = response.json()

        # Extract text from response
        text = ""
        for block in data.get("content", []):
            if block.get("type") == "text":
                text += block.get("text", "")
        return text


class _OpenAICompatibleBackend:
    """Backend for OpenAI-compatible APIs (OpenAI, Ollama, Groq, Together, etc.)."""

    def __init__(self, api_key: Optional[str], model: str, base_url: str):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url

    async def call(self, system_prompt: str, user_prompt: str) -> str:
        """Make a request to OpenAI-compatible API and return the text response."""
        headers = {
            "Content-Type": "application/json",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{self.base_url}/chat/completions",
                headers=headers,
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt}
                    ],
                },
            )
            response.raise_for_status()
            data = response.json()

        # Extract text from response
        return data["choices"][0]["message"]["content"]


async def _resolve_backend() -> Optional[tuple]:
    """
    Resolve which LLM backend to use based on env vars.

    Returns (backend_instance, model_name) or None if no backend is available.

    Selection order:
    1. LOG_CONTEXT_BACKEND env var if set (anthropic/openai/ollama)
    2. ANTHROPIC_API_KEY → use Anthropic native backend
    3. OPENAI_API_KEY → use OpenAI-compatible backend
    4. Ollama at http://localhost:11434 → use OpenAI-compatible backend
    5. None found → return None
    """
    explicit_backend = os.environ.get("LOG_CONTEXT_BACKEND", "").lower()

    if explicit_backend == "anthropic":
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            return None
        model = os.environ.get("LOG_CONTEXT_MODEL", "claude-haiku-4-5-20251001")
        return _AnthropicBackend(api_key, model), model

    if explicit_backend == "openai":
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            return None
        base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
        model = os.environ.get("LOG_CONTEXT_MODEL", "gpt-4o-mini")
        return _OpenAICompatibleBackend(api_key, model, base_url), model

    if explicit_backend == "ollama":
        model = os.environ.get("LOG_CONTEXT_MODEL", "llama3")
        base_url = os.environ.get("OPENAI_BASE_URL", "http://localhost:11434/v1")
        return _OpenAICompatibleBackend(None, model, base_url), model

    # Auto-detect
    if os.environ.get("ANTHROPIC_API_KEY"):
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        model = os.environ.get("LOG_CONTEXT_MODEL", "claude-haiku-4-5-20251001")
        return _AnthropicBackend(api_key, model), model

    if os.environ.get("OPENAI_API_KEY"):
        api_key = os.environ.get("OPENAI_API_KEY")
        base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
        model = os.environ.get("LOG_CONTEXT_MODEL", "gpt-4o-mini")
        return _OpenAICompatibleBackend(api_key, model, base_url), model

    # Try Ollama at localhost
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            response = await client.get("http://localhost:11434/api/version")
            if response.status_code == 200:
                model = os.environ.get("LOG_CONTEXT_MODEL", "llama3")
                url = "http://localhost:11434/v1"
                return _OpenAICompatibleBackend(None, model, url), model
    except Exception:  # pylint: disable=broad-except
        # Ollama is optional, silently skip if not available
        pass

    return None


@dataclass
class SemanticAnalysis:
    """Parsed result from the semantic analyzer."""
    primary_issue: str
    error_signatures: list[dict]
    root_cause: str
    timeline: list[dict]
    attention_needed: list[str]
    noise_assessment: str
    raw_json: dict

    def to_summary(self) -> str:
        """Format as a concise text summary for the main agent."""
        parts = []
        parts.append("### Semantic Analysis")
        parts.append(f"**Primary Issue**: {self.primary_issue}")
        parts.append(f"**Root Cause**: {self.root_cause}")

        if self.error_signatures:
            parts.append("\n**Error Signatures**:")
            for sig in self.error_signatures:
                component = sig.get("affected_component", "unknown")
                category = sig.get("category", "unknown")
                count = sig.get("count", 1)
                parts.append(
                    f"- `{sig['pattern']}` [{category}] in {component} (×{count})"
                )

        if self.timeline:
            parts.append("\n**Timeline**:")
            for event in self.timeline:
                ts = event.get("timestamp", "?")
                parts.append(f"- [{ts}] {event['event']}")

        if self.attention_needed:
            parts.append("\n**Needs Attention**:")
            for item in self.attention_needed:
                parts.append(f"- ⚠️ {item}")

        parts.append(f"\n**Noise Assessment**: {self.noise_assessment}")
        return "\n".join(parts)


def _build_analysis_prompt(result: PreprocessorResult) -> str:
    """Build the user prompt from preprocessed data.

    We send a condensed representation — NOT the full raw logs.
    This keeps the Haiku call cheap.
    """
    parts = []
    parts.append(f"Total lines: {result.total_lines}, Unique: {result.unique_lines}, "
                 f"Noise removed: {result.noise_lines_removed}")

    if result.first_timestamp:
        parts.append(f"Time range: {result.first_timestamp} → {result.last_timestamp}")

    parts.append(f"\nSeverity counts: {json.dumps(result.severity_counts)}")

    # Send error/warning/fatal lines (deduplicated)
    important = [(l, c, s) for l, c, s in result.deduplicated
                 if s in (Severity.ERROR, Severity.WARNING, Severity.FATAL)]
    if important:
        parts.append(f"\n--- ERROR/WARNING/FATAL LINES ({len(important)} unique) ---")
        for line, count, sev in important[:100]:  # cap at 100
            parts.append(f"[{sev.value.upper()} ×{count}] {line}")

    # Send stack traces (summarized)
    if result.stack_traces:
        parts.append(f"\n--- STACK TRACES ({len(result.stack_traces)}) ---")
        for i, st in enumerate(result.stack_traces[:10]):
            parts.append(f"\nTrace {i+1} (line {st.header_line}):")
            parts.append(st.summary)

    # Send a sample of unknown-severity lines (might be important)
    unknowns = [(l, c, s) for l, c, s in result.deduplicated
                if s == Severity.UNKNOWN]
    if unknowns:
        parts.append(f"\n--- UNCLASSIFIED LINES (sample of {min(20, len(unknowns))}) ---")
        for line, count, sev in unknowns[:20]:
            suffix = f" (×{count})" if count > 1 else ""
            parts.append(f"{line}{suffix}")

    return "\n".join(parts)


async def analyze(
    result: PreprocessorResult, api_key: Optional[str] = None
) -> Optional[SemanticAnalysis]:
    """
    Call an LLM to semantically analyze preprocessed log data.

    Returns None if:
    - httpx is not installed
    - No backend is available
    - The API call fails

    The MCP server should fall back to deterministic-only mode in these cases.

    Args:
        result: Preprocessed log data
        api_key: Optional Anthropic API key (for backward compatibility).
                 If provided, Anthropic backend is used directly.
    """
    if not HAS_HTTPX:
        return None

    # If api_key is provided, use Anthropic backend directly (backward compatibility)
    if api_key:
        model = os.environ.get("LOG_CONTEXT_MODEL", "claude-haiku-4-5-20251001")
        backend = _AnthropicBackend(api_key, model)
    else:
        # Resolve backend from env vars
        backend_result = await _resolve_backend()
        if backend_result is None:
            return None
        backend, _model = backend_result

    prompt = _build_analysis_prompt(result)

    try:
        text = await backend.call(ANALYSIS_SYSTEM_PROMPT, prompt)

        # Parse JSON
        text = text.strip().removeprefix("```json").removesuffix("```").strip()
        parsed = json.loads(text)

        return SemanticAnalysis(
            primary_issue=parsed.get("primary_issue", "Unknown"),
            error_signatures=parsed.get("error_signatures", []),
            root_cause=parsed.get("root_cause", "Unknown"),
            timeline=parsed.get("timeline", []),
            attention_needed=parsed.get("attention_needed", []),
            noise_assessment=parsed.get("noise_assessment", "Unknown"),
            raw_json=parsed,
        )

    except Exception as e:  # pylint: disable=broad-except
        # Log to stderr, don't crash — the deterministic layer still works
        print(f"[log_context] Semantic analysis failed: {e}", file=sys.stderr)
        return None
