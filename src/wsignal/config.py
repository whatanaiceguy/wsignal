from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

METERED_ADAPTERS: tuple[str, ...] = ("brave", "epo", "rospatent", "x")

COMPOSITE_QUOTA: dict[str, int] = {"brave": 3}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="WSIGNAL_", env_file=".env", extra="ignore", populate_by_name=True,
        env_ignore_empty=True,
    )

    database_url: str = "postgresql+asyncpg://wsignal:wsignal@localhost:5432/wsignal"
    mode: Literal["auto", "local", "relay"] = "auto"
    relay_url: str = "https://api.1f608.com"
    route_llm: str = ""
    route_corpus: str = ""
    route_brave: str = ""
    route_epo: str = ""
    route_rospatent: str = ""
    route_x: str = ""

    corpus_url: str = Field(
        default="", validation_alias=AliasChoices("WSIGNAL_CORPUS_URL", "CORPUS_URL")
    )
    corpus_search_default_limit: int = 25
    corpus_search_max_limit: int = 25
    corpus_command_timeout_s: float = 25.0
    agent_corpus_command_timeout_s: float = 25.0
    citation_quote_similarity_threshold: float = 0.90
    debug: bool = False
    resume_on_startup: bool = False

    llm_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("WSIGNAL_LLM_API_KEY", "OPENROUTER_API_KEY"),
    )
    llm_base_url: str = "https://openrouter.ai/api/v1"
                                                            #openai/gpt-6-luna
    llm_model_orchestrator: str = "z-ai/glm-5.3-flash" # z-ai/glm-5.3 best for reasoning, xiaomi/mimo-v2.6-pro too slow 
    llm_model_assistant: str = "z-ai/glm-5.3-flash"   # openai/gpt-6-luna best for general use and speed for large volumes (or is it? glm flash might be best),
    llm_model_researcher: str = "openai/gpt-6-luna" # but it lacks reasoning, so refuter might benefit from smarter one.
    llm_model_refuter: str = "openai/gpt-6-luna" # z-ai/glm-5.3-flash probably best fit for sub agents?
    llm_model_extractor: str = "openai/gpt-6-luna"
    
    llm_reasoning_effort_by_role: dict[str, str] = {
        "orchestrator": "high",
        "assistant": "high",
        "researcher": "medium",
        "refuter": "medium",
        "extractor": "medium",
    }
    llm_provider_by_role: dict[str, dict] = {
        "researcher": {"only": ["openai/flex"], "allow_fallbacks": False},
        "refuter": {"only": ["openai/flex"], "allow_fallbacks": False},
    }

    max_research: int = 20
    split_max_fields: int = 30
    shard_size: int = 10
    assistant_max_searches: int = 8
    assistant_lookup_max_steps: int = 10
    assistant_lookup_web_calls: int = 15
    assistant_lookup_answer_chars: int = 2500
    orchestrator_web_calls: int = 10
    researcher_web_calls: int = 50
    refuter_web_calls: int = 30

    top_n: int = 15
    entry_score_substance_weight: float = 0.75
    entry_score_momentum_weight: float = 0.25
    signal_noise_below: float = 0.25
    signal_faintness_gate_centre: float = 0.275
    signal_faintness_gate_width: float = 0.05
    signal_strong_substance_at_least: float = 0.6

    report_dir: str = "tmp/runs"
    web_dist: str = "web/dist"

    epo_consumer_key: str = Field(
        default="",
        validation_alias=AliasChoices("WSIGNAL_EPO_CONSUMER_KEY", "EPO_CONSUMER_KEY"),
    )
    epo_consumer_secret_key: str = Field(
        default="",
        validation_alias=AliasChoices(
            "WSIGNAL_EPO_CONSUMER_SECRET_KEY", "EPO_CONSUMER_SECRET_KEY"
        ),
    )
    rospatent_api_key: str = Field(
        default="",
        validation_alias=AliasChoices(
            "WSIGNAL_ROSPATENT_API_KEY", "SEARCHPLATFORM_ROSPATENT_API"
        ),
    )
    x_bearer_token: str = Field(
        default="",
        validation_alias=AliasChoices("WSIGNAL_X_BEARER_TOKEN", "X_BEARER_TOKEN"),
    )
    brave_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("WSIGNAL_BRAVE_API_KEY", "BRAVE_API_KEY"),
    )

    quota_brave: int = 20
    quota_epo: int = 10
    quota_rospatent: int = 10
    quota_x: int = 10

    quota_brave_orchestrator: int = 10
    quota_epo_orchestrator: int = 10
    quota_rospatent_orchestrator: int = 10
    quota_x_orchestrator: int = 50


    def quota_for(self, adapter: str, role: str) -> int | None:
        if adapter not in METERED_ADAPTERS:
            return None
        if role == "orchestrator":
            return int(getattr(self, f"quota_{adapter}_orchestrator"))
        return int(getattr(self, f"quota_{adapter}"))

    agent_max_steps: int = 150
    researcher_max_steps: int = 40
    refuter_max_steps: int = 30
    orchestrator_max_steps: int = 300
    repeat_tool_call_limit: int = 3
    repeat_tool_call_stop_after: int = 3
    document_max_age_hours: int = 720

    db_pool_size: int = 20
    db_max_overflow: int = 40

    llm_connect_timeout_s: float = 10.0
    llm_read_timeout_s: float = 120.0
    llm_write_timeout_s: float = 30.0
    llm_call_deadline_s: float = 300.0
    llm_first_token_timeout_s: float = 30.0
    llm_first_token_failovers: int = 2
    retry_queue_timeout_s: float = 600.0
    llm_max_attempts: int = 4
    llm_retry_base_delay_s: float = 1.0
    llm_provider_lock_enabled: bool = True
    llm_provider_probe_timeout_s: float = 5.0

    fetch_connect_timeout_s: float = 10.0
    fetch_read_timeout_s: float = 30.0
    fetch_deadline_s: float = 120.0
    fetch_queue_timeout_s: float = 300.0
    fetch_max_attempts: int = 3
    fetch_part_chars: int = 20000
    fetch_retry_base_delay_s: float = 1.0
    page_parse_timeout_s: float = 20.0

    search_provider_timeout_s: float = 20.0
    search_default_limit: int = 8
    search_max_limit: int = 20
    search_snippet_chars: int = 200
    heartbeat_s: float = 15.0
    heartbeat_write_s: float = 3.0
    dead_run_wait_s: float = 10.0
    run_budget_minutes: int = 20
    time_budget_grace_s: float = Field(default=600, ge=0)
    time_budget_max_extensions: int = Field(default=2, ge=0)
    submission_text_max_chars: int = 2400
    submission_quote_max_chars: int = 2400
    submission_max_json_chars: int = 30000
    submission_max_patterns: int = 3
    submission_max_citations: int = 3
    submission_max_refutations: int = 4
    submission_max_rebuttal_responses: int = 4
    return_fallback_max_chars: int = 3000
    read_return_part_chars: int = 20000


    openalex_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("WSIGNAL_OPENALEX_API_KEY", "OPENALEX_API_KEY"),
    )
    github_api_token: str = Field(
        default="",
        validation_alias=AliasChoices("WSIGNAL_GITHUB_API_TOKEN", "GITHUB_API_TOKEN"),
    )
    firecrawl_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("WSIGNAL_FIRECRAWL_API_KEY", "FIRECRAWL_API_KEY"),
    )


    contact_email: str = Field(
        default="",
        validation_alias=AliasChoices("WSIGNAL_CONTACT_EMAIL", "CONTACT_EMAIL"),
    )

    @property
    def user_agent(self) -> str:
        contact = f"; contact: {self.contact_email}" if self.contact_email else ""
        return f"wsignal/0.1 (+https://github.com/whatanaiceguy/wsignal; automated research{contact})"


@lru_cache
def get_settings() -> Settings:
    return Settings()
