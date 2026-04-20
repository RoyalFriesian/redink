from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Core
    redink_home: Path = Path.home() / ".redink"
    redink_log_level: str = "INFO"
    redink_api_url: str = "http://localhost:8080"

    # Database / queue
    database_url: str = "postgresql+psycopg://redink:redink@localhost:5432/redink"
    redis_url: str = "redis://localhost:6379/0"

    # Engine
    redink_engine: str = "ollama"
    # Optional default model override. If unset, each engine uses its own
    # default (ollama → `ollama_model`, claude-code → `claude_code_model`).
    # Set this to force a specific model for every review regardless of engine.
    redink_model: str = ""
    ollama_host: str = "http://localhost:11434"
    ollama_model: str = "gemma4:e2b"
    ollama_max_parallel: int = 2
    # Claude Code CLI engine — shells out to the `claude` binary in headless
    # `-p` mode. No API key needed: the CLI uses the user's existing
    # Claude Code credentials.
    claude_code_binary: str = "claude"
    claude_code_model: str = "claude-sonnet-4-6"
    claude_code_effort: str = "high"  # low | medium | high | max
    claude_code_timeout_s: float = 600.0
    anthropic_api_key: str = ""
    openai_api_key: str = ""

    # GitHub — either a personal access token (POC path) or a GitHub App.
    # PAT takes precedence when set. Webhook secret is only needed for the M3
    # comment-reply engagement loop (GitHub signs incoming webhooks with it).
    github_pat: str = ""
    github_app_id: str = ""
    github_app_private_key_path: str = ""
    github_webhook_secret: str = ""

    # Slack
    slack_bot_token: str = ""
    slack_signing_secret: str = ""
    slack_app_token: str = ""  # xapp-... for socket mode
    slack_review_channel: str = "redink-reviews"

    # Context providers (all optional — empty creds disable the provider).
    #
    # Atlassian (Jira + Confluence): one API token works for both products on
    # Cloud. Set the shared pair below and leave the product-specific fields
    # empty. Only populate `jira_*` / `confluence_*` when you genuinely need
    # separate creds (e.g. Jira Cloud + Confluence Server).
    atlassian_email: str = ""
    atlassian_api_token: str = ""
    jira_base_url: str = ""
    jira_email: str = ""  # override atlassian_email for Jira only
    jira_api_token: str = ""  # override atlassian_api_token for Jira only
    confluence_base_url: str = ""
    confluence_email: str = ""  # override atlassian_email for Confluence only
    confluence_api_token: str = ""  # override atlassian_api_token for Confluence only

    def jira_auth(self) -> tuple[str, str] | None:
        email = self.jira_email or self.atlassian_email
        token = self.jira_api_token or self.atlassian_api_token
        return (email, token) if email and token else None

    def confluence_auth(self) -> tuple[str, str] | None:
        email = self.confluence_email or self.atlassian_email
        token = self.confluence_api_token or self.atlassian_api_token
        return (email, token) if email and token else None

    # Safety
    redink_allow_external_llm: bool = False
    redink_max_clarification_rounds: int = 3
    redink_max_comment_engagement_rounds: int = 3
    redink_per_pr_token_ceiling: int = 200_000
    redink_per_pr_wall_clock_ceiling_s: int = 600
    # Budget (in tokens) allotted to external-context chunks inside each prompt.
    # Kept well below the total ceiling because it applies to every engine call.
    redink_context_chunk_token_budget: int = 1500

    # Memory layer. SQLite backend is always available; Mempalace is opt-in
    # because it installs a ~300MB embedding model on first use.
    redink_memory_enabled: bool = True
    redink_mempalace_enabled: bool = False
    # Per-file content cap when snapshotting the repo (bytes). Larger files are
    # truncated; the compressor will squeeze further inside the prompt budget.
    redink_snapshot_file_bytes: int = 40_000
    # Total bytes across all snapshot chunks before we stop adding more.
    redink_snapshot_total_bytes: int = 400_000

    api_host: str = "0.0.0.0"
    api_port: int = 8080


@lru_cache
def settings() -> Settings:
    return Settings()
