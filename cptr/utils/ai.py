"""Provider streaming and completion functions: raw httpx.

Each streaming function takes a ChatCompletionForm + url + key.
All yield normalized events: text_delta, output, tool_call, usage, done.

chat_completion() is the low-level non-streaming provider call.
generate_text()/generate_json() add model-id resolution and inheritance for
small internal calls.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from email.utils import parsedate_to_datetime
from typing import Dict, List

import httpx
from pydantic import BaseModel

from cptr.env import (
    STREAM_CONNECT_TIMEOUT_SECONDS,
    STREAM_READ_TIMEOUT_SECONDS,
    STREAM_WRITE_TIMEOUT_SECONDS,
)
from cptr.utils.logger import log_upstream_request

logger = logging.getLogger(__name__)


def _reasoning_output_item(text: str = "", *, status: str = "in_progress") -> dict:
    """Create a Responses-style reasoning output item."""
    return {
        "type": "reasoning",
        "id": f"rs_{uuid.uuid4().hex}",
        "status": status,
        "content": [{"type": "text", "text": text}],
    }


def _openrouter_headers(url: str) -> dict[str, str]:
    """Return OpenRouter app-info headers when the URL is an OpenRouter endpoint."""
    if "openrouter.ai" in url:
        return {
            "HTTP-Referer": "https://cptr.sh/",
            "X-Title": "cptr / open-webui",
        }
    return {}


_STREAM_RETRY_ATTEMPTS = 3
_STREAM_RETRY_MAX_DELAY_SECONDS = 30
_STREAM_RETRY_STATUS_CODES = {408, 409, 429, 500, 502, 503, 504}
_STREAM_TIMEOUT = httpx.Timeout(
    STREAM_CONNECT_TIMEOUT_SECONDS,
    read=STREAM_READ_TIMEOUT_SECONDS,
    write=STREAM_WRITE_TIMEOUT_SECONDS,
)


class EmptyCompletionError(RuntimeError):
    pass


_STREAM_RETRY_ERRORS = (
    EmptyCompletionError,
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.HTTPStatusError,
    httpx.ReadError,
    httpx.ReadTimeout,
    httpx.RemoteProtocolError,
    httpx.WriteTimeout,
)


class ChatCompletionForm(BaseModel):
    """Mirrors OpenAI Responses API request shape."""

    model: str
    messages: List[Dict]
    instructions: str = ""
    tools: List[Dict] = []


def _is_retryable_stream_error(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _STREAM_RETRY_STATUS_CODES
    return isinstance(exc, _STREAM_RETRY_ERRORS)


def _stream_retry_delay(exc: BaseException, attempt: int) -> float:
    delay = 0.5 * (attempt + 1)
    if not isinstance(exc, httpx.HTTPStatusError):
        return delay

    retry_after = exc.response.headers.get("retry-after")
    if not retry_after:
        return delay

    try:
        delay = float(retry_after)
    except ValueError:
        try:
            delay = parsedate_to_datetime(retry_after).timestamp() - time.time()
        except (TypeError, ValueError, OverflowError):
            return delay

    return min(max(delay, 0), _STREAM_RETRY_MAX_DELAY_SECONDS)


# ── Non-streaming completion ────────────────────────────────


async def chat_completion(
    provider: str,
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict],
    system: str = "",
    max_tokens: int = 100,
    api_type: str = "chat_completions",
    request_params: dict | None = None,
) -> str:
    """Simple non-streaming chat completion. Returns the text content.

    Works with Anthropic, OpenAI Chat Completions, and OpenAI Responses API.
    Useful for lightweight tasks like title/summary generation.
    """
    async with httpx.AsyncClient(timeout=_STREAM_TIMEOUT) as client:
        if provider == "anthropic":
            body: dict = {
                "model": model,
                "messages": messages,
                "max_tokens": max_tokens,
            }
            if system:
                body["system"] = system
            if request_params:
                body.update(request_params)
            log_upstream_request(
                provider="anthropic",
                endpoint=f"{base_url}/messages",
                model=model,
                api_type="messages",
                body=body,
            )
            resp = await client.post(
                f"{base_url}/messages",
                json=body,
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    **_openrouter_headers(base_url),
                },
            )
        elif api_type == "responses":
            # OpenAI Responses API
            body_r: dict = {
                "model": model,
                "input": messages,
                "max_output_tokens": max_tokens,
            }
            if system:
                body_r["instructions"] = system
            if request_params:
                body_r.update(request_params)
            log_upstream_request(
                provider=provider,
                endpoint=f"{base_url}/responses",
                model=model,
                api_type="responses",
                body=body_r,
            )
            resp = await client.post(
                f"{base_url}/responses",
                json=body_r,
                headers={"Authorization": f"Bearer {api_key}", **_openrouter_headers(base_url)},
            )
        else:
            # OpenAI Chat Completions (default)
            all_messages = messages[:]
            if system:
                all_messages.insert(0, {"role": "system", "content": system})
            body_cc: dict = {
                "model": model,
                "messages": all_messages,
                "max_completion_tokens": max_tokens,
            }
            if request_params:
                body_cc.update(request_params)
            log_upstream_request(
                provider=provider,
                endpoint=f"{base_url}/chat/completions",
                model=model,
                api_type="chat_completions",
                body=body_cc,
            )
            resp = await client.post(
                f"{base_url}/chat/completions",
                json=body_cc,
                headers={"Authorization": f"Bearer {api_key}", **_openrouter_headers(base_url)},
            )
        if resp.status_code >= 400:
            logger.debug(
                "[chat_completion] %s %s → %s: %s",
                provider,
                model,
                resp.status_code,
                resp.text[:500],
            )
        resp.raise_for_status()
        data = resp.json()

    if provider == "anthropic":
        return data.get("content", [{}])[0].get("text", "")
    if api_type == "responses":
        # Responses API: output is a list of items
        for item in data.get("output", []):
            if item.get("type") == "message":
                for c in item.get("content", []):
                    if c.get("type") == "output_text":
                        return c.get("text", "")
        return ""
    return data.get("choices", [{}])[0].get("message", {}).get("content", "")


async def generate_text(
    request,
    *,
    model_id: str | None,
    messages: list[dict],
    system: str = "",
    max_tokens: int = 100,
    active_connection: dict | None = None,
    active_model: str = "",
    request_params: dict | None = None,
    timeout_seconds: float = STREAM_READ_TIMEOUT_SECONDS,
) -> str | None:
    """Generate text; model_id None inherits active/default."""
    from cptr.models import Config
    from cptr.utils.config import _get_jwt_secret
    from cptr.utils.crypto import decrypt_key
    from cptr.utils.model_targets import first_api_model_target, resolve_api_model_target

    app_state = getattr(getattr(request, "app", None), "state", None)
    connection = active_connection
    runtime_model = active_model
    selected = model_id.strip() if isinstance(model_id, str) else ""
    if selected:
        try:
            target = await resolve_api_model_target(selected, app_state)
            connection = target.connection
            runtime_model = target.runtime_model
        except Exception:
            logger.debug("[generate_text] model not available: %s", selected, exc_info=True)

    if not connection or not runtime_model:
        default_model = await Config.get("chat.default_model")
        target = None
        if isinstance(default_model, str) and default_model.strip():
            try:
                target = await resolve_api_model_target(default_model.strip(), app_state)
            except Exception:
                logger.debug("[generate_text] default model not available", exc_info=True)
        if target is None:
            try:
                target = await first_api_model_target(app_state)
            except Exception:
                logger.debug("[generate_text] no fallback API model available", exc_info=True)
                return None
        connection = target.connection
        runtime_model = target.runtime_model

    provider = connection["provider"]
    base_url = connection.get("base_url") or {
        "anthropic": "https://api.anthropic.com/v1",
        "openai": "https://api.openai.com/v1",
    }.get(provider, "https://api.openai.com/v1")
    try:
        return await asyncio.wait_for(
            chat_completion(
                provider=provider,
                base_url=base_url,
                api_key=decrypt_key(connection.get("api_key", ""), _get_jwt_secret()),
                model=runtime_model,
                messages=messages,
                system=system,
                max_tokens=max_tokens,
                api_type=connection.get("api_type", "chat_completions"),
                request_params=request_params,
            ),
            timeout=timeout_seconds,
        )
    except Exception:
        logger.debug("[generate_text] generation failed", exc_info=True)
        return None


async def generate_json(
    request,
    *,
    model_id: str | None,
    messages: list[dict],
    system: str = "",
    max_tokens: int = 100,
    active_connection: dict | None = None,
    active_model: str = "",
    request_params: dict | None = None,
    timeout_seconds: float = STREAM_READ_TIMEOUT_SECONDS,
) -> dict | None:
    from cptr.utils.json_parser import extract_json

    text = await generate_text(
        request,
        model_id=model_id,
        messages=messages,
        system=system,
        max_tokens=max_tokens,
        active_connection=active_connection,
        active_model=active_model,
        request_params=request_params,
        timeout_seconds=timeout_seconds,
    )
    if not text:
        return None
    parsed = extract_json(text)
    return parsed if isinstance(parsed, dict) else None


# ── Anthropic ────────────────────────────────────────────────


def _to_anthropic_messages(messages: list[dict]) -> list[dict]:
    """Canonical messages → Anthropic format."""
    result = []
    for m in messages:
        role = m["role"]
        if role == "system":
            continue  # system goes in body.system

        content = m.get("content", "")
        if isinstance(content, list):
            formatted_content = []
            for block in content:
                if block.get("type") == "text":
                    formatted_content.append({"type": "text", "text": block.get("text", "")})
                elif block.get("type") == "image":
                    formatted_content.append(
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": block.get("media_type", "image/jpeg"),
                                "data": block.get("base64", ""),
                            },
                        }
                    )
            content = formatted_content
        if role == "tool":
            # tool result → Anthropic tool_result block
            # Content may be a string or a list of blocks (multimodal image results)
            if isinstance(content, list):
                # Multimodal tool result — convert blocks to Anthropic format
                tool_content = []
                for block in content:
                    if block.get("type") == "text":
                        tool_content.append({"type": "text", "text": block.get("text", "")})
                    elif block.get("type") == "image":
                        tool_content.append(
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": block.get("media_type", "image/jpeg"),
                                    "data": block.get("base64", ""),
                                },
                            }
                        )
            else:
                tool_content = content
            result.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": m.get("tool_call_id", ""),
                            "content": tool_content,
                        }
                    ],
                }
            )
        elif role == "assistant" and m.get("tool_calls"):
            # assistant with tool calls → Anthropic tool_use blocks
            blocks: list[dict] = []
            if content:
                if isinstance(content, list):
                    blocks.extend(block for block in content if block.get("type") == "text")
                else:
                    blocks.append({"type": "text", "text": content})
            for tc in m["tool_calls"]:
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": tc.get("id", ""),
                        "name": tc["function"]["name"],
                        "input": json.loads(tc["function"].get("arguments", "{}")),
                    }
                )
            result.append({"role": "assistant", "content": blocks})
        else:
            result.append({"role": role, "content": content})
    return result


async def stream_anthropic(
    form_data: ChatCompletionForm, url: str, key: str, *, request_params: dict | None = None
) -> AsyncIterator[dict]:
    tools = [
        {
            "name": t["name"],
            "description": t["description"],
            "input_schema": t["parameters"],
        }
        for t in form_data.tools
    ]

    body = {
        "model": form_data.model,
        "system": form_data.instructions,
        "messages": _to_anthropic_messages(form_data.messages),
        "tools": tools if tools else None,
        "stream": True,
        "max_tokens": 4096,
    }
    if request_params:
        body.update(request_params)
    # Remove None values
    body = {k: v for k, v in body.items() if v is not None}
    log_upstream_request(
        provider="anthropic",
        endpoint=f"{url}/messages",
        model=form_data.model,
        api_type="messages",
        body=body,
    )
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01", **_openrouter_headers(url)}

    emitted = False
    for attempt in range(_STREAM_RETRY_ATTEMPTS):
        try:
            async with httpx.AsyncClient(timeout=_STREAM_TIMEOUT) as client:
                logger.info("[stream] anthropic POST %s/messages model=%s", url, form_data.model)
                async with client.stream(
                    "POST", f"{url}/messages", json=body, headers=headers
                ) as resp:
                    logger.info("[stream] anthropic status=%s", resp.status_code)
                    resp.raise_for_status()
                    current_block: dict = {}
                    usage_data: dict = {}
                    async for line in resp.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        event = json.loads(line[6:])
                        etype = event.get("type")

                        if etype == "message_start":
                            msg_usage = event.get("message", {}).get("usage", {})
                            if msg_usage:
                                usage_data["input_tokens"] = msg_usage.get("input_tokens", 0)
                                for cache_key in (
                                    "cache_read_input_tokens",
                                    "cache_creation_input_tokens",
                                ):
                                    if msg_usage.get(cache_key):
                                        usage_data[cache_key] = msg_usage[cache_key]

                        if etype == "content_block_start":
                            block = event["content_block"]
                            current_block = {"type": block["type"], "index": event["index"]}
                            if block["type"] == "tool_use":
                                current_block["id"] = block["id"]
                                current_block["name"] = block["name"]
                                current_block["input_json"] = ""

                        elif etype == "content_block_delta":
                            delta = event["delta"]
                            if delta["type"] == "text_delta":
                                emitted = True
                                yield {"type": "text_delta", "content": delta["text"]}
                            elif delta["type"] == "input_json_delta":
                                current_block["input_json"] += delta["partial_json"]

                        elif etype == "content_block_stop":
                            if current_block.get("type") == "tool_use":
                                emitted = True
                                yield {
                                    "type": "tool_call",
                                    "call_id": current_block["id"],
                                    "name": current_block["name"],
                                    "arguments": json.loads(current_block["input_json"]),
                                }

                        elif etype == "message_delta":
                            delta_usage = event.get("usage", {})
                            if delta_usage:
                                usage_data["output_tokens"] = delta_usage.get("output_tokens", 0)
                                emitted = True
                                yield {"type": "usage", **usage_data}

                        elif etype == "message_stop":
                            emitted = True
                            yield {"type": "done"}
            return
        except _STREAM_RETRY_ERRORS as exc:
            if (
                emitted
                or attempt == _STREAM_RETRY_ATTEMPTS - 1
                or not _is_retryable_stream_error(exc)
            ):
                raise
            logger.warning(
                "[stream] anthropic transient stream failure before first event; retrying (%s/%s)",
                attempt + 1,
                _STREAM_RETRY_ATTEMPTS,
                exc_info=True,
            )
            await asyncio.sleep(_stream_retry_delay(exc, attempt))


# ── OpenAI Chat Completions ──────────────────────────────────


def _reasoning_items_to_content(items: list[dict]) -> str:
    """Convert replayable reasoning items to a reasoning_content string.

    Reasoning items may expose text in content or summary blocks. We extract
    those text blocks and join them for Chat Completions-compatible providers.
    """
    texts: list[str] = []
    for item in items:
        for blocks in (item.get("content"), item.get("summary")):
            if isinstance(blocks, str):
                if blocks:
                    texts.append(blocks)
            elif isinstance(blocks, list):
                for block in blocks:
                    if isinstance(block, str):
                        if block:
                            texts.append(block)
                    elif (
                        isinstance(block, dict)
                        and block.get("type") in ("text", "output_text", "summary_text")
                        and block.get("text")
                    ):
                        texts.append(block["text"])
    return "\n".join(texts)


def _to_openai_messages(
    messages: list[dict], instructions: str, *, provider_type: str = "default"
) -> list[dict]:
    """Canonical messages → OpenAI Chat Completions format.

    Strict default OpenAI-compatible requests do not receive provider-specific
    reasoning fields.  llama.cpp compatibility replays text reasoning as
    reasoning_content. Other non-standard fields (fc_id in tool_calls) are
    stripped.
    """
    result = []
    if instructions:
        result.append({"role": "system", "content": instructions})
    for m in messages:
        if m["role"] == "system":
            continue

        content = m.get("content", "")
        if isinstance(content, list):
            formatted_content = []
            for block in content:
                if block.get("type") == "text":
                    formatted_content.append({"type": "text", "text": block.get("text", "")})
                elif block.get("type") == "image_url":
                    # Already in OpenAI-native format (e.g. from image extraction)
                    formatted_content.append(block)
                elif block.get("type") == "image":
                    data_uri = f"data:{block.get('media_type', 'image/jpeg')};base64,{block.get('base64', '')}"
                    formatted_content.append({"type": "image_url", "image_url": {"url": data_uri}})
            new_m = dict(m)
            new_m.pop("reasoning_items", None)
            if new_m.get("tool_calls"):
                new_m["tool_calls"] = [
                    {k: v for k, v in tc.items() if k != "fc_id"} for tc in new_m["tool_calls"]
                ]
            new_m["content"] = formatted_content
            ri = m.get("reasoning_items")
            if provider_type == "llama.cpp" and ri and new_m.get("role") == "assistant":
                rc = _reasoning_items_to_content(ri)
                if rc:
                    new_m["reasoning_content"] = rc
            result.append(new_m)
        else:
            out = dict(m)
            out.pop("reasoning_items", None)
            out["content"] = content
            if out.get("tool_calls"):
                out["tool_calls"] = [
                    {k: v for k, v in tc.items() if k != "fc_id"} for tc in out["tool_calls"]
                ]
            ri = m.get("reasoning_items")
            if provider_type == "llama.cpp" and ri and out.get("role") == "assistant":
                rc = _reasoning_items_to_content(ri)
                if rc:
                    out["reasoning_content"] = rc
            result.append(out)
    return result


async def stream_openai_completions(
    form_data: ChatCompletionForm,
    url: str,
    key: str,
    *,
    request_params: dict | None = None,
    provider_type: str = "default",
) -> AsyncIterator[dict]:
    tools = [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["parameters"],
            },
        }
        for t in form_data.tools
    ]

    body: dict = {
        "model": form_data.model,
        "messages": _to_openai_messages(
            form_data.messages, form_data.instructions, provider_type=provider_type
        ),
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if tools:
        body["tools"] = tools
    if request_params:
        body.update(request_params)
    log_upstream_request(
        provider="openai",
        endpoint=f"{url}/chat/completions",
        model=form_data.model,
        api_type="chat_completions",
        body=body,
    )
    headers = {"Authorization": f"Bearer {key}", **_openrouter_headers(url)}

    emitted = False
    for attempt in range(_STREAM_RETRY_ATTEMPTS):
        try:
            async with httpx.AsyncClient(timeout=_STREAM_TIMEOUT) as client:
                logger.info(
                    "[stream] openai completions POST %s/chat/completions model=%s",
                    url,
                    form_data.model,
                )
                async with client.stream(
                    "POST", f"{url}/chat/completions", json=body, headers=headers
                ) as resp:
                    logger.info("[stream] openai completions status=%s", resp.status_code)
                    if resp.status_code >= 400:
                        error_body = await resp.aread()
                        logger.error(
                            "[stream] openai completions error body: %s",
                            error_body.decode(errors="replace"),
                        )
                    resp.raise_for_status()
                    tool_calls: dict[int, dict] = {}
                    reasoning_buffer = ""
                    reasoning_item: dict | None = None
                    reasoning_details: list = []

                    def complete_reasoning_item() -> dict | None:
                        nonlocal reasoning_buffer, reasoning_item, reasoning_details
                        if reasoning_item is None:
                            return None
                        reasoning_item["status"] = "completed"
                        if reasoning_details:
                            reasoning_item["reasoning_details"] = copy.deepcopy(reasoning_details)
                        item = copy.deepcopy(reasoning_item)
                        reasoning_item = None
                        reasoning_buffer = ""
                        reasoning_details = []
                        return item

                    async for line in resp.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        raw = line.removeprefix("data:").strip()
                        if raw == "[DONE]":
                            break
                        if not raw:
                            continue
                        chunk = json.loads(raw)
                        choices = chunk.get("choices", [])
                        delta = choices[0].get("delta", {}) if choices else {}

                        reasoning_delta = (
                            delta.get("reasoning_content")
                            or delta.get("reasoning")
                            or delta.get("thinking")
                        )
                        if delta.get("reasoning_details"):
                            details = delta["reasoning_details"]
                            if isinstance(details, list):
                                reasoning_details.extend(copy.deepcopy(details))
                            else:
                                reasoning_details.append(copy.deepcopy(details))
                            if reasoning_item is None:
                                reasoning_item = _reasoning_output_item()
                            reasoning_item["reasoning_details"] = copy.deepcopy(reasoning_details)
                            emitted = True
                            yield {"type": "output", "item": copy.deepcopy(reasoning_item)}

                        if reasoning_delta:
                            reasoning_buffer += reasoning_delta
                            if reasoning_item is None:
                                reasoning_item = _reasoning_output_item()
                            reasoning_item["content"][0]["text"] = reasoning_buffer
                            emitted = True
                            yield {"type": "output", "item": copy.deepcopy(reasoning_item)}

                        if delta.get("content"):
                            item = complete_reasoning_item()
                            if item is not None:
                                emitted = True
                                yield {"type": "output", "item": item}
                            emitted = True
                            yield {"type": "text_delta", "content": delta["content"]}

                        if delta.get("tool_calls"):
                            item = complete_reasoning_item()
                            if item is not None:
                                emitted = True
                                yield {"type": "output", "item": item}

                        for tc in delta.get("tool_calls") or []:
                            idx = tc["index"]
                            if idx not in tool_calls:
                                tool_calls[idx] = {
                                    "id": tc["id"],
                                    "name": tc["function"]["name"],
                                    "arguments_json": "",
                                }
                            tool_calls[idx]["arguments_json"] += tc["function"].get("arguments", "")

                        if choices and choices[0].get("finish_reason") == "tool_calls":
                            # Emit accumulated reasoning before tool calls
                            item = complete_reasoning_item()
                            if item is not None:
                                emitted = True
                                yield {"type": "output", "item": item}
                            for tc in tool_calls.values():
                                emitted = True
                                yield {
                                    "type": "tool_call",
                                    "call_id": tc["id"],
                                    "name": tc["name"],
                                    "arguments": json.loads(tc["arguments_json"]),
                                }

                        if chunk.get("usage"):
                            # Emit any remaining reasoning BEFORE usage
                            # (usage triggers immediate save+return in chat_task)
                            item = complete_reasoning_item()
                            if item is not None:
                                emitted = True
                                yield {"type": "output", "item": item}
                            raw = chunk["usage"]
                            usage_tokens = sum(
                                raw.get(key, 0)
                                for key in ("prompt_tokens", "completion_tokens", "total_tokens")
                            )
                            if not emitted and usage_tokens <= 0:
                                continue
                            emitted = True
                            yield {
                                "type": "usage",
                                "input_tokens": raw.get("prompt_tokens", 0),
                                "output_tokens": raw.get("completion_tokens", 0),
                                "total_tokens": raw.get("total_tokens", 0),
                            }

                    # Emit any remaining reasoning if no usage event was received
                    item = complete_reasoning_item()
                    if item is not None:
                        emitted = True
                        yield {"type": "output", "item": item}
                    if not emitted:
                        raise EmptyCompletionError(
                            "Upstream provider returned an empty completion with no text, "
                            "output items, tool calls, or usage tokens."
                        )
                    emitted = True
                    yield {"type": "done"}
            return
        except _STREAM_RETRY_ERRORS as exc:
            if (
                emitted
                or attempt == _STREAM_RETRY_ATTEMPTS - 1
                or not _is_retryable_stream_error(exc)
            ):
                raise
            logger.warning(
                "[stream] openai completions transient stream failure before first event; retrying (%s/%s)",
                attempt + 1,
                _STREAM_RETRY_ATTEMPTS,
                exc_info=True,
            )
            await asyncio.sleep(_stream_retry_delay(exc, attempt))


# ── OpenAI Responses API ─────────────────────────────────────


def _replayable_reasoning_items(items: list[dict] | None, *, provider_type: str) -> list[dict]:
    replayable: list[dict] = []
    for item in items or []:
        if (
            not isinstance(item, dict)
            or item.get("type") != "reasoning"
            or item.get("status") not in (None, "completed")
            or str(item.get("id", "")).startswith("reasoning-")
        ):
            continue
        text = _reasoning_items_to_content([item])
        if not text and not item.get("encrypted_content") and not item.get("reasoning_details"):
            continue
        out = copy.deepcopy(item)
        if provider_type == "llama.cpp" and text:
            out["content"] = [{"type": "text", "text": text}]
        replayable.append(out)
    return replayable


def _to_responses_input(
    messages: list[dict], instructions: str, *, provider_type: str = "default"
) -> list[dict]:
    """Canonical messages → Responses API input items."""
    # Pre-collect all tool result call_ids so we can validate function_calls
    # have matching outputs.  This prevents orphaned function_calls (from
    # crashes, data corruption, etc.) from permanently breaking the chat.
    tool_result_ids = {m.get("tool_call_id", "") for m in messages if m.get("role") == "tool"}

    items = []
    for m in messages:
        role = m["role"]
        if role == "system":
            continue
        if role == "tool":
            content = m.get("content", "")
            if isinstance(content, list):
                # Multimodal tool content — extract text only for output
                # (images are handled in the agentic loop)
                text_parts = []
                for block in content:
                    if block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                content = "\n".join(text_parts)
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": m.get("tool_call_id", ""),
                    "output": content,
                    "status": "completed",
                }
            )
        elif role == "assistant" and m.get("tool_calls"):
            # Emit reasoning items before function calls (required by reasoning models)
            for ri in _replayable_reasoning_items(
                m.get("reasoning_items"), provider_type=provider_type
            ):
                items.append(ri)
            content = m.get("content", "")
            if isinstance(content, list):
                text = "".join(
                    block.get("text") or ""
                    for block in content
                    if isinstance(block, dict)
                    and block.get("type") in ("text", "output_text", "input_text")
                )
            else:
                text = content
            if isinstance(text, str) and text.strip():
                items.append(
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": text}],
                    }
                )
            for tc in m["tool_calls"]:
                call_id = tc.get("id", "")
                # Skip function_calls that have no matching tool result
                if call_id and call_id not in tool_result_ids:
                    logger.warning(
                        "[responses] Skipping orphaned function_call %s (%s) — no matching tool result",
                        call_id,
                        tc.get("function", {}).get("name", "?"),
                    )
                    continue
                args = tc["function"].get("arguments", "{}")
                # Responses API requires id to start with "fc_"
                fc_id = tc.get("fc_id", "")
                if not fc_id or not fc_id.startswith("fc_"):
                    fc_id = f"fc_{call_id.replace('call_', '', 1) or uuid.uuid4().hex}"
                items.append(
                    {
                        "type": "function_call",
                        "id": fc_id,
                        "call_id": call_id,
                        "name": tc["function"]["name"],
                        "arguments": args if isinstance(args, str) else json.dumps(args),
                        "status": "completed",
                    }
                )
        else:
            if role == "assistant":
                for ri in _replayable_reasoning_items(
                    m.get("reasoning_items"), provider_type=provider_type
                ):
                    items.append(ri)
            content = m.get("content", "")
            if isinstance(content, list):
                formatted_content = []
                for block in content:
                    if block.get("type") == "text":
                        formatted_content.append(
                            {"type": "input_text", "text": block.get("text", "")}
                        )
                    elif block.get("type") == "image":
                        data_uri = f"data:{block.get('media_type', 'image/jpeg')};base64,{block.get('base64', '')}"
                        formatted_content.append({"type": "input_image", "image_url": data_uri})
                items.append({"type": "message", "role": role, "content": formatted_content})
            else:
                items.append({"type": "message", "role": role, "content": content})
    return items


async def stream_openai_responses(
    form_data: ChatCompletionForm,
    url: str,
    key: str,
    *,
    request_params: dict | None = None,
    provider_type: str = "default",
) -> AsyncIterator[dict]:
    tools = [
        {
            "type": "function",
            "name": t["name"],
            "description": t["description"],
            "parameters": t["parameters"],
        }
        for t in form_data.tools
    ]

    body: dict = {
        "model": form_data.model,
        "input": _to_responses_input(
            form_data.messages, form_data.instructions, provider_type=provider_type
        ),
        "stream": True,
    }
    if form_data.instructions:
        body["instructions"] = form_data.instructions
    if tools:
        body["tools"] = tools
    if request_params:
        body.update(request_params)
    log_upstream_request(
        provider="openai",
        endpoint=f"{url}/responses",
        model=form_data.model,
        api_type="responses",
        body=body,
    )
    headers = {"Authorization": f"Bearer {key}", **_openrouter_headers(url)}

    emitted = False
    for attempt in range(_STREAM_RETRY_ATTEMPTS):
        try:
            async with httpx.AsyncClient(timeout=_STREAM_TIMEOUT) as client:
                logger.info(
                    "[stream] openai responses POST %s/responses model=%s input_items=%d types=%s",
                    url,
                    form_data.model,
                    len(body.get("input", [])),
                    [i.get("type", i.get("role", "?")) for i in body.get("input", [])],
                )
                async with client.stream(
                    "POST", f"{url}/responses", json=body, headers=headers
                ) as resp:
                    logger.info("[stream] openai responses status=%s", resp.status_code)
                    if resp.status_code >= 400:
                        error_body = await resp.aread()
                        logger.error(
                            "[stream] openai responses error body: %s",
                            error_body.decode(errors="replace"),
                        )
                    resp.raise_for_status()
                    output_items_by_id: dict[str, dict] = {}
                    active_reasoning_item: dict | None = None

                    def get_reasoning_item(event: dict) -> dict:
                        nonlocal active_reasoning_item
                        item_id = event.get("item_id") or event.get("output_item_id") or "rs_stream"
                        if item_id in output_items_by_id:
                            active_reasoning_item = output_items_by_id[item_id]
                        if active_reasoning_item is None:
                            active_reasoning_item = _reasoning_output_item()
                            active_reasoning_item["id"] = item_id
                            output_items_by_id[item_id] = active_reasoning_item
                        return active_reasoning_item

                    async for line in resp.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        raw = line[6:]
                        if raw == "[DONE]":
                            break
                        event = json.loads(raw)
                        etype = event.get("type")

                        if etype == "response.output_text.delta":
                            emitted = True
                            yield {"type": "text_delta", "content": event["delta"]}

                        elif etype == "response.output_item.added":
                            item = event.get("item") or {}
                            item_id = item.get("id")
                            if item_id:
                                output_items_by_id[item_id] = copy.deepcopy(item)
                            if item.get("type") == "reasoning":
                                active_reasoning_item = output_items_by_id.get(item_id) or item

                        elif etype in (
                            "response.reasoning.delta",
                            "response.reasoning_text.delta",
                        ):
                            item = get_reasoning_item(event)
                            delta = event.get("delta", "")
                            content = item.setdefault("content", [{"type": "text", "text": ""}])
                            if not content:
                                content.append({"type": "text", "text": ""})
                            if isinstance(content[0], dict):
                                content[0]["type"] = content[0].get("type") or "text"
                                content[0]["text"] = f"{content[0].get('text', '')}{delta}"
                            emitted = True
                            yield {"type": "output", "item": copy.deepcopy(item)}

                        elif etype == "response.reasoning_summary_text.delta":
                            item = get_reasoning_item(event)
                            delta = event.get("delta", "")
                            summary = item.setdefault(
                                "summary", [{"type": "summary_text", "text": ""}]
                            )
                            if not summary:
                                summary.append({"type": "summary_text", "text": ""})
                            if isinstance(summary[0], dict):
                                summary[0]["type"] = summary[0].get("type") or "summary_text"
                                summary[0]["text"] = f"{summary[0].get('text', '')}{delta}"
                            emitted = True
                            yield {"type": "output", "item": copy.deepcopy(item)}

                        elif etype == "response.reasoning_summary_part.added":
                            item = get_reasoning_item(event)
                            part = event.get("part")
                            if part:
                                item.setdefault("summary", []).append(copy.deepcopy(part))
                            emitted = True
                            yield {"type": "output", "item": copy.deepcopy(item)}

                        elif etype == "response.output_item.done":
                            item = event["item"]
                            if item.get("id"):
                                output_items_by_id[item["id"]] = copy.deepcopy(item)
                            if item["type"] == "function_call":
                                emitted = True
                                yield {
                                    "type": "tool_call",
                                    "id": item.get("id", ""),
                                    "call_id": item["call_id"],
                                    "name": item["name"],
                                    "arguments": json.loads(item["arguments"]),
                                }
                            elif item["type"] == "reasoning":
                                # Reasoning items must be round-tripped for reasoning models
                                if active_reasoning_item is not None and active_reasoning_item.get(
                                    "id"
                                ) == item.get("id"):
                                    active_reasoning_item = None
                                emitted = True
                                yield {
                                    "type": "output",
                                    "item": item,
                                }

                        elif etype == "response.failed":
                            error = event.get("response", {}).get("error", {})
                            msg = error.get("message", "Response failed")
                            raise RuntimeError(f"Responses API error: {msg}")

                        elif etype == "response.completed":
                            if active_reasoning_item is not None and _reasoning_items_to_content(
                                [active_reasoning_item]
                            ):
                                active_reasoning_item["status"] = "completed"
                                emitted = True
                                yield {
                                    "type": "output",
                                    "item": copy.deepcopy(active_reasoning_item),
                                }
                            usage = event.get("response", {}).get("usage", {})
                            if usage:
                                emitted = True
                                yield {"type": "usage", **usage}
                            emitted = True
                            yield {"type": "done"}
            return
        except _STREAM_RETRY_ERRORS as exc:
            if (
                emitted
                or attempt == _STREAM_RETRY_ATTEMPTS - 1
                or not _is_retryable_stream_error(exc)
            ):
                raise
            logger.warning(
                "[stream] openai responses transient stream failure before first event; retrying (%s/%s)",
                attempt + 1,
                _STREAM_RETRY_ATTEMPTS,
                exc_info=True,
            )
            await asyncio.sleep(_stream_retry_delay(exc, attempt))
