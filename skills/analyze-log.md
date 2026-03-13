Analyze a log file or log text using the log-context MCP server.

## Steps

1. **Ingest the log** — call `log_ingest` with `enable_semantic=false`:
   - If the user provided a file path, use `file_path`
   - If the user pasted log text, use `log_text`
   - Use a descriptive `label` (e.g. `build_log`, `crash_dump`)

2. **Analyze the preprocessed summary** — from the Layer 1 output, identify:
   - **Primary issue**: what went wrong in one sentence
   - **Root cause**: the underlying reason (not just symptoms)
   - **Error timeline**: sequence of events that led to the failure
   - **Blast radius**: what services/components were affected
   - **Action items**: concrete next steps to investigate or fix

3. **Drill down if needed** — use `log_get_lines` to fetch raw lines for:
   - The first occurrence of the primary error
   - Any stack traces referenced in the summary
   - Lines immediately before the first error (to find the trigger)

4. **Report findings** concisely — lead with the root cause, not the symptom list.

## Notes

- Prefer `file_path` over pasting large logs as `log_text` to avoid bloating context
- Use `log_list_sessions` if the user references a previously ingested log
- Layer 1 alone (no external API) is sufficient — do not set `enable_semantic=true`
- If the log is ambiguous, ask the user what system/service it is from before analyzing
