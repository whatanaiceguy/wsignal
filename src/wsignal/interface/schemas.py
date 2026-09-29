from datetime import date, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class SourceTier(StrEnum):

    UNKNOWN = "unknown"
    AUTHORITATIVE = "authoritative"
    TRADE = "trade"
    SOCIAL = "social"


class SourceType(StrEnum):


    PAPER = "paper"
    PATENT = "patent"
    STANDARD = "standard"
    NEWS = "news"
    REPORT = "report"
    REPO = "repo"
    SOCIAL = "social"
    PAGE = "page"
    FETCH_FAILURE = "fetch_failure"


class EntryState(StrEnum):
    BANKED = "banked"
    PARKED = "parked"
    NOISE = "noise"
    INSUFFICIENT = "insufficient"


class PatternKind(StrEnum):
    FAINTNESS = "faintness"
    SUBSTANCE = "substance"
    DELIVERY = "delivery"


class Citation(BaseModel):

    url: str = Field(description="Ссылка на источник")
    quote: str = Field(description="Дословная цитата")
    verified: bool = Field(
        description="Цитата найдена в сохранённом тексте страницы"
    )

    name: str = Field(description="Наименование источника")
    published_at: date | None = Field(description="Дата публикации")
    type: SourceType = Field(description="Тип источника")
    lang: str = Field(description="Язык оригинала, ISO 639-1")
    tier: SourceTier = Field(description="Уровень доверенности")

    title_original: str | None = Field(
        default=None, description="Оригинальное название, если источник не на русском"
    )
    summary_ru: str | None = Field(
        default=None, description="Краткое изложение на русском языке"
    )
    summary_is_generated: bool = Field(
        default=False,
        description="Изложение получено автоматическим переводом или генерацией",
    )


class Pattern(BaseModel):

    kind: PatternKind
    pattern: str = Field(description="Идентификатор признака из METHODOLOGY")
    citation_index: int = Field(description="Индекс цитаты в списке citations")
    strength: float | None = Field(default=None, ge=0.0, le=1.0)


class RefutationClaim(BaseModel):
    verdict: Literal["proven", "partly", "not_proven"]
    claim: str
    opposite: str
    effect: str
    url: str
    quote: str


class RebuttalResponse(BaseModel):
    attack: str
    response: str
    changes: str


class Refutation(BaseModel):
    state: str
    rebutted: bool
    claims: list[RefutationClaim] = Field(default_factory=list)
    responses: list[RebuttalResponse] = Field(default_factory=list)
    queries_and_venues: str | None = None


class EntryEdit(BaseModel):
    created_at: datetime
    reason: str
    changed: list[str]


class Entry(BaseModel):
    edits: list[EntryEdit] = Field(default_factory=list)
    id: int = Field(
        description="Идентификатор записи; в отличие от rank не меняется, пока исследование идёт"
    )
    rank: int
    refutation: Refutation | None = None
    name_ru: str = Field(description="Название технологии")
    topic_original_focus: str | None = None
    topic_rename_history: list[dict] = Field(default_factory=list)
    name_en: str | None = None
    corpus_query: str | None = None
    forced_reason: str | None = Field(
        default=None,
        description=(
            "Запись сделана без исследования (принудительно); здесь причина. "
            "Такие записи не проверены"
        ),
    )

    transition_ru: str = Field(
        description="Какой порог пересекается сейчас. Это и делает запись гипотезой, "
        "а не описанием"
    )

    score: float = Field(
        ge=0.0, le=1.0, description="Уверенность, что сигнал реален и развивается"
    )
    weak_score: float | None = Field(
        default=None, ge=0.0, le=1.0, description="Рейтинг слабости сигнала"
    )
    signal_class: Literal["weak", "strong", "noise"] | None = None
    substance: float | None = Field(default=None, ge=0.0, le=1.0)
    momentum: float | None = Field(default=None, ge=0.0, le=1.0)
    faintness: float | None = Field(default=None, ge=0.0, le=1.0)
    state: EntryState

    why_ru: str = Field(
        description="Рассуждение модели: почему это слабый сигнал, со ссылками"
    )
    current_state_ru: str | None = Field(default=None, description="Текущее состояние")
    dynamics_ru: str | None = Field(default=None, description="Динамика")

    what_would_refute_ru: str | None = Field(
        default=None,
        description="Что опровергло бы гипотезу: конкретная проверяемая находка",
    )

    searches_run: int = Field(default=0, description="Сколько запросов выполнено")
    sources_checked: int = Field(default=0, description="Сколько площадок проверено")

    problem_ru: str | None = Field(default=None, description="Какую проблему решает")
    advantage_ru: str | None = Field(
        default=None, description="Потенциальное преимущество"
    )
    case_example_ru: str | None = Field(default=None, description="Кейс-пример")

    patterns: list[Pattern] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)


class RunCounts(BaseModel):
    entries: int
    citations: int
    citations_verified: int
    documents: int
    agents: int
    cost_usd: float


class RunSummary(BaseModel):
    id: int
    query: str
    direction_ru: str | None
    state: str
    started_at: datetime
    finished_at: datetime | None
    elapsed_s: float
    is_live: bool
    counts: RunCounts


class SearchStats(BaseModel):
    candidates_found: int = Field(
        description="Количество найденных технологий-кандидатов"
    )
    sources_processed: int = Field(description="Количество обработанных источников")
    high_confidence_count: int = Field(
        description="Слабые сигналы, в которых модель уверена более чем на 75% (ТЗ)"
    )
    confidence_threshold: float = Field(default=0.75)
    signals_count: int = Field(
        default=0, description="Записи с уверенностью от 0.5: вероятно слабый сигнал"
    )
    probable_count: int = Field(
        default=0, description="Уверенность от 0.5 до 0.65: вероятно слабый сигнал"
    )
    likely_count: int = Field(
        default=0, description="Уверенность от 0.65 до 0.9: скорее всего слабый сигнал"
    )
    very_high_count: int = Field(
        default=0,
        description="Уверенность от 0.9: сигнал мог уже перестать быть слабым, "
        "но остаётся в верхней части выдачи",
    )

    agents_spawned: int = Field(description="Число агентов, созданных за исследование")
    citations_verified: int = Field(
        description="Цитат, найденных в сохранённом тексте источника"
    )
    citations_total: int = Field(description="Цитат всего")
    elapsed_seconds: float


class SearchResponse(BaseModel):
    run_id: int = Field(description="Идентификатор запуска")
    state: str = Field(
        description="Состояние запуска: running, finished, exhausted или failed"
    )
    query: str
    generated_at: datetime
    stats: SearchStats
    top_n: int | None = Field(
        default=None, description="Сколько записей запуск обещал показать сверху"
    )

    entries: list[Entry] = Field(
        description=(
            "Все проработанные кандидаты по weak_score по убыванию; старые записи без "
            "weak_score идут после новых, по score. Отсечка по ТОП-15 делается "
            "на стороне отображения"
        )
    )

    models_used: dict[str, str] = Field(
        description="Соответствие роли и модели для данного ответа"
    )
