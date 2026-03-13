Analyze a log file or log text using the log-context MCP server.

## Steps

1. **Ingest the log** — call `log_ingest` with `enable_semantic=false`:
   - If the user provided a file path, use `file_path`
   - If the user pasted log text, use `log_text`
   - Use a descriptive `label` (e.g. `build_log`, `crash_dump`)

2. **Spin up the `log-analyzer` sub-agent** (runs on Haiku) using the Agent tool, passing the full `log_ingest` output as the prompt.

3. **Drill down if needed** — use `log_get_lines` to fetch raw lines for stack traces or patterns the sub-agent flagged.

4. **Report findings** back to the user, leading with root cause.

## Notes

- The `log-analyzer` agent runs on Haiku — cheap and fast for this task
- Layer 1 compression means the agent only sees ~1000 tokens regardless of original log size
- Prefer `file_path` over `log_text` for large logs
- Use `log_list_sessions` if the user references a previously ingested log
