"""
log-context-mcp: Token-efficient log preprocessing for LLM coding agents.

A three-layer approach to log analysis:
- Layer 1: Deterministic preprocessing (dedup, severity detection, stack traces)
- Layer 2: Semantic analysis (LLM-powered classification and root cause detection)
- Layer 3: Drill-down on demand (specific line retrieval with context)
"""

__version__ = "0.1.1"
