# log-context-mcp
An MCP server that sits between raw logs and the LLM context window. Instead of dumping thousands of log lines into the context (burning tokens on noise), the coding agent calls `log_ingest` and gets back a structured, deduplicated summary in ~500-2000 tokens. It can then drill down into specific patterns on demand.
