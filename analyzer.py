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
from dataclasses import dataclass
from typing import Optional

from preprocessor import PreprocessorResult, Severity

# Will use httpx for async Anthropic API calls
try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False


ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = "claude-haiku-4-5-20251001"
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
        parts.append(f"### Semantic Analysis")
        parts.append(f"**Primary Issue**: {self.primary_issue}")
        parts.append(f"**Root Cause**: {self.root_cause}")

        if self.error_signatures:
            parts.append(f"\n**Error Signatures**:")
            for sig in self.error_signatures:
                component = sig.get("affected_component", "unknown")
                category = sig.get("category", "unknown")
                count = sig.get("count", 1)
                parts.append(f"- `{sig['pattern']}` [{category}] in {component} (×{count})")

        if self.timeline:
            parts.append(f"\n**Timeline**:")
            for event in self.timeline:
                ts = event.get("timestamp", "?")
                parts.append(f"- [{ts}] {event['event']}")

        if self.attention_needed:
            parts.append(f"\n**Needs Attention**:")
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


async def analyze(result: PreprocessorResult, api_key: Optional[str] = None) -> Optional[SemanticAnalysis]:
    """
    Call the Haiku model to semantically analyze preprocessed log data.

    Returns None if:
    - httpx is not installed
    - No API key is available
    - The API call fails

    The MCP server should fall back to deterministic-only mode in these cases.
    """
    if not HAS_HTTPX:
        return None

    key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None

    prompt = _build_analysis_prompt(result)

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                ANTHROPIC_API_URL,
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": key,
                    "anthropic-version": "2023-06-01",
                },
                json={
                    "model": DEFAULT_MODEL,
                    "max_tokens": MAX_TOKENS,
                    "system": ANALYSIS_SYSTEM_PROMPT,
                    "messages": [
                        {"role": "user", "content": prompt}
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

    except Exception as e:
        # Log to stderr, don't crash — the deterministic layer still works
        import sys
        print(f"[log_context] Semantic analysis failed: {e}", file=sys.stderr)
        return None
