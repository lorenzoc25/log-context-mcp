"""
log_context_mcp — Token-efficient log preprocessing for LLM coding agents.

An MCP server that sits between raw logs and the LLM context window.
Instead of dumping thousands of log lines into the context, the agent calls
`log_ingest` and gets back a structured, deduplicated summary. It can then
drill down into specific patterns with `log_get_lines` or retrieve the
full semantic analysis with `log_get_analysis`.

Architecture:
  Layer 1 (deterministic): dedup, severity filter, stack trace grouping — free
  Layer 2 (semantic):      Haiku-class LLM classifies & summarizes — cheap
  Layer 3 (drill-down):    agent requests specific raw lines — on demand

Usage with Claude Code:
  claude mcp add log-context -- python /path/to/server.py

Then in Claude Code:
  > cat error.log | pbcopy   (or just reference the file)
  > "Use log_ingest to analyze /tmp/error.log and help me debug"
"""

import json
import re
import sys
import os
from typing import Optional
from pydantic import BaseModel, Field, ConfigDict
from enum import Enum

from mcp.server.fastmcp import FastMCP

from preprocessor import preprocess, PreprocessorResult, Severity
from analyzer import analyze, SemanticAnalysis


# ---------------------------------------------------------------------------
# Server initialization
# ---------------------------------------------------------------------------

mcp = FastMCP("log_context_mcp")

# In-memory store for ingested logs (keyed by a label/session ID)
_sessions: dict[str, dict] = {}


# ---------------------------------------------------------------------------
# Input models
# ---------------------------------------------------------------------------

class LogIngestInput(BaseModel):
    """Input for ingesting raw log text."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    log_text: Optional[str] = Field(
        default=None,
        description=(
            "Raw log text to analyze. Provide EITHER log_text OR file_path, not both. "
            "For large logs, prefer file_path to avoid bloating the context."
        ),
    )
    file_path: Optional[str] = Field(
        default=None,
        description="Path to a log file on disk. The server reads it directly — no need to cat the file into the prompt.",
    )
    label: str = Field(
        default="default",
        description="Session label to reference this log later (e.g., 'build_log', 'crash_dump'). Default: 'default'.",
        max_length=64,
    )
    enable_semantic: bool = Field(
        default=True,
        description="Whether to run the semantic analysis (Layer 2) via Haiku. Set to false for faster, deterministic-only analysis.",
    )


class LogGetLinesInput(BaseModel):
    """Input for retrieving specific raw lines from an ingested log."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    label: str = Field(
        default="default",
        description="Session label of the ingested log.",
    )
    pattern: Optional[str] = Field(
        default=None,
        description="Regex or substring pattern to filter lines. Only matching lines are returned.",
    )
    severity: Optional[str] = Field(
        default=None,
        description="Filter by severity: fatal, error, warning, info, debug.",
    )
    max_lines: int = Field(
        default=30,
        description="Maximum number of lines to return.",
        ge=1,
        le=200,
    )
    around_line: Optional[int] = Field(
        default=None,
        description="Return lines around this line number (±context_lines).",
        ge=1,
    )
    context_lines: int = Field(
        default=5,
        description="Number of context lines above/below around_line.",
        ge=0,
        le=50,
    )


class LogGetAnalysisInput(BaseModel):
    """Input for retrieving the full semantic analysis."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    label: str = Field(
        default="default",
        description="Session label of the ingested log.",
    )


class LogListSessionsInput(BaseModel):
    """Input for listing active log sessions."""
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool(
    name="log_ingest",
    annotations={
        "title": "Ingest & Analyze Log",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def log_ingest(params: LogIngestInput) -> str:
    """Ingest raw log text or a log file path and return a token-efficient summary.

    Instead of pasting thousands of log lines into the context window, use this
    tool to preprocess logs. It deduplicates lines, groups stack traces, filters
    noise, and optionally runs semantic analysis via a cheap LLM.

    The returned summary is designed to fit in ~500-2000 tokens regardless of
    the original log size. Use `log_get_lines` to drill into specific patterns.

    Args:
        params (LogIngestInput): Contains either log_text or file_path, plus options.

    Returns:
        str: Structured summary with severity breakdown, stack traces, error
             signatures, and optional semantic analysis.
    """
    # Resolve log text
    raw_text = None
    if params.log_text:
        raw_text = params.log_text
    elif params.file_path:
        path = os.path.expanduser(params.file_path)
        if not os.path.isfile(path):
            return f"Error: File not found: {path}. Check the path and try again."
        try:
            with open(path, "r", errors="replace") as f:
                raw_text = f.read()
        except PermissionError:
            return f"Error: Permission denied reading {path}."
        except Exception as e:
            return f"Error reading file: {e}"
    else:
        return "Error: Provide either `log_text` or `file_path`. Neither was given."

    if not raw_text.strip():
        return "Error: Log input is empty."

    # Layer 1: Deterministic preprocessing
    result = preprocess(raw_text)

    # Layer 2: Semantic analysis (optional)
    semantic: Optional[SemanticAnalysis] = None
    if params.enable_semantic:
        semantic = await analyze(result)

    # Store session for drill-down
    _sessions[params.label] = {
        "raw_text": raw_text,
        "raw_lines": raw_text.splitlines(),
        "result": result,
        "semantic": semantic,
    }

    # Build response
    parts = []
    parts.append(result.to_summary())

    if semantic:
        parts.append("\n" + semantic.to_summary())
    else:
        if params.enable_semantic:
            parts.append(
                "\n*Semantic analysis unavailable (no ANTHROPIC_API_KEY or httpx not installed). "
                "Showing deterministic analysis only.*"
            )

    parts.append(f"\n---")
    parts.append(f"💡 **Drill down**: Use `log_get_lines` with pattern/severity filters to see specific raw lines.")
    parts.append(f"📋 **Session**: `{params.label}` ({result.total_lines} lines stored)")

    return "\n".join(parts)


@mcp.tool(
    name="log_get_lines",
    annotations={
        "title": "Get Raw Log Lines",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def log_get_lines(params: LogGetLinesInput) -> str:
    """Retrieve specific raw lines from a previously ingested log.

    Use this after `log_ingest` to drill into specific errors, patterns, or
    line ranges. This is the Layer 3 "on-demand" retrieval — only the lines
    you ask for enter the context window.

    Args:
        params (LogGetLinesInput): Filters including pattern, severity, line range.

    Returns:
        str: Matching raw log lines with line numbers.
    """
    session = _sessions.get(params.label)
    if not session:
        available = list(_sessions.keys()) if _sessions else ["(none)"]
        return (
            f"Error: No log session found with label '{params.label}'. "
            f"Available sessions: {', '.join(available)}. "
            f"Run `log_ingest` first."
        )

    raw_lines = session["raw_lines"]

    # If around_line is specified, return context window
    if params.around_line is not None:
        center = params.around_line - 1  # 0-indexed
        start = max(0, center - params.context_lines)
        end = min(len(raw_lines), center + params.context_lines + 1)
        output_lines = []
        for i in range(start, end):
            marker = " >>> " if i == center else "     "
            output_lines.append(f"{marker}{i+1:>6} | {raw_lines[i]}")
        return (
            f"Lines {start+1}-{end} (centered on line {params.around_line}):\n"
            + "\n".join(output_lines)
        )

    # Filter by pattern and/or severity
    matches = []
    result = session["result"]

    for i, line in enumerate(raw_lines):
        if params.pattern:
            try:
                if not re.search(params.pattern, line, re.IGNORECASE):
                    continue
            except re.error:
                # Fallback to substring match if regex is invalid
                if params.pattern.lower() not in line.lower():
                    continue

        if params.severity:
            from preprocessor import detect_severity, strip_ansi
            cleaned = strip_ansi(line)
            sev = detect_severity(cleaned)
            if sev.value != params.severity.lower():
                continue

        matches.append((i + 1, line))
        if len(matches) >= params.max_lines:
            break

    if not matches:
        return (
            f"No lines matched the filters (pattern={params.pattern!r}, "
            f"severity={params.severity!r}). Try broader filters."
        )

    output_lines = [f"  {num:>6} | {line}" for num, line in matches]
    total_matching = len(matches)
    header = f"Found {total_matching} matching lines"
    if total_matching >= params.max_lines:
        header += f" (capped at {params.max_lines} — increase max_lines for more)"

    return header + ":\n" + "\n".join(output_lines)


@mcp.tool(
    name="log_get_analysis",
    annotations={
        "title": "Get Full Semantic Analysis",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def log_get_analysis(params: LogGetAnalysisInput) -> str:
    """Retrieve the full semantic analysis JSON for a previously ingested log.

    Returns the raw structured analysis from the Haiku model, useful when
    you need programmatic access to error signatures, timeline, etc.

    Args:
        params (LogGetAnalysisInput): Session label.

    Returns:
        str: JSON semantic analysis or error message.
    """
    session = _sessions.get(params.label)
    if not session:
        return f"Error: No session '{params.label}'. Run `log_ingest` first."

    semantic = session.get("semantic")
    if not semantic:
        return (
            "No semantic analysis available for this session. "
            "Re-run `log_ingest` with `enable_semantic=true` and ensure "
            "ANTHROPIC_API_KEY is set."
        )

    return json.dumps(semantic.raw_json, indent=2)


@mcp.tool(
    name="log_list_sessions",
    annotations={
        "title": "List Log Sessions",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def log_list_sessions(params: LogListSessionsInput) -> str:
    """List all active log sessions with basic stats.

    Args:
        params (LogListSessionsInput): No parameters needed.

    Returns:
        str: Table of active sessions with line counts and severity info.
    """
    if not _sessions:
        return "No active log sessions. Use `log_ingest` to analyze a log."

    lines = ["Active log sessions:\n"]
    for label, session in _sessions.items():
        result = session["result"]
        has_semantic = "✓" if session.get("semantic") else "✗"
        errors = result.severity_counts.get("error", 0) + result.severity_counts.get("fatal", 0)
        lines.append(
            f"- **{label}**: {result.total_lines} lines, "
            f"{result.unique_lines} unique, "
            f"{errors} errors, "
            f"semantic={has_semantic}"
        )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
