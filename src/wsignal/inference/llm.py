from __future__ import annotations

import asyncio
import copy
import json
import re
import time
from dataclasses import dataclass, field
from typing import TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from wsignal.config import get_settings
from wsignal.retry import RetryExhausted, with_retries
from wsignal.routes import llm_endpoint

T = TypeVar("T", bound=BaseModel)

ROLE_SETTING = {
    "orchestrator": "llm_model_orchestrator",
    "assistant": "llm_model_assistant",
    "researcher": "llm_model_researcher",
    "refuter": "llm_model_refuter",
    "extractor": "llm_model_extractor",
}


@dataclass(slots=True)
class ModelCall:
    role: str
    model: str
    duration_s: float
    prompt_tokens: int
    completion_tokens: int
    attempts: int
    ok: bool
    error: str | None = None
    resolved_model: str | None = None
    provider: str | None = None
    generation_id: str | None = None
    cached_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    cost_usd: float | None = None


@dataclass
class ModelLog:
    calls: list[ModelCall] = field(default_factory=list)

    def add(self, call: ModelCall) -> None:
        self.calls.append(call)

    @property
    def total_calls(self) -> int:
        return len(self.calls)

    @property
    def prompt_tokens(self) -> int:
        return sum(c.prompt_tokens for c in self.calls)

    @property
    def completion_tokens(self) -> int:
        return sum(c.completion_tokens for c in self.calls)


class LlmError(RuntimeError):
    pass


class MalformedReply(LlmError):
    pass


class ProviderUnavailable(LlmError):
    pass


class FirstTokenTimeout(LlmError):
    def __init__(self, provider: str | None, elapsed_s: float, generation_id: str | None) -> None:
        self.provider = provider
        self.elapsed_s = elapsed_s
        self.generation_id = generation_id
        super().__init__(
            f"no first token from {provider or 'an unnamed provider'} within {elapsed_s:.1f}s"
        )


class LlmUnavailable(LlmError):
    def __init__(
        self, role: str, model: str, reason: str, attempts: int, elapsed_s: float, detail: str
    ) -> None:
        self.role = role
        self.model = model
        self.reason = reason
        self.attempts = attempts
        self.elapsed_s = elapsed_s
        self.detail = detail
        super().__init__(
            f"{role} via {model} unavailable after {attempts} attempt(s) "
            f"in {elapsed_s:.1f}s ({reason}): {detail}"
        )


@dataclass(slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass(slots=True)
class Reply:

    message: dict
    content: str | None
    tool_calls: list[ToolCall]
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    cost_usd: float | None = None
    duration_ms: int = 0
    attempts: int = 1
    provider: str | None = None
    generation_id: str | None = None
    resolved_model: str | None = None

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)

    def accounting(self) -> dict:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cached_tokens": self.cached_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "cost_usd": self.cost_usd,
            "duration_ms": self.duration_ms,
            "attempts": self.attempts,
            "provider": self.provider,
            "generation_id": self.generation_id,
        }


def _strip_fence(raw: str) -> str:
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    return text.strip()


@dataclass(slots=True)
class _Failover:
    ignored: list[str] = field(default_factory=list)
    stalls: int = 0


_JOINED_DETAIL_FIELDS = ("text", "summary", "data")


def _merge_reasoning_detail(details: list[dict], piece: dict) -> None:
    index = piece.get("index")
    target = next(
        (item for item in details if index is not None and item.get("index") == index), None
    )
    if target is None:
        details.append(dict(piece))
        return
    for key, value in piece.items():
        if key in _JOINED_DETAIL_FIELDS and isinstance(value, str):
            target[key] = (target.get(key) or "") + value
        elif value is not None:
            target[key] = value


class _StreamedReply:
    def __init__(self) -> None:
        self.payload: dict = {}
        self.content: list[str] = []
        self.reasoning: list[str] = []
        self.reasoning_details: list[dict] = []
        self.tool_calls: dict[int, dict] = {}
        self.finish_reason: str | None = None

    def feed(self, chunk: dict) -> bool:
        for key in ("id", "model", "provider"):
            if chunk.get(key):
                self.payload[key] = chunk[key]
        if chunk.get("usage"):
            self.payload["usage"] = chunk["usage"]
        produced = False
        for choice in chunk.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            if choice.get("finish_reason"):
                self.finish_reason = choice["finish_reason"]
            delta = choice.get("delta") or {}
            if delta.get("content"):
                self.content.append(delta["content"])
                produced = True
            if delta.get("reasoning"):
                self.reasoning.append(delta["reasoning"])
                produced = True
            for piece in delta.get("reasoning_details") or []:
                if isinstance(piece, dict):
                    _merge_reasoning_detail(self.reasoning_details, piece)
                    produced = True
            for position, call in enumerate(delta.get("tool_calls") or []):
                if not isinstance(call, dict):
                    continue
                slot = self.tool_calls.setdefault(
                    call.get("index", position),
                    {"id": None, "type": "function", "function": {"name": "", "arguments": ""}},
                )
                if call.get("id"):
                    slot["id"] = call["id"]
                if call.get("type"):
                    slot["type"] = call["type"]
                function = call.get("function") or {}
                slot["function"]["name"] += function.get("name") or ""
                slot["function"]["arguments"] += function.get("arguments") or ""
                produced = True
        return produced

    def result(self) -> dict:
        message: dict = {"role": "assistant", "content": "".join(self.content)}
        if self.reasoning:
            message["reasoning"] = "".join(self.reasoning)
        if self.reasoning_details:
            message["reasoning_details"] = self.reasoning_details
        if self.tool_calls:
            message["tool_calls"] = [self.tool_calls[index] for index in sorted(self.tool_calls)]
        return {
            **self.payload,
            "choices": [{"message": message, "finish_reason": self.finish_reason}],
        }


def _caches_only_when_asked(model: str) -> bool:
    return model.startswith(("anthropic/", "qwen/"))


def _add_reasoning(body: dict, role: str, settings=None) -> dict:
    settings = settings or get_settings()
    effort = settings.llm_reasoning_effort_by_role.get(role)
    if effort is not None:
        body["reasoning"] = {"effort": effort}
    return body


def request_body(
    model: str,
    messages: list[dict],
    tools: list[dict] | None = None,
    temperature: float = 0.0,
    session_id: str | None = None,
    *,
    role: str,
) -> dict:
    body: dict = {"model": model, "messages": messages, "temperature": temperature}
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    if _caches_only_when_asked(model):
        body["cache_control"] = {"type": "ephemeral"}
    if session_id:
        body["session_id"] = session_id[:256]
    return _add_reasoning(body, role)


class LlmClient:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._settings = get_settings()
        endpoint = llm_endpoint(self._settings)
        self.route = endpoint.route
        self._base_url = endpoint.base_url
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=self._settings.llm_connect_timeout_s,
                read=self._settings.llm_read_timeout_s,
                write=self._settings.llm_write_timeout_s,
                pool=self._settings.llm_connect_timeout_s,
            ),
            headers={
                "Authorization": f"Bearer {endpoint.key}",
                "Content-Type": "application/json",
            },
        )
        self.log = ModelLog()
        self._provider_locks: dict[str, str] = {}
        self._provider_slugs: dict[str, dict[str, set[str]]] = {}
        self._event_sink = None

    def set_event_sink(self, sink) -> None:
        self._event_sink = sink

    def _emit(self, kind: str, **fields) -> None:
        if self._event_sink is not None:
            try:
                self._event_sink({"kind": kind, **fields})
            except Exception:
                pass

    def _pinned_route(self, role: str | None) -> dict | None:
        route = self._settings.llm_provider_by_role.get(role) if role else None
        return copy.deepcopy(route) if route else None

    def _apply_provider_preference(
        self, body: dict, model: str, ignored: list[str] | None = None,
        role: str | None = None,
    ) -> None:
        pinned = self._pinned_route(role)
        if pinned is not None:
            body["provider"] = pinned
            return
        preference: dict = {}
        provider = self._provider_locks.get(model)
        if provider is not None:
            preference = {"order": [provider], "allow_fallbacks": True}
        if ignored:
            preference["ignore"] = list(ignored)
        if preference:
            body["provider"] = preference
        else:
            body.pop("provider", None)

    async def _slugs_for(self, model: str, provider: str) -> list[str]:
        if model not in self._provider_slugs:
            try:
                response = await self._client.get(
                    f"{self._base_url}/models/{model}/endpoints",
                    timeout=self._settings.llm_provider_probe_timeout_s,
                )
                response.raise_for_status()
                table: dict[str, set[str]] = {}
                for endpoint in response.json()["data"]["endpoints"]:
                    name, tag = endpoint.get("provider_name"), endpoint.get("tag")
                    if isinstance(name, str) and isinstance(tag, str) and tag:
                        table.setdefault(name.casefold(), set()).add(tag.split("/", 1)[0])
                self._provider_slugs[model] = table
            except Exception:
                pass
        known = self._provider_slugs.get(model, {}).get(provider.casefold())
        if known:
            return sorted(known)
        return [re.sub(r"[^a-z0-9]+", "-", provider.casefold()).strip("-")]

    async def _stream_completion(
        self, model: str, body: dict, timeout: float | httpx.Timeout
    ) -> tuple[httpx.Response, object]:
        started = time.monotonic()
        limit = self._settings.llm_first_token_timeout_s
        provider: str | None = None
        generation_id: str | None = None
        try:
            async with asyncio.timeout(limit if limit > 0 else None) as first_token:
                async with self._client.stream(
                    "POST",
                    f"{self._base_url}/chat/completions",
                    json={**body, "stream": True},
                    timeout=timeout,
                ) as response:
                    generation_id = response.headers.get("x-generation-id")
                    if response.status_code >= 400 or "text/event-stream" not in (
                        response.headers.get("content-type") or ""
                    ):
                        await response.aread()
                        first_token.reschedule(None)
                        try:
                            return response, response.json()
                        except ValueError:
                            return response, response.text
                    reply = _StreamedReply()
                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                        except ValueError:
                            continue
                        if not isinstance(chunk, dict):
                            continue
                        if isinstance(chunk.get("provider"), str):
                            provider = chunk["provider"]
                        if chunk.get("error"):
                            self._drop_provider_lock(model, "stream error")
                            raise ProviderUnavailable(
                                f"stream error from {provider}: {chunk['error']}"[:300]
                            )
                        if reply.feed(chunk):
                            first_token.reschedule(None)
                    return response, reply.result()
        except TimeoutError as exc:
            if not first_token.expired():
                raise
            raise FirstTokenTimeout(
                provider, time.monotonic() - started, generation_id
            ) from exc

    async def _complete(
        self,
        role: str,
        model: str,
        body: dict,
        timeout: float | httpx.Timeout,
        failover: _Failover,
        session_id: str | None = None,
    ) -> tuple[httpx.Response, object]:
        while True:
            self._apply_provider_preference(body, model, failover.ignored, role)
            try:
                return await self._stream_completion(model, body, timeout)
            except FirstTokenTimeout as stall:
                failover.stalls += 1
                retrying = failover.stalls <= self._settings.llm_first_token_failovers
                await self._record_stall(role, model, stall, failover, retrying, session_id)
                if not retrying:
                    raise

    async def _record_stall(
        self,
        role: str,
        model: str,
        stall: FirstTokenTimeout,
        failover: _Failover,
        retrying: bool,
        session_id: str | None,
    ) -> None:
        pinned = self._pinned_route(role)
        locked = None if pinned is not None else self._provider_locks.get(model)
        provider, source = stall.provider, "stream"
        if provider is None and locked is not None:
            provider, source = locked, "lock"
        if provider is None:
            source = None
        elif pinned is not None:
            source = "pinned"
        else:
            for slug in await self._slugs_for(model, provider):
                if slug not in failover.ignored:
                    failover.ignored.append(slug)
            if locked is not None and locked.casefold() == provider.casefold():
                self._drop_provider_lock(model, str(stall))
        self._emit(
            "first_token_timeout",
            role=role,
            model=model,
            provider=provider,
            provider_source=source,
            elapsed_s=round(stall.elapsed_s, 2),
            stall=failover.stalls,
            retrying=retrying,
            ignored=list(failover.ignored),
            session_id=session_id,
            generation_id=stall.generation_id,
            error=(
                f"no first token from {provider or 'unknown provider'} "
                f"within {stall.elapsed_s:.1f}s"
            ),
        )

    async def probe_providers(self) -> dict[str, str]:
        if not self._settings.llm_provider_lock_enabled:
            return {}
        models = list(dict.fromkeys(self.model_for(role) for role in ROLE_SETTING))

        async def probe(model: str) -> tuple[str, str | None, str | None]:
            try:
                response = await self._client.post(
                    f"{self._base_url}/chat/completions",
                    json={
                        "model": model,
                        "messages": [{"role": "user", "content": "Reply 1."}],
                        "max_tokens": 2,
                    },
                    timeout=self._settings.llm_provider_probe_timeout_s,
                )
                response.raise_for_status()
                payload = response.json()
                provider = payload.get("provider") if isinstance(payload, dict) else None
                if not isinstance(provider, str) or not provider.strip():
                    return model, None, None
                return model, provider.strip(), None
            except Exception as exc:
                return model, None, f"{type(exc).__name__}: {exc}"[:200]

        results = await asyncio.gather(*(probe(model) for model in models))
        chosen: dict[str, str] = {}
        for model, provider, error in results:
            if provider is None:
                self._provider_locks.pop(model, None)
                if error is not None:
                    self._emit("provider_probe_failed", model=model, error=error)
                continue
            self._provider_locks[model] = provider
            chosen[model] = provider
            self._emit("provider_locked", model=model, provider=provider)
        return chosen

    def _drop_provider_lock(self, model: str, reason: str) -> None:
        provider = self._provider_locks.pop(model, None)
        if provider is not None:
            self._emit(
                "provider_unlocked", model=model, provider=provider, reason=reason[:200]
            )

    @staticmethod
    def _provider_could_not_serve(payload: object) -> bool:
        text = json.dumps(payload, ensure_ascii=False, default=str).lower()
        return bool(
            re.search(
                r"provider.{0,80}(?:unavailable|could not serve|unable to serve|failed to serve)"
                r"|(?:unavailable|could not serve|unable to serve|failed to serve).{0,80}provider",
                text,
            )
        )

    def _check_provider_failure(self, model: str, status: int, payload: object) -> bool:
        unavailable = self._provider_could_not_serve(payload)
        if status >= 500 or unavailable:
            self._drop_provider_lock(model, f"HTTP {status}: provider-side failure")
        return unavailable

    def model_for(self, role: str) -> str:
        setting = ROLE_SETTING.get(role, "llm_model_orchestrator")
        return getattr(self._settings, setting)

    @property
    def models_used(self) -> dict[str, str]:
        return {role: self.model_for(role) for role in ROLE_SETTING}

    async def structured(
        self,
        role: str,
        system: str,
        user: str,
        schema: type[T],
        temperature: float = 0.0,
        max_attempts: int = 2,
    ) -> T:
        model = self.model_for(role)
        shape = json.dumps(schema.model_json_schema(), ensure_ascii=False)
        messages = [
            {
                "role": "system",
                "content": f"{system}\n\nAnswer with JSON matching this schema:\n{shape}",
            },
            {"role": "user", "content": user},
        ]

        started = time.monotonic()
        last_error = ""
        prompt_tokens = completion_tokens = 0
        failover = _Failover()

        for attempt in range(1, max_attempts + 1):
            body = {
                "model": model,
                "messages": messages,
                "temperature": temperature,
                "response_format": {"type": "json_object"},
            }
            _add_reasoning(body, role, self._settings)

            async def request(remaining_s: float, request_body=body) -> dict:
                nonlocal prompt_tokens, completion_tokens
                response, payload = await self._complete(
                    role, model, request_body, remaining_s, failover
                )
                provider_unavailable = self._check_provider_failure(
                    model, response.status_code, payload
                )
                if provider_unavailable and response.status_code < 500:
                    raise ProviderUnavailable(f"HTTP {response.status_code}: {payload}")
                if isinstance(payload, dict):
                    usage = payload.get("usage") or {}
                    prompt_tokens += int(usage.get("prompt_tokens") or 0)
                    completion_tokens += int(usage.get("completion_tokens") or 0)
                if 400 <= response.status_code < 500 and response.status_code != 429:
                    raise LlmError(f"HTTP {response.status_code}: {payload}")
                response.raise_for_status()
                if not isinstance(payload, dict):
                    raise ValueError("provider response was not a JSON object")
                return payload

            try:
                payload, _ = await with_retries(
                    request,
                    deadline_s=self._settings.llm_call_deadline_s,
                    max_attempts=max_attempts,
                    base_delay=self._settings.llm_retry_base_delay_s,
                    label=f"{role} via {model}",
                    transient_also=(ProviderUnavailable,),
                )
            except RetryExhausted as exc:
                last_error = exc.last_error
                break

            content = ""
            try:
                content = payload["choices"][0]["message"]["content"]
                parsed = schema.model_validate_json(_strip_fence(content))
            except (KeyError, IndexError, ValidationError, ValueError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                messages.append({"role": "assistant", "content": content})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"That did not validate: {last_error}. "
                            "Answer again as JSON only."
                        ),
                    }
                )
                continue

            usage = payload.get("usage") or {}
            prompt_details = usage.get("prompt_tokens_details") or {}
            completion_details = usage.get("completion_tokens_details") or {}
            self.log.add(
                ModelCall(
                    role=role,
                    model=model,
                    duration_s=time.monotonic() - started,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    attempts=attempt,
                    ok=True,
                    resolved_model=payload.get("model") or model,
                    provider=payload.get("provider"),
                    generation_id=payload.get("id"),
                    cached_tokens=int(prompt_details.get("cached_tokens") or 0),
                    cache_write_tokens=int(prompt_details.get("cache_write_tokens") or 0),
                    reasoning_tokens=int(completion_details.get("reasoning_tokens") or 0),
                    cost_usd=usage.get("cost"),
                )
            )
            return parsed

        self.log.add(
            ModelCall(
                role=role,
                model=model,
                duration_s=time.monotonic() - started,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                attempts=max_attempts,
                ok=False,
                error=last_error,
            )
        )
        raise LlmError(f"{role} via {model} failed after {max_attempts} attempts: {last_error}")

    async def converse(
        self,
        role: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        temperature: float = 0.0,
        session_id: str | None = None,
    ) -> Reply:
        model = self.model_for(role)
        body = request_body(model, messages, tools, temperature, session_id, role=role)

        started = time.monotonic()
        failover = _Failover()

        async def attempt(remaining_s: float) -> tuple[dict, dict]:
            response, payload = await self._complete(
                role,
                model,
                body,
                httpx.Timeout(
                    connect=min(self._settings.llm_connect_timeout_s, remaining_s),
                    read=min(self._settings.llm_read_timeout_s, remaining_s),
                    write=self._settings.llm_write_timeout_s,
                    pool=min(self._settings.llm_connect_timeout_s, remaining_s),
                ),
                failover,
                session_id,
            )
            provider_unavailable = self._check_provider_failure(
                model, response.status_code, payload
            )
            if provider_unavailable and response.status_code < 500:
                raise ProviderUnavailable(f"HTTP {response.status_code}: {payload}")
            response.raise_for_status()
            try:
                message = payload["choices"][0]["message"]
            except (KeyError, IndexError, TypeError) as exc:
                raise MalformedReply(str(payload)[:300]) from exc
            return payload, message

        try:
            (payload, message), attempts = await with_retries(
                attempt,
                deadline_s=self._settings.llm_call_deadline_s,
                max_attempts=self._settings.llm_max_attempts,
                base_delay=self._settings.llm_retry_base_delay_s,
                label=f"{role} via {model}",
                transient_also=(MalformedReply, ProviderUnavailable),
            )
        except RetryExhausted as exc:
            self.log.add(
                ModelCall(
                    role=role,
                    model=model,
                    duration_s=time.monotonic() - started,
                    prompt_tokens=0,
                    completion_tokens=0,
                    attempts=max(1, exc.attempts),
                    ok=False,
                    error=exc.last_error,
                )
            )
            raise LlmUnavailable(
                role, model, exc.reason, exc.attempts, exc.elapsed_s, exc.last_error
            ) from exc

        usage = payload.get("usage") or {}
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        prompt_details = usage.get("prompt_tokens_details") or {}
        completion_details = usage.get("completion_tokens_details") or {}

        calls: list[ToolCall] = []
        for raw in message.get("tool_calls") or []:
            function = raw.get("function") or {}
            try:
                arguments = json.loads(function.get("arguments") or "{}")
            except ValueError:
                arguments = {"_unparsed": function.get("arguments")}
            if not isinstance(arguments, dict):
                arguments = {"_unparsed": arguments}
            calls.append(
                ToolCall(
                    id=raw.get("id") or f"call_{len(calls)}",
                    name=function.get("name") or "",
                    arguments=arguments,
                )
            )

        self.log.add(
            ModelCall(
                role=role,
                model=model,
                duration_s=time.monotonic() - started,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                attempts=attempts,
                ok=True,
                resolved_model=payload.get("model") or model,
                provider=payload.get("provider"),
                generation_id=payload.get("id"),
                cached_tokens=int(prompt_details.get("cached_tokens") or 0),
                cache_write_tokens=int(prompt_details.get("cache_write_tokens") or 0),
                reasoning_tokens=int(completion_details.get("reasoning_tokens") or 0),
                cost_usd=usage.get("cost"),
            )
        )
        return Reply(
            message=message,
            content=message.get("content"),
            tool_calls=calls,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cached_tokens=int(prompt_details.get("cached_tokens") or 0),
            cache_write_tokens=int(prompt_details.get("cache_write_tokens") or 0),
            reasoning_tokens=int(completion_details.get("reasoning_tokens") or 0),
            cost_usd=usage.get("cost"),
            duration_ms=round((time.monotonic() - started) * 1000),
            attempts=attempts,
            provider=payload.get("provider"),
            generation_id=payload.get("id"),
            resolved_model=payload.get("model") or model,
        )

    async def aclose(self) -> None:
        await self._client.aclose()
