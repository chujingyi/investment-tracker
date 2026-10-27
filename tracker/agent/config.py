"""Agent configuration, all read from environment variables with safe defaults."""

import os

DEFAULT_BASE_URL = "https://api.deepseek.com/v1"


class AgentNotConfigured(RuntimeError):
    """Raised when the LLM key is missing and an agent feature was invoked."""


class BudgetExceeded(RuntimeError):
    """Raised when today's LLM token usage exceeds the configured budget."""


def llm_base_url():
    return os.environ.get("LLM_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def llm_api_key():
    return os.environ.get("LLM_API_KEY", "").strip()


def llm_model():
    return os.environ.get("LLM_MODEL", "deepseek-chat")


def llm_fast_model():
    """Model for high-frequency light tasks (monitor agent). Falls back to llm_model."""
    return os.environ.get("LLM_FAST_MODEL") or llm_model()


def llm_strong_model():
    """Model for deep analysis tasks (research agent). Falls back to llm_model."""
    return os.environ.get("LLM_STRONG_MODEL") or llm_model()


def llm_review_model():
    """Model for periodic review tasks. Falls back to llm_model."""
    return os.environ.get("LLM_REVIEW_MODEL") or llm_model()


def input_price_per_m():
    """Estimated RMB per 1M prompt tokens; verify against provider billing."""
    try:
        return float(os.environ.get("LLM_INPUT_PRICE_PER_M", "1.0"))
    except ValueError:
        return 1.0


def output_price_per_m():
    """Estimated RMB per 1M completion tokens; verify against provider billing."""
    try:
        return float(os.environ.get("LLM_OUTPUT_PRICE_PER_M", "2.0"))
    except ValueError:
        return 2.0


def llm_temperature():
    try:
        return float(os.environ.get("LLM_TEMPERATURE", "0.3"))
    except ValueError:
        return 0.3


def max_iterations():
    try:
        return max(1, int(os.environ.get("AGENT_MAX_ITERATIONS", "8")))
    except ValueError:
        return 8


def daily_token_budget():
    try:
        return max(0, int(os.environ.get("AGENT_DAILY_TOKEN_BUDGET", "200000")))
    except ValueError:
        return 200000


def morning_enabled():
    return os.environ.get("AGENT_MORNING_ENABLED", "1").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def request_timeout():
    try:
        return max(10, int(os.environ.get("LLM_TIMEOUT_SECONDS", "60")))
    except ValueError:
        return 60


def is_configured():
    return bool(llm_api_key())


def require_configured():
    if not is_configured():
        raise AgentNotConfigured(
            "LLM is not configured: set LLM_API_KEY (and optionally LLM_BASE_URL / LLM_MODEL) in .env"
        )


def run_timeout():
    try:
        return max(10, min(600, int(os.environ.get("AGENT_RUN_TIMEOUT_SECONDS", "90"))))
    except ValueError:
        return 90


def version():
    return os.environ.get("AGENT_VERSION", "investment-v1")


def model_prices(model):
    """Optional per-model estimated CNY pricing; malformed config fails visibly."""
    import json
    mapping = json.loads(os.environ.get("LLM_MODEL_PRICES", "{}"))
    pair = mapping.get(model)
    if pair is None:
        return input_price_per_m(), output_price_per_m()
    incoming, outgoing = float(pair["input"]), float(pair["output"])
    if incoming < 0 or outgoing < 0:
        raise ValueError("模型单价不能为负")
    return incoming, outgoing


def max_output_tokens():
    try:
        return max(64, min(8192, int(os.environ.get("LLM_MAX_OUTPUT_TOKENS", "1024"))))
    except ValueError:
        return 1024
