from __future__ import annotations

import asyncio
import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from wsignal.config import get_settings
from wsignal.corpus import validate_corpus_query
from wsignal.inference.agent import AgentResult, AgentRuntime, RunCostBudget, model_calls_for
from wsignal.inference.events import Sink, emit
from wsignal.inference.llm import LlmClient
from wsignal.inference.prompts import load_assistant_lookup_prompt
from wsignal.inference.scoring import (
    classify_signal,
    compute_entry_score,
    compute_weak_score,
    entry_ordering,
)
from wsignal.inference.seeded import metadata_from_rationale, supplied_claim
from wsignal.inference.tools import (
    TOOL_SCHEMAS,
    RetrievalCache,
    Toolbox,
    content_sha,
    effort_for,
    newest_fetchable_document,
    record_source_event,
    validate_submission,
)
from wsignal.models import (
    Agent,
    Citation,
    Document,
    Entry,
    EntryEdit,
    EntryPattern,
    Field,
    FieldRename,
    Refutation,
    Run,
    Technology,
    Turn,
)
from wsignal.parsing.http import Fetcher


async def interrupt_pending_refutations(
    session: AsyncSession,
    reviews: list[Refutation],
    reason: str,
    completed_at: datetime | None = None,
) -> None:
    interrupted_at = completed_at or datetime.now(UTC)
    for review in reviews:
        refuter = await session.get(Agent, review.refuter_agent_id)
        if refuter is not None and refuter.state == "running":
            refuter.state = "failed"
        if review.rebuttal_required:
            last_user = await session.scalar(
                select(Turn.content)
                .where(
                    Turn.agent_id == review.researcher_agent_id,
                    Turn.kind == "user",
                )
                .order_by(Turn.seq.desc())
                .limit(1)
            )
            content = last_user if isinstance(last_user, dict) else {}
            researcher = await session.get(Agent, review.researcher_agent_id)
            if (
                researcher is not None
                and researcher.state == "running"
                and str(content.get("content") or "").startswith("Критик пытался доказать")
            ):
                researcher.state = "failed"
        review.state = "interrupted"
        review.delivery_version += 1
        review.collected = False
        review.outcome = {
            "kind": "refutation",
            "refutation_id": review.id,
            "refuter_agent_id": review.refuter_agent_id,
            "attacked_agent_id": review.researcher_agent_id,
            "delivery_version": review.delivery_version,
            "stopped": "interrupted",
            "incomplete": reason,
            "attack": None,
            "rebuttal": None,
        }
        review.completed_at = interrupted_at


def partition_fields(fields: list[Any], shard_size: int = 10) -> list[list[Any]]:
    count = math.ceil(len(fields) / shard_size)
    if count == 0:
        return []
    base, extra = divmod(len(fields), count)
    shards = []
    start = 0
    for number in range(count):
        size = base + (1 if number < extra else 0)
        shards.append(fields[start : start + size])
        start += size
    return shards


_QUOTE_NOISE = re.compile(r"[\s ​\"'«»“”„‘’`]+")


def _russian_name_error(name: Any) -> dict | None:
    if isinstance(name, str) and re.search("[А-Яа-яЁё]", name):
        return None
    return {
        "error": (
            "name_ru is required and must be the technology's name in Russian; "
            "put the English name in name_en and call again"
        )
    }


def _quote_key(text: str) -> str:
    return _QUOTE_NOISE.sub(" ", text or "").strip(" .,;:…").casefold()


def _match_quote(quoted: dict[str, int], quote: str) -> int | None:
    if not quote:
        return None
    if quote in quoted:
        return quoted[quote]
    key = _quote_key(quote)
    if not key:
        return None
    keyed = {_quote_key(text): citation_id for text, citation_id in quoted.items()}
    if key in keyed:
        return keyed[key]
    for text, citation_id in keyed.items():
        if min(len(text), len(key)) >= 20 and (key in text or text in key):
            return citation_id
    return None


ERROR_WARNING_AT = (30, 65, 100)
_LIMIT_ERROR = re.compile(r"budget|quota|ceiling|cost limit|calls_remaining", re.IGNORECASE)
_INPUT_ERROR = re.compile(
    r"must |required|is not (?:a|one|an)|unknown adapter|no such|not a field|"
    r"only the|only this|already has|conflicts|missing|invalid",
    re.IGNORECASE,
)
_ENVIRONMENT_TOOLS = {"search", "fetch", "corpus", "list_sources"}


def _error_group(tool: str, message: str) -> str:
    if _LIMIT_ERROR.search(message):
        return "limits"
    if tool in _ENVIRONMENT_TOOLS and not _INPUT_ERROR.search(message):
        return "environment"
    if _INPUT_ERROR.search(message):
        return "input"
    return "environment"


ORCHESTRATOR_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "ask_assistant",
            "description": (
                "Ask a short-lived assistant agent to look up one question using "
                "its own search and fetch tools. Returns a short answer and relied-on "
                "URLs; its pages and transcript are not added to your context."
            ),
            "parameters": {
                "type": "object",
                "properties": {"question": {"type": "string"}},
                "required": ["question"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "split_direction",
            "description": (
                "Hand the direction to the assistant and get back the fields it "
                "can source. Every field is written down whether or not it is "
                "ever worked. Ask for as many as it can find - fifteen is the "
                "floor, a hundred is fine - because the ones you do not reach "
                "are standing work rather than waste."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "instruction": {
                        "type": "string",
                        "description": "What to tell the assistant beyond the direction itself.",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_fields",
            "description": "The fields of this run and what state each is in.",
            "parameters": {
                "type": "object",
                "properties": {
                    "state": {
                        "type": "string",
                        "enum": ["pending", "dispatched", "done", "dropped"],
                    },
                    "limit": {"type": "integer"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "dispatch_researchers",
            "description": (
                "Put a researcher on each of these fields. Returns their agent "
                "ids AT ONCE, while they work: nothing here waits. Call collect "
                "to take returns as they land. Dispatch is cheap, so dispatch "
                "what the budget can pay for and collect while they run."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "field_ids": {"type": "array", "items": {"type": "integer"}},
                    "brief": {
                        "type": "string",
                        "description": "What every researcher in this batch is being asked for.",
                    },
                },
                "required": ["field_ids"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "collect",
            "description": (
                "Take whatever has finished - researchers AND refuters, they "
                "share one queue and each return says which it is in `kind`. "
                "Returns immediately with everything already done; if nothing "
                "is done yet but work is running, it waits for the first return. "
                "If nothing is running or waiting, it reports the pending fields "
                "you must dispatch, or says to write remaining entries or finish. "
                "Anything that failed comes back here too, with what it had and "
                "how it stopped, so nothing disappears by failing."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "wait_s": {
                        "type": "integer",
                        "description": "Seconds to wait if none are done yet. 120 by default.",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_return",
            "description": (
                "Read one page of an agent's full stored final text; "
                "collect returns are summaries."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_id": {"type": "integer"},
                    "part": {"type": "integer", "minimum": 1},
                },
                "required": ["agent_id", "part"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "refute",
            "description": (
                "Hand this researcher's work to a refuter who tries to prove the "
                "opposite of each of its claims, whichever direction they point, "
                "and get the refuter's id AT ONCE: like dispatch, nothing here "
                "waits. The refuter receives the researcher's verbatim answer, "
                "the search log and cited URLs with quoted text, not page contents. "
                "Fetch cited pages in parts if needed. By default "
                "the originating researcher is then resumed with the refuter's "
                "own evidence and answers it, and both sides come back through "
                "collect as one return. Refute every return, a noise verdict "
                "included; they run in parallel and cost you no waiting. "
                "A collected failed exchange resumes under its existing review."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_id": {"type": "integer"},
                    "instruction": {"type": "string"},
                    "rebut": {
                        "type": "boolean",
                        "description": (
                            "Resume the researcher to answer the attack. True by "
                            "default; false gives you the attack alone."
                        ),
                    },
                },
                "required": ["agent_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "resume_agent",
            "description": (
                "Continue an existing agent with its full history. For a failed or "
                "cancelled or interrupted refutation, resume its refuter or researcher: "
                "same tracked exchange continues and returns through collect. "
                "Collect the incomplete exchange before resuming it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_id": {"type": "integer"},
                    "message": {"type": "string"},
                },
                "required": ["agent_id", "message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_entry",
            "description": (
                "Write one claim down. Write every claim researched, including "
                "the ones that turned out to be noise or too thin to judge: "
                "rank decides what is shown, not this. A low score with a "
                "paragraph saying what failed is the exclusion demonstration."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "field_id": {
                        "type": ["integer", "null"],
                        "description": (
                            "The field this claim came out of, from the return "
                            "you are writing up. Without it the field is never "
                            "marked done and a later run researches it again. "
                            "May be null only with force=true."
                        ),
                    },
                    "researched_by_agent_id": {
                        "type": ["integer", "null"],
                        "description": (
                            "The researcher whose work this is, from the return "
                            "you are writing up. It is where the search and "
                            "source counts on the published entry come from: "
                            "omit it and the entry states that nobody searched. "
                            "null with force=true."
                        ),
                    },
                    "force": {
                        "type": "boolean",
                        "description": (
                            "Write without a researched field: only when the user asked "
                            "for it, or research cannot run at all. The entry is ranked "
                            "like any other but marked unverified in the report, and it "
                            "spends none of the research ceiling. Never a way around it."
                        ),
                    },
                    "force_reason": {
                        "type": "string",
                        "description": (
                            "Required with force=true: in Russian, why this entry was "
                            "written without research. Shown on the entry."
                        ),
                    },
                    "name_ru": {
                        "type": "string",
                        "description": (
                            "Required: the technology's name in Russian. Keep product and "
                            "standard names as written."
                        ),
                    },
                    "name_en": {
                        "type": "string",
                        "description": "English name; defaults to the field's current focus.",
                    },
                    "corpus_query": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 200,
                        "description": (
                            "Short English websearch-syntax query with 1-4 OR alternatives "
                            "naming the technology itself. Use concise names and specific "
                            "abbreviations; never standalone AI, ML, IT, API or KV. "
                            "Do not copy the full transition or its application qualifiers. "
                            "Examples: photonic processors OR optical processors; "
                            "MCP security OR tool poisoning; co-packaged optics OR CPO."
                        ),
                    },
                    "transition_ru": {
                        "type": "string",
                        "description": (
                            "Which threshold is being crossed, stated so it can be wrong."
                        ),
                    },
                    "state": {
                        "type": "string",
                        "enum": ["banked", "parked", "noise", "insufficient"],
                    },
                    "substance": {"type": "number", "description": "0 to 1."},
                    "momentum": {"type": "number", "description": "0 to 1."},
                    "faintness": {"type": "number", "description": "0 to 1."},
                    "why_ru": {
                        "type": "string",
                        "description": (
                            "Why this is or is not a weak signal, argued from the "
                            "evidence as facts about the technology now. The "
                            "deliverable, not a list of quotes, and never an account "
                            "of the researcher/refuter exchange."
                        ),
                    },
                    "review_incomplete_reason": {
                        "type": "string",
                        "description": (
                            "Required for an unreviewed or failed-review "
                            "noise/insufficient result. "
                            "Which claims were not independently tested and why, "
                            "stated as a fact about the evidence without naming "
                            "agents or failures; this is shown in the report."
                        ),
                    },
                    "current_state_ru": {"type": "string"},
                    "dynamics_ru": {"type": "string"},
                    "what_would_refute_ru": {
                        "type": "string",
                        "description": (
                            "The findings that would overturn the claim, looked for "
                            "and not found, and where. Facts about the evidence, "
                            "without naming who searched."
                        ),
                    },
                    "problem_ru": {"type": "string"},
                    "advantage_ru": {"type": "string"},
                    "case_example_ru": {"type": "string"},
                    "technology_id": {
                        "type": ["integer", "null"],
                        "description": (
                            "The technology this claim is about, from "
                            "`known_technologies` or `open_technology`. A FIELD "
                            "id is not a technology id. Omit it, use null, or use "
                            "0 to inherit the technology carried by the field. "
                            "An entry whose field has no technology is refused."
                        ),
                    },
                    "citations": {
                        "type": "array",
                        "description": (
                            "Required on every write. Copy the full URLs and verbatim quotes "
                            "from collected evidence, with summary_ru for non-Russian sources. "
                            "Use [] only when there genuinely are no citations."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "url": {"type": "string"},
                                "quote": {
                                    "type": "string",
                                    "description": "Verbatim from the stored page. It is checked.",
                                },
                                "summary_ru": {
                                    "type": "string",
                                    "description": (
                                        "Optional for Russian sources; required otherwise. "
                                        "Write 2-3 sentences in Russian explaining what the "
                                        "source is and what it says that bears on the claim."
                                    ),
                                },
                            },
                            "required": ["url", "quote"],
                        },
                    },
                    "patterns": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "kind": {
                                    "type": "string",
                                    "enum": ["faintness", "substance", "delivery"],
                                },
                                "pattern": {
                                    "type": "string",
                                    "description": "The name from METHODOLOGY §3 or §4.",
                                },
                                "quote": {
                                    "type": "string",
                                    "description": (
                                        "Optional. A faintness pattern usually has none, "
                                        "because no page asserts an absence."
                                    ),
                                },
                                "strength": {"type": "number", "description": "0 to 1."},
                            },
                            "required": ["kind", "pattern", "strength"],
                        },
                    },
                },
                "required": [
                    "transition_ru",
                    "state",
                    "substance",
                    "momentum",
                    "faintness",
                    "why_ru",
                    "current_state_ru",
                    "dynamics_ru",
                    "what_would_refute_ru",
                    "problem_ru",
                    "advantage_ru",
                    "case_example_ru",
                    "corpus_query",
                    "field_id",
                    "researched_by_agent_id",
                    "citations",
                ],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "summon_orchestrators",
            "description": (
                "Partition selected fields that exceed the configured shard size across "
                "independent shard orchestrators. Call once after selecting fields. "
                "The summoner owns shard 1; the result names its field ids, which it "
                "must dispatch itself."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "field_ids": {"type": "array", "items": {"type": "integer"}},
                    "brief": {"type": "string"},
                },
                "required": ["field_ids", "brief"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "end_run",
            "description": (
                "End this run early; entries written so far are kept. state=finished "
                "when nothing more is worth doing: what was asked is done, or cannot be "
                "done here. state=failed when the harness itself keeps failing in a way "
                "you cannot route around: you cannot dispatch, collect or write, or "
                "every source the work needs fails. Scattered failures (a source down, "
                "a page that will not render, a timeout) are normal: route around them "
                "and carry on instead."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string"},
                    "state": {"type": "string", "enum": ["finished", "failed"]},
                },
                "required": ["reason", "state"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_entries",
            "description": (
                "What has been written in this run so far, highest weak_score first; "
                "legacy rows without it follow by score."
            ),
            "parameters": {"type": "object", "properties": {"limit": {"type": "integer"}}},
        },
    },
]


_TRAILING_COMMA = re.compile(r",(\s*[}\]])")
_FENCE = re.compile(r"^\s*```[a-zA-Z]*\n|\n```\s*$")


EDIT_ENTRY_FIELDS = (
    "name_ru", "name_en", "transition_ru", "why_ru", "current_state_ru",
    "dynamics_ru", "what_would_refute_ru", "problem_ru", "advantage_ru",
    "case_example_ru", "corpus_query", "state", "substance", "momentum",
    "faintness", "citations", "patterns",
)

ORCHESTRATOR_TOOLS.append({
    "type": "function",
    "function": {
        "name": "edit_entry",
        "description": (
            "Correct an existing entry with a reason; never write a second entry "
            "for the same field. Citations and patterns are full replacement lists."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "entry_id": {"type": "integer"},
                "reason": {"type": "string", "minLength": 1},
                "changes": {
                    "type": "object",
                    "minProperties": 1,
                    "additionalProperties": False,
                    "properties": {
                        key: schema
                        for tool in ORCHESTRATOR_TOOLS
                        if tool["function"]["name"] == "write_entry"
                        for key, schema in tool["function"]["parameters"]["properties"].items()
                        if key in EDIT_ENTRY_FIELDS
                    },
                },
            },
            "required": ["entry_id", "reason", "changes"],
        },
    },
})


def _clamped_limit(arguments: dict, default: int) -> int:
    raw = arguments.get("limit")
    return max(1, min(200, int(default if raw is None else raw)))


def _agent_result_text(result: AgentResult) -> str:
    if result.submission is not None:
        return json.dumps(result.submission, ensure_ascii=False, indent=2)
    return result.content or ""


def _parse_json_candidate(content: str, prefer_array: bool) -> tuple[Any, str | None]:
    text = _FENCE.sub("", (content or "").strip())
    first_object, first_array = text.find("{"), text.find("[")
    use_array = (
        first_array != -1 and (first_object == -1 or first_array < first_object)
        if prefer_array
        else first_object == -1 or (first_array != -1 and first_array < first_object)
    )
    start, end = (first_array, text.rfind("]")) if use_array else (
        first_object,
        text.rfind("}"),
    )
    candidate = text[start : end + 1] if start != -1 and end > start else text
    payload: Any = None
    error: str | None = None
    for attempt in (candidate, _TRAILING_COMMA.sub(r"\1", candidate)):
        try:
            payload = json.loads(attempt)
            error = None
            break
        except (ValueError, IndexError) as exc:
            error = f"{type(exc).__name__}: {exc}"
    return payload, error


def parse_fields(content: str) -> tuple[list, str | None]:
    payload, error = _parse_json_candidate(content, prefer_array=True)
    if payload is None:
        return [], error or "no JSON found in the answer"

    items = payload.get("fields") if isinstance(payload, dict) else payload
    if not isinstance(items, list) or not items:
        return [], "parsed, but there is no non-empty `fields` list in it"
    return items, None


def parse_corrections(content: str) -> tuple[dict | list, str | None]:
    payload, error = _parse_json_candidate(content, prefer_array=False)
    if payload is None:
        return {}, error or "no JSON found in the answer"
    if isinstance(payload, list):
        if payload and all(isinstance(item, dict) for item in payload):
            return payload, None
        return {}, "parsed, but the full field list is not a list of objects"
    if isinstance(payload, dict) and isinstance(payload.get("fields"), list):
        fields = payload["fields"]
        if fields and all(isinstance(item, dict) for item in fields):
            return fields, None
        return {}, "parsed, but the full field list is not a non-empty list of objects"
    if not isinstance(payload, dict):
        return {}, "parsed, but the correction is not an object"
    expected = ("fixes", "merges", "keep")
    if not any(key in payload for key in expected):
        return {}, "parsed, but there is no correction list in it"
    return payload, None


def apply_corrections(items: list, correction: dict) -> tuple[list, dict]:
    result = [dict(item) for item in items if isinstance(item, dict)]
    report: dict[str, list] = {"applied": [], "unmatched": [], "skipped": []}
    if not isinstance(correction, dict):
        report["skipped"].append({"kind": "correction", "reason": "expected an object"})
        return result, report

    def match(focus: Any) -> int | None:
        if not isinstance(focus, str):
            return None
        for index, item in enumerate(result):
            if item.get("focus", item.get("name")) == focus:
                return index
        wanted = " ".join(focus.split()).casefold()
        for index, item in enumerate(result):
            current = item.get("focus", item.get("name"))
            if isinstance(current, str) and " ".join(current.split()).casefold() == wanted:
                return index
        return None

    fixes = correction.get("fixes", [])
    merges = correction.get("merges", [])
    keeps = correction.get("keep", [])
    for name, entries in (("fixes", fixes), ("merges", merges), ("keep", keeps)):
        if not isinstance(entries, list):
            report["skipped"].append({"kind": name, "reason": "expected a list"})
    if not isinstance(fixes, list):
        fixes = []
    for entry in fixes:
        if not isinstance(entry, dict) or not isinstance(entry.get("focus"), str):
            report["skipped"].append({"kind": "fix", "reason": "malformed"})
            continue
        index = match(entry["focus"])
        if index is None:
            report["unmatched"].append({"kind": "fix", "focus": entry["focus"]})
            continue
        technology_id = entry.get("technology_id")
        if (
            isinstance(technology_id, bool)
            or not isinstance(technology_id, int)
            or technology_id <= 0
        ):
            report["skipped"].append(
                {
                    "kind": "fix",
                    "focus": entry["focus"],
                    "reason": "technology_id must be a positive integer",
                }
            )
            continue
        result[index]["technology_id"] = technology_id
        report["applied"].append(
            {
                "kind": "fix",
                "focus": result[index].get("focus"),
                "technology_id": technology_id,
            }
        )
    if not isinstance(merges, list):
        merges = []
    for entry in merges:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("focus"), str)
            or not isinstance(entry.get("into_focus"), str)
        ):
            report["skipped"].append({"kind": "merge", "reason": "malformed"})
            continue
        loser = match(entry["focus"])
        target = match(entry["into_focus"])
        if loser is None:
            report["unmatched"].append({"kind": "merge", "focus": entry["focus"]})
        elif target is None:
            report["unmatched"].append(
                {
                    "kind": "merge",
                    "focus": entry["focus"],
                    "into_focus": entry["into_focus"],
                    "reason": "target not found",
                }
            )
        elif loser == target:
            report["skipped"].append(
                {
                    "kind": "merge",
                    "focus": entry["focus"],
                    "reason": "source and target are the same",
                }
            )
        else:
            removed = result.pop(loser)
            target_index = target if target < loser else target - 1
            report["applied"].append(
                {
                    "kind": "merge",
                    "focus": removed.get("focus"),
                    "into_focus": result[target_index].get("focus"),
                }
            )
    if not isinstance(keeps, list):
        keeps = []
    for entry in keeps:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("focus"), str)
            or not isinstance(entry.get("why"), str)
            or not entry["why"].strip()
        ):
            report["skipped"].append({"kind": "keep", "reason": "malformed"})
            continue
        index = match(entry["focus"])
        if index is None:
            report["unmatched"].append({"kind": "keep", "focus": entry["focus"]})
            continue
        rationale = str(result[index].get("rationale") or "").strip()
        why = entry["why"].strip()
        result[index]["rationale"] = f"{rationale}\n\n{why}" if rationale else why
        report["applied"].append({"kind": "keep", "focus": result[index].get("focus")})
    return result, report


def _resolution_demand(problems: dict) -> str:
    parts: list[str] = [
        "Твой список не принят как есть. Ниже только поля, требующие решения. "
        "Ответь небольшим JSON-объектом только по ним и не присылай заново поля, "
        "которые были в порядке. Используй ровно такую форму: "
        '{"fixes": [{"focus": "<фокус поля, скопируй точно>", "technology_id": 12}], '
        '"merges": [{"focus": "<поле, которое убрать>", "into_focus": "<поле-дубликат>"}], '
        '"keep": [{"focus": "<поле>", "why": "<одно предложение>"}]}. '
        "В `fixes` укажи id технологии для поля без технологии или для пары, "
        "которую ты считаешь одной технологией: сначала вызови `open_technology` "
        "с этим id и своей формулировкой. В `merges` поле исчезает как дубликат. "
        "В `keep` одно предложение будет добавлено в `rationale` этого поля. "
        "Не присылай поля, которые были в порядке."
    ]

    unresolved = problems["unresolved"]
    if unresolved:
        listed = "\n".join(f"  - {focus}" for focus in unresolved[:40])
        parts.append(
            f"НЕТ ТЕХНОЛОГИИ В БАЗЕ ({len(unresolved)}) — отвечай в `fixes`. "
            "У этих полей `technology_id` отсутствует или указывает на строку, "
            "которой нет. Номер поля, порядковый номер твоего списка и догадка — "
            "это не id. Найди каждую через `known_technologies`; если её там нет, "
            "заведи через `open_technology` и подставь полученный id.\n" + listed
        )

    shared = problems["shared"]
    if shared:
        blocks = []
        for item in shared:
            focuses = "\n".join(f"    - {focus}" for focus in item["fields"])
            blocks.append(f"  technology_id={item['technology_id']}:\n{focuses}")
        parts.append(
            "ОДНА ТЕХНОЛОГИЯ, НЕСКОЛЬКО ПОЛЕЙ. Это допустимо только если переход "
            "разный. Если это одно и то же — `merges`, лишнее поле исчезает. "
            "Если переходы разные — `keep`, и в `why` назови переход второго поля "
            "и скажи, почему это не одно утверждение.\n"
            + "\n".join(blocks)
        )

    near = problems["near"]
    if near:
        blocks = [
            f"  id {pair['left_id']}: {pair['left']}\n"
            f"  id {pair['right_id']}: {pair['right']}"
            for pair in near
        ]
        parts.append(
            "ДВЕ СТРОКИ ПОХОЖИ НА ОДНУ ТЕХНОЛОГИЮ. Проверь каждую пару. Если это "
            "одно и то же — привяжи второе написание к выжившему id через "
            "`open_technology` и пришли это поле в `fixes` с тем же id. Если это "
            "разные вещи — `keep` с одним предложением на поле. Ложная пара "
            "здесь стоит одного предложения: порог намеренно широкий.\n"
            + "\n\n".join(blocks)
        )

    return "\n\n".join(parts)


@dataclass(slots=True)
class Budget:
    started_at: datetime
    minutes: int
    grace_s: float = 600
    max_extensions: int = 2
    extensions: int = 0
    deadline_s: float | None = None

    @property
    def research_closed(self) -> bool:
        return self.extensions > 0 or self.elapsed_s >= self.minutes * 60

    def extend(self, counts: dict[str, int]) -> bool:
        if (
            not self.spent or not any(counts.values())
            or self.extensions >= self.max_extensions or self.grace_s <= 0
        ):
            return False
        self.extensions += 1
        self.deadline_s = self.elapsed_s + self.grace_s
        return True

    @property
    def elapsed_s(self) -> float:
        return (datetime.now(UTC) - self.started_at).total_seconds()

    @property
    def remaining_s(self) -> float:
        deadline = self.deadline_s if self.deadline_s is not None else self.minutes * 60
        return deadline - self.elapsed_s

    @property
    def spent(self) -> bool:
        return self.remaining_s <= 0


class Orchestration:
    def __init__(
        self,
        session: AsyncSession,
        sessionmaker: async_sessionmaker[AsyncSession],
        runtime: AgentRuntime,
        llm: LlmClient,
        fetcher: Fetcher,
        run: Run,
        prompts: dict[str, str],
        budget_minutes: int = 20,
        max_research: int = 15,
        top_n: int = 15,
        agent_max_steps: int = 25,
        cache: RetrievalCache | None = None,
        sink: Sink | None = None,
        owner_agent_id: int | None = None,
        initial_agent_id: int | None = None,
        shard_number: int = 1,
        child_shard: bool = False,
        partition_worker: bool = False,
        summon_callback: Any = None,
        budget_started_at: datetime | None = None,
        shard_size: int = 10,
        cost_budget: RunCostBudget | None = None,
    ) -> None:
        self._sink = sink
        self._owner_agent_id = owner_agent_id
        self._initial_agent_id = initial_agent_id
        self._shard_number = shard_number
        self._child_shard = child_shard
        self._partition_worker = partition_worker
        self._summon_callback = summon_callback
        self._session = session
        self._sessionmaker = sessionmaker
        self._runtime = runtime
        self._llm = llm
        self._fetcher = fetcher
        self._run = run
        self._cost_budget = cost_budget
        self._prompts = prompts
        self._budget = Budget(
            budget_started_at or datetime.now(UTC), budget_minutes,
            get_settings().time_budget_grace_s, get_settings().time_budget_max_extensions,
        )
        self._max_research = max(1, max_research)
        self._shard_size = max(1, shard_size)
        self._run_ceiling = self._max_research
        self._research_budget = self._max_research
        self._top_n = max(1, top_n)
        self._researched: set[int] = set()
        self._cache = cache if cache is not None else RetrievalCache()
        self._agent_max_steps = agent_max_steps
        self._selected_fields: set[int] = set()
        self._inflight: dict[int, asyncio.Task] = {}
        self._returns: asyncio.Queue[dict] = asyncio.Queue()
        self._max_model_calls = (
            run.orchestrator_max_steps
            if getattr(run, "orchestrator_max_steps", None) is not None
            else get_settings().orchestrator_max_steps
        )
        self._end_run_callback = None
        self._end_run_reason: str | None = None

    def _time_dispatch_error(self) -> str | None:
        budget = getattr(self, "_budget", None)
        if budget is not None and budget.research_closed:
            return (
                "Active-time budget is up: no new research or fields. Finish, collect, "
                "refute and write work already in flight; resume agents for rebuttals."
            )
        return None

    def _max_steps_for(self, role: str) -> int:
        settings = get_settings()
        return {
            "orchestrator": self._max_model_calls,
            "researcher": settings.researcher_max_steps,
            "refuter": settings.refuter_max_steps,
        }.get(role, self._agent_max_steps)

    def install(self, toolbox: Toolbox) -> None:
        toolbox._orchestrator_status = self.status
        if getattr(self._run, "seeded", False):
            toolbox._technology_opener = self._open_seeded_technology
        handlers = {
            "split_direction": self._split_direction,
            "summon_orchestrators": self._summon_orchestrators,
            "read_fields": self._read_fields,
            "dispatch_researchers": self._dispatch_researchers,
            "collect": self._collect,
            "read_return": self._read_return,
            "refute": self._refute,
            "resume_agent": self._resume_agent,
            "write_entry": self._write_entry,
            "edit_entry": self._edit_entry,
            "rename_topic": self._rename_topic,
            "ask_assistant": self._ask_assistant,
            "end_run": self._end_run,
            "read_entries": self._read_entries,
        }
        for schema in ORCHESTRATOR_TOOLS + [
            item for item in TOOL_SCHEMAS
            if item["function"]["name"] == "rename_topic"
        ]:
            name = schema["function"]["name"]
            if (
                self._child_shard
                and name in ("split_direction", "summon_orchestrators", "end_run")
            ) or (getattr(self._run, "seeded", False) and name == "split_direction"):
                continue
            toolbox.register(schema, self._with_status(handlers[name]), ("orchestrator",))

    async def _summon_orchestrators(self, arguments: dict, agent_id: int | None) -> Any:
        if error := self._time_dispatch_error():
            return {"error": error}
        cost_budget = getattr(self, "_cost_budget", None)
        if cost_budget is not None and cost_budget.reason is not None:
            return {"error": cost_budget.reason}
        if agent_id is None or self._child_shard:
            return {"error": "only the initial orchestrator can summon shards"}
        if agent_id != self._initial_agent_id:
            return {"error": "only the initial orchestrator can summon shards"}
        partitioned = await self._partitioned()
        if partitioned:
            existing = (
                await self._session.scalars(
                    select(Field).where(
                        Field.run_id == self._run.id,
                        Field.orchestrator_agent_id.is_not(None),
                    ).order_by(Field.ordinal.nulls_last(), Field.id)
                )
            ).all()
            return self._manifest(existing)
        raw_ids = arguments.get("field_ids")
        if not isinstance(raw_ids, list) or any(type(value) is not int for value in raw_ids):
            return {"error": "field_ids must be an array of integers"}
        requested = list(dict.fromkeys(raw_ids))
        if getattr(self._run, "seeded", False):
            seeded_fields = (
                await self._session.scalars(
                    select(Field.id).where(
                        Field.run_id == self._run.id,
                        Field.state.in_(("pending", "dispatched")),
                    )
                )
            ).all()
            if set(requested) != set(seeded_fields):
                return {
                    "error": "a seeded run must summon every field together",
                    "field_ids": seeded_fields,
                }
        found = (
            await self._session.scalars(
                select(Field).where(
                    Field.run_id == self._run.id,
                    Field.id.in_(set(requested) | self._researched),
                )
            )
        ).all()
        by_id = {field.id: field for field in found}
        unusable = [
            field_id
            for field_id in requested
            if field_id not in by_id
            or (by_id[field_id].state != "pending" and field_id not in self._researched)
        ]
        if unusable:
            return {
                "error": "field_ids must be pending fields or fields you already dispatched",
                "field_ids": unusable,
            }
        already = sorted(
            (by_id[field_id] for field_id in self._researched if field_id in by_id),
            key=lambda field: (field.ordinal is None, field.ordinal or 0, field.id),
        )
        pending = [by_id[field_id] for field_id in requested if field_id not in self._researched]
        total = len(already) + len(pending)
        shard_size = getattr(self, "_shard_size", 10)
        if total <= shard_size:
            return {
                "error": (
                    f"{shard_size} or fewer fields in total, counting "
                    "the ones already dispatched: "
                    "dispatch them with dispatch_researchers"
                )
            }
        if total > self._run_ceiling:
            return {
                "error": "the selection exceeds this run's research ceiling",
                "research_ceiling": self._run_ceiling,
                "already_researched": len(already),
                "may_still_add": max(0, self._run_ceiling - len(already)),
                "asked_to_add": len(pending),
            }
        partition = partition_fields(already + pending, shard_size)
        if len(already) > len(partition[0]):
            partition = [already, *partition_fields(pending, shard_size)]
        shards = []
        for number, shard in enumerate(partition):
            size = len(shard)
            owner = agent_id
            if number:
                owner_agent = await self._runtime.create(
                    run_id=self._run.id,
                    role="orchestrator",
                    system=self._child_system(number + 1, size),
                    brief=str(arguments.get("brief") or ""),
                    parent_agent_id=agent_id,
                    commit_system=False,
                )
                owner = owner_agent.id
            for field in shard:
                field.orchestrator_agent_id = owner
            shards.append({
                "shard": number + 1,
                "orchestrator_agent_id": owner,
                "field_ids": [field.id for field in shard],
                "fields": [
                    {
                        "id": field.id,
                        "focus": field.focus,
                        "rationale": field.rationale,
                        "state": field.state,
                    }
                    for field in shard
                ],
                "ceiling": size,
                "brief": str(arguments.get("brief") or ""),
            })
        self._owner_agent_id = agent_id
        self._initial_agent_id = agent_id
        self._selected_fields = set(shards[0]["field_ids"])
        self._partition_worker = True
        self._research_budget = len(shards[0]["field_ids"])
        self._max_research = self._research_budget
        await self._session.commit()
        manifest = {
            "orchestrators": shards,
            "count": len(shards),
            "summoner_owned_field_ids": list(shards[0]["field_ids"]),
            "instruction": (
                "The summoning orchestrator owns the first shard. Dispatch every "
                "pending field in summoner_owned_field_ids with dispatch_researchers."
            ),
        }
        if self._summon_callback is not None:
            await self._summon_callback(manifest)
        return manifest

    def _child_system(self, shard_number: int, ceiling: int) -> str:
        return (
            self._prompts["orchestrator"]
            + f"\n\nYou are shard worker {shard_number}. Work only on your assigned fields. "
            "Do not split the direction or summon orchestrators. "
            f"Your local research ceiling is {ceiling} fields."
        )

    async def _partitioned(self) -> bool:
        return await self._session.scalar(
            select(Field.id).where(
                Field.run_id == self._run.id, Field.orchestrator_agent_id.is_not(None)
            ).limit(1)
        ) is not None

    def _manifest(self, fields: list[Field]) -> dict:
        by_owner: dict[int, list[Field]] = {}
        for field in fields:
            by_owner.setdefault(field.orchestrator_agent_id, []).append(field)
        manifest = {
            "orchestrators": [
                {
                    "orchestrator_agent_id": owner,
                    "field_ids": [field.id for field in owned],
                    "fields": [
                        {"id": field.id, "focus": field.focus, "rationale": field.rationale}
                        for field in owned
                    ],
                    "ceiling": len(owned),
                }
                for owner, owned in by_owner.items()
            ],
            "count": len(by_owner),
        }
        summoner = next(
            (
                shard for shard in manifest["orchestrators"]
                if shard["orchestrator_agent_id"] == self._initial_agent_id
            ),
            None,
        )
        if summoner is not None:
            manifest["summoner_owned_field_ids"] = list(summoner["field_ids"])
            manifest["instruction"] = (
                "The summoning orchestrator owns the first shard. Dispatch every "
                "pending field in summoner_owned_field_ids with dispatch_researchers."
            )
        return manifest

    def _with_status(self, handler: Any) -> Any:
        async def wrapped(arguments: dict, agent_id: int | None) -> Any:
            if self._partition_worker and agent_id != self._owner_agent_id:
                payload = {"error": "only this shard orchestrator may use orchestration tools"}
            else:
                payload = await handler(arguments, agent_id)
            if isinstance(payload, dict):
                payload["_status"] = await self.status()
                budget = getattr(self, "_budget", None)
                if budget is not None and budget.extensions > getattr(self, "_extension_told", 0):
                    self._extension_told = budget.extensions
                    payload["_time_warning"] = self._time_dispatch_error()
                warning = await self._error_warning()
                if warning is not None:
                    payload["_warning"] = warning
            return payload

        return wrapped

    async def _error_warning(self) -> str | None:
        if getattr(self, "_child_shard", False):
            return None
        warned = getattr(self, "_errors_warned", None)
        if warned is None:
            warned = self._errors_warned = set()
        pending = [n for n in ERROR_WARNING_AT if n not in warned]
        if not pending:
            return None
        try:
            rows = (
                await self._session.execute(
                    select(
                        Turn.content["name"].astext,
                        Turn.content["payload"]["error"].astext,
                    )
                    .join(Agent, Agent.id == Turn.agent_id)
                    .where(
                        Agent.run_id == self._run.id,
                        Turn.kind == "tool_result",
                        Turn.content["payload"].has_key("error"),
                    )
                )
            ).all()
        except Exception:
            return None
        crossed = [n for n in pending if len(rows) >= n]
        if not crossed:
            return None
        warned.update(crossed)
        groups = {"environment": 0, "input": 0, "limits": 0}
        tools: dict[str, int] = {}
        for name, message in rows:
            groups[_error_group(str(name or ""), str(message or ""))] += 1
            tools[str(name or "?")] = tools.get(str(name or "?"), 0) + 1
        top = ", ".join(
            f"{name} {count}"
            for name, count in sorted(tools.items(), key=lambda item: -item[1])[:5]
        )
        return (
            f"{len(rows)} tool errors so far in this run, all agents together. "
            f"Environment {groups['environment']}: sources, pages or the corpus did not "
            "answer; normal in small numbers, agents route around them. "
            f"Inputs {groups['input']}: arguments a tool refused; each error says what "
            f"to change. Limits {groups['limits']}: budgets or quotas spent. By tool: "
            f"{top}. If environment errors now mean the work cannot be done, consider "
            "end_run; if they are inputs, read the errors and fix the calls; otherwise "
            "carry on."
        )

    async def status(self) -> dict:
        rows = (
            await self._session.execute(
                select(Agent.role, Agent.state, func.count())
                .where(Agent.run_id == self._run.id)
                .group_by(Agent.role, Agent.state)
            )
        ).all()
        fields = (
            await self._session.execute(
                select(Field.state, func.count())
                .where(Field.run_id == self._run.id)
                .group_by(Field.state)
            )
        ).all()
        written = await self._session.scalar(
            select(func.count()).select_from(Entry).where(Entry.run_id == self._run.id)
        )
        model_calls = (
            await model_calls_for(self._session, self._owner_agent_id)
            if self._owner_agent_id is not None
            else 0
        )
        return {
            "elapsed_s": round(self._budget.elapsed_s),
            "remaining_s": round(self._budget.remaining_s),
            "budget_spent": self._budget.spent,
            "time": (
                f"extended {self._budget.extensions}/{self._budget.max_extensions}, "
                f"{max(0, round(self._budget.remaining_s))} s left, no new research"
                if self._budget.extensions else
                f"{max(0, round(self._budget.remaining_s))} s left"
            ),
            "agents": {f"{role}:{state}": n for role, state, n in rows},
            "fields": {state: n for state, n in fields},
            "entries_written": written,
            "research_budget": self._research_budget,
            "technologies_researched": len(self._researched),
            "research_remaining": max(0, self._research_budget - len(self._researched)),
            "shown_at_the_end": self._top_n,
            "model_calls": model_calls,
            "calls_remaining": max(0, self._max_model_calls - model_calls),
            "http_requests": self._fetcher.stats.requests,
        }

    async def _end_run(self, arguments: dict, agent_id: int | None) -> Any:
        reason = arguments.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            return {"error": "reason is required"}
        if agent_id is None or agent_id != self._initial_agent_id or self._child_shard:
            return {"error": "only the top-level orchestrator may end the run"}
        if self._end_run_reason is not None:
            return {"error": "the run has already been ended", "reason": self._end_run_reason}
        state = "finished" if arguments.get("state") == "finished" else "failed"
        self._end_run_reason = reason.strip()
        self._run.state = state
        self._run.finished_at = datetime.now(UTC)
        await self._session.commit()
        emit(
            self._sink,
            "run_ended",
            run_id=self._run.id,
            reason=self._end_run_reason,
            state=state,
        )
        if self._end_run_callback is not None:
            await self._end_run_callback(self._end_run_reason)
        return {"run_ended": True, "reason": self._end_run_reason, "state": state}

    async def _rename_topic(self, arguments: dict, agent_id: int | None) -> Any:
        field_id = arguments.get("field_id")
        if type(field_id) is not int or field_id <= 0:
            return {"error": "field_id must be a positive integer for orchestrator renames"}
        field = await self._session.get(Field, field_id)
        if field is None or field.run_id != self._run.id:
            return {
                "error": (
                    f"field_id {field_id} is not a field in this run; rename one listed "
                    "by read_fields"
                ),
                **await self._fields_brief(),
            }
        if agent_id is None:
            return {"error": "orchestrator agent is required"}
        writer = await self._session.get(Agent, agent_id)
        if writer is None or writer.run_id != self._run.id or writer.role != "orchestrator":
            return {"error": "only this run's orchestrator may rename fields"}
        if self._partition_worker or self._child_shard:
            if field.orchestrator_agent_id != self._owner_agent_id:
                return {"error": "field belongs to another orchestrator shard"}
        toolbox = Toolbox(self._session, self._fetcher, self._cache)
        toolbox._partition_worker = self._partition_worker or self._child_shard
        toolbox._owner_agent_id = self._owner_agent_id
        return await toolbox._rename_topic(arguments, agent_id)

    async def _open_seeded_technology(
        self, arguments: dict, agent_id: int | None
    ) -> Any:
        return {"error": "open_technology is unavailable on a seeded run"}

    async def _split_direction(self, arguments: dict, agent_id: int | None) -> Any:
        if error := self._time_dispatch_error():
            return {"error": error}
        cost_budget = getattr(self, "_cost_budget", None)
        if cost_budget is not None and cost_budget.reason is not None:
            return {"error": cost_budget.reason}
        if getattr(self._run, "seeded", False):
            return {"error": "split_direction is unavailable on a seeded run"}
        assistant = await self._runtime.create(
            run_id=self._run.id,
            role="assistant",
            system=self._prompts["assistant"],
            parent_agent_id=agent_id,
        )
        instruction = str(arguments.get("instruction") or "")
        cap = max(1, get_settings().split_max_fields)
        result = await self._runtime.run(
            assistant,
            (
                f"Направление: {self._run.query}\n\n{instruction}".strip()
                + f"\n\nAt most {cap} fields in total. This limit is the system's and "
                "holds whatever the instruction above asks for."
            ),
            max_steps=self._agent_max_steps,
            partial_on_failure=True,
        )
        if not result.content and getattr(result, "stopped", None) == "max_steps":
            emit(self._sink, "split_steps_spent", agent_id=assistant.id)
            retry = await self._runtime.resume(
                assistant.id,
                (
                    "Your step budget for this split is spent. Send the field list now, as "
                    "the JSON object your instructions describe, from what you have already "
                    "found. Call no more tools."
                ),
                max_steps=3,
                partial_on_failure=True,
            )
            if retry.content:
                result = retry
        if not result.content:
            return {
                "error": (
                    f"the assistant returned no field list: it stopped "
                    f"({getattr(result, 'stopped', 'unknown')}) after "
                    f"{getattr(result, 'steps', '?')} steps without answering. Call "
                    "split_direction again with a narrower instruction or fewer fields."
                ),
                "agent_id": assistant.id,
            }

        content = result.content
        items, error = parse_fields(content)

        if error:
            emit(self._sink, "split_reparse", agent_id=assistant.id, error=error[:200])
            retry = await self._runtime.resume(
                assistant.id,
                (
                    f"Твой ответ не разобрался как JSON: {error}. "
                    "Пришли тот же список снова, одним объектом JSON и больше ничем: "
                    "без пояснений, без ограждений кода и без запятых перед закрывающей "
                    "скобкой. Ничего не сокращай и ничего не выбрасывай."
                ),
                max_steps=self._agent_max_steps,
                partial_on_failure=True,
            )
            if retry.content:
                content = retry.content
                items, error = parse_fields(content)

        if error:
            field = Field(
                run_id=self._run.id,
                focus=content[:2000],
                rationale="unparsed assistant output, kept rather than dropped",
                state="dropped",
            )
            self._session.add(field)
            await self._session.commit()
            emit(self._sink, "split_failed", agent_id=assistant.id, error=error[:200])
            return {
                "error": "the assistant's split did not parse as JSON",
                "detail": error,
                "assistant_agent_id": assistant.id,
                "fields_written": 0,
                "kept_as_field_id": field.id,
                "note": (
                    "nothing was split. the raw answer is kept on that field, "
                    "state `dropped`, so it is not research work. split again, "
                    "or resume the assistant and ask for the list as plain JSON."
                ),
            }

        over_cap = 0
        if len(items) > cap:
            emit(self._sink, "split_over_cap", agent_id=assistant.id, count=len(items), cap=cap)
            trim = await self._runtime.resume(
                assistant.id,
                (
                    f"You sent {len(items)} fields; the limit is {cap}. Send the list "
                    f"again with at most {cap}: keep the fields most likely to be a real "
                    "transition now, drop the rest, change nothing else. The same JSON "
                    "object and nothing more."
                ),
                max_steps=self._agent_max_steps,
                partial_on_failure=True,
            )
            if trim.content:
                trimmed, trim_error = parse_fields(trim.content)
                if not trim_error:
                    items = trimmed
            if len(items) > cap:
                over_cap = len(items) - cap
                items = items[:cap]

        problems = await self._field_problems(items)
        correction_report = None
        if problems["unresolved"] or problems["shared"] or problems["near"]:
            emit(
                self._sink,
                "split_audit",
                agent_id=assistant.id,
                unresolved=len(problems["unresolved"]),
                shared=len(problems["shared"]),
                near=len(problems["near"]),
            )
            retry = await self._runtime.resume(
                assistant.id,
                _resolution_demand(problems),
                max_steps=self._agent_max_steps,
                partial_on_failure=True,
            )
            if retry.content:
                parsed, parse_error = parse_corrections(retry.content)
                if not parse_error:
                    if isinstance(parsed, list):
                        items = parsed
                        correction_report = {
                            "mode": "full_field_list",
                            "applied": [],
                            "unmatched": [],
                            "skipped": [],
                        }
                    else:
                        items, correction_report = apply_corrections(items, parsed)
                    problems = await self._field_problems(items)
                else:
                    correction_report = {
                        "mode": "correction_parse_failed",
                        "error": parse_error,
                    }

        if len(items) > cap:
            over_cap += len(items) - cap
            items = items[:cap]
        written = await self._write_fields(items)
        emit(self._sink, "fields_written", count=len(written), fields=written[:5])
        unresolved = [field for field in written if field["technology_id"] is None]
        return {
            "assistant_agent_id": assistant.id,
            "fields_written": len(written),
            **({"fields_over_cap_dropped": over_cap} if over_cap else {}),
            "fields": written[:40],
            "fields_without_a_technology": len(unresolved),
            "fields_sharing_a_technology": problems["shared"],
            "technologies_that_may_be_one": problems["near"],
            "correction": correction_report,
            "note": (
                "all of them are rows. dispatch what the budget affords; the rest "
                "stay pending and are the standing work a later run starts from."
                + (
                    " what is listed above survived a round of correction: the "
                    "assistant was asked about every one of them and this is "
                    "what it answered with. two fields on one technology are "
                    "only worth two researchers if the transition differs - "
                    "read their rationale before dispatching both."
                    if unresolved or problems["shared"] or problems["near"]
                    else ""
                )
            ),
        }

    async def _write_fields(self, items: list) -> list[dict]:
        written: list[dict] = []
        for ordinal, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                continue
            focus = str(item.get("focus") or item.get("name") or "").strip()
            if not focus:
                continue
            field = Field(
                run_id=self._run.id,
                area=str(item.get("area") or "").strip() or None,
                focus=focus,
                rationale=str(item.get("rationale") or "").strip() or None,
                technology_id=await self._technology_of(item),
                ordinal=ordinal,
                state="pending",
            )
            self._session.add(field)
            await self._session.flush()
            written.append(
                {
                    "id": field.id,
                    "area": field.area,
                    "focus": field.focus,
                    "ordinal": ordinal,
                    "technology_id": field.technology_id,
                    "rationale": field.rationale,
                }
            )
        return written

    async def _technology_of(self, item: dict) -> int | None:
        raw = item.get("technology_id")
        if raw in (None, "", 0, "0"):
            return None
        try:
            candidate = int(raw)
        except (TypeError, ValueError):
            return None
        if candidate <= 0:
            return None
        if await self._session.get(Technology, candidate) is None:
            return None
        return candidate

    async def _field_problems(self, items: list) -> dict:
        unresolved: list[str] = []
        by_technology: dict[int, list[str]] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            focus = str(item.get("focus") or item.get("name") or "").strip()
            if not focus:
                continue
            technology_id = await self._technology_of(item)
            if technology_id is None:
                unresolved.append(focus)
            else:
                by_technology.setdefault(technology_id, []).append(focus)

        shared = [
            {"technology_id": technology_id, "fields": focuses}
            for technology_id, focuses in by_technology.items()
            if len(focuses) > 1
        ]
        return {
            "unresolved": unresolved,
            "shared": shared,
            "near": await self._near_technologies(sorted(by_technology)),
        }

    async def _near_technologies(self, technology_ids: list[int]) -> list[dict]:
        if len(technology_ids) < 2:
            return []
        left, right = aliased(Technology), aliased(Technology)
        closeness = func.greatest(
            func.similarity(
                func.coalesce(left.canonical_name_en, ""),
                func.coalesce(right.canonical_name_en, ""),
            ),
            func.similarity(
                func.coalesce(left.canonical_name_ru, ""),
                func.coalesce(right.canonical_name_ru, ""),
            ),
        )
        rows = (
            await self._session.execute(
                select(
                    left.id,
                    right.id,
                    func.coalesce(left.canonical_name_en, left.canonical_name_ru),
                    func.coalesce(right.canonical_name_en, right.canonical_name_ru),
                    closeness,
                )
                .where(
                    left.id < right.id,
                    left.id.in_(technology_ids),
                    right.id.in_(technology_ids),
                    closeness > 0.35,
                )
                .order_by(closeness.desc())
                .limit(20)
            )
        ).all()
        return [
            {
                "left_id": left_id,
                "left": left_name,
                "right_id": right_id,
                "right": right_name,
                "closeness": round(float(closeness_value), 2),
            }
            for left_id, right_id, left_name, right_name, closeness_value in rows
        ]

    async def _read_fields(self, arguments: dict, agent_id: int | None) -> Any:
        query = select(Field).where(Field.run_id == self._run.id)
        if self._partition_worker or self._child_shard:
            query = query.where(Field.orchestrator_agent_id == self._owner_agent_id)
        state = arguments.get("state")
        if state:
            query = query.where(Field.state == str(state))
        query = query.order_by(Field.ordinal.nulls_last(), Field.id).limit(
            _clamped_limit(arguments, 50)
        )
        fields = (await self._session.scalars(query)).all()
        return {
            "fields": [
                {
                    "id": f.id,
                    "area": f.area,
                    "focus": f.focus,
                    "rationale": f.rationale,
                    "state": f.state,
                    "agent_id": f.agent_id,
                }
                for f in fields
            ]
        }

    async def _dispatch_researchers(self, arguments: dict, agent_id: int | None) -> Any:
        if error := self._time_dispatch_error():
            return {"error": error}
        cost_budget = getattr(self, "_cost_budget", None)
        if cost_budget is not None and cost_budget.reason is not None:
            return {"error": cost_budget.reason}
        field_ids = [int(i) for i in (arguments.get("field_ids") or [])]
        if not field_ids:
            return {
                "error": "field_ids is required: a list of pending field ids from read_fields",
                **await self._fields_brief(),
            }
        brief = str(arguments.get("brief") or "")
        partitioned = await self._partitioned()
        requested_ids = list(dict.fromkeys(field_ids))
        fields = (
            await self._session.scalars(
                select(Field).where(Field.id.in_(requested_ids), Field.run_id == self._run.id)
            )
        ).all()
        by_id = {field.id: field for field in fields}
        unknown_field_ids = [field_id for field_id in requested_ids if field_id not in by_id]
        if not fields:
            return {
                "error": (
                    "none of these ids is a field in this run; fields come from "
                    "split_direction and are listed by read_fields"
                ),
                "asked_for": requested_ids,
                "unknown_field_ids": unknown_field_ids,
                **await self._fields_brief(),
            }
        if self._partition_worker or self._child_shard:
            foreign = [
                field.id
                for field in fields
                if field.orchestrator_agent_id != self._owner_agent_id
            ]
            if foreign:
                return {
                    "error": "fields belong to another orchestrator shard",
                    "field_ids": foreign,
                    "unknown_field_ids": unknown_field_ids,
                    "not_dispatched": [
                        {"field_id": field_id, "reason": "field does not exist in this run"}
                        for field_id in unknown_field_ids
                    ],
                }

        not_dispatched = [
            {"field_id": field_id, "reason": "field does not exist in this run"}
            for field_id in unknown_field_ids
        ]
        candidates = []
        for field_id in requested_ids:
            field = by_id.get(field_id)
            if field is None:
                continue
            existing_entry = await self._session.scalar(
                select(Entry.id).where(
                    Entry.run_id == self._run.id,
                    Entry.field_id == field.id,
                ).limit(1)
            )
            if existing_entry is not None:
                not_dispatched.append(
                    {
                        "field_id": field_id,
                        "reason": (
                            f"field {field_id} already has an entry; researching it again "
                            "would change nothing that is shown"
                        ),
                    }
                )
            elif field.state != "pending":
                reason = (
                    "field is already dispatched"
                    if field.state == "dispatched"
                    else f"field is {field.state}"
                )
                not_dispatched.append({"field_id": field_id, "reason": reason})
            else:
                candidates.append(field)

        if (
            not partitioned
            and not self._child_shard
            and len(self._selected_fields | {field.id for field in candidates})
            > getattr(self, "_shard_size", 10)
        ):
            return {
                "error": (
                    f"one orchestrator researches at most "
                    f"{getattr(self, '_shard_size', 10)} fields. call "
                    "summon_orchestrators with every field you want researched, "
                    "including the ones already dispatched"
                ),
                "unknown_field_ids": unknown_field_ids,
                "not_dispatched": not_dispatched,
            }
        if not partitioned:
            self._selected_fields.update(field.id for field in candidates)
        room = self._research_budget - len(self._researched)
        if room <= 0:
            refused = candidates
            fields = []
        else:
            refused = candidates[room:]
            fields = candidates[:room]
        not_dispatched.extend(
            {"field_id": field.id, "reason": "research budget exhausted"}
            for field in refused
        )

        dispatched = []
        for field in fields:
            cost_budget = getattr(self, "_cost_budget", None)
            if cost_budget is not None and cost_budget.reason is not None:
                not_dispatched.extend(
                    {"field_id": item.id, "reason": cost_budget.reason}
                    for item in fields[len(dispatched):]
                )
                break
            field_metadata = metadata_from_rationale(field.rationale)
            field_brief = brief
            claim = supplied_claim(field_metadata)
            if claim:
                field_brief = f"{brief}\n\n{claim}".strip()
            agent = await self._runtime.create(
                run_id=self._run.id,
                role="researcher",
                system=self._prompts["researcher"],
                brief=field_brief,
                field=field.focus,
                parent_agent_id=self._owner_agent_id or agent_id,
            )
            field.state = "dispatched"
            field.agent_id = agent.id
            await self._session.commit()

            task = asyncio.create_task(
                self._researcher_task(agent.id, field.id, field.focus, field_brief),
                name=f"researcher-{agent.id}",
            )
            self._inflight[agent.id] = task
            self._researched.add(field.id)
            dispatched.append(
                {"agent_id": agent.id, "field_id": field.id, "focus": field.focus}
            )

        return {
            "dispatched": dispatched,
            "running": len(self._inflight),
            "not_dispatched": not_dispatched,
            "unknown_field_ids": unknown_field_ids,
            "research_remaining": max(0, self._research_budget - len(self._researched)),
            "note": (
                "they are working now. call collect to take returns as they "
                "land, and write what is settled while the rest are still out."
                + (
                    f" {len(not_dispatched)} requested field(s) were not started; "
                    "see not_dispatched for each reason."
                    if not_dispatched
                    else ""
                )
            ),
        }

    async def _researcher_task(
        self,
        agent_id: int,
        field_id: int,
        focus: str,
        brief: str,
        resume_existing: bool = False,
        resume_message: str | None = None,
    ) -> None:
        payload: dict
        try:
            async with self._sessionmaker() as session:
                agent = await session.get(Agent, agent_id)
                if agent is None:
                    raise RuntimeError(f"agent {agent_id} vanished before it ran")
                toolbox = Toolbox(session, self._fetcher, self._cache)
                runtime = AgentRuntime(
                    session, self._llm, toolbox, self._max_steps_for("researcher"), self._sink
                )
                runtime._cost_budget = getattr(self, "_cost_budget", None)
                if resume_existing:
                    result = await runtime.resume(
                        agent_id,
                        resume_message or (
                            "Продолжай исследование с того места, где остановилась история. "
                            "Заверши работу и дай итоговый ответ."
                        ),
                        partial_on_failure=True,
                    )
                else:
                    result = await runtime.run(
                        agent,
                        f"{brief}\n\nПоле: {focus}".strip(),
                        partial_on_failure=True,
                    )
                effort = await effort_for(session, [agent_id])
                await session.commit()
                if result.submission is None:
                    result.submission = await self._stored_submission(
                        agent_id, "submit_findings", session
                    )
                    if result.submission is not None:
                        result.stopped = "submitted"

            emit(
                self._sink,
                "researcher_returned",
                agent_id=agent_id,
                focus=focus,
                stopped=result.stopped,
                steps=result.steps,
                searches=effort["searches_run"],
                sources=effort["sources_checked"],
                chars=len(result.content or ""),
            )
            payload = {
                "kind": "research",
                "field_id": field_id,
                "focus": focus,
                "agent_id": agent_id,
                "stopped": result.stopped,
                "steps": result.steps,
                "effort": effort,
                **self._agent_return(result),
            }
        except asyncio.CancelledError:
            self._returns.put_nowait(
                {
                    "kind": "research",
                    "field_id": field_id,
                    "focus": focus,
                    "agent_id": agent_id,
                    "stopped": "cancelled",
                    "analysis": None,
                }
            )
            self._inflight.pop(agent_id, None)
            raise
        except Exception as exc:
            if resume_existing:
                await self._mark_agent_failed(agent_id)
            emit(
                self._sink,
                "researcher_returned",
                agent_id=agent_id,
                focus=focus,
                stopped="error",
                steps=0,
                searches=0,
                sources=0,
                chars=0,
            )
            payload = {
                "kind": "research",
                "field_id": field_id,
                "focus": focus,
                "agent_id": agent_id,
                "stopped": "failed" if resume_existing else "error",
                "error": f"{type(exc).__name__}: {exc}"[:300],
                "analysis": None,
                "submitted": False,
            }

        self._inflight.pop(agent_id, None)
        if payload.get("kind") == "refutation":
            payload = self._compact_return(payload)
        self._returns.put_nowait(payload)

    def _compact_return(self, item: dict) -> dict:
        allowed = (
            {
                "kind", "field_id", "focus", "agent_id", "stopped", "steps",
                "effort", "analysis", "submitted", "note", "error", "rename_history",
            }
            if item.get("kind") == "research"
            else {
                "kind", "refutation_id", "refuter_agent_id", "attacked_agent_id",
                "attack", "attack_submitted", "attack_note", "stopped", "effort",
                "rebuttal", "rebuttal_submitted", "rebuttal_note", "rebuttal_stopped",
                "incomplete", "error", "delivery_version", "focus", "field_id",
                "rename_history",
            }
        )
        compact = {key: value for key, value in item.items() if key in allowed}
        cap = max(0, get_settings().return_fallback_max_chars)
        if compact.get("kind") == "research":
            if compact.get("submitted"):
                compact["analysis"] = validate_submission(
                    "submit_findings", compact.get("analysis")
                )
            else:
                compact["analysis"] = str(compact.get("analysis") or "")[:cap]
                if "stopped" in compact or "submitted" in compact:
                    compact["note"] = (
                        "agent did not submit structured findings; final text "
                        f"truncated to {cap} characters"
                    )
        elif compact.get("kind") == "refutation":
            if compact.get("attack_submitted"):
                compact["attack"] = validate_submission(
                    "submit_refutation", compact.get("attack")
                )
            elif compact.get("attack") is not None:
                compact["attack"] = str(compact["attack"])[:cap]
                if "stopped" in compact or "attack_submitted" in compact:
                    compact["attack_note"] = (
                        "agent did not submit structured findings; final text "
                        f"truncated to {cap} characters"
                    )
            if compact.get("rebuttal_submitted"):
                compact["rebuttal"] = validate_submission(
                    "submit_findings", compact.get("rebuttal")
                )
            elif compact.get("rebuttal") is not None:
                compact["rebuttal"] = str(compact["rebuttal"])[:cap]
                if "rebuttal_stopped" in compact or "rebuttal_submitted" in compact:
                    compact["rebuttal_note"] = (
                        "agent did not submit structured findings; final text "
                        f"truncated to {cap} characters"
                    )
        return compact

    def _agent_return(self, result: AgentResult) -> dict:
        if result.submission is not None:
            return {"analysis": result.submission, "submitted": True}
        cap = max(0, get_settings().return_fallback_max_chars)
        text = result.content or ""
        return {
            "analysis": text[:cap],
            "submitted": False,
            "note": (
                "agent did not submit structured findings; final text "
                f"truncated to {cap} characters"
            ),
        }

    async def _read_return(self, arguments: dict, agent_id: int | None) -> Any:
        target_id = arguments.get("agent_id")
        part = arguments.get("part")
        if type(target_id) is not int or type(part) is not int or part < 1:
            return {"error": "agent_id and positive part are required"}
        target = await self._session.get(Agent, target_id)
        if (
            target is None
            or target.run_id != self._run.id
            or target.role not in ("researcher", "refuter")
        ):
            return {
                "error": (
                    f"no researcher or refuter {target_id} in this run; agent ids come "
                    "from dispatch_researchers, refute and collect"
                ),
                **await self._fields_brief(),
            }
        text = await self._final_answer(target_id)
        if text is None:
            submission = await self._stored_submission(
                target_id,
                "submit_findings" if target.role == "researcher" else "submit_refutation",
            )
            if submission is not None:
                text = json.dumps(submission, ensure_ascii=False, separators=(",", ":"))
        if text is None:
            turns = (
                await self._session.scalars(
                    select(Turn.content)
                    .where(Turn.agent_id == target_id, Turn.kind == "assistant")
                    .order_by(Turn.seq.desc())
                )
            ).all()
            text = next(
                (
                    str(content.get("error"))
                    for content in turns
                    if isinstance(content, dict) and content.get("error")
                ),
                None,
            )
        if text is None:
            return {"error": "agent has no stored final text", "agent_id": target_id}
        cap = max(1, min(20000, get_settings().read_return_part_chars))
        total = len(text)
        parts = max(1, (total + cap - 1) // cap)
        if part > parts:
            return {
                "error": "part is out of range",
                "agent_id": target_id,
                "total": total,
                "parts": parts,
            }
        start = (part - 1) * cap
        return {
            "agent_id": target_id,
            "total": total,
            "part": part,
            "parts": parts,
            "text": text[start:start + cap],
        }

    async def _collect(self, arguments: dict, agent_id: int | None) -> Any:
        wait_s = float(arguments.get("wait_s") or 120)
        if self._partition_worker and agent_id != self._owner_agent_id:
            return {"error": "only this shard orchestrator may collect its queue"}
        returns: list[dict] = []
        deferred: list[dict] = []

        while not self._returns.empty():
            item = self._returns.get_nowait()
            if (
                item.get("kind") == "research"
                and item.get("agent_id") is not None
                and not item.get("submitted")
            ):
                recovered = await self._stored_submission(
                    item.get("agent_id"), "submit_findings"
                ) if item.get("agent_id") else None
                if recovered is not None:
                    item = dict(item)
                    item["analysis"] = recovered
                    item["submitted"] = True
                    item.pop("note", None)
                elif (
                    item.get("agent_id") is None
                    and item.get("analysis") is not None
                    and "stopped" not in item
                    and "submitted" not in item
                ):
                    deferred.append(item)
                    continue
            returns.append(item)

        if not returns and self._inflight:
            try:
                returns.append(await asyncio.wait_for(self._returns.get(), wait_s))
            except TimeoutError:
                pass
            while not self._returns.empty():
                item = self._returns.get_nowait()
                if (
                    item.get("kind") == "research"
                    and item.get("agent_id") is not None
                    and not item.get("submitted")
                ):
                    recovered = await self._stored_submission(
                        item.get("agent_id"), "submit_findings"
                    ) if item.get("agent_id") else None
                    if recovered is not None:
                        item = dict(item)
                        item["analysis"] = recovered
                        item["submitted"] = True
                        item.pop("note", None)
                    elif (
                        item.get("agent_id") is None
                        and item.get("analysis") is not None
                        and "stopped" not in item
                        and "submitted" not in item
                    ):
                        deferred.append(item)
                        continue
                returns.append(item)

        if self._partition_worker:
            owned: list[dict] = []
            foreign: list[dict] = []
            for item in returns:
                (owned if await self._owns_return(item) else foreign).append(item)
            if foreign:
                emit(
                    self._sink,
                    "foreign_returns",
                    orchestrator_agent_id=self._owner_agent_id,
                    items=[
                        {
                            key: item.get(key)
                            for key in ("kind", "field_id", "agent_id", "refutation_id")
                        }
                        for item in foreign
                    ],
                )
            returns = owned
        delivered_reviews = []
        for item in returns:
            field = None
            if item.get("kind") == "research" and item.get("field_id"):
                field = await self._session.get(Field, item["field_id"])
            elif item.get("kind") == "refutation" and item.get("refutation_id"):
                review = await self._session.get(Refutation, item["refutation_id"])
                researcher = (
                    await self._session.get(Agent, review.researcher_agent_id)
                    if review is not None
                    else None
                )
                if researcher is not None:
                    field = await self._session.scalar(
                        select(Field).where(
                            Field.run_id == self._run.id,
                            Field.agent_id == researcher.id,
                        )
                    )
            if field is not None:
                history = (
                    await self._session.scalars(
                        select(FieldRename).where(FieldRename.field_id == field.id)
                        .order_by(FieldRename.created_at, FieldRename.id)
                    )
                ).all()
                item["field_id"] = field.id
                item["focus"] = field.focus
                if history:
                    item["rename_history"] = [
                        {
                            "previous_focus": row.previous_focus,
                            "new_focus": row.new_focus,
                            "reason": row.reason,
                            "verdict_on_previous": row.verdict_on_previous,
                        }
                        for row in history
                    ]
            review_id = item.get("refutation_id")
            if review_id:
                review = await self._session.get(Refutation, review_id)
                if review is not None and review.state != "pending":
                    item["delivery_version"] = review.delivery_version
                    review.collected = True
                    delivered_reviews.append(review_id)
        returns = [self._compact_return(item) for item in returns]
        for item in deferred:
            self._returns.put_nowait(item)
        await self._session.commit()
        result = {
            "returns": returns,
            "collected": len(returns),
            "refutations_delivered": delivered_reviews,
            "still_running": sorted(self._inflight),
        }
        if self._inflight:
            result["note"] = "the rest are still out. write what is settled, then collect again."
        else:
            pending_query = select(Field).where(
                Field.run_id == self._run.id,
                Field.state == "pending",
            )
            if getattr(self, "_partition_worker", False) or getattr(
                self, "_child_shard", False
            ):
                pending_query = pending_query.where(
                    Field.orchestrator_agent_id == self._owner_agent_id
                )
            else:
                pending_query = pending_query.where(Field.orchestrator_agent_id.is_(None))
            pending = (
                await self._session.scalars(
                    pending_query.order_by(Field.ordinal.nulls_last(), Field.id)
                )
            ).all()
            field_ids = [field.id for field in pending]
            if self._time_dispatch_error():
                result["pending_field_ids"] = field_ids
                if returns or not self._returns.empty():
                    result["next_action"] = (
                        "No new research: collect, refute and write already-researched work, "
                        "then finish. Leave pending fields for another run."
                    )
                else:
                    result["next_action"] = (
                        "Nothing is running and no new research is allowed: write entries "
                        "for delivered returns that have none, then finish."
                    )
                    if await self._repeats_idle_collect(agent_id, result["next_action"]):
                        result["orchestrator_finished"] = True
                        result["note"] = (
                            "collect found nothing to do twice in a row with research closed, "
                            "so this orchestrator is finished. Pending fields stay for "
                            "another run."
                        )
            elif field_ids:
                result["pending_field_ids"] = field_ids
                dispatch = (
                    "Dispatch these pending fields with dispatch_researchers: "
                    + ", ".join(str(field_id) for field_id in field_ids)
                )
                if returns:
                    result["next_action"] = "Write the delivered returns' entries. " + dispatch
                    result["note"] = (
                        "nothing is running or waiting; the returns above are delivered. "
                        "You still own pending fields and must dispatch the named ids."
                    )
                elif (
                    self._research_budget > len(self._researched)
                    and await self._repeats_idle_collect(agent_id, dispatch)
                ):
                    dispatched = await self._dispatch_researchers(
                        {"field_ids": field_ids, "brief": await self._dispatch_brief(agent_id)},
                        agent_id,
                    )
                    result.update(
                        {
                            "dispatch": dispatched,
                            "still_running": sorted(self._inflight),
                            "next_action": (
                                "Collect to take the returns of the fields dispatched above "
                                "as they land, and write what is settled."
                            ),
                            "note": (
                                "collect dispatched these fields itself because they were "
                                "pending and nothing was running; dispatch shows what started "
                                "and why anything did not."
                            ),
                        }
                    )
                else:
                    result.update(
                        {
                            "error": "collect has no returns because no researchers are running",
                            "next_action": dispatch,
                            "note": (
                                "nothing is running and no return is waiting. You still own "
                                "pending fields; dispatch the named ids before collecting again."
                            ),
                        }
                    )
            elif returns:
                result["note"] = (
                    "nothing is left running; these returns are delivered. Write their "
                    "entries or finish."
                )
            else:
                result.update(
                    {
                        "done": True,
                        "next_action": (
                            "Write any remaining entries for delivered returns, or finish."
                        ),
                        "note": (
                            "nothing is running, no return is waiting, and no pending "
                            "fields remain in this shard. Write any remaining entries "
                            "for delivered returns, or finish."
                        ),
                    }
                )
        return result

    async def _repeats_idle_collect(self, agent_id: int | None, next_action: str) -> bool:
        if agent_id is None:
            return False
        content = await self._session.scalar(
            select(Turn.content)
            .where(
                Turn.agent_id == agent_id,
                or_(
                    Turn.kind == "tool_result",
                    and_(
                        Turn.kind == "tool_call",
                        Turn.content["name"].astext != "collect",
                    ),
                ),
            )
            .order_by(Turn.seq.desc())
            .limit(1)
        )
        payload = content.get("payload") if isinstance(content, dict) else None
        return (
            isinstance(payload, dict)
            and content.get("name") == "collect"
            and not payload.get("returns")
            and not payload.get("still_running")
            and payload.get("next_action") == next_action
        )

    async def _dispatch_brief(self, agent_id: int | None) -> str:
        content = await self._session.scalar(
            select(Turn.content)
            .where(
                Turn.agent_id == agent_id,
                Turn.kind == "tool_call",
                Turn.content["name"].astext == "dispatch_researchers",
            )
            .order_by(Turn.seq.desc())
            .limit(1)
        )
        arguments = content.get("arguments") if isinstance(content, dict) else None
        if isinstance(arguments, dict) and arguments.get("brief"):
            return str(arguments["brief"])
        agent = await self._session.get(Agent, agent_id) if agent_id is not None else None
        return str(getattr(agent, "brief", None) or "")

    async def _owns_return(self, item: dict) -> bool:
        if item.get("kind") == "research":
            return item.get("field_id") in self._selected_fields
        review_id = item.get("refutation_id")
        if not review_id:
            return True
        review = await self._session.get(Refutation, review_id)
        if review is None:
            return True
        researcher = await self._session.get(Agent, review.researcher_agent_id)
        return researcher is None or researcher.parent_agent_id == self._owner_agent_id

    async def restore(self) -> dict:
        pending_query = select(Refutation).where(
            Refutation.run_id == self._run.id,
            Refutation.state == "pending",
        )
        if self._partition_worker or self._child_shard:
            pending_query = pending_query.join(
                Agent, Refutation.researcher_agent_id == Agent.id
            ).where(Agent.parent_agent_id == self._owner_agent_id)
        pending_reviews = (await self._session.scalars(pending_query)).all()
        completed_query = select(Refutation).where(
            Refutation.run_id == self._run.id,
            Refutation.state != "pending",
        )
        if self._partition_worker or self._child_shard:
            completed_query = completed_query.join(
                Agent, Refutation.researcher_agent_id == Agent.id
            ).where(Agent.parent_agent_id == self._owner_agent_id)
        completed_uncollected = (await self._session.scalars(completed_query)).all()
        review_returns = 0
        for review in completed_uncollected:
            if review.outcome and not await self._review_delivered(
                review,
                self._owner_agent_id if self._partition_worker or self._child_shard else None,
            ):
                review.collected = False
                self._returns.put_nowait(dict(review.outcome))
                review_returns += 1
        await self._session.commit()

        fields_query = select(Field).where(
            Field.run_id == self._run.id,
            Field.state.in_(("dispatched", "done")),
        )
        if self._partition_worker or self._child_shard:
            fields_query = fields_query.where(
                Field.orchestrator_agent_id == self._owner_agent_id
            )
        fields = (
            await self._session.scalars(
                fields_query.order_by(Field.ordinal.nulls_last(), Field.id)
            )
        ).all()
        delivered_research = await self._delivered_research()
        if not self._partition_worker:
            self._selected_fields.update(field.id for field in fields)

        replayed: list[dict] = []
        without_work: list[int] = []
        pending_researchers = {review.researcher_agent_id for review in pending_reviews}
        continuation_tasks: list[asyncio.Task] = []
        continuation_ids: set[int] = set()
        for review in pending_reviews:
            refuter = await self._session.get(Agent, review.refuter_agent_id)
            researcher = await self._session.get(Agent, review.researcher_agent_id)
            task = asyncio.create_task(
                self._refuter_task(
                    review.id,
                    review.refuter_agent_id,
                    review.researcher_agent_id,
                    refuter.brief if refuter is not None else "",
                    "",
                    review.rebuttal_required,
                    resume_existing=True,
                ),
                name=f"refuter-{review.refuter_agent_id}-restore",
            )
            self._inflight[review.refuter_agent_id] = task
            continuation_tasks.append(task)
            continuation_ids.add(review.refuter_agent_id)
            if review.rebuttal_required:
                continuation_ids.add(review.researcher_agent_id)
            if researcher is None:
                without_work.append(review.researcher_agent_id)
        for field in fields:
            self._researched.add(field.id)
            if (
                field.state == "done"
                or field.agent_id is None
                or (field.id, field.agent_id) in delivered_research
                or field.agent_id in pending_researchers
            ):
                continue
            agent = await self._session.get(Agent, field.agent_id)
            if agent is None:
                without_work.append(field.agent_id)
                continue
            if agent.state == "running":
                task = asyncio.create_task(
                    self._researcher_task(
                        agent.id,
                        field.id,
                        field.focus,
                        getattr(agent, "brief", None) or "",
                        resume_existing=True,
                    ),
                    name=f"researcher-{agent.id}-restore",
                )
                self._inflight[agent.id] = task
                continuation_tasks.append(task)
                continuation_ids.add(agent.id)
                continue
            analysis = await self._final_answer(agent.id)
            submission = await self._stored_submission(
                agent.id, "submit_findings", self._session
            )
            if not analysis and submission is None:
                agent.state = "failed"
                without_work.append(agent.id)
                self._returns.put_nowait(
                    {
                        "kind": "research",
                        "field_id": field.id,
                        "focus": field.focus,
                        "agent_id": agent.id,
                        "stopped": "failed",
                        "analysis": None,
                        "submitted": False,
                    }
                )
                continue
            self._returns.put_nowait(
                {
                    "kind": "research",
                    "field_id": field.id,
                    "focus": field.focus,
                    "agent_id": agent.id,
                    "stopped": "answered" if agent.state == "idle" else f"partial:{agent.state}",
                    "effort": await effort_for(self._session, [agent.id]),
                    "analysis": submission if submission is not None else str(analysis)[
                        :max(0, get_settings().return_fallback_max_chars)
                    ],
                    "submitted": submission is not None,
                    "note": (
                        None
                        if submission is not None
                        else (
                            "agent did not submit structured findings; final text "
                            f"truncated to {max(0, get_settings().return_fallback_max_chars)} "
                            "characters. this return is from before the interruption, "
                            "replayed from the store rather than newly arrived."
                        )
                    ),
                }
            )
            replayed.append(
                {"agent_id": agent.id, "field_id": field.id, "focus": field.focus}
            )
        await self._session.commit()
        if continuation_tasks:
            await asyncio.gather(*continuation_tasks, return_exceptions=True)
        failed_agents = []
        for agent_id in sorted(continuation_ids):
            agent = await self._session.get(Agent, agent_id)
            if agent is None or agent.state == "failed":
                failed_agents.append(agent_id)
                if agent_id not in without_work:
                    without_work.append(agent_id)
        continued_agents = sorted(continuation_ids - set(failed_agents))

        entries_written = await self._session.scalar(
            select(func.count()).select_from(Entry).where(Entry.run_id == self._run.id)
        )
        return {
            "returns_replayed": len(replayed) + review_returns,
            "refutations_replayed": review_returns,
            "replayed": replayed,
            "agents_with_nothing_to_replay": without_work,
            "continued_agents": continued_agents,
            "failed_agents": failed_agents,
            "fields_counted_against_the_ceiling": len(self._researched),
            "entries_already_written": entries_written or 0,
        }

    async def _delivered_research(self) -> set[tuple[int, int]]:
        query = select(Turn.content).join(Agent, Turn.agent_id == Agent.id).where(
            Agent.run_id == self._run.id,
            Agent.role == "orchestrator",
            Turn.kind == "tool_result",
            Turn.content["name"].astext == "collect",
        )
        if self._partition_worker or self._child_shard:
            query = query.where(Turn.agent_id == self._owner_agent_id)
        rows = (await self._session.scalars(query)).all()
        delivered: set[tuple[int, int]] = set()
        for content in rows:
            if not isinstance(content, dict):
                continue
            payload = content.get("payload")
            returns = payload.get("returns") if isinstance(payload, dict) else None
            if not isinstance(returns, list):
                continue
            for item in returns:
                if not isinstance(item, dict) or item.get("kind") != "research":
                    continue
                field_id = item.get("field_id")
                researcher_id = item.get("agent_id")
                if isinstance(field_id, int) and isinstance(researcher_id, int):
                    delivered.add((field_id, researcher_id))
        return delivered

    async def _stored_submission(
        self, agent_id: int, name: str, session: AsyncSession | None = None
    ) -> dict | None:
        session = session or self._session
        rows = (
            await session.scalars(
                select(Turn.content)
                .where(Turn.agent_id == agent_id, Turn.kind == "tool_result")
                .order_by(Turn.seq.desc())
            )
        ).all()
        for content in rows:
            if (
                isinstance(content, dict)
                and content.get("name") == name
                and isinstance(content.get("payload"), dict)
                and content["payload"].get("submitted") is True
            ):
                try:
                    return validate_submission(name, content["payload"].get("submission"))
                except ValueError:
                    return None
        return None

    async def _final_answer(
        self, agent_id: int, session: AsyncSession | None = None
    ) -> str | None:
        session = session or self._session
        rows = (
            await session.scalars(
                select(Turn.content)
                .where(Turn.agent_id == agent_id, Turn.kind == "assistant")
                .order_by(Turn.seq.desc())
                .limit(8)
            )
        ).all()
        for content in rows:
            if not isinstance(content, dict):
                continue
            message = content.get("message")
            if isinstance(message, dict) and message.get("content"):
                return str(message["content"])
        return None

    @property
    def inflight(self) -> int:
        return len(self._inflight)

    async def stop_for_cost_limit(self) -> None:
        cost_budget = getattr(self, "_cost_budget", None)
        reason = cost_budget.reason if cost_budget is not None else "run cost limit reached"
        self._end_run_reason = reason
        tasks = list(self._inflight.values())
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._inflight.clear()
        self._end_run_reason = None

    async def shutdown(self) -> None:
        tasks = list(self._inflight.values())
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    async def _refute(self, arguments: dict, agent_id: int | None) -> Any:
        cost_budget = getattr(self, "_cost_budget", None)
        if cost_budget is not None and cost_budget.reason is not None:
            return {"error": cost_budget.reason}
        target_id = int(arguments.get("agent_id") or 0)
        target = await self._session.get(Agent, target_id)
        if target is None or target.run_id != self._run.id or target.role != "researcher":
            return {
                "error": (
                    f"no researcher {target_id} in this run; refute takes the "
                    "researched_by_agent_id of a dispatched field"
                ),
                **await self._fields_brief(),
            }
        if self._partition_worker and target.parent_agent_id != self._owner_agent_id:
            return {"error": "researcher belongs to another orchestrator shard"}

        conflict = await self._research_change_error(target)
        if conflict:
            return {"error": conflict}
        recent_reviews = (await self._session.scalars(
            select(Refutation).where(
                Refutation.run_id == self._run.id,
                Refutation.researcher_agent_id == target_id,
            ).order_by(Refutation.id.desc()).limit(1)
        )).all()
        recent_review = recent_reviews[0] if recent_reviews else None
        if recent_review is not None and recent_review.state in (
            "cancelled", "failed", "interrupted"
        ):
            outcome = recent_review.outcome or {}
            resume_id = (
                target_id if outcome.get("attack_submitted") or outcome.get("attack")
                else recent_review.refuter_agent_id
            )
            instruction = str(arguments.get("instruction") or "")
            return await self._resume_agent({
                "agent_id": resume_id,
                "message": instruction or "Continue and complete the interrupted review.",
            }, agent_id)
        bundle = await self._lift_bundle(
            target_id, "ЗАЯВЛЕНИЕ ИССЛЕДОВАТЕЛЯ, ДОСЛОВНО", include_citations=True
        )
        instruction = str(arguments.get("instruction") or "")
        rebut = arguments.get("rebut")
        rebut = True if rebut is None else bool(rebut)

        refuter = await self._runtime.create(
            run_id=self._run.id,
            role="refuter",
            system=self._prompts["refuter"],
            brief=instruction,
            field=target.field,
            parent_agent_id=self._owner_agent_id or agent_id,
        )
        review = Refutation(
            run_id=self._run.id,
            researcher_agent_id=target_id,
            refuter_agent_id=refuter.id,
            rebuttal_required=rebut,
            state="pending",
        )
        self._session.add(review)
        await self._session.commit()

        task = asyncio.create_task(
            self._refuter_task(review.id, refuter.id, target_id, instruction, bundle, rebut),
            name=f"refuter-{refuter.id}",
        )
        self._inflight[refuter.id] = task

        return {
            "refuter_agent_id": refuter.id,
            "attacked_agent_id": target_id,
            "refutation_id": review.id,
            "rebuttal_will_follow": rebut,
            "running": len(self._inflight),
            "note": (
                "it is attacking now. the attack and the researcher's answer to "
                "it come back together through collect. refute the others while "
                "this one works."
            ),
        }

    async def _save_refutation(self, review_id: int, payload: dict, state: str) -> None:
        payload = self._compact_return(payload)
        async with self._sessionmaker() as session:
            review = await session.get(Refutation, review_id)
            if review is None:
                raise RuntimeError(f"refutation {review_id} disappeared")
            review.delivery_version += 1
            payload["delivery_version"] = review.delivery_version
            review.state = state
            review.outcome = dict(payload)
            review.collected = False
            review.completed_at = datetime.now(UTC)
            await session.commit()
        self._returns.put_nowait(payload)

    async def _refuter_task(
        self,
        review_id: int,
        refuter_id: int,
        target_id: int,
        instruction: str,
        bundle: str,
        rebut: bool,
        resume_existing: bool = False,
        resume_target_id: int | None = None,
        resume_message: str | None = None,
    ) -> None:
        payload = {
            "kind": "refutation",
            "refutation_id": review_id,
            "refuter_agent_id": refuter_id,
            "attacked_agent_id": target_id,
            "attack": None,
            "rebuttal": None,
            "rebuttal_stopped": None,
        }
        state = "failed"
        active_agent_id = refuter_id
        try:
            async with self._sessionmaker() as session:
                refuter = await session.get(Agent, refuter_id)
                if refuter is None:
                    raise RuntimeError(f"refuter {refuter_id} vanished before it ran")
                runtime = AgentRuntime(
                    session, self._llm, Toolbox(session, self._fetcher, self._cache),
                    self._max_steps_for("refuter"), self._sink,
                )
                runtime._cost_budget = getattr(self, "_cost_budget", None)
                if (
                    resume_existing and refuter.state == "idle"
                    and resume_target_id != refuter_id
                ):
                    answer = await self._final_answer(refuter_id, session)
                    stored = await self._stored_submission(
                        refuter_id, "submit_refutation", session
                    )
                    if not answer and stored is None:
                        raise RuntimeError(f"refuter {refuter_id} has no answer to continue")
                    attack = AgentResult(
                        refuter_id, answer, 0,
                        "submitted" if stored is not None else "answered", 0, stored,
                    )
                elif resume_existing:
                    attack = await runtime.resume(
                        refuter_id,
                        resume_message if resume_target_id == refuter_id else (
                            "Продолжай атаку с того места, где остановилась история, "
                            "и заверши её ответом."
                        ),
                        partial_on_failure=True,
                    )
                else:
                    attack = await runtime.run(
                        refuter,
                        f"{instruction}\n\n{bundle}".strip(),
                        partial_on_failure=True,
                    )
                effort = await effort_for(session, [refuter_id])
                await session.commit()
                if attack.submission is None:
                    attack.submission = await self._stored_submission(
                        refuter_id, "submit_refutation", session
                    )
                    if attack.submission is not None:
                        attack.stopped = "submitted"
                if attack.submission is not None:
                    attack.submission = validate_submission(
                        "submit_refutation", attack.submission
                    )
            attack_text = _agent_result_text(attack)
            rebuttal_text = ""
            payload.update(
                attack=self._agent_return(attack)["analysis"],
                attack_submitted=attack.submission is not None,
                attack_note=(
                    None
                    if attack.submission is not None
                    else self._agent_return(attack)["note"]
                ),
                stopped=attack.stopped,
                effort=effort,
            )
            emit(
                self._sink, "refuter_returned", agent_id=refuter_id, attacked=target_id,
                stopped=attack.stopped, searches=effort["searches_run"],
                chars=len(attack_text),
            )
            if rebut and attack_text.strip():
                active_agent_id = target_id
                async with self._sessionmaker() as session:
                    counter = await self._lift_bundle(
                        refuter_id, "ВОЗРАЖЕНИЕ КРИТИКА, ДОСЛОВНО", session,
                        answer_override=attack_text,
                    )
                    researcher = await session.get(Agent, target_id)
                    runtime = AgentRuntime(
                        session, self._llm, Toolbox(session, self._fetcher, self._cache),
                        self._max_steps_for("researcher"), self._sink,
                    )
                    runtime._cost_budget = getattr(self, "_cost_budget", None)
                    has_rebuttal_prompt = (
                        await self._has_rebuttal_prompt(target_id, session)
                        if resume_existing
                        else False
                    )
                    if (
                        resume_existing
                        and researcher is not None
                        and researcher.state == "idle"
                        and has_rebuttal_prompt
                        and await self._has_rebuttal_answer(target_id, session)
                        and resume_target_id not in (target_id, refuter_id)
                    ):
                        previous_answer = await self._final_answer(target_id, session)
                        stored = await self._stored_submission(
                            target_id, "submit_findings", session
                        )
                        answer = AgentResult(
                            target_id, previous_answer, 0,
                            "submitted" if stored is not None else "answered", 0, stored,
                        )
                    else:
                        rebuttal_message = (
                            "Продолжай ответ критику с того места, где остановилась "
                            "история, и заверши ответ."
                            if (
                                resume_existing and has_rebuttal_prompt
                                and resume_target_id != refuter_id
                            )
                            else (
                                "Критик пытался доказать обратное каждому твоему "
                                "утверждению. Ниже его ответ дословно, вместе с тем, "
                                "что он искал и что нашёл. "
                                "Ответь по существу: где он прав, где нет, и какие "
                                "из своих утверждений ты снимаешь. Если ответ уже "
                                "есть в том, что ты собрал, покажи его; если нужно "
                                "искать заново — ищи.\n\n" + counter
                            )
                        )
                        if resume_target_id == target_id and resume_message:
                            rebuttal_message += "\n\n" + resume_message
                        answer = await runtime.resume(
                            target_id,
                            rebuttal_message,
                            partial_on_failure=True,
                        )
                    await session.commit()
                    if answer.submission is None:
                        answer.submission = await self._stored_submission(
                            target_id, "submit_findings", session
                        )
                        if answer.submission is not None:
                            answer.stopped = "submitted"
                    if answer.submission is not None:
                        answer.submission = validate_submission(
                            "submit_findings", answer.submission
                        )
                rebuttal_text = _agent_result_text(answer)
                payload.update(
                    rebuttal=self._agent_return(answer)["analysis"],
                    rebuttal_submitted=answer.submission is not None,
                    rebuttal_note=(
                        None
                        if answer.submission is not None
                        else self._agent_return(answer)["note"]
                    ),
                    rebuttal_stopped=answer.stopped,
                )
                emit(
                    self._sink, "rebuttal_returned", agent_id=target_id,
                    stopped=answer.stopped, chars=len(rebuttal_text),
                )
            completed = (
                attack.stopped in ("answered", "submitted")
                and bool(attack_text.strip())
                and (
                    not rebut or (
                        payload["rebuttal_stopped"] in ("answered", "submitted")
                        and bool(rebuttal_text.strip())
                    )
                )
            )
            if completed:
                state = "completed"
            else:
                payload["incomplete"] = "attack or requested rebuttal did not finish with an answer"
        except asyncio.CancelledError:
            payload.update(stopped="cancelled", incomplete="the exchange was cancelled")
            try:
                await asyncio.shield(self._save_refutation(review_id, payload, "cancelled"))
            finally:
                self._inflight.pop(refuter_id, None)
            raise
        except Exception as exc:
            if resume_existing:
                await self._mark_agent_failed(active_agent_id)
            payload.update(
                stopped="failed" if resume_existing else "error",
                error=f"{type(exc).__name__}: {exc}"[:300],
                incomplete="the exchange failed",
            )
            emit(
                self._sink, "refuter_returned", agent_id=refuter_id, attacked=target_id,
                stopped="error", searches=0, chars=0,
            )
        try:
            await self._save_refutation(review_id, payload, state)
        finally:
            self._inflight.pop(refuter_id, None)

    async def _has_rebuttal_prompt(
        self, agent_id: int, session: AsyncSession
    ) -> bool:
        last_user = await session.scalar(
            select(Turn)
            .where(Turn.agent_id == agent_id, Turn.kind == "user")
            .order_by(Turn.seq.desc())
            .limit(1)
        )
        content = last_user.content if last_user and isinstance(last_user.content, dict) else {}
        return str(content.get("content") or "").startswith("Критик пытался доказать")

    async def _has_rebuttal_answer(
        self, agent_id: int, session: AsyncSession
    ) -> bool:
        turns = (
            await session.scalars(
                select(Turn).where(Turn.agent_id == agent_id).order_by(Turn.seq)
            )
        ).all()
        last_user_seq = 0
        last_answer = None
        for turn in turns:
            content = turn.content if isinstance(turn.content, dict) else {}
            if turn.kind == "user":
                last_user_seq = turn.seq
            elif turn.kind == "assistant" and turn.seq > last_user_seq:
                message = content.get("message")
                if isinstance(message, dict) and message.get("content"):
                    last_answer = turn
        return last_answer is not None and not (last_answer.content or {}).get("error")

    async def _mark_agent_failed(self, agent_id: int) -> None:
        async with self._sessionmaker() as session:
            agent = await session.get(Agent, agent_id)
            if agent is not None:
                agent.state = "failed"
                await session.commit()

    async def _lift_bundle(
        self,
        agent_id: int,
        title: str,
        session: AsyncSession | None = None,
        include_citations: bool = False,
        answer_override: str | None = None,
    ) -> str:
        session = session or self._session
        submission = (
            await self._stored_submission(agent_id, "submit_findings", session)
            if answer_override is None
            else None
        )
        if answer_override is not None:
            claim = answer_override
        elif submission is not None:
            claim = json.dumps(submission, ensure_ascii=False, indent=2)
        else:
            claim = await self._final_answer(agent_id, session) or ""
        calls = (
            await session.scalars(
                select(Turn.content).where(
                    Turn.agent_id == agent_id, Turn.kind == "tool_call"
                )
            )
        ).all()
        searches = [
            f"  {call.get('arguments', {}).get('adapter', '?')}: "
            f"{call.get('arguments', {}).get('query', '')}"
            if call.get("name") == "search"
            else f"  corpus {call.get('arguments', {}).get('action')}: "
            f"{call.get('arguments', {}).get('query', '')}"
            for call in calls
            if isinstance(call, dict)
            and (
                call.get("name") == "search"
                or (
                    call.get("name") == "corpus"
                    and call.get("arguments", {}).get("action") in ("search", "counts")
                )
            )
        ]
        bundle = (
            f"=== {title} ===\n"
            f"{claim}\n\n"
            f"=== ЧТО ОН ИСКАЛ ({len(searches)} запросов) ===\n"
            + "\n".join(searches)
        )
        if include_citations:
            documents = (
                await session.scalars(
                    select(Document)
                    .where(Document.fetched_by_agent_id == agent_id)
                    .order_by(Document.fetched_at.desc())
                    .limit(60)
                )
            ).all()
            citation_rows = (
                await session.execute(
                    select(Entry.id, Document.url, Citation.quote)
                    .join(Citation, Citation.entry_id == Entry.id)
                    .join(Document, Document.id == Citation.document_id)
                    .where(Entry.researched_by_agent_id == agent_id)
                    .order_by(Citation.id)
                    .limit(60)
                )
            ).all()
            pairs = [(url, quote) for _entry_id, url, quote in citation_rows]
            if submission is not None:
                for item in [
                    *submission.get("patterns", []), *submission.get("citations", [])
                ]:
                    if item.get("url") and item.get("quote"):
                        pairs.append((item["url"], item["quote"]))
            pairs = list(dict.fromkeys(pairs))
            cited_sources = [f"  {url}\n    {quote}" for url, quote in pairs]
            cited_urls = {url for url, _quote in pairs}
            for document in documents:
                if document.url in cited_urls:
                    continue
                if document.url in claim:
                    quote_pattern = re.compile(r"[«“\"]([^»”\"\n]{8,1000})[»”\"]")
                    cited_sources.extend(
                        f"  {document.url}\n    {match.group(1)}"
                        for match in quote_pattern.finditer(claim)
                        if match.group(1).casefold() in document.content.casefold()
                    )
            bundle += (
                f"\n\n=== ЦИТИРОВАННЫЕ ИСТОЧНИКИ И ЦИТАТЫ ({len(cited_sources)}) ===\n"
                + "\n".join(cited_sources)
            )
        return bundle

    async def _research_change_error(self, target: Agent) -> str | None:
        if target.id in self._inflight or target.state == "running":
            return "agent is still running; collect before starting more work"
        if await self._session.scalar(
            select(Entry.id).where(
                Entry.run_id == self._run.id, Entry.researched_by_agent_id == target.id
            ).limit(1)
        ):
            return "this investigation already has a final entry; use a new field for new research"
        if await self._session.scalar(
            select(Refutation.id).where(
                Refutation.run_id == self._run.id,
                Refutation.researcher_agent_id == target.id,
                Refutation.state == "pending",
            ).limit(1)
        ):
            return "this researcher already has a pending refutation; collect it first"
        return None

    async def _resume_agent(self, arguments: dict, agent_id: int | None) -> Any:
        cost_budget = getattr(self, "_cost_budget", None)
        if cost_budget is not None and cost_budget.reason is not None:
            return {"error": cost_budget.reason}
        target_id = int(arguments.get("agent_id") or 0)
        message = str(arguments.get("message") or "")
        target = await self._session.get(Agent, target_id)
        if target is None or target.run_id != self._run.id:
            return {
                "error": (
                    f"no agent {target_id} in this run; agent ids come from "
                    "dispatch_researchers, refute and collect"
                ),
                **await self._fields_brief(),
            }
        if (
            self._partition_worker
            and target.role in ("researcher", "refuter")
            and target.parent_agent_id != self._owner_agent_id
        ):
            return {"error": "agent belongs to another orchestrator shard"}
        if not message:
            return {"error": "message is required"}
        if target_id in self._inflight:
            return {"error": "agent is still running; collect before starting more work"}
        if target.role in ("researcher", "refuter"):
            review_query = select(Refutation).where(Refutation.run_id == self._run.id)
            review_query = review_query.where(
                Refutation.researcher_agent_id == target_id
                if target.role == "researcher"
                else Refutation.refuter_agent_id == target_id
            ).order_by(Refutation.id.desc()).limit(1)
            reviews = (await self._session.scalars(review_query)).all()
            review = reviews[0] if reviews else None
            if review is not None and review.state in (
                "cancelled", "failed", "interrupted"
            ):
                if not review.collected:
                    return {"error": "collect the incomplete refutation before resuming it"}
                researcher = await self._session.get(Agent, review.researcher_agent_id)
                refuter = await self._session.get(Agent, review.refuter_agent_id)
                if researcher is None or refuter is None:
                    return {"error": "the refutation's agents are missing"}
                if refuter.id in self._inflight or refuter.state == "running":
                    return {"error": "refuter is still running"}
                conflict = await self._research_change_error(researcher)
                if conflict:
                    return {"error": conflict}
                review.state = "pending"
                review.collected = False
                review.outcome = None
                review.completed_at = None
                await self._session.commit()
                task = asyncio.create_task(
                    self._refuter_task(
                        review.id, refuter.id, researcher.id, refuter.brief or "",
                        "", review.rebuttal_required, resume_existing=True,
                        resume_target_id=target_id, resume_message=message,
                    ),
                    name=f"refuter-{refuter.id}",
                )
                self._inflight[refuter.id] = task
                return {
                    "resumed": True,
                    "agent_id": target_id,
                    "refutation_id": review.id,
                    "refuter_agent_id": refuter.id,
                    "attacked_agent_id": researcher.id,
                    "running": len(self._inflight),
                    "note": "the existing exchange will return through collect",
                }
        if target.role == "refuter":
            return {"error": "this refutation is complete; use refute for a new review"}
        if target.role == "researcher":
            conflict = await self._research_change_error(target)
            if conflict:
                return {"error": conflict}
            reviews = (await self._session.scalars(
                select(Refutation).where(
                    Refutation.run_id == self._run.id,
                    Refutation.researcher_agent_id == target_id,
                    Refutation.state == "completed",
                )
            )).all()
            field = await self._session.scalar(
                select(Field).where(
                    Field.run_id == self._run.id,
                    Field.agent_id == target_id,
                )
            )
            if field is None:
                return {"error": "researcher has no dispatched field in this run"}
            for review in reviews:
                review.state = "interrupted"
                review.collected = False
                review.delivery_version += 1
                review.outcome = {
                    **(review.outcome or {}),
                    "kind": "refutation", "refutation_id": review.id,
                    "refuter_agent_id": review.refuter_agent_id,
                    "attacked_agent_id": target_id,
                    "delivery_version": review.delivery_version,
                    "stopped": "interrupted",
                    "incomplete": "research resumed after this review; refute the revised answer",
                }
                self._returns.put_nowait(dict(review.outcome))
            await self._session.commit()
            task = asyncio.create_task(
                self._researcher_task(
                    target_id,
                    field.id,
                    field.focus,
                    target.brief or "",
                    resume_existing=True,
                    resume_message=message,
                ),
                name=f"researcher-{target_id}-resume",
            )
            self._inflight[target_id] = task
            return {
                "resumed": True,
                "agent_id": target_id,
                "running": len(self._inflight),
                "note": "the researcher is running and will return through collect",
            }

        async with self._sessionmaker() as session:
            toolbox = Toolbox(session, self._fetcher, self._cache)
            runtime = AgentRuntime(
                session, self._llm, toolbox, self._max_steps_for(target.role), self._sink
            )
            runtime._cost_budget = getattr(self, "_cost_budget", None)
            result = await runtime.resume(target_id, message, partial_on_failure=True)
            effort = await effort_for(session, [target_id])
            await session.commit()

        return {
            "agent_id": result.agent_id,
            "role": target.role,
            "stopped": result.stopped,
            "effort": effort,
            "answer": result.content,
        }

    async def _review_delivered(
        self, review: Refutation, writer_id: int | None = None
    ) -> bool:
        query = (
            select(Turn.id).join(Agent, Turn.agent_id == Agent.id).where(
                Agent.run_id == self._run.id,
                Agent.role == "orchestrator",
                Turn.kind == "tool_result",
                Turn.content["name"].astext == "collect",
                Turn.content["payload"]["returns"].contains([{
                    "refutation_id": review.id,
                    "delivery_version": review.delivery_version,
                }]),
            ).limit(1)
        )
        if writer_id is not None:
            query = query.where(Turn.agent_id == writer_id)
        return await self._session.scalar(query) is not None

    async def _validate_entry_arguments(
        self, arguments: dict, agent_id: int | None, *, partial: bool = False
    ) -> tuple[dict | None, dict | None]:
        if agent_id is None:
            return None, {"error": "writer role is required"}
        writer = await self._session.get(Agent, agent_id)
        if writer is None or writer.run_id != self._run.id or writer.role != "orchestrator":
            return None, {"error": "only this run's orchestrator may write entries"}
        if self._partition_worker and writer.id != self._owner_agent_id:
            return None, {"error": "only this shard orchestrator may write entries"}
        required_text = (
            "transition_ru",
            "why_ru",
            "current_state_ru",
            "dynamics_ru",
            "what_would_refute_ru",
            "problem_ru",
            "advantage_ru",
            "case_example_ru",
        )
        missing = [
            key for key in required_text if not partial or key in arguments
            if not isinstance(arguments.get(key), str) or not arguments[key].strip()
        ]
        if missing:
            return None, {
                "error": (
                    "required prose is missing: every one of these is a Russian paragraph, "
                    "for noise and insufficient entries too. Where the evidence says "
                    "nothing, write 'не установлено' and why."
                ),
                "fields": missing,
            }
        try:
            corpus_query = (
                validate_corpus_query(arguments.get("corpus_query"))
                if not partial or "corpus_query" in arguments else None
            )
        except ValueError as exc:
            return None, {"error": str(exc)}
        state = str(arguments.get("state", "noise" if partial else "") or "")
        if state not in ("banked", "parked", "noise", "insufficient"):
            return None, {
                "error": (
                    f"state {state!r} is not one of banked, parked, noise, insufficient. "
                    "banked and parked need a completed, collected refutation; noise "
                    "and insufficient do not."
                ),
                "state": state,
            }
        dimensions = {}
        for key in ("substance", "momentum", "faintness"):
            if partial and key not in arguments:
                continue
            value = arguments.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return None, {"error": f"{key} must be a finite number from 0 to 1"}
            try:
                number = float(value)
            except (OverflowError, TypeError, ValueError):
                return None, {"error": f"{key} must be a finite number from 0 to 1"}
            if not math.isfinite(number) or not 0 <= number <= 1:
                return None, {"error": f"{key} must be a finite number from 0 to 1"}
            dimensions[key] = number
        patterns = arguments.get("patterns") or []
        if not isinstance(patterns, list):
            return None, {"error": "patterns must be an array"}
        for index, pattern in enumerate(patterns):
            if not isinstance(pattern, dict):
                return None, {"error": f"patterns[{index}] must be an object"}
            strength = pattern.get("strength")
            if isinstance(strength, bool) or not isinstance(strength, (int, float)):
                return None, {
                    "error": f"patterns[{index}].strength must be a finite number from 0 to 1"
                }
            try:
                strength = float(strength)
            except (OverflowError, TypeError, ValueError):
                return None, {
                    "error": f"patterns[{index}].strength must be a finite number from 0 to 1"
                }
            if not math.isfinite(strength) or not 0 <= strength <= 1:
                return None, {
                    "error": f"patterns[{index}].strength must be a finite number from 0 to 1"
                }
        if "citations" not in arguments and not partial:
            return None, {
                "error": (
                    "omitting citations is forbidden: every write_entry call must explicitly "
                    "include the citations array. Resend with the full URLs and verbatim quotes "
                    "from collect, plus summary_ru for non-Russian sources. Citations are not "
                    "copied automatically from research or refutation. Use citations: [] only "
                    "when there genuinely are no citations."
                )
            }
        citations = arguments.get("citations", [])
        if not isinstance(citations, list):
            return None, {"error": "citations must be an array; use [] when there are none"}
        for index, citation in enumerate(citations):
            if not isinstance(citation, dict):
                return None, {"error": f"citations[{index}] must be an object with url and quote"}
            for key in ("url", "quote"):
                value = citation.get(key)
                if not isinstance(value, str) or not value.strip():
                    return None, {
                        "error": (
                            f"citations[{index}].{key} must be a non-empty string; "
                            "copy the full URL and verbatim quote from collected evidence"
                        )
                    }

        def as_id(value: Any) -> Any:
            if isinstance(value, str):
                text = value.strip()
                if text.casefold() in ("", "null", "none"):
                    return None
                if text.isascii() and text.isdigit():
                    return int(text)
            return value

        field_id = as_id(arguments.get("field_id"))
        researcher_id = as_id(arguments.get("researched_by_agent_id"))
        technology_id = as_id(arguments.get("technology_id"))
        if type(technology_id) is int and technology_id == 0:
            technology_id = None
        for key, value in (
            ("field_id", field_id),
            ("researched_by_agent_id", researcher_id),
            ("technology_id", technology_id),
        ):
            if value is not None and (type(value) is not int or value <= 0):
                return None, {
                    "error": (
                        f"{key} must be a positive integer id from this run, got "
                        f"{value!r}. Omit it rather than guess: field_id comes from "
                        "read_fields, and the researcher is taken from the field."
                    ),
                }
        return {
            "writer": writer,
            "corpus_query": corpus_query,
            "state": state,
            **dimensions,
            "field_id": field_id,
            "researcher_id": researcher_id,
            "technology_id": technology_id,
        }, None

    async def _fields_brief(self) -> dict:
        try:
            rows = (
                await self._session.execute(
                    select(Field.id, Field.state, Field.agent_id)
                    .where(Field.run_id == self._run.id)
                    .order_by(Field.id)
                )
            ).all()
        except Exception:
            return {}
        return {
            "for_reference_fields_in_this_run": {
                "total": len(rows),
                "pending_ids": [row.id for row in rows if row.agent_id is None][:30],
                "researched": [
                    f"field {row.id}: researcher {row.agent_id} ({row.state})"
                    for row in rows
                    if row.agent_id is not None
                ][:30],
            }
        }

    async def _validate_entry_field(
        self, validated: dict
    ) -> tuple[dict | None, dict | None]:
        writer = validated["writer"]
        field_id = validated["field_id"]
        researcher_id = validated["researcher_id"]
        technology_id = validated["technology_id"]
        field = await self._session.get(Field, field_id) if field_id else None
        if field_id is not None and (field is None or field.run_id != self._run.id):
            return None, {
                "error": (
                    f"field_id {field_id} is not a field in this run. Fields come from "
                    "split_direction and are listed by read_fields; use an id from "
                    "there rather than guessing."
                ),
                **await self._fields_brief(),
            }
        if (
            field is not None
            and self._partition_worker
            and (
                field.orchestrator_agent_id != writer.id
                or field.id not in self._selected_fields
            )
        ):
            return None, {"error": "field belongs to another orchestrator shard"}
        if field is None and researcher_id is not None:
            matches = (
                await self._session.scalars(
                    select(Field).where(
                        Field.agent_id == researcher_id, Field.run_id == self._run.id
                    )
                )
            ).all()
            if len(matches) != 1:
                return None, {
                    "error": (
                        f"researcher {researcher_id} matches {len(matches)} fields in this "
                        "run, so the field cannot be inferred; pass field_id as well"
                    ),
                    "matching_field_ids": [match.id for match in matches],
                    **await self._fields_brief(),
                }
            field = matches[0]
        if field is None or field.agent_id is None:
            where = (
                f"field {field.id} has no researcher yet"
                if field is not None
                else "no field_id was given"
            )
            return None, {
                "error": (
                    f"{where}: an entry is written up from a researcher's return, so "
                    "dispatch_researchers on the field, collect its return, then write. "
                    "If research cannot run at all (the user asked for no agents, or "
                    "every source is down), write_entry with force=true and a "
                    "force_reason: it is stored and shown as unverified."
                ),
                **await self._fields_brief(),
            }
        if self._partition_worker and field.orchestrator_agent_id != writer.id:
            return None, {"error": "field belongs to another orchestrator shard"}
        if researcher_id is not None and researcher_id != field.agent_id:
            return None, {
                "error": (
                    f"field {field.id} was researched by agent {field.agent_id}, not "
                    f"{researcher_id}; omit researched_by_agent_id and it is taken "
                    "from the field"
                ),
            }
        researcher_id = field.agent_id
        researcher = await self._session.get(Agent, researcher_id)
        existing_entry_id = await self._session.scalar(
            select(Entry.id).where(
                Entry.run_id == self._run.id,
                Entry.field_id == field.id,
            ).limit(1)
        )
        if existing_entry_id is not None:
            return None, {
                "error": (
                    f"field {field.id} already has entry {existing_entry_id}; one entry "
                    "per field, and the existing one stands. A different claim needs "
                    "its own field."
                ),
                "existing_entry_id": existing_entry_id,
            }
        if (
            researcher is None
            or researcher.run_id != self._run.id
            or researcher.role != "researcher"
        ):
            return None, {
                "error": (
                    f"field {field.id} names agent {researcher_id}, which is not a "
                    "researcher of this run; this is a harness inconsistency, not your "
                    "input. Leave a harness_note and move on to another field."
                ),
            }
        if self._partition_worker and researcher.parent_agent_id != self._owner_agent_id:
            return None, {"error": "researcher belongs to another orchestrator shard"}
        if researcher_id in self._inflight or researcher.state == "running":
            return None, {
                "error": (
                    f"researcher {researcher_id} is still running; call collect and "
                    "write once its return has arrived"
                ),
            }
        if technology_id is not None and field.technology_id != technology_id:
            if not getattr(self._run, "seeded", False):
                technology = await self._session.get(Technology, field.technology_id)
                canonical_name = (
                    getattr(technology, "canonical_name_ru", None)
                    or getattr(technology, "canonical_name_en", None)
                    if technology is not None
                    else "unknown"
                )
                return None, {
                    "error": (
                        f"technology_id conflicts with the field's technology "
                        f"(technology_id={field.technology_id}, "
                        f"canonical_name={canonical_name!r}); "
                        "omit technology_id or pass null/0 to inherit it"
                    )
                }
        technology_id = field.technology_id
        if technology_id is None or await self._session.get(Technology, technology_id) is None:
            return None, {
                "error": (
                    f"field {field.id} points at technology {technology_id!r}, which does "
                    "not exist; this is a harness inconsistency, not your input. Leave a "
                    "harness_note and move on."
                ),
            }
        return {
            "field": field,
            "researcher": researcher,
            "researcher_id": researcher_id,
            "technology_id": technology_id,
        }, None

    async def _ask_assistant(self, arguments: dict, agent_id: int | None) -> Any:
        question = str(arguments.get("question") or "").strip()
        if not question:
            return {"error": "question is required"}
        cost_budget = getattr(self, "_cost_budget", None)
        if cost_budget is not None and cost_budget.reason is not None:
            return {"error": cost_budget.reason}
        settings = get_settings()
        assistant = await self._runtime.create(
            run_id=self._run.id,
            role="assistant",
            system=load_assistant_lookup_prompt(),
            brief=question,
            parent_agent_id=agent_id,
        )
        toolbox = Toolbox(self._session, self._fetcher, self._cache)
        toolbox._technology_opener = lambda args, _agent: {
            "error": "technology lookup is unavailable for this question-only assistant"
        }
        toolbox.configure_assistant_lookup(
            assistant.id, settings.assistant_lookup_web_calls
        )
        runtime = AgentRuntime(
            self._session,
            self._llm,
            toolbox,
            settings.assistant_lookup_max_steps,
            self._sink,
        )
        runtime._cost_budget = getattr(self, "_cost_budget", None)
        result = await runtime.run(
            assistant, question, partial_on_failure=True
        )
        answer = (result.content or "").strip()[:max(0, settings.assistant_lookup_answer_chars)]
        turns = (
            await self._session.scalars(
                select(Turn).where(
                    Turn.agent_id == assistant.id, Turn.kind == "tool_result"
                ).order_by(Turn.seq)
            )
        ).all()
        turn_contents = [
            turn.content for turn in turns if isinstance(turn.content, dict)
        ]
        rows = await self._session.execute(
            select(Document.url).where(Document.fetched_by_agent_id == assistant.id)
        )
        urls = list(rows.scalars().all())
        for content in turn_contents:
            payload = content.get("payload")
            if isinstance(payload, dict):
                records = payload.get("records")
                if isinstance(records, list):
                    urls.extend(
                        record["url"]
                        for record in records
                        if isinstance(record, dict)
                        and isinstance(record.get("url"), str)
                    )
                if isinstance(payload.get("url"), str):
                    urls.append(payload["url"])
        return {
            "answer": answer,
            "urls": list(dict.fromkeys(urls))[:20],
            "stopped": result.stopped,
        }

    async def _edit_entry(self, arguments: dict, agent_id: int | None) -> Any:
        changes = arguments.get("changes")
        reason = arguments.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            return {"error": (
                f"reason must explain the correction in Russian or English, got {reason!r}; "
                "resend with the evidence or mistake that prompted this edit."
            )}
        if not isinstance(changes, dict) or not changes:
            return {"error": (
                f"changes must be a non-empty object, got {changes!r}; "
                "supply the fields to correct and their replacement values."
            )}
        unknown = sorted(set(changes) - set(EDIT_ENTRY_FIELDS))
        if unknown:
            return {"error": (
                f"unsupported changes {unknown}; editable fields are {list(EDIT_ENTRY_FIELDS)}. "
                "Remove unsupported keys and resend."
            )}
        validated, error = await self._validate_entry_arguments(changes, agent_id, partial=True)
        if error is not None:
            return {**error, "error": (
                f"{error['error']}. Received changes: {changes!r}. "
                "Correct these values and retry edit_entry."
            )}
        assert validated is not None
        for key in ("name_ru", "name_en"):
            if key in changes and (
                not isinstance(changes[key], str)
                or (key == "name_ru" and not changes[key].strip())
            ):
                return {"error": (
                    f"{key} must be a non-empty name, got {changes[key]!r}; "
                    "supply the corrected technology name."
                )}
        entry_id = arguments.get("entry_id")
        entry = None
        if type(entry_id) is int and entry_id > 0:
            entry = await self._session.scalar(
                select(Entry).where(
                    Entry.id == entry_id, Entry.run_id == self._run.id
                ).with_for_update().execution_options(populate_existing=True)
            )
        if entry is None:
            rows = (await self._session.execute(
                select(Entry.id, Entry.name_ru).where(Entry.run_id == self._run.id)
                .order_by(Entry.id).limit(30)
            )).all()
            return {
                "error": (
                    f"entry_id {entry_id!r} is not an entry of run {self._run.id}; "
                    "use an id from entries_in_this_run and retry."
                ),
                "entries_in_this_run": [
                    {"entry_id": row.id, "name_ru": row.name_ru} for row in rows
                ],
            }
        if self._partition_worker or getattr(self, "_child_shard", False):
            field = await self._session.get(Field, entry.field_id) if entry.field_id else None
            if (
                field is None or field.run_id != self._run.id
                or field.orchestrator_agent_id != agent_id
                or (
                    self._partition_worker and field.id not in self._selected_fields
                )
            ):
                return {"error": (
                    f"entry {entry.id} has field {entry.field_id}, outside shard "
                    f"orchestrator {agent_id}; edit only entries of your own read_fields."
                )}
        state = changes.get("state", entry.state)
        if state in ("banked", "parked"):
            reviews = (await self._session.scalars(
                select(Refutation).where(
                    Refutation.run_id == self._run.id,
                    Refutation.researcher_agent_id == entry.researched_by_agent_id,
                ).order_by(Refutation.id).execution_options(populate_existing=True)
            )).all() if entry.researched_by_agent_id is not None else []
            if (
                not reviews or reviews[-1].state != "completed"
                or any(review.state == "pending" for review in reviews)
                or not all([
                    await self._review_delivered(review, agent_id) for review in reviews
                ])
            ):
                return {"error": (
                    f"entry {entry.id}, researcher {entry.researched_by_agent_id}, state "
                    f"{state!r} needs a completed, collected refutation; reviews are "
                    f"{[(r.id, r.state) for r in reviews]}. Refute and collect first, "
                    "or change state to noise or insufficient."
                )}
        replacements = dict(changes)
        if "name_ru" in replacements:
            replacements["name_ru"] = replacements["name_ru"][:2000]
        if "name_en" in replacements:
            replacements["name_en"] = replacements["name_en"] or None
        for key in ("corpus_query", "substance", "momentum", "faintness"):
            if key in changes:
                replacements[key] = validated[key]
        pairs = []
        patterns = []
        old = {key: getattr(entry, key) for key in changes if key not in ("citations", "patterns")}
        if "citations" in changes or "patterns" in changes:
            pairs = (await self._session.execute(
                select(Citation, Document).join(Document, Document.id == Citation.document_id)
                .where(Citation.entry_id == entry.id).order_by(Citation.id)
            )).all()
            patterns = (await self._session.scalars(
                select(EntryPattern).where(EntryPattern.entry_id == entry.id)
                .order_by(EntryPattern.id)
            )).all()
        old_citations = [
            {"url": doc.url, "quote": citation.quote, "summary_ru": citation.summary_ru}
            for citation, doc in pairs
        ]
        quote_by_id = {citation.id: citation.quote for citation, _ in pairs}
        old_patterns = [
            {"kind": p.kind, "pattern": p.pattern, "strength": p.strength,
             "quote": quote_by_id.get(p.citation_id, "")}
            for p in patterns
        ]
        if "citations" in changes:
            old["citations"] = old_citations
            replacements["citations"] = [
                {"url": c["url"], "quote": c["quote"], "summary_ru": c.get("summary_ru")}
                for c in changes["citations"]
            ]
        if "patterns" in changes:
            old["patterns"] = old_patterns
            replacements["patterns"] = [
                {"kind": str(p.get("kind") or "").strip(),
                 "pattern": str(p.get("pattern") or "").strip()[:64],
                 "strength": float(p["strength"]), "quote": str(p.get("quote") or "")}
                for p in changes["patterns"] or []
                if str(p.get("kind") or "").strip() in ("faintness", "substance", "delivery")
                and str(p.get("pattern") or "").strip()
            ]
        delta = {
            key: {"old": old[key], "new": value}
            for key, value in replacements.items() if old[key] != value
        }
        if not delta:
            return {"error": (
                f"entry {entry.id} already has these values: {replacements!r}; "
                "no edit was saved. Supply a value that differs from the stored entry."
            )}
        metrics = {key: replacements.get(key, getattr(entry, key))
                   for key in ("substance", "momentum", "faintness")}
        if any(key in delta for key in metrics) and any(v is None for v in metrics.values()):
            return {"error": (
                f"entry {entry.id} has incomplete metrics {metrics}; "
                "supply substance, momentum and faintness together to recompute scores."
            )}
        citation_documents = []
        unresolved = []
        if "citations" in delta:
            citation_documents, unresolved, error = await self._resolve_entry_citations(
                replacements["citations"], agent_id
            )
            if error is not None:
                return error
        for key in delta:
            if key not in ("citations", "patterns"):
                setattr(entry, key, replacements[key])
        if any(key in delta for key in metrics):
            settings = get_settings()
            entry.score = compute_entry_score(
                metrics["substance"], metrics["momentum"],
                settings.entry_score_substance_weight, settings.entry_score_momentum_weight,
            )
            entry.weak_score = compute_weak_score(
                metrics["substance"], metrics["momentum"], metrics["faintness"],
                settings.signal_faintness_gate_centre, settings.signal_faintness_gate_width,
            )
            entry.signal_class = classify_signal(
                metrics["substance"], metrics["faintness"], settings.signal_noise_below,
                settings.signal_faintness_gate_centre, settings.signal_strong_substance_at_least,
            )
        if "citations" in delta or "patterns" in delta:
            for pattern in patterns:
                await self._session.delete(pattern)
            await self._session.flush()
            quoted = {citation.quote: citation.id for citation, _ in pairs}
            if "citations" in delta:
                for citation, _ in pairs:
                    await self._session.delete(citation)
                await self._session.flush()
                quoted = {}
                for item, document in citation_documents:
                    citation = Citation(
                        entry_id=entry.id, document_id=document.id,
                        quote=item["quote"], summary_ru=item.get("summary_ru"),
                    )
                    self._session.add(citation)
                    await self._session.flush()
                    quoted[citation.quote] = citation.id
            for pattern in replacements.get("patterns", old_patterns):
                self._session.add(EntryPattern(
                    entry_id=entry.id, kind=pattern["kind"], pattern=pattern["pattern"],
                    strength=pattern["strength"],
                    citation_id=_match_quote(quoted, pattern["quote"] or ""),
                ))
        if "citations" in delta:
            delta["citations"] = {
                side: [{"url": c["url"], "quote": c["quote"]} for c in values]
                for side, values in delta["citations"].items()
            }
        edit = EntryEdit(
            entry_id=entry.id, run_id=self._run.id, agent_id=agent_id,
            reason=reason.strip(), changes=delta,
        )
        self._session.add(edit)
        await self._session.flush()
        response = {
            "entry_id": entry.id, "changed": list(delta), "score": entry.score,
            "weak_score": entry.weak_score, "signal_class": entry.signal_class,
            "edit_id": edit.id,
            "note": (
                f"Edited; citations stored unverified because retrieval failed: {unresolved}"
                if unresolved
                else "Edited and logged; replacement quotes are checked after the run."
            ),
        }
        await self._session.commit()
        return response

    async def _write_entry(self, arguments: dict, agent_id: int | None) -> Any:
        validated, error = await self._validate_entry_arguments(arguments, agent_id)
        if error is not None:
            return error
        assert validated is not None
        if arguments.get("force") is True or str(arguments.get("force")).lower() == "true":
            return await self._write_forced_entry(arguments, validated, agent_id)
        validated_field, error = await self._validate_entry_field(validated)
        if error is not None:
            return error
        assert validated_field is not None
        field = validated_field["field"]
        field_id = field.id
        researcher_id = validated_field["researcher_id"]
        technology_id = validated_field["technology_id"]
        state = validated["state"]
        settings = get_settings()
        score = compute_entry_score(
            validated["substance"],
            validated["momentum"],
            settings.entry_score_substance_weight,
            settings.entry_score_momentum_weight,
        )
        weak_score = compute_weak_score(
            validated["substance"],
            validated["momentum"],
            validated["faintness"],
            settings.signal_faintness_gate_centre,
            settings.signal_faintness_gate_width,
        )
        signal_class = classify_signal(
            validated["substance"],
            validated["faintness"],
            settings.signal_noise_below,
            settings.signal_faintness_gate_centre,
            settings.signal_strong_substance_at_least,
        )

        siblings = (
            await self._session.execute(
                select(
                    Entry.id, Entry.name_ru, Entry.transition_ru, Entry.score, Entry.weak_score
                )
                .where(
                    Entry.run_id == self._run.id,
                    Entry.technology_id == technology_id,
                )
                .order_by(*entry_ordering(Entry.weak_score, Entry.score, Entry.id))
                .limit(5)
            )
        ).all()

        reviews = (
            await self._session.scalars(
                select(Refutation).where(
                    Refutation.run_id == self._run.id,
                    Refutation.researcher_agent_id == researcher_id,
                ).order_by(Refutation.id).execution_options(populate_existing=True)
            )
        ).all()
        pending = [r.id for r in reviews if r.state == "pending"]
        if pending:
            return {"error": (
                        "a refutation of this research is still running; call collect "
                        "and write once it has arrived"
                    ),
                    "refutation_ids": pending}
        undelivered = [r.id for r in reviews if not await self._review_delivered(r, agent_id)]
        if undelivered:
            return {"error": (
                        "these refutations have finished but you have not read them: "
                        "call collect first, so the entry is written knowing the attack"
                    ),
                    "refutation_ids": undelivered}
        latest_review = reviews[-1] if reviews else None
        completed_review = (
            latest_review if latest_review and latest_review.state == "completed" else None
        )
        incomplete_reason = arguments.get("review_incomplete_reason")
        if completed_review is None:
            if state not in ("noise", "insufficient"):
                return {"error": (
                    f"state {state} needs a completed, collected refutation and this "
                    "research has none: refute it and collect, or write it as noise or "
                    "insufficient with review_incomplete_reason"
                )}
            if not isinstance(incomplete_reason, str) or not incomplete_reason.strip():
                return {"error": (
                    "review_incomplete_reason is required when no completed refutation "
                    "exists: say in Russian which claims were not independently tested "
                    "and why"
                )}
        effort = await effort_for(
            self._session, [researcher_id] + [r.refuter_agent_id for r in reviews]
        )
        citation_documents, unresolved, citation_error = await self._resolve_entry_citations(
            arguments["citations"], agent_id
        )
        if citation_error is not None:
            return citation_error
        why_ru = arguments["why_ru"]
        if completed_review is None:
            why_ru += "\n\nПроверка не завершена: " + incomplete_reason.strip()
        name_error = _russian_name_error(arguments.get("name_ru"))
        if name_error is not None:
            return name_error
        entry_arguments = dict(arguments)
        entry_arguments["corpus_query"] = validated["corpus_query"]
        entry_arguments["name_ru"] = arguments["name_ru"].strip()
        entry_arguments["name_en"] = arguments.get("name_en") or getattr(field, "focus", None)
        (
            entry,
            quoted,
            patterns_without_matching_citation_quote,
            dropped_pattern_kinds,
        ) = await self._persist_entry(
            entry_arguments,
            agent_id,
            field_id,
            researcher_id,
            technology_id,
            state,
            score,
            weak_score,
            signal_class,
            why_ru,
            latest_review,
            completed_review,
            effort,
            citation_documents,
        )
        return self._entry_response(
            entry,
            technology_id,
            field_id,
            researcher_id,
            quoted,
            patterns_without_matching_citation_quote,
            dropped_pattern_kinds,
            unresolved,
            siblings,
        )

    async def _fetch_citation_page(
        self, url: str, writer_id: int | None
    ) -> Document:
        if writer_id is None:
            raise RuntimeError("citation writer is unavailable")
        toolbox = Toolbox(self._session, self._fetcher, self._cache)
        try:
            result = await toolbox._fetch(
                {"url": url}, writer_id, via="citation_autofetch"
            )
            if result.get("error") or result.get("stored") is False:
                raise RuntimeError(str(result.get("error") or "page fetch failed"))
            document = await newest_fetchable_document(self._session, url)
            if document is None:
                raise RuntimeError("fetched page was not stored as a retrievable document")
        except Exception as exc:
            await record_source_event(
                adapter="web_call",
                query="fetch",
                via="citation_autofetch",
                ok=False,
                error=f"{type(exc).__name__}: {exc}"[:300],
                agent_id=writer_id,
            )
            raise
        await record_source_event(
            adapter="web_call",
            query="fetch",
            via="citation_autofetch",
            ok=True,
            agent_id=writer_id,
        )
        return document

    async def _store_citation_failure(
        self, url: str, reason: str, writer_id: int | None
    ) -> Document:
        import httpx
        from sqlalchemy.dialects.postgresql import insert

        from wsignal.parsing.tiering import host_tier

        host = httpx.URL(url).host or "unknown"
        content = f"Citation page retrieval failed: {reason}"
        statement = (
            insert(Document.__table__)
            .values(
                url=url,
                title=host,
                content=content,
                content_sha=content_sha(content),
                retrieved=False,
                source_name=host,
                source_type="fetch_failure",
                source_lang="und",
                source_tier=host_tier(host),
                date_source=None,
                fetched_by_agent_id=writer_id,
            )
            .on_conflict_do_nothing(index_elements=["url", "content_sha"])
        )
        await self._session.execute(statement)
        document = await self._session.scalar(
            select(Document).where(
                Document.url == url,
                Document.content_sha == content_sha(content),
            ).limit(1)
        )
        if document is None:
            document = Document(
                url=url,
                title=host,
                content=content,
                content_sha=content_sha(content),
                retrieved=False,
                source_name=host,
                source_type="fetch_failure",
                source_lang="und",
                source_tier=host_tier(host),
                date_source=None,
                fetched_by_agent_id=writer_id,
            )
            self._session.add(document)
            await self._session.flush()
        return document

    async def _resolve_entry_citations(
        self, citations: list, writer_id: int | None = None
    ) -> tuple[list[tuple[dict, Document]], list[str], dict | None]:
        citation_documents: list[tuple[dict, Document]] = []
        unresolved: list[str] = []
        missing_summaries: list[str] = []
        for item in citations:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "")
            document = await newest_fetchable_document(self._session, url)
            fetch_failure = None
            if document is None:
                try:
                    document = await self._fetch_citation_page(url, writer_id)
                except Exception as exc:
                    fetch_failure = f"{type(exc).__name__}: {exc}"[:500]
                    document = await self._store_citation_failure(
                        url, fetch_failure, writer_id
                    )
                if fetch_failure is not None:
                    unresolved.append(f"{url} ({fetch_failure})")
            citation_documents.append((item, document))
            summary = item.get("summary_ru")
            if (
                fetch_failure is None
                and document.source_lang != "ru"
                and (not isinstance(summary, str) or not summary.strip())
            ):
                missing_summaries.append(url)
        if missing_summaries:
            return citation_documents, unresolved, {
                "error": (
                    "foreign-language citations require summary_ru; resend with "
                    "summary_ru for these URLs: " + ", ".join(missing_summaries)
                ),
                "urls": missing_summaries,
            }
        return citation_documents, unresolved, None

    async def _write_forced_entry(
        self, arguments: dict, validated: dict, agent_id: int | None
    ) -> Any:
        reason = arguments.get("force_reason")
        if not isinstance(reason, str) or not reason.strip():
            return {
                "error": (
                    "force=true needs force_reason: in Russian, why this entry is written "
                    "without research. It is shown on the entry."
                )
            }
        field = None
        field_id = validated["field_id"]
        if field_id is not None:
            field = await self._session.get(Field, field_id)
            if field is None or field.run_id != self._run.id:
                return {
                    "error": (
                        f"field_id {field_id} is not a field in this run; a forced entry "
                        "may name a field from read_fields or pass null"
                    ),
                    **await self._fields_brief(),
                }
            existing_entry_id = await self._session.scalar(
                select(Entry.id).where(
                    Entry.run_id == self._run.id, Entry.field_id == field.id
                ).limit(1)
            )
            if existing_entry_id is not None:
                return {
                    "error": (
                        f"field {field.id} already has entry {existing_entry_id}; one "
                        "entry per field. Pass field_id null for a forced entry."
                    ),
                    "existing_entry_id": existing_entry_id,
                }
        technology_id = validated["technology_id"] or (
            field.technology_id if field is not None else None
        )
        if technology_id is None or await self._session.get(Technology, technology_id) is None:
            return {
                "error": (
                    "a forced entry still needs a technology: pass technology_id from "
                    "known_technologies, or open_technology first"
                ),
            }
        settings = get_settings()
        score = compute_entry_score(
            validated["substance"],
            validated["momentum"],
            settings.entry_score_substance_weight,
            settings.entry_score_momentum_weight,
        )
        weak_score = compute_weak_score(
            validated["substance"],
            validated["momentum"],
            validated["faintness"],
            settings.signal_faintness_gate_centre,
            settings.signal_faintness_gate_width,
        )
        signal_class = classify_signal(
            validated["substance"],
            validated["faintness"],
            settings.signal_noise_below,
            settings.signal_faintness_gate_centre,
            settings.signal_strong_substance_at_least,
        )
        citation_documents, unresolved, citation_error = await self._resolve_entry_citations(
            arguments["citations"], agent_id
        )
        if citation_error is not None:
            return citation_error
        name_error = _russian_name_error(arguments.get("name_ru"))
        if name_error is not None:
            return name_error
        entry_arguments = dict(arguments)
        entry_arguments["corpus_query"] = validated["corpus_query"]
        entry_arguments["name_ru"] = arguments["name_ru"].strip()
        entry_arguments["name_en"] = arguments.get("name_en") or getattr(field, "focus", None)
        entry, quoted, unmatched, dropped = await self._persist_entry(
            entry_arguments,
            agent_id,
            field.id if field is not None else None,
            None,
            technology_id,
            validated["state"],
            score,
            weak_score,
            signal_class,
            arguments["why_ru"],
            None,
            None,
            {"searches_run": 0, "sources_checked": 0},
            citation_documents,
            forced_reason=reason.strip(),
        )
        response = self._entry_response(
            entry,
            technology_id,
            field.id if field is not None else None,
            None,
            quoted,
            unmatched,
            dropped,
            unresolved,
            [],
        )
        response["forced"] = True
        response["note"] = (
            "written as a forced entry: ranked like any other, marked unverified in "
            "the report, and no research was spent"
        )
        return response

    async def _persist_entry(
        self,
        arguments: dict,
        agent_id: int,
        field_id: int | None,
        researcher_id: int | None,
        technology_id: int,
        state: str,
        score: float,
        weak_score: float,
        signal_class: str,
        why_ru: str,
        latest_review: Refutation | None,
        completed_review: Refutation | None,
        effort: dict,
        citation_documents: list[tuple[dict, Document]],
        forced_reason: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, int], list[dict], list[str]]:
        entry = Entry(
            run_id=self._run.id,
            field_id=field_id,
            researched_by_agent_id=researcher_id,
            written_by_agent_id=agent_id,
            technology_id=technology_id,
            name_ru=str(arguments.get("name_ru") or "")[:2000],
            name_en=(arguments.get("name_en") or None),
            corpus_query=arguments.get("corpus_query"),
            forced_reason=forced_reason,
            transition_ru=str(arguments.get("transition_ru") or ""),
            state=state,
            score=score,
            weak_score=weak_score,
            signal_class=signal_class,
            substance=float(arguments["substance"]),
            momentum=float(arguments["momentum"]),
            faintness=float(arguments["faintness"]),
            why_ru=why_ru,
            current_state_ru=arguments.get("current_state_ru"),
            dynamics_ru=arguments.get("dynamics_ru"),
            what_would_refute_ru=arguments.get("what_would_refute_ru"),
            problem_ru=arguments.get("problem_ru"),
            advantage_ru=arguments.get("advantage_ru"),
            case_example_ru=arguments.get("case_example_ru"),
            refuter_agent_id=(latest_review.refuter_agent_id if latest_review else None),
            rebutted=bool(
                completed_review and completed_review.rebuttal_required
                and (completed_review.outcome or {}).get("rebuttal")
            ),
            searches_run=effort["searches_run"],
            sources_checked=effort["sources_checked"],
        )
        self._session.add(entry)
        await self._session.flush()
        quoted: dict[str, int] = {}
        for item, document in citation_documents:
            quote = str(item.get("quote") or "")
            summary = item.get("summary_ru")
            citation = Citation(
                entry_id=entry.id,
                document_id=document.id,
                quote=quote,
                summary_ru=summary if isinstance(summary, str) else None,
            )
            self._session.add(citation)
            await self._session.flush()
            quoted[quote] = citation.id
        patterns_without_matching_citation_quote = []
        dropped_pattern_kinds = []
        for item in arguments.get("patterns") or []:
            if not isinstance(item, dict):
                continue
            pattern = str(item.get("pattern") or "").strip()[:64]
            kind = str(item.get("kind") or "").strip()
            if kind not in ("faintness", "substance", "delivery"):
                dropped_pattern_kinds.append(kind)
                continue
            if not pattern:
                continue
            quote = str(item.get("quote") or "")
            citation_id = _match_quote(quoted, quote)
            if quote and citation_id is None:
                patterns_without_matching_citation_quote.append(
                    {"kind": kind, "pattern": pattern, "quote": quote}
                )
            from wsignal.models import EntryPattern

            self._session.add(
                EntryPattern(
                    entry_id=entry.id,
                    kind=kind,
                    pattern=pattern,
                    strength=float(item["strength"]),
                    citation_id=citation_id,
                )
            )
        field = await self._session.get(Field, field_id) if field_id is not None else None
        if field is not None:
            field.state = "done"
        response_fields = {
            "id": entry.id,
            "name_ru": entry.name_ru,
            "score": entry.score,
            "state": entry.state,
            "searches_run": entry.searches_run,
            "sources_checked": entry.sources_checked,
        }
        await self._session.commit()
        return (
            response_fields,
            quoted,
            patterns_without_matching_citation_quote,
            dropped_pattern_kinds,
        )

    def _entry_response(
        self,
        entry: dict[str, Any],
        technology_id: int,
        field_id: int,
        researcher_id: int,
        quoted: dict[str, int],
        patterns_without_matching_citation_quote: list,
        dropped_pattern_kinds: list[str],
        unresolved: list[str],
        siblings: list,
    ) -> dict:
        emit(
            self._sink,
            "entry_written",
            entry_id=entry["id"],
            name=entry["name_ru"],
            score=entry["score"],
            state=entry["state"],
            citations=len(quoted),
            searches=entry["searches_run"],
        )
        return {
            "entry_id": entry["id"],
            "technology_id": technology_id,
            "field_id": field_id,
            "researched_by_agent_id": researcher_id,
            "effort_is_unknown": researcher_id is None,
            "searches_run": entry["searches_run"],
            "sources_checked": entry["sources_checked"],
            "citations_stored": len(quoted),
            "patterns_without_matching_citation_quote": patterns_without_matching_citation_quote,
            "dropped_pattern_kinds": dropped_pattern_kinds,
            "urls_not_in_store": unresolved,
            "same_technology_in_this_run": [
                {
                    "entry_id": sibling_id,
                    "name_ru": name_ru,
                    "transition_ru": transition_ru,
                    "score": score,
                    "weak_score": weak_score,
                }
                for sibling_id, name_ru, transition_ru, score, weak_score in siblings
            ],
            "note": (
                "some citation pages could not be fetched. Their citations were "
                "stored unverified with the retrieval failure; the entry was written."
                if unresolved
                else (
                    "written, and this run already holds the entries listed "
                    "above on the same technology. that is right only if the "
                    "transitions differ. if they do not, this is one signal "
                    "written twice and it will take two of the places in the "
                    "answer: resume the researchers and decide which claim "
                    "survives."
                    if siblings
                    else "written. quotes are checked after the run."
                )
            ),
        }

    async def _read_entries(self, arguments: dict, agent_id: int | None) -> Any:
        query = select(Entry).where(Entry.run_id == self._run.id)
        if (
            self._partition_worker or self._child_shard
        ) and agent_id == self._owner_agent_id:
            query = query.join(Field, Entry.field_id == Field.id).where(
                Field.orchestrator_agent_id == self._owner_agent_id
            )
        entries = (
            await self._session.scalars(
                query.order_by(*entry_ordering(Entry.weak_score, Entry.score, Entry.id))
                .limit(_clamped_limit(arguments, 30))
            )
        ).all()
        return {
            "entries": [
                {
                    "id": e.id,
                    "name_ru": e.name_ru,
                    "transition_ru": e.transition_ru,
                    "state": e.state,
                    "score": e.score,
                    "weak_score": e.weak_score,
                    "signal_class": e.signal_class,
                }
                for e in entries
            ]
        }
