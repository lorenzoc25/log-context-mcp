"""
Layer 1: Deterministic log preprocessor.

Zero-LLM-cost filtering that handles:
- ANSI color code stripping
- Line deduplication with occurrence counting
- Severity detection via common patterns
- Stack trace grouping
- Blank line / noise removal
- Timestamp extraction
"""

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
from collections import Counter, OrderedDict


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

# Broad severity patterns (case-insensitive) covering most log frameworks
SEVERITY_PATTERNS = [
    (re.compile(r"\b(FATAL|CRITICAL|CRIT|EMERG|ALERT)\b", re.I), "fatal"),
    (re.compile(r"\b(ERROR|ERR)\b", re.I), "error"),
    (re.compile(r"\b(WARN(?:ING)?)\b", re.I), "warning"),
    (re.compile(r"\b(INFO|NOTICE)\b", re.I), "info"),
    (re.compile(r"\b(DEBUG|TRACE|VERBOSE)\b", re.I), "debug"),
]

# Patterns that indicate a stack trace continuation line
STACK_TRACE_INDICATORS = [
    re.compile(r"^\s+at\s+"),            # Java / Node.js
    re.compile(r"^\s+File\s+\""),         # Python
    re.compile(r"^\s+\.\.\."),            # Elided frames
    re.compile(r"^\s*\|"),               # Rust / some formatters
    re.compile(r"^\s+\d+:\s"),           # Go goroutine stacks
    re.compile(r"^Traceback \("),         # Python traceback header
    re.compile(r"^\s+raise\s"),           # Python raise line
    re.compile(r"^\s*Caused by:"),        # Java chained exceptions
    re.compile(r"^\s+\^+"),              # Caret error indicators
]

# Matches bare exception/error lines at the start of a line.
# Heuristic: starts with a PascalCase identifier followed by a colon,
# with no leading whitespace (e.g. "TimeoutError: ...", "RuntimeError: ...",
# "java.lang.NullPointerException: ...", "panic: ...").
_EXCEPTION_LINE_RE = re.compile(
    r"^(?:[a-z]+\.)*[A-Z]\w*(?:[:,]\s|\s*$)"
)

# Lines that are pure noise
NOISE_PATTERNS = [
    re.compile(r"^\s*$"),                            # blank
    re.compile(r"^-{3,}$"),                          # separator lines
    re.compile(r"^={3,}$"),
    re.compile(r"^\s*\.\.\.\s*$"),                   # ellipsis lines
]

# Timestamp extraction (ISO-8601, common log formats)
TIMESTAMP_PATTERNS = [
    re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}"),           # ISO-8601
    re.compile(r"\d{2}/\w{3}/\d{4}:\d{2}:\d{2}:\d{2}"),              # nginx/CLF
    re.compile(r"\[?\w{3}\s+\w{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}\s+\d{4}\]?"),  # Apache
    re.compile(r"\w{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}"),              # syslog
]


class Severity(str, Enum):
    """Log line severity levels."""

    FATAL = "fatal"
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"
    DEBUG = "debug"
    UNKNOWN = "unknown"


@dataclass
class LogLine:
    """A single processed log line."""
    raw: str
    cleaned: str
    severity: Severity
    timestamp: Optional[str]
    line_number: int
    is_stack_trace: bool = False


@dataclass
class StackTrace:
    """A grouped stack trace block."""
    header_line: int
    lines: list[str] = field(default_factory=list)
    severity: Severity = Severity.ERROR

    @property
    def text(self) -> str:
        """Full stack trace text."""
        return "\n".join(self.lines)

    @property
    def summary(self) -> str:
        """First and last meaningful lines of the trace."""
        if len(self.lines) <= 4:
            return self.text
        omitted = len(self.lines) - 4
        return "\n".join(
            self.lines[:2]
            + [f"  ... ({omitted} frames omitted)"]
            + self.lines[-2:]
        )


@dataclass
class PreprocessorResult:  # pylint: disable=too-many-instance-attributes
    """Output of the deterministic preprocessing pass."""
    total_lines: int
    unique_lines: int
    noise_lines_removed: int
    severity_counts: dict[str, int]
    deduplicated: list[tuple[str, int, Severity]]  # (line, count, severity)
    stack_traces: list[StackTrace]
    timestamps_seen: list[str]
    first_timestamp: Optional[str] = None
    last_timestamp: Optional[str] = None

    @property
    def reduction_pct(self) -> float:
        """Percentage of lines removed by deduplication."""
        if self.total_lines == 0:
            return 0.0
        return round((1 - self.unique_lines / self.total_lines) * 100, 1)

    def to_summary(self, max_dedup_lines: int = 50) -> str:
        """Produce a token-efficient text summary for the LLM."""
        parts = []
        parts.append("## Log Preprocessing Summary")
        parts.append(f"- **Total lines**: {self.total_lines}")
        parts.append(
            f"- **Unique lines**: {self.unique_lines} ({self.reduction_pct}% reduction)"
        )
        parts.append(f"- **Noise removed**: {self.noise_lines_removed}")
        if self.first_timestamp and self.last_timestamp:
            parts.append(
                f"- **Time range**: {self.first_timestamp} → {self.last_timestamp}"
            )

        # Severity breakdown
        parts.append("\n### Severity Breakdown")
        for sev in ["fatal", "error", "warning", "info", "debug", "unknown"]:
            count = self.severity_counts.get(sev, 0)
            if count > 0:
                parts.append(f"- {sev.upper()}: {count}")

        # Stack traces
        if self.stack_traces:
            num_traces = len(self.stack_traces)
            parts.append(f"\n### Stack Traces ({num_traces} found)")
            for i, st in enumerate(self.stack_traces[:5]):
                parts.append(f"\n**Trace {i + 1}** (line {st.header_line}):")
                parts.append(f"```\n{st.summary}\n```")
            if len(self.stack_traces) > 5:
                num_more = len(self.stack_traces) - 5
                parts.append(f"\n... and {num_more} more traces")

        # Deduplicated error/warning lines (most important)
        error_warn = [
            (l, c, s)
            for l, c, s in self.deduplicated
            if s in (Severity.ERROR, Severity.WARNING, Severity.FATAL)
        ]
        if error_warn:
            parts.append("\n### Error/Warning Lines (deduplicated)")
            for line, count, sev in error_warn[:max_dedup_lines]:
                prefix = f"[{sev.value.upper()}]"
                suffix = f" (×{count})" if count > 1 else ""
                parts.append(f"- {prefix} {_clean_for_display(line)}{suffix}")

        # High-frequency info/debug (only if repeated many times)
        high_freq = [
            (l, c, s)
            for l, c, s in self.deduplicated
            if s not in (Severity.ERROR, Severity.WARNING, Severity.FATAL)
            and c >= 5
        ]
        if high_freq:
            parts.append("\n### High-Frequency Lines (≥5 occurrences)")
            for line, count, sev in high_freq[:20]:
                parts.append(f"- [{sev.value.upper()}] {_clean_for_display(line)} (×{count})")

        return "\n".join(parts)


# ---------------------------------------------------------------------------
# Core preprocessing functions
# ---------------------------------------------------------------------------

def strip_ansi(text: str) -> str:
    """Remove ANSI escape codes from text."""
    return ANSI_ESCAPE.sub("", text)


def detect_severity(line: str) -> Severity:
    """Detect the severity level of a log line."""
    for pattern, sev_name in SEVERITY_PATTERNS:
        if pattern.search(line):
            return Severity(sev_name)
    return Severity.UNKNOWN


def extract_timestamp(line: str) -> Optional[str]:
    """Extract timestamp from a log line if present."""
    for pattern in TIMESTAMP_PATTERNS:
        match = pattern.search(line)
        if match:
            return match.group(0)
    return None


def is_noise(line: str) -> bool:
    """Check if a line is pure noise (blank, separator, etc)."""
    for pattern in NOISE_PATTERNS:
        if pattern.match(line):
            return True
    return False


def is_stack_trace_line(line: str) -> bool:
    """Check if a line is part of a stack trace."""
    for pattern in STACK_TRACE_INDICATORS:
        if pattern.match(line):
            return True
    return False


def _classify_lines(raw_lines: list[str]) -> tuple[list[LogLine], int]:
    """Phase 1: Clean, classify, and filter noise from raw log lines."""
    processed: list[LogLine] = []
    noise_count = 0
    for i, raw_line in enumerate(raw_lines):
        cleaned = strip_ansi(raw_line).rstrip()
        if is_noise(cleaned):
            noise_count += 1
            continue
        processed.append(LogLine(
            raw=raw_line,
            cleaned=cleaned,
            severity=detect_severity(cleaned),
            timestamp=extract_timestamp(cleaned),
            line_number=i + 1,
            is_stack_trace=is_stack_trace_line(cleaned),
        ))
    return processed, noise_count


def _group_stack_traces(processed: list[LogLine]) -> list[StackTrace]:
    """Phase 2: Group consecutive stack trace lines into StackTrace objects."""
    stack_traces: list[StackTrace] = []
    current_trace: Optional[StackTrace] = None

    for ll in processed:
        if ll.is_stack_trace:
            if current_trace is None:
                current_trace = StackTrace(header_line=ll.line_number)
            current_trace.lines.append(ll.cleaned)
        elif current_trace is not None:
            if ll.cleaned and ll.cleaned[0] in (" ", "\t"):
                # Indented code context line within the trace
                current_trace.lines.append(ll.cleaned)
            elif _EXCEPTION_LINE_RE.match(ll.cleaned):
                # Bare exception line e.g. "TimeoutError: No connections..."
                current_trace.lines.append(ll.cleaned)
                stack_traces.append(current_trace)
                current_trace = None
            else:
                # Non-indented, non-exception line — trace ended
                if ll.severity in (Severity.ERROR, Severity.FATAL):
                    current_trace.lines.append(ll.cleaned)
                    current_trace.severity = ll.severity
                stack_traces.append(current_trace)
                current_trace = None

    if current_trace is not None:
        stack_traces.append(current_trace)
    return stack_traces


def _deduplicate(processed: list[LogLine]) -> list[tuple[str, int, "Severity"]]:
    """Phase 3: Deduplicate non-stack-trace lines, normalizing timestamps."""
    line_counter: Counter = Counter()
    line_severity: dict[str, Severity] = {}
    seen_order: OrderedDict = OrderedDict()
    norm_to_raw: dict[str, str] = {}

    for ll in processed:
        if not ll.is_stack_trace:
            norm_key = _normalize_for_dedup(ll.cleaned)
            line_counter[norm_key] += 1
            existing = line_severity.get(norm_key, Severity.UNKNOWN)
            if _severity_rank(ll.severity) > _severity_rank(existing):
                line_severity[norm_key] = ll.severity
            if norm_key not in seen_order:
                seen_order[norm_key] = True
                norm_to_raw[norm_key] = ll.cleaned

    return [
        (norm_to_raw[key], line_counter[key], line_severity.get(key, Severity.UNKNOWN))
        for key in seen_order
    ]


def preprocess(raw_text: str) -> PreprocessorResult:
    """
    Run the full deterministic preprocessing pipeline on raw log text.
    Returns a PreprocessorResult with deduplicated lines, grouped stack traces,
    and summary statistics.
    """
    raw_lines = raw_text.splitlines()
    total = len(raw_lines)

    processed, noise_count = _classify_lines(raw_lines)
    stack_traces = _group_stack_traces(processed)
    deduplicated = _deduplicate(processed)

    # Collect timestamps
    timestamps = [ll.timestamp for ll in processed if ll.timestamp]

    # Severity counts
    sev_counts: dict[str, int] = {}
    for ll in processed:
        sev_counts[ll.severity.value] = sev_counts.get(ll.severity.value, 0) + 1

    return PreprocessorResult(
        total_lines=total,
        unique_lines=len(deduplicated),
        noise_lines_removed=noise_count,
        severity_counts=sev_counts,
        deduplicated=deduplicated,
        stack_traces=stack_traces,
        timestamps_seen=timestamps,
        first_timestamp=timestamps[0] if timestamps else None,
        last_timestamp=timestamps[-1] if timestamps else None,
    )


def _normalize_for_dedup(line: str) -> str:
    """Strip timestamps and variable numbers for dedup comparison.

    This allows lines that differ only in their timestamp or request ID
    to be counted as duplicates.
    """
    result = line
    # Remove common timestamp patterns
    for pattern in TIMESTAMP_PATTERNS:
        result = pattern.sub("<TS>", result)
    # Normalize UUIDs
    uuid_pattern = (
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    )
    result = re.sub(uuid_pattern, "<UUID>", result, flags=re.I)
    # Normalize hex addresses / pointers
    result = re.sub(r"0x[0-9a-f]+", "<ADDR>", result, flags=re.I)
    # Normalize pure numeric sequences (ports, PIDs, etc.)
    # Only normalize numbers that look like IDs (standalone numbers > 3 digits)
    result = re.sub(r"\b\d{4,}\b", "<NUM>", result)
    return result


_SEVERITY_WORD_RE = re.compile(
    r"\[?\s*\b(FATAL|CRITICAL|CRIT|ALERT|EMERG|NOTICE"
    r"|ERROR|ERR|WARN(?:ING)?|INFO|DEBUG|TRACE|VERBOSE)\b\s*\]?\s*",
    re.I,
)


def _clean_for_display(line: str) -> str:
    """Strip timestamps, empty brackets, and leading severity keywords for compact display."""
    result = line
    for pattern in TIMESTAMP_PATTERNS:
        result = pattern.sub("", result)
    # Remove empty brackets left after timestamp removal (e.g. "[]" from "[Sun Dec 04...]")
    result = re.sub(r"\[\s*\]", "", result)
    # Remove leading severity word left over after timestamp removal
    result = _SEVERITY_WORD_RE.sub("", result, count=1)
    return result.strip()


def _severity_rank(sev: Severity) -> int:
    return {
        Severity.FATAL: 5,
        Severity.ERROR: 4,
        Severity.WARNING: 3,
        Severity.INFO: 2,
        Severity.DEBUG: 1,
        Severity.UNKNOWN: 0,
    }[sev]
