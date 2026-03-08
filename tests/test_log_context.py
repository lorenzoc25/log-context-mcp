"""
Test suite for log-context MCP server.

Covers:
- Layer 1: Deterministic preprocessing (no API calls)
- Prompt generation (no API calls)
- Backend resolution logic (env var mocking)
"""

import json
import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from log_context_mcp.preprocessor import (
    Severity,
    LogLine,
    StackTrace,
    PreprocessorResult,
    preprocess,
    strip_ansi,
    detect_severity,
    extract_timestamp,
    is_noise,
    is_stack_trace_line,
)
from log_context_mcp.analyzer import (
    _build_analysis_prompt,
    _resolve_backend,
    _AnthropicBackend,
    _OpenAICompatibleBackend,
)


# ============================================================================
# Layer 1: Deterministic Preprocessing Tests
# ============================================================================

class TestANSIStripping:
    """Test ANSI escape code removal."""

    def test_strip_simple_color(self):
        """Strip basic ANSI color codes."""
        text = "\x1b[31mRED\x1b[0m"
        assert strip_ansi(text) == "RED"

    def test_strip_multiple_codes(self):
        """Strip multiple ANSI codes in sequence."""
        text = "\x1b[1;31mBold Red\x1b[0m \x1b[32mGreen\x1b[0m"
        assert strip_ansi(text) == "Bold Red Green"

    def test_strip_256_color(self):
        """Strip 256-color ANSI codes."""
        text = "\x1b[38;5;208mOrange\x1b[0m"
        assert strip_ansi(text) == "Orange"

    def test_no_ansi_codes(self):
        """Text without ANSI codes should be unchanged."""
        text = "Plain text"
        assert strip_ansi(text) == "Plain text"


class TestSeverityDetection:
    """Test severity classification."""

    def test_fatal_detection(self):
        """Detect FATAL severity."""
        assert detect_severity("[FATAL] System crash") == Severity.FATAL
        assert detect_severity("[CRITICAL] Database down") == Severity.FATAL
        assert detect_severity("CRIT: Error occurred") == Severity.FATAL

    def test_error_detection(self):
        """Detect ERROR severity."""
        assert detect_severity("[ERROR] Something went wrong") == Severity.ERROR
        assert detect_severity("ERR: File not found") == Severity.ERROR

    def test_warning_detection(self):
        """Detect WARNING severity."""
        assert detect_severity("[WARN] Low memory") == Severity.WARNING
        assert detect_severity("[WARNING] Deprecated API") == Severity.WARNING

    def test_info_detection(self):
        """Detect INFO severity."""
        assert detect_severity("[INFO] Server started") == Severity.INFO

    def test_debug_detection(self):
        """Detect DEBUG/TRACE severity."""
        assert detect_severity("[DEBUG] Variable x = 5") == Severity.DEBUG
        assert detect_severity("[TRACE] Entering function") == Severity.DEBUG
        assert detect_severity("[VERBOSE] Details here") == Severity.DEBUG

    def test_unknown_severity(self):
        """Return UNKNOWN for unclassified lines."""
        assert detect_severity("Random log line") == Severity.UNKNOWN
        assert detect_severity("2024-01-01 Server ready") == Severity.UNKNOWN

    def test_case_insensitive(self):
        """Severity detection should be case-insensitive."""
        assert detect_severity("error: Something failed") == Severity.ERROR
        assert detect_severity("Error: Something failed") == Severity.ERROR
        assert detect_severity("ERROR: Something failed") == Severity.ERROR


class TestTimestampExtraction:
    """Test timestamp detection and extraction."""

    def test_iso8601_timestamp(self):
        """Extract ISO-8601 timestamps."""
        line = "2024-01-15T10:30:45 [ERROR] Something failed"
        ts = extract_timestamp(line)
        assert ts == "2024-01-15T10:30:45"

    def test_iso8601_with_space(self):
        """Extract ISO-8601 timestamps with space instead of T."""
        line = "2024-01-15 10:30:45 [INFO] Server started"
        ts = extract_timestamp(line)
        assert ts == "2024-01-15 10:30:45"

    def test_apache_log_timestamp(self):
        """Extract Apache common log format timestamps."""
        line = '15/Jan/2024:10:30:45 +0000 "GET / HTTP/1.1"'
        ts = extract_timestamp(line)
        assert ts == "15/Jan/2024:10:30:45"

    def test_syslog_timestamp(self):
        """Extract syslog-style timestamps."""
        line = "Jan 15 10:30:45 hostname process[123]: Something happened"
        ts = extract_timestamp(line)
        assert ts == "Jan 15 10:30:45"

    def test_no_timestamp(self):
        """Return None if no timestamp present."""
        assert extract_timestamp("Random log line") is None
        assert extract_timestamp("[ERROR] No timestamp here") is None


class TestNoiseDetection:
    """Test noise pattern detection."""

    def test_blank_lines(self):
        """Detect blank and whitespace-only lines."""
        assert is_noise("")
        assert is_noise("   ")
        assert is_noise("\t")

    def test_separator_lines(self):
        """Detect separator lines."""
        assert is_noise("---")
        assert is_noise("----")
        assert is_noise("===")
        assert is_noise("=====")

    def test_ellipsis_lines(self):
        """Detect ellipsis lines."""
        assert is_noise("...")
        assert is_noise("  ...  ")

    def test_non_noise(self):
        """Non-noise lines should not match."""
        assert not is_noise("Real log message")
        assert not is_noise("[ERROR] Something")
        assert not is_noise("--no-hyphen-at-start")


class TestStackTraceDetection:
    """Test stack trace line identification."""

    def test_java_stack_trace(self):
        """Detect Java stack trace lines."""
        assert is_stack_trace_line("    at java.io.IOException")
        assert is_stack_trace_line("    at com.example.Class.method(Class.java:42)")

    def test_python_stack_trace(self):
        """Detect Python stack trace lines."""
        assert is_stack_trace_line('  File "script.py", line 10')
        assert is_stack_trace_line('    raise RuntimeError("error")')

    def test_go_goroutine_stack(self):
        """Detect Go goroutine stack traces."""
        assert is_stack_trace_line("    42:  main()")
        assert is_stack_trace_line("    1:  runtime.main()")

    def test_rust_caret_indicator(self):
        """Detect Rust error caret indicators."""
        assert is_stack_trace_line("       ^^^^")

    def test_java_caused_by(self):
        """Detect Java 'Caused by' lines."""
        assert is_stack_trace_line("    Caused by: java.io.IOException")

    def test_non_stack_trace(self):
        """Non-stack-trace lines should not match."""
        assert not is_stack_trace_line("ERROR: Something failed")
        assert not is_stack_trace_line("2024-01-15 10:30:45")


class TestFullPreprocessing:
    """Test the complete preprocessing pipeline."""

    def test_empty_input(self):
        """Handle empty input gracefully."""
        result = preprocess("")
        assert result.total_lines == 0
        assert result.unique_lines == 0
        assert result.noise_lines_removed == 0

    def test_all_noise(self):
        """Discard all-noise input."""
        text = "\n\n---\n===\n..."
        result = preprocess(text)
        assert result.total_lines == 5
        assert result.unique_lines == 0
        assert result.noise_lines_removed == 5

    def test_deduplication(self):
        """Deduplicate repeated lines."""
        text = "[ERROR] Connection failed\n[ERROR] Connection failed\n[ERROR] Connection failed"
        result = preprocess(text)
        assert result.total_lines == 3
        assert result.unique_lines == 1
        assert result.deduplicated[0][1] == 3  # count is 3

    def test_timestamp_normalization(self):
        """Deduplicate lines differing only in timestamp."""
        text = "[2024-01-15 10:00:00] User logged in\n[2024-01-15 10:00:01] User logged in"
        result = preprocess(text)
        # Both lines should be deduplicated as the same
        assert result.unique_lines == 1
        assert result.deduplicated[0][1] == 2

    def test_uuid_normalization(self):
        """Deduplicate lines differing only in UUID."""
        text = "Request a1b2c3d4-e5f6-4789-0123-456789abcdef completed\nRequest f9e8d7c6-b5a4-3210-fedc-ba9876543210 completed"
        result = preprocess(text)
        # Should be deduplicated
        assert result.unique_lines == 1

    def test_severity_counting(self):
        """Count severities correctly."""
        text = "[ERROR] Error 1\n[ERROR] Error 2\n[WARN] Warning\n[INFO] Info"
        result = preprocess(text)
        assert result.severity_counts.get("error") == 2
        assert result.severity_counts.get("warning") == 1
        assert result.severity_counts.get("info") == 1

    def test_stack_trace_grouping(self):
        """Group stack traces correctly."""
        text = """[ERROR] Exception occurred
    at java.io.IOException
    at com.example.Class.method(Class.java:42)
    Caused by: RuntimeException"""
        result = preprocess(text)
        assert len(result.stack_traces) == 1
        assert result.stack_traces[0].header_line is not None
        assert len(result.stack_traces[0].lines) > 0

    def test_timestamp_extraction(self):
        """Extract first and last timestamps."""
        text = "2024-01-15T10:00:00 Start\n[INFO] Middle\n2024-01-15T10:00:30 End"
        result = preprocess(text)
        assert result.first_timestamp == "2024-01-15T10:00:00"
        assert result.last_timestamp == "2024-01-15T10:00:30"

    def test_ansi_code_stripping(self):
        """Strip ANSI codes during preprocessing."""
        text = "\x1b[31m[ERROR]\x1b[0m Connection failed"
        result = preprocess(text)
        assert result.unique_lines == 1
        # ANSI codes should be removed
        assert "\x1b" not in result.deduplicated[0][0]

    def test_reduction_percentage(self):
        """Calculate deduplication reduction percentage."""
        text = "[ERROR] Same\n[ERROR] Same\n[ERROR] Same\n[ERROR] Other"
        result = preprocess(text)
        # 2 unique lines out of 4 = 50% reduction
        assert result.total_lines == 4
        assert result.unique_lines == 2
        assert result.reduction_pct == 50.0


# ============================================================================
# Prompt Generation Tests
# ============================================================================

class TestPromptGeneration:
    """Test Layer 2 prompt generation (no API calls)."""

    def test_basic_prompt_structure(self):
        """Prompt should contain expected sections."""
        result = PreprocessorResult(
            total_lines=100,
            unique_lines=50,
            noise_lines_removed=10,
            severity_counts={"error": 5, "warning": 3, "info": 2, "debug": 40},
            deduplicated=[
                ("ERROR: File not found", 5, Severity.ERROR),
                ("WARN: Low memory", 3, Severity.WARNING),
            ],
            stack_traces=[],
            timestamps_seen=[],
        )
        prompt = _build_analysis_prompt(result)

        assert "Total lines: 100" in prompt
        assert "Unique: 50" in prompt
        assert "ERROR" in prompt and "×5" in prompt
        assert "WARNING" in prompt and "×3" in prompt

    def test_prompt_with_timestamps(self):
        """Prompt should include timestamp range."""
        result = PreprocessorResult(
            total_lines=10,
            unique_lines=5,
            noise_lines_removed=0,
            severity_counts={},
            deduplicated=[],
            stack_traces=[],
            timestamps_seen=["2024-01-15T10:00:00", "2024-01-15T10:00:30"],
            first_timestamp="2024-01-15T10:00:00",
            last_timestamp="2024-01-15T10:00:30",
        )
        prompt = _build_analysis_prompt(result)
        assert "2024-01-15T10:00:00" in prompt
        assert "2024-01-15T10:00:30" in prompt

    def test_prompt_with_stack_traces(self):
        """Prompt should include stack traces."""
        st = StackTrace(header_line=5, lines=["at java.io.IOException", "at com.example.Class"])
        result = PreprocessorResult(
            total_lines=10,
            unique_lines=5,
            noise_lines_removed=0,
            severity_counts={},
            deduplicated=[],
            stack_traces=[st],
            timestamps_seen=[],
        )
        prompt = _build_analysis_prompt(result)
        assert "STACK TRACES" in prompt
        assert "Trace 1" in prompt

    def test_prompt_caps_at_100_errors(self):
        """Prompt should cap error lines at 100."""
        errors = [("ERROR: " + str(i), 1, Severity.ERROR) for i in range(150)]
        result = PreprocessorResult(
            total_lines=150,
            unique_lines=150,
            noise_lines_removed=0,
            severity_counts={"error": 150},
            deduplicated=errors,
            stack_traces=[],
            timestamps_seen=[],
        )
        prompt = _build_analysis_prompt(result)
        # Should only include 100 error lines
        assert prompt.count("[ERROR") == 100


# ============================================================================
# Backend Resolution Tests
# ============================================================================

class TestBackendResolution:
    """Test backend selection logic."""

    @pytest.mark.asyncio
    async def test_explicit_anthropic_backend(self):
        """Explicitly select Anthropic backend via env var."""
        with patch.dict(os.environ, {
            "LOG_CONTEXT_BACKEND": "anthropic",
            "ANTHROPIC_API_KEY": "test-key-123",
        }):
            result = await _resolve_backend()
            assert result is not None
            backend, model = result
            assert isinstance(backend, _AnthropicBackend)
            assert backend.api_key == "test-key-123"
            assert model == "claude-haiku-4-5-20251001"

    @pytest.mark.asyncio
    async def test_explicit_openai_backend(self):
        """Explicitly select OpenAI backend via env var."""
        with patch.dict(os.environ, {
            "LOG_CONTEXT_BACKEND": "openai",
            "OPENAI_API_KEY": "sk-test-123",
        }):
            result = await _resolve_backend()
            assert result is not None
            backend, model = result
            assert isinstance(backend, _OpenAICompatibleBackend)
            assert backend.api_key == "sk-test-123"
            assert model == "gpt-4o-mini"

    @pytest.mark.asyncio
    async def test_explicit_ollama_backend(self):
        """Explicitly select Ollama backend via env var."""
        with patch.dict(os.environ, {
            "LOG_CONTEXT_BACKEND": "ollama",
        }, clear=False):
            result = await _resolve_backend()
            assert result is not None
            backend, model = result
            assert isinstance(backend, _OpenAICompatibleBackend)
            assert backend.api_key is None
            assert backend.base_url == "http://localhost:11434/v1"
            assert model == "llama3"

    @pytest.mark.asyncio
    async def test_auto_detect_anthropic(self):
        """Auto-detect Anthropic backend from ANTHROPIC_API_KEY."""
        with patch.dict(os.environ, {
            "ANTHROPIC_API_KEY": "test-key-456",
            "LOG_CONTEXT_BACKEND": "",
        }):
            result = await _resolve_backend()
            assert result is not None
            backend, model = result
            assert isinstance(backend, _AnthropicBackend)

    @pytest.mark.asyncio
    async def test_auto_detect_openai(self):
        """Auto-detect OpenAI backend from OPENAI_API_KEY."""
        with patch.dict(os.environ, {
            "OPENAI_API_KEY": "sk-test-456",
            "ANTHROPIC_API_KEY": "",
            "LOG_CONTEXT_BACKEND": "",
        }):
            result = await _resolve_backend()
            assert result is not None
            backend, model = result
            assert isinstance(backend, _OpenAICompatibleBackend)

    @pytest.mark.asyncio
    async def test_custom_model_name(self):
        """Use custom model name from LOG_CONTEXT_MODEL."""
        with patch.dict(os.environ, {
            "LOG_CONTEXT_BACKEND": "anthropic",
            "ANTHROPIC_API_KEY": "test-key",
            "LOG_CONTEXT_MODEL": "claude-opus-4-20250514",
        }):
            result = await _resolve_backend()
            assert result is not None
            backend, model = result
            assert model == "claude-opus-4-20250514"
            assert backend.model == "claude-opus-4-20250514"

    @pytest.mark.asyncio
    async def test_custom_openai_base_url(self):
        """Use custom base URL from OPENAI_BASE_URL."""
        with patch.dict(os.environ, {
            "LOG_CONTEXT_BACKEND": "openai",
            "OPENAI_API_KEY": "test-key",
            "OPENAI_BASE_URL": "https://custom.example.com/v1",
        }):
            result = await _resolve_backend()
            assert result is not None
            backend, model = result
            assert backend.base_url == "https://custom.example.com/v1"

    @pytest.mark.asyncio
    async def test_no_backend_available(self):
        """Return None when no backend is configured."""
        with patch.dict(os.environ, {
            "ANTHROPIC_API_KEY": "",
            "OPENAI_API_KEY": "",
            "LOG_CONTEXT_BACKEND": "",
        }, clear=True):
            # Mock httpx to prevent Ollama check from succeeding
            with patch("log_context_mcp.analyzer.httpx.AsyncClient") as mock_client:
                mock_instance = AsyncMock()
                mock_instance.__aenter__.return_value = mock_instance
                mock_instance.get.return_value = AsyncMock(status_code=404)
                mock_client.return_value = mock_instance

                result = await _resolve_backend()
                assert result is None

    @pytest.mark.asyncio
    async def test_explicit_backend_requires_api_key(self):
        """Explicit Anthropic backend requires API key."""
        with patch.dict(os.environ, {
            "LOG_CONTEXT_BACKEND": "anthropic",
            "ANTHROPIC_API_KEY": "",
        }):
            result = await _resolve_backend()
            assert result is None


# ============================================================================
# Backend Class Tests
# ============================================================================

class TestAnthropicBackend:
    """Test Anthropic backend implementation."""

    def test_initialization(self):
        """Backend should store API key and model."""
        backend = _AnthropicBackend("test-key", "claude-haiku-4-5-20251001")
        assert backend.api_key == "test-key"
        assert backend.model == "claude-haiku-4-5-20251001"

    @pytest.mark.asyncio
    async def test_call_request_format(self):
        """Anthropic backend should format request correctly."""
        backend = _AnthropicBackend("test-key", "test-model")

        with patch("log_context_mcp.analyzer.httpx.AsyncClient") as mock_client:
            mock_response = AsyncMock()
            mock_response.json = MagicMock(return_value={
                "content": [{"type": "text", "text": "Analysis result"}]
            })
            mock_response.raise_for_status = MagicMock()

            mock_instance = AsyncMock()
            mock_instance.__aenter__.return_value = mock_instance
            mock_instance.post = AsyncMock(return_value=mock_response)
            mock_client.return_value = mock_instance

            result = await backend.call("system", "user prompt")

            # Verify request was made correctly
            mock_instance.post.assert_called_once()
            call_args = mock_instance.post.call_args
            assert call_args[0][0] == "https://api.anthropic.com/v1/messages"
            assert call_args[1]["headers"]["x-api-key"] == "test-key"
            assert call_args[1]["json"]["model"] == "test-model"
            assert result == "Analysis result"


class TestOpenAICompatibleBackend:
    """Test OpenAI-compatible backend implementation."""

    def test_initialization_with_key(self):
        """Backend should store API key, model, and base URL."""
        backend = _OpenAICompatibleBackend("sk-123", "gpt-4", "https://api.openai.com/v1")
        assert backend.api_key == "sk-123"
        assert backend.model == "gpt-4"
        assert backend.base_url == "https://api.openai.com/v1"

    def test_initialization_without_key(self):
        """Backend should work without API key (e.g., local Ollama)."""
        backend = _OpenAICompatibleBackend(None, "llama3", "http://localhost:11434/v1")
        assert backend.api_key is None
        assert backend.model == "llama3"

    @pytest.mark.asyncio
    async def test_call_with_api_key(self):
        """OpenAI backend should include Authorization header when key is present."""
        backend = _OpenAICompatibleBackend("sk-test", "gpt-4", "https://api.openai.com/v1")

        with patch("log_context_mcp.analyzer.httpx.AsyncClient") as mock_client:
            mock_response = AsyncMock()
            mock_response.json = MagicMock(return_value={
                "choices": [{"message": {"content": "Analysis result"}}]
            })
            mock_response.raise_for_status = MagicMock()

            mock_instance = AsyncMock()
            mock_instance.__aenter__.return_value = mock_instance
            mock_instance.post = AsyncMock(return_value=mock_response)
            mock_client.return_value = mock_instance

            result = await backend.call("system", "user prompt")

            call_args = mock_instance.post.call_args
            assert "Authorization" in call_args[1]["headers"]
            assert call_args[1]["headers"]["Authorization"] == "Bearer sk-test"
            assert result == "Analysis result"

    @pytest.mark.asyncio
    async def test_call_without_api_key(self):
        """OpenAI backend should omit Authorization header when key is None."""
        backend = _OpenAICompatibleBackend(None, "llama3", "http://localhost:11434/v1")

        with patch("log_context_mcp.analyzer.httpx.AsyncClient") as mock_client:
            mock_response = AsyncMock()
            mock_response.json = MagicMock(return_value={
                "choices": [{"message": {"content": "Analysis result"}}]
            })
            mock_response.raise_for_status = MagicMock()

            mock_instance = AsyncMock()
            mock_instance.__aenter__.return_value = mock_instance
            mock_instance.post = AsyncMock(return_value=mock_response)
            mock_client.return_value = mock_instance

            result = await backend.call("system", "user prompt")

            call_args = mock_instance.post.call_args
            assert "Authorization" not in call_args[1]["headers"]

    @pytest.mark.asyncio
    async def test_call_uses_correct_endpoint(self):
        """OpenAI backend should use correct endpoint with base URL."""
        backend = _OpenAICompatibleBackend("sk-123", "gpt-4", "https://custom.com/v1")

        with patch("log_context_mcp.analyzer.httpx.AsyncClient") as mock_client:
            mock_response = AsyncMock()
            mock_response.json = MagicMock(return_value={
                "choices": [{"message": {"content": "result"}}]
            })
            mock_response.raise_for_status = MagicMock()

            mock_instance = AsyncMock()
            mock_instance.__aenter__.return_value = mock_instance
            mock_instance.post = AsyncMock(return_value=mock_response)
            mock_client.return_value = mock_instance

            await backend.call("system", "user")

            call_args = mock_instance.post.call_args
            assert "custom.com/v1/chat/completions" in call_args[0][0]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
