"""Chat task runner: agentic loop with tool calling.

Runs as an asyncio.Task. Streams deltas via Socket.IO, persists to DB.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from pathlib import Path
from typing import Any

from cptr.events import EVENTS, publish_event
from cptr.env import CHAT_MAX_ITERATIONS, CHAT_TOOL_COMMAND_MAX_CHARS, CHAT_TOOL_MAX_CHARS
from cptr.utils.context import (
    build_context_usage,
    estimate_messages_tokens,
    estimate_tokens,
    normalize_usage,
    resolve_compact_token_threshold,
    should_compact,
    usage_context_tokens,
)
from cptr.utils.skills import (
    bump_skill_view,
    discover_skills,
    format_skill_content,
    get_skill_settings,
    load_skill,
)
from cptr.utils.summarize import summarize_messages
from cptr.models import (
    Chat,
    ChatMessage,
    Config,
    is_internal_chat,
    is_pending_subagent_result_message,
    is_subagent_result_message,
)
from cptr.socket.main import emit_to_user
from cptr.utils.ai import (
    ChatCompletionForm,
    generate_json,
    stream_anthropic,
    stream_openai_completions,
    stream_openai_responses,
)
from cptr.utils.config import _get_jwt_secret, now_ms
from cptr.utils.crypto import decrypt_key
from cptr.utils.tools import (
    ALL_TOOLS,
    clear_active_tasks,
    create_artifact,
    disabled_builtin_tool_names,
    execute_tool,
    get_tool_list,
    resolve_builtin_tool_approval,
    _fn_to_schema,
)
from cptr.utils.chat_export import export_chat_to_file
from cptr.utils.prompt_templates import load_system_prompt as _load_system_prompt
from cptr.utils.agents.events import (
    AgentDone,
    AgentError,
    AgentReasoningDelta,
    AgentTextDelta,
    AgentToolOutputDelta,
    AgentToolUpdate,
)
from cptr.utils.agents.attachments import prepare_agent_attachments
from cptr.utils.model_targets import AgentModelTarget, ApiModelTarget, ModelTarget
from cptr.utils.identity import identity_for_context

logger = logging.getLogger(__name__)


def _is_tool_result_error(value: Any) -> bool:
    if isinstance(value, str):
        text = value.strip().lower()
        if (
            text.startswith("error:")
            or text.startswith("exception:")
            or text.startswith("traceback")
            or text.startswith("http error!")
        ):
            return True

    parsed = value
    while isinstance(parsed, str):
        try:
            parsed = json.loads(parsed)
        except (json.JSONDecodeError, TypeError, ValueError):
            break

    if not isinstance(parsed, dict):
        return False

    error = parsed.get("error")
    if isinstance(error, str):
        has_error = bool(error.strip())
    else:
        has_error = isinstance(error, (dict, list)) and bool(error)
    if has_error:
        return True

    status = parsed.get("status")
    if isinstance(status, str) and status.strip().lower() in {"error", "failed"}:
        return True

    if parsed.get("success") is False or parsed.get("ok") is False:
        message = parsed.get("message")
        return has_error or (
            bool(message.strip())
            if isinstance(message, str)
            else isinstance(message, (dict, list)) and bool(message)
        )

    return False


ASK_USER_NAME = "ask_user"
DEFAULT_AUTO_RESOLUTION_MS = 120_000
ASK_USER_SCHEMA = {
    "name": ASK_USER_NAME,
    "description": "Ask the user one to three material planning questions and wait for their answers.",
    "parameters": {
        "type": "object",
        "properties": {
            "questions": {
                "type": "array",
                "minItems": 1,
                "maxItems": 3,
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "header": {"type": "string"},
                        "question": {"type": "string"},
                        "options": {
                            "type": "array",
                            "minItems": 2,
                            "maxItems": 3,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "label": {"type": "string"},
                                    "description": {"type": "string"},
                                },
                                "required": ["label", "description"],
                            },
                        },
                    },
                    "required": ["id", "header", "question", "options"],
                },
            },
            "autoResolutionMs": {
                "type": "integer",
                "minimum": 60_000,
                "maximum": 240_000,
                "default": DEFAULT_AUTO_RESOLUTION_MS,
            },
        },
        "required": ["questions"],
    },
}


def validate_ask_user_request(arguments: dict[str, Any]) -> dict[str, Any]:
    questions = arguments.get("questions")
    if not isinstance(questions, list) or not questions:
        raise ValueError("ask_user requires one to three questions")
    questions = questions[:3]
    normalized_questions = []
    ids: set[str] = set()
    for question in questions:
        if not isinstance(question, dict):
            raise ValueError("each question must be an object")
        if not all(
            isinstance(question.get(key), str) and question[key].strip()
            for key in ("id", "header", "question")
        ):
            raise ValueError("each question needs an id, header, and question")
        if question["id"] in ids:
            raise ValueError("question ids must be unique")
        ids.add(question["id"])
        options = question.get("options")
        if not isinstance(options, list) or len(options) < 2:
            raise ValueError("each question requires two or three options")
        options = options[:3]
        if any(
            not isinstance(option, dict)
            or not isinstance(option.get("label"), str)
            or not option["label"].strip()
            or not isinstance(option.get("description"), str)
            or not option["description"].strip()
            for option in options
        ):
            raise ValueError("each option needs a label and description")
        normalized_questions.append({**question, "options": options})
    timeout = arguments.get("autoResolutionMs", DEFAULT_AUTO_RESOLUTION_MS)
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, int)
        or not 60_000 <= timeout <= 240_000
    ):
        timeout = DEFAULT_AUTO_RESOLUTION_MS
    return {"questions": normalized_questions, "autoResolutionMs": timeout}


def ask_user_answers(
    arguments: dict[str, Any], answers: dict[str, str] | None = None
) -> dict[str, Any]:
    request = validate_ask_user_request(arguments)
    result: dict[str, Any] = {}
    for question in request["questions"]:
        answer = question["options"][0]["label"] if answers is None else answers.get(question["id"])
        if not isinstance(answer, str) or not answer.strip():
            raise ValueError(f"missing answer for {question['id']}")
        result[question["id"]] = {"answers": [answer.strip()]}
    if answers is not None and set(answers) != set(result):
        raise ValueError("answers must match the requested question ids")
    return {"answers": result}


PLAN_MODE_PROMPT = (
    "[Plan Mode] Research with read-only tools before planning. When a material decision "
    "cannot be discovered, use ask_user with one to three questions, two to three options "
    "each, and the recommended option first. Ask only after research and never alongside "
    "another tool call. Then present a concise plan in your response. Use create_artifact "
    "only when a substantial plan or analysis benefits from being saved for review. Wait for "
    "an explicit "
    "approval message before using write tools or implementing."
)

SKILLS_CREATE_RE = re.compile(r"^/skills:create(?:\s+(.*))?$", re.IGNORECASE | re.DOTALL)


def resolve_builtin_tools_config(
    chat_models_config: dict | None, *model_ids: str | None
) -> dict | None:
    """Merge global/model builtin tool group config. Missing/null means default."""
    if not isinstance(chat_models_config, dict):
        return None
    merged: dict = {}
    found = False
    for model_id in ("*", *[m for m in model_ids if m]):
        model_config = chat_models_config.get(model_id)
        if not isinstance(model_config, dict):
            continue
        params = model_config.get("params", {})
        value = params.get("builtin_tools") if isinstance(params, dict) else None
        if isinstance(value, dict):
            merged.update(value)
            found = True
    return merged if found else None


COMPUTER_SKILL_AUTHORING_STANDARDS = """\
Follow the Computer skill-authoring standards:

Frontmatter:
- name: lowercase-hyphenated, <=64 chars, no spaces.
- description: one sentence, <=60 characters, ends with a period. State the
  capability, not the implementation. Do not repeat the skill name. Avoid
  marketing words like powerful, comprehensive, seamless, advanced, or robust.
  Count the characters before saving.
- version: 0.1.0.
- platforms: declare [macos], [linux], or [windows] only when the skill uses
  OS-bound primitives. Omit it for portable skills.

Body section order:
1. "# <Human Title>" plus a short intro covering what it does, what it does not
   do, and important dependency assumptions.
2. "## When to Use" with concrete trigger phrases.
3. "## Prerequisites" with exact env vars, credentials, install steps, or "None".
4. "## How to Run" with the canonical workflow framed through Computer tools.
5. "## Quick Reference" with flat commands, routes, files, or APIs.
6. "## Procedure" with numbered, copy-paste-exact steps.
7. "## Pitfalls" with known limits and failure modes.
8. "## Verification" with one focused check that proves the skill works.

Computer-tool framing:
- Reference Computer tools by name in backticks: `read_file`, `list_directory`,
  `search_files`, `web_search`, `read_url`, `run_command`, `view_skill`,
  and `manage_skill`.
- Frame shell work as "run through `run_command`".
- Prefer Computer tools in prose over raw shell utilities when a tool exists:
  say `read_file` instead of cat/head/tail, `search_files` instead of grep/rg/find,
  and `read_url` instead of curl-to-scrape.
- Third-party CLIs are fine inside procedures or scripts, but explain that the
  agent invokes them through `run_command`.

Quality bar:
- Prefer exact commands, routes, file paths, function names, config keys, and
  error text found verbatim in the sources. Do not invent flags, paths, APIs,
  or behavior.
- Keep SKILL.md tight and scannable: about 100 lines for a simple workflow,
  about 200 for a complex one.
- Do not create a router/index/hub skill that only points at other skills.
- Put larger reusable scripts in `scripts/`, detailed docs in `references/`,
  reusable outputs in `templates/`, and binary or visual assets in `assets/`.
- New skills default to workspace scope. Do not use scope="global" unless the
  user explicitly asks for a global skill."""


def _build_skill_create_prompt(user_request: str) -> str:
    req = (user_request or "").strip()
    if not req:
        req = (
            "the workflow we just went through in this conversation - review the "
            "steps taken and distill them into a reusable skill"
        )
    return (
        "[/skills:create] The user wants you to create a reusable Computer skill "
        "from the request below and save it.\n\n"
        f"THE REQUEST:\n{req}\n\n"
        "The request is open-ended and may mix SOURCES to gather (directories, "
        "file paths, URLs, what we just did, pasted notes) and REQUIREMENTS that "
        "shape the skill (focus, exclusions, scope, naming, style, constraints). "
        "Treat every part of the request as load-bearing. Prose after a path or "
        "URL is not incidental; it is authoring guidance. Never fetch the first "
        "source and ignore the rest.\n\n"
        "Do this:\n"
        "1. Gather every source the user named with the tools you already have: "
        "`read_file`/`search_files`/`list_directory` for local files or directories, "
        "`web_search`/`read_url` for web sources, the current conversation if they "
        "refer to what just happened, and pasted text as-is. If scope is ambiguous, "
        "make a reasonable choice and note it; do not stall.\n"
        "2. Apply every requirement, focus, and constraint in the request to what "
        "the skill covers and emphasizes.\n"
        "3. Author one SKILL.md using the standards below.\n"
        '4. Save it with manage_skill(action="create", name=..., content=...). '
        "If the skill needs supporting files, add them after the create with "
        'manage_skill(action="write_file", name=..., file_path=..., '
        "file_content=...).\n\n"
        f"{COMPUTER_SKILL_AUTHORING_STANDARDS}\n\n"
        "When done, tell the user the skill name, location, and one-line summary."
    )


def _message_has_real_content(message: dict) -> bool:
    text = _plain_message_text(message.get("content")).strip()
    return bool(
        text or message.get("tool_calls") or message.get("reasoning_items") or message.get("output")
    )


def _has_prior_real_chat_content(messages: list[dict], loaded_summary: str | None = None) -> bool:
    if loaded_summary and loaded_summary.strip():
        return True
    last_user_idx = next(
        (idx for idx in range(len(messages) - 1, -1, -1) if messages[idx].get("role") == "user"),
        len(messages),
    )
    return any(
        message.get("role") == "user" and _message_has_real_content(message)
        for message in messages[:last_user_idx]
    )


def _build_skill_create_gate_prompt(reason: str = "empty_chat") -> str:
    if reason == "disabled":
        return (
            "[/skills:create] Skill creation is disabled in admin settings. "
            "Explain this briefly and do not try to create or update a skill."
        )
    return (
        "[/skills:create] The user tried to create a skill before this chat had "
        "any prior real content. Explain briefly that skill creation needs an "
        "existing chat with the workflow or source material already in it, then "
        "ask them to continue in a chat with content first."
    )


def _apply_skills_create_prompt(
    messages: list[dict],
    *,
    allowed: bool,
    denial_reason: str = "empty_chat",
) -> bool:
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        text = _plain_message_text(message.get("content"))
        match = SKILLS_CREATE_RE.match(text.strip())
        if not match:
            return False
        message["content"] = (
            _build_skill_create_prompt(match.group(1) or "")
            if allowed
            else _build_skill_create_gate_prompt(denial_reason)
        )
        return True
    return False


# ── Task registry ───────────────────────────────────────────

_tasks: dict[str, asyncio.Task] = {}  # message_id → asyncio.Task
_task_state: dict[str, dict] = {}  # message_id → {content, output}
_task_chat: dict[str, str] = {}  # message_id → chat_id
_pending_input_locks: dict[str, asyncio.Lock] = {}  # chat_id → Lock


def get_pending_input_lock(chat_id: str) -> asyncio.Lock:
    return _pending_input_locks.setdefault(chat_id, asyncio.Lock())


def start_task(
    request,
    *,
    message_id: str,
    chat_id: str,
    user_id: str,
    workspace: str,
    model: str = "",
    connection: dict | None = None,
    regeneration_prompt: str | None = None,
    output_queue: asyncio.Queue | None = None,
    target: ModelTarget | None = None,
):
    """Launch the agentic loop as a background asyncio.Task."""
    if target is None:
        if connection is None:
            raise ValueError("start_task requires either target or connection")
        target = ApiModelTarget(
            kind="api",
            connection=connection,
            runtime_model=model,
            full_model_id=model,
        )
    task = asyncio.create_task(
        run_chat_task(
            request,
            message_id=message_id,
            chat_id=chat_id,
            user_id=user_id,
            target=target,
            workspace=workspace,
            regeneration_prompt=regeneration_prompt,
            output_queue=output_queue,
        )
    )
    _tasks[message_id] = task
    _task_chat[message_id] = chat_id

    async def emit_active():
        unread_counts = await Chat.unread_counts_by_workspace(
            user_id, [workspace], get_active_chat_ids()
        )
        await emit_to_user(
            user_id,
            {
                "type": "chat:active",
                "chat_id": chat_id,
                "workspace": workspace,
                "active": True,
                "workspace_unread_count": unread_counts.get(workspace, 0),
            },
        )

    asyncio.create_task(emit_active())


async def cancel_task(message_id: str) -> bool:
    """Cancel a running task. Returns True if found."""
    task = _tasks.get(message_id)
    if task:
        task.cancel()
        return True
    return False


def is_running(message_id: str) -> bool:
    """Check if a task is currently running."""
    task = _tasks.get(message_id)
    return task is not None and not task.done()


def get_live_state(message_id: str) -> dict | None:
    """Get live in-memory state for a running task."""
    return _task_state.get(message_id)


def get_active_chat_ids() -> set[str]:
    """Return the set of chat_ids that currently have a running task."""
    return {cid for mid, cid in _task_chat.items() if mid in _tasks and not _tasks[mid].done()}


def _plain_message_text(content) -> str:
    if isinstance(content, list):
        return " ".join(
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return str(content or "")


def _memory_recall_inputs(
    messages: list[dict],
    regeneration_prompt: str | None = None,
) -> tuple[str, list[str]]:
    current_message = regeneration_prompt or ""
    if not current_message:
        for message in reversed(messages):
            if message.get("role") == "user":
                current_message = _plain_message_text(message.get("content"))
                break
    mentioned_files = re.findall(
        r"(?:^|\s)([./~]?[A-Za-z0-9_.\-/]+(?:\.[A-Za-z0-9_]+))",
        current_message,
    )[:12]
    return current_message, mentioned_files


# ── Pending input processing ────────────────────────────────


def _merge_subagent_result_meta(messages: list[ChatMessage]) -> dict | None:
    if not messages:
        return None

    if all(is_subagent_result_message(m.meta) for m in messages):
        delegation_ids = [
            m.meta.get("delegation_id") for m in messages if m.meta and m.meta.get("delegation_id")
        ]
        subagent_chat_ids = [
            m.meta.get("subagent_chat_id")
            for m in messages
            if m.meta and m.meta.get("subagent_chat_id")
        ]
        meta = {"internal": True, "type": "subagent"}
        if len(delegation_ids) == 1:
            meta["delegation_id"] = delegation_ids[0]
        elif delegation_ids:
            meta["delegation_ids"] = delegation_ids
        if len(subagent_chat_ids) == 1:
            meta["subagent_chat_id"] = subagent_chat_ids[0]
        elif subagent_chat_ids:
            meta["subagent_chat_ids"] = subagent_chat_ids
        return meta

    return None


def _is_pending_internal_subagent_result(message: ChatMessage) -> bool:
    return is_pending_subagent_result_message(message.meta)


def _is_pending_chat_input(message: ChatMessage) -> bool:
    meta = message.meta or {}
    return bool(meta.get("queued") or is_pending_subagent_result_message(meta))


def _merge_pending_input_meta(messages: list[ChatMessage]) -> dict | None:
    subagent_meta = _merge_subagent_result_meta(messages)
    if subagent_meta:
        return subagent_meta

    files: list[dict] = []
    for message in messages:
        meta = message.meta or {}
        message_files = meta.get("files")
        if isinstance(message_files, list):
            files.extend(message_files)

    return {"files": files} if files else None


def _pending_input_ready(message: ChatMessage, msg_map: dict[str, ChatMessage]) -> bool:
    parent = msg_map.get(message.parent_id) if message.parent_id else None
    return not (parent and parent.role == "assistant" and not parent.done)


def _first_ready_pending_input_batch(messages: list[ChatMessage]) -> list[ChatMessage]:
    """Return the first ready pending batch on a single branch."""
    msg_map = {m.id: m for m in messages}
    pending_inputs = [m for m in messages if m.role == "user" and _is_pending_chat_input(m)]
    if not pending_inputs:
        return []

    first = next((m for m in pending_inputs if _pending_input_ready(m, msg_map)), None)
    if not first:
        return []

    first_is_internal_subagent_result = _is_pending_internal_subagent_result(first)
    batch = []
    for message in pending_inputs:
        if message is first:
            batch.append(message)
            continue
        if not batch:
            continue
        if message.parent_id != first.parent_id:
            break
        if message.model != first.model:
            break
        if _is_pending_internal_subagent_result(message) != first_is_internal_subagent_result:
            break
        if not _pending_input_ready(message, msg_map):
            break
        batch.append(message)
    return batch


async def process_pending_chat_inputs(request, chat_id: str, user_id: str, workspace: str):
    """Start the next task from user-queued prompts or internal subagent results.

    Uses a per-chat lock to prevent concurrent processing from
    both the task's finally block and the API double-check.
    """
    lock = get_pending_input_lock(chat_id)
    async with lock:
        while True:
            all_msgs = await ChatMessage.get_all_by_chat(chat_id)

            input_batch = _first_ready_pending_input_batch(all_msgs)
            if not input_batch:
                return

            combined_content = "\n\n".join(m.content for m in input_batch if m.content)
            combined_meta = _merge_pending_input_meta(input_batch)

            chat = await Chat.get_by_id(chat_id)
            if not chat:
                return

            parent_id = input_batch[0].parent_id

            for m in input_batch:
                await ChatMessage.delete(m.id)

            combined_msg = await ChatMessage.create(
                chat_id=chat_id,
                role="user",
                content=combined_content,
                parent_id=parent_id,
                meta=combined_meta,
                created_at=now_ms(),
            )

            # Resolve model from the queued input, then the chat's last used model.
            model_id = input_batch[0].model or (chat.meta or {}).get("last_model", "")
            if not model_id:
                # Fall back to the model from the last assistant message
                done_assistants = [m for m in all_msgs if m.role == "assistant" and m.done]
                last_asst = done_assistants[-1] if done_assistants else None
                model_id = (last_asst.model if last_asst else "") or ""
            if not model_id:
                logger.error(
                    "[chat-input] No model found for chat %s, cannot process pending input",
                    chat_id,
                )
                return

            # Resolve model target
            try:
                from cptr.utils.model_targets import resolve_model_target

                target = await resolve_model_target(model_id)
            except Exception:
                logger.exception("[chat-input] Failed to resolve model target for %s", model_id)
                return

            # Create assistant placeholder
            assistant_msg = await ChatMessage.create(
                chat_id=chat_id,
                role="assistant",
                content="",
                parent_id=combined_msg.id,
                model=model_id,
                done=False,
                created_at=now_ms(),
            )
            await Chat.update_current_message(chat_id, assistant_msg.id, now_ms())

            # Notify frontend that pending inputs became transcript messages.
            await emit_to_user(
                user_id,
                {
                    "chat_id": chat_id,
                    "message_id": assistant_msg.id,
                    "pending_inputs_processed": True,
                },
            )

            # Start new task and continue draining other ready branch batches.
            start_task(
                request,
                message_id=assistant_msg.id,
                chat_id=chat_id,
                user_id=user_id,
                workspace=workspace,
                target=target,
            )
            logger.info(
                "[chat-input] Processed %d pending input message(s) for chat %s",
                len(input_batch),
                chat_id[:8],
            )


async def reconcile_chat_state():
    """Recover from server crash: fix stuck messages and resume pending inputs.

    Called once on startup when ENABLE_CHAT_RECONCILE_ON_STARTUP=true (default).
    Finds:
      1. Assistant messages with done=False that have no running task → mark done
      2. Chats with pending user prompts or subagent results → process them
    """
    from sqlalchemy import select, and_
    from cptr.utils.db import get_db

    async with await get_db() as db:
        result = await db.execute(
            select(ChatMessage).where(
                and_(
                    ChatMessage.role == "assistant",
                    ChatMessage.done == False,  # noqa: E712
                )
            )
        )
        stuck = list(result.scalars().all())

    healed_chats: set[str] = set()
    for msg in stuck:
        if not is_running(msg.id):
            logger.warning("[reconcile] Marking stuck message %s as done", msg.id)
            meta = dict(msg.meta or {})
            meta["error"] = "interrupted by server restart"
            await ChatMessage.update(msg.id, done=True, meta=meta)
            healed_chats.add(msg.chat_id)

    # Resume pending inputs for healed chats.
    for cid in healed_chats:
        chat = await Chat.get_by_id(cid)
        if chat:
            workspace = (chat.meta or {}).get("workspace", "")
            try:
                from cptr.utils.identity import internal_request_for_user

                request = await internal_request_for_user(None, chat.user_id)
                await process_pending_chat_inputs(request, cid, chat.user_id, workspace)
            except Exception:
                logger.exception("[reconcile] Failed to process pending inputs for chat %s", cid)

    if healed_chats:
        logger.info("[reconcile] Recovered %d chat(s) on startup", len(healed_chats))


VOICE_MODE_SYSTEM_PROMPT = (
    "You are in voice mode. Keep responses brief, conversational, and easy to hear aloud. "
    "Prefer one or two short paragraphs. Ask at most one focused follow-up question when needed. "
    "Avoid long lists, code blocks, tables, and verbose explanations unless the user explicitly asks."
)


async def _apply_voice_mode_system_prompt(system: str, chat_params: dict) -> str:
    if chat_params.get("voice_mode") is not True:
        return system
    prompt = str(
        (await Config.get("audio.voice_mode_system_prompt")) or VOICE_MODE_SYSTEM_PROMPT
    ).strip()
    if not prompt:
        return system
    return f"{system}\n\n[VOICE MODE]\n{prompt}"


# ── Title generation ────────────────────────────────────────


TITLE_PROMPT = (
    "Generate a concise title (max 8 words) for this conversation based on the user's message. "
    'Respond with ONLY a JSON object: {"title": "Your Title Here"}. '
    "Do not include quotes around the JSON. Do not explain."
)

TOOL_APPROVAL_REVIEW_PROMPT = (
    "Decide whether Computer may run the proposed tool call without asking the user. "
    'Return ONLY JSON: {"decision":"approve" or "deny","reason":"short reason"}. '
    "Approve when the call is a reasonable next step for the user's request and the arguments "
    "are not destructive, surprising, credential-seeking, or unrelated. Approve harmless "
    "read-only shell commands such as checking the current time/date, current directory, file "
    "listings, or git status when they directly answer the request. Deny when uncertain."
)


async def review_tool_approval(
    *,
    messages: list[dict],
    assistant_text: str,
    connection: dict,
    model: str,
    tool_name: str,
    arguments: dict,
    approval_policy: str = "review",
) -> bool:
    """Return True when Auto mode can run a pending tool without prompting."""
    latest_user = ""
    for message in reversed(messages):
        if message.get("role") == "user":
            latest_user = _plain_message_text(message.get("content"))
            break
    args_text = json.dumps(arguments, ensure_ascii=False, default=str)
    if len(args_text) > 4000:
        args_text = args_text[:3500] + "\n...(truncated)"
    from cptr.utils.utility_models import configured_utility_model

    configured_model = await configured_utility_model("tool_approval_review")
    logger.info(
        "[tool-approval] auto review start tool=%s policy=%s active_model=%s review_model=%s args=%s",
        tool_name,
        approval_policy,
        model,
        configured_model or "<current>",
        args_text[:1000],
    )
    parsed = await generate_json(
        None,
        model_id=configured_model,
        active_connection=connection,
        active_model=model,
        messages=[
            {
                "role": "user",
                "content": (
                    f"Latest user request:\n{latest_user[:2000]}\n\n"
                    f"Assistant text before tool call:\n{assistant_text[-2000:]}\n\n"
                    f"Tool: {tool_name}\nArguments:\n{args_text}"
                ),
            }
        ],
        system=TOOL_APPROVAL_REVIEW_PROMPT,
        max_tokens=150,
        timeout_seconds=20,
    )
    decision = str((parsed or {}).get("decision") or "").strip().lower()
    reason = str((parsed or {}).get("reason") or "").strip()
    approved = decision == "approve"
    logger.info(
        "[tool-approval] auto review decision tool=%s decision=%s approved=%s reason=%s raw=%s",
        tool_name,
        decision or "<invalid>",
        approved,
        reason[:500],
        parsed,
    )
    return approved


async def generate_chat_title(
    chat_id: str,
    user_id: str,
    connection: dict,
    model: str,
    user_message: str,
):
    """Generate a proper title for a new chat via a lightweight LLM call.

    Uses the configured title model, inheriting the active/default model when unset.
    On success, updates the DB and emits a socket event so the frontend
    updates the tab label in real time.
    """
    title_generation_enabled = await Config.get("chat.title_generation.enabled")
    if title_generation_enabled is False or title_generation_enabled == "false":
        return

    # Truncate very long messages to keep the prompt small
    truncated = user_message[:200].strip()
    if len(user_message) > 200:
        truncated += "..."

    try:
        from cptr.utils.utility_models import configured_utility_model

        parsed = await generate_json(
            None,
            model_id=await configured_utility_model("title_generation"),
            active_connection=connection,
            active_model=model,
            messages=[{"role": "user", "content": truncated}],
            system=TITLE_PROMPT,
            max_tokens=50,
        )
        title = (parsed.get("title", "") if isinstance(parsed, dict) else "").strip()
        if not title:
            return

        # Do not overwrite a title manually set while this request was running.
        chat = await Chat.get_by_id(chat_id)
        fallback = user_message[:50].strip() or "New Chat"
        if not chat or chat.title != fallback:
            return

        # Persist and notify
        await Chat.update_title(chat_id, title, now_ms())
        await emit_to_user(user_id, {"chat_id": chat_id, "title": title})
        logger.info("[title] Generated title for chat %s: %s", chat_id[:8], title)

    except Exception:
        logger.debug("[title] Failed to generate title for chat %s", chat_id[:8], exc_info=True)


# ── Message history ─────────────────────────────────────────


def _output_items_to_messages(
    output_items: list[dict], message_id: str | None = None
) -> list[dict]:
    """Convert ordered persisted output items into model-visible messages."""
    native_agent_call_ids = {
        item["call_id"]
        for item in output_items
        if item.get("type") == "function_call"
        and item.get("call_id")
        and _is_native_agent_tool_item(item)
    }
    output_call_ids = {
        item["call_id"]
        for item in output_items
        if item.get("type") == "function_call_output"
        and item.get("call_id") not in native_agent_call_ids
    }
    messages: list[dict] = []
    pending_content: list[str] = []
    pending_reasoning_items: list[dict] = []
    pending_tool_calls: list[dict] = []
    call_names: dict[str, str] = {}

    def flush_pending() -> None:
        nonlocal pending_content, pending_reasoning_items, pending_tool_calls
        if not pending_content and not pending_reasoning_items and not pending_tool_calls:
            return
        assistant_msg: dict = {
            "role": "assistant",
            "content": "".join(pending_content),
        }
        if message_id:
            assistant_msg["id"] = message_id
        if pending_tool_calls:
            assistant_msg["tool_calls"] = pending_tool_calls
        if pending_reasoning_items:
            assistant_msg["reasoning_items"] = pending_reasoning_items
        messages.append(assistant_msg)
        pending_content = []
        pending_reasoning_items = []
        pending_tool_calls = []

    for item in output_items:
        itype = item.get("type")
        if itype == "message":
            text = "".join(
                block.get("text") or ""
                for block in item.get("content") or []
                if isinstance(block, dict) and block.get("type") in ("text", "output_text")
            )
            if text:
                pending_content.append(text)
        elif itype == "reasoning":
            if (
                item.get("status") not in (None, "completed")
                or str(item.get("id", "")).startswith("reasoning-")
                or (
                    not item.get("encrypted_content")
                    and not item.get("reasoning_details")
                    and _reasoning_text_len(item) <= 0
                )
            ):
                continue
            pending_reasoning_items.append(item)
        elif itype == "function_call" and item.get("status") in {
            "completed",
            "failed",
            "rejected",
        }:
            call_id = item.get("call_id")
            if not call_id or call_id in native_agent_call_ids:
                continue
            if call_id not in output_call_ids:
                logger.warning(
                    "[history] Skipping orphaned function_call %s (%s) — no matching output",
                    call_id,
                    item.get("name", "?"),
                )
                continue
            arguments = item.get("arguments", {})
            if not isinstance(arguments, str):
                arguments = json.dumps(arguments)
            tc = {
                "id": call_id,
                "type": "function",
                "function": {
                    "name": item.get("name", ""),
                    "arguments": arguments,
                },
            }
            if item.get("fc_id"):
                tc["fc_id"] = item["fc_id"]
            call_names[call_id] = item.get("name", "")
            pending_tool_calls.append(tc)
        elif itype == "function_call_output" and item.get("call_id") not in native_agent_call_ids:
            call_id = item.get("call_id")
            if call_id not in call_names:
                continue
            flush_pending()
            tool_msg = {
                "role": "tool",
                "tool_call_id": call_id,
                "content": _tool_result_for_model(
                    call_names.get(call_id, ""),
                    item.get("output", ""),
                ),
            }
            if message_id:
                tool_msg["id"] = message_id
            messages.append(tool_msg)

    flush_pending()
    return messages


async def _load_message_history(chat_id: str, message_id: str) -> tuple[list[dict], str | None]:
    """Load the ancestor chain from message_id to root as LLM messages.

    Walks up via parent_id so only the active branch is included.
    The current message (message_id) is always included even if done=False,
    since it may contain completed tool calls from prior approval rounds.

    If any message in the chain has a chat_summary, everything before it
    is skipped and the summary is returned separately for the system prompt.

    Returns (messages, chat_summary_or_None).
    """
    all_msgs = await ChatMessage.get_all_by_chat(chat_id)
    msg_map = {m.id: m for m in all_msgs}

    # Trace from message_id up to root
    chain: list = []
    cur = msg_map.get(message_id)
    while cur:
        chain.append(cur)
        cur = msg_map.get(cur.parent_id) if cur.parent_id else None
    chain.reverse()  # root → leaf

    # Find the most recent message with a chat_summary
    existing_summary = None
    for i, m in enumerate(chain):
        if m.chat_summary:
            chain = chain[i:]  # keep this message and everything after
            existing_summary = m.chat_summary
            break

    result = []
    for m in chain:
        # Skip in-progress assistant placeholders, but NOT the current
        # message being continued, which may have accumulated tool call
        # results from prior approval rounds that the LLM needs to see.
        if m.role == "assistant" and not m.done and m.id != message_id:
            continue
        # For the current message, skip if it has no content and no output
        # (truly empty placeholder on first run)
        if m.id == message_id and not m.done and not m.content and not m.output:
            continue
        entry: dict = {"id": m.id, "role": m.role, "content": m.content or ""}

        # Transform uploaded images into base64 multimodal blocks; inline text files
        if m.role == "user":
            attached_files = (m.meta or {}).get("files", [])
            images = [
                f
                for f in attached_files
                if isinstance(f, dict)
                and (f.get("type") == "image" or (f.get("content_type") or "").startswith("image/"))
            ]
            non_images = [f for f in attached_files if isinstance(f, dict) and f not in images]

            if images or non_images:
                from cptr.utils.storage import get_storage
                import base64

                text_content = entry["content"]

                # Append file:// references so the AI can read them with read_file
                if non_images:
                    from cptr.utils.storage import UPLOADS_DIR

                    file_refs = []
                    for f in non_images:
                        file_id = f.get("id")
                        if not file_id:
                            continue
                        name = f.get("name", "file")
                        file_path = UPLOADS_DIR / file_id
                        file_refs.append(f"[{name}](file://{file_path})")
                    if file_refs:
                        text_content += "\n\nAttached files:\n" + "\n".join(file_refs)

                content_blocks = [{"type": "text", "text": text_content}] if text_content else []

                for img in images:
                    file_id = img.get("id")
                    if not file_id:
                        continue
                    data = await get_storage().get(file_id)
                    if data:
                        b64_str = base64.b64encode(data).decode("utf-8")
                        ctype = img.get("content_type") or "image/png"
                        content_blocks.append(
                            {"type": "image", "media_type": ctype, "base64": b64_str}
                        )

                if len(content_blocks) > (1 if text_content else 0):
                    entry["content"] = content_blocks
                elif text_content != entry["content"]:
                    entry["content"] = text_content

        # Reconstruct model-visible messages from the ordered output stream.
        # `m.output` is the source of truth: assistant text/reasoning,
        # function_call, function_call_output, then possibly more text.
        if m.output:
            output_messages = _output_items_to_messages(m.output, m.id)
            if output_messages:
                result.extend(output_messages)
                continue

        result.append(entry)

    # ── Final sanitization: ensure every tool_call has a matching tool result ──
    # This catches edge cases from compaction, DB corruption, or partial persistence.
    result = _sanitize_tool_pairs(result)

    return result, existing_summary


def _sanitize_tool_pairs(messages: list[dict]) -> list[dict]:
    """Ensure every tool_call in an assistant message has a matching tool result.

    Walks the message list and collects all tool result call_ids.  Then
    strips any tool_call entries from assistant messages that have no
    matching result.  Also removes orphaned tool-result messages.

    This is the last line of defence against 400 errors from providers
    that require strict tool_call ↔ tool_result pairing (OpenAI).
    """
    # Collect all tool-result call_ids in the conversation
    tool_result_ids = {
        m["tool_call_id"] for m in messages if m.get("role") == "tool" and m.get("tool_call_id")
    }

    # Collect all tool_call ids declared by assistant messages
    tool_call_ids = set()
    for m in messages:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"]:
                tool_call_ids.add(tc.get("id", ""))

    sanitized = []
    for m in messages:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            # Keep only tool_calls that have a matching tool result
            kept = [tc for tc in m["tool_calls"] if tc.get("id") in tool_result_ids]
            dropped = len(m["tool_calls"]) - len(kept)
            if dropped:
                logger.warning(
                    "[sanitize] Dropped %d orphaned tool_call(s) from assistant message",
                    dropped,
                )
            if kept:
                m = dict(m)
                m["tool_calls"] = kept
                sanitized.append(m)
            else:
                # No tool_calls left — keep as plain text if there's content
                m = dict(m)
                del m["tool_calls"]
                m.pop("reasoning_items", None)
                if m.get("content"):
                    sanitized.append(m)
                # else: drop empty assistant message entirely
        elif m.get("role") == "tool":
            # Drop tool results that have no matching tool_call
            if m.get("tool_call_id") not in tool_call_ids:
                logger.warning(
                    "[sanitize] Dropped orphaned tool result for call_id=%s",
                    m.get("tool_call_id", "?"),
                )
                continue
            sanitized.append(m)
        else:
            sanitized.append(m)

    return sanitized


def _parse_image_data_uri(result: str) -> tuple[str, str] | None:
    """Check if a tool result is a data URI image (from read_file on image files).

    Returns (media_type, base64_data) if it's a data URI image, else None.
    """
    if not result.startswith("data:image/"):
        return None
    # data:image/png;base64,iVBOR...
    try:
        header, b64_data = result.split(",", 1)
        media_type = header.split(";")[0].replace("data:", "")
        return media_type, b64_data
    except (ValueError, IndexError):
        return None


def _tool_result_for_model(tool_name: str, result: str) -> str:
    """Return the tool result text to send back to the model."""
    if tool_name != "image_generate":
        return result

    try:
        meta = json.loads(result)
    except (json.JSONDecodeError, TypeError):
        return result

    images = meta.get("images")
    if not isinstance(images, list):
        return result

    image_files = [
        {
            "path": image.get("path"),
            "name": image.get("name"),
            "content_type": image.get("content_type"),
        }
        for image in images
        if isinstance(image, dict) and image.get("path")
    ]

    return json.dumps(
        {
            "status": meta.get("status", "success"),
            "displayed_to_user": False,
            "must_call": "display_file",
            "instruction": (
                "Do not answer the user yet. The image file is saved but not rendered. "
                "Call display_file once for each path below."
            ),
            "display_file_calls": [
                {
                    "name": "display_file",
                    "arguments": {"path": image["path"]},
                }
                for image in image_files
            ],
            "images": image_files,
        }
    )


def _scrub_incomplete_items(output_items: list[dict]) -> None:
    """Mark any in-progress function_call items as 'failed'.

    Called before persisting output_items after an error or cancellation.
    Without this, "in_progress" items stay in the DB.  While
    _load_message_history skips non-"completed" items today, marking
    them explicitly prevents any future code from misinterpreting them.
    """
    for item in output_items:
        if item.get("type") == "function_call" and item.get("status") in (
            "in_progress",
            "pending",
            "queued",
        ):
            item["status"] = "failed"


def _reasoning_text_len(item: dict) -> int:
    """Return displayable reasoning text length for diagnostics."""
    total = 0
    for blocks in (item.get("summary"), item.get("content")):
        if isinstance(blocks, str):
            total += len(blocks)
        elif isinstance(blocks, list):
            total += sum(
                len(block.get("text") or "")
                for block in blocks
                if isinstance(block, dict)
                and block.get("type") in ("text", "output_text", "summary_text")
            )
    return total


def _output_debug_stats(output_items: list[dict] | None) -> tuple[dict[str, int], int, int]:
    """Return output type counts plus reasoning count/text length."""
    counts: dict[str, int] = {}
    reasoning_count = 0
    reasoning_chars = 0
    for item in output_items or []:
        itype = item.get("type", "?")
        counts[itype] = counts.get(itype, 0) + 1
        if itype == "reasoning":
            reasoning_count += 1
            reasoning_chars += _reasoning_text_len(item)
    return counts, reasoning_count, reasoning_chars


def _output_item_key(item: dict) -> tuple | None:
    """Stable key for Responses-style output item updates."""
    if item.get("id"):
        return ("id", item["id"])
    if item.get("type") and item.get("call_id"):
        return ("call", item["type"], item["call_id"])
    return None


def _upsert_output_item(items: list[dict], item: dict) -> bool:
    """Insert or replace a Responses-style output item by id/call_id."""
    key = _output_item_key(item)
    if key is not None:
        for idx, existing in enumerate(items):
            if _output_item_key(existing) == key:
                items[idx] = item
                return False
    items.append(item)
    return True


def _safe_tool_name(name: str | None) -> str:
    if not name:
        return "agent_tool"
    value = re.sub(r"[^a-zA-Z0-9_-]+", "_", name.strip()).strip("_")
    return value or "agent_tool"


def _is_native_agent_tool_item(item: dict) -> bool:
    return bool(
        item.get("native_agent")
        or (isinstance(item.get("id"), str) and item["id"].startswith("agent-"))
    )


def _append_capped_output(existing: str, delta: str, max_chars: int) -> str:
    text = f"{existing or ''}{delta}"
    if len(text) <= max_chars:
        return text
    marker = "\n\n...(truncated native agent output)...\n\n"
    if max_chars <= len(marker) + 2:
        return text[:max_chars]
    keep = max_chars - len(marker)
    head = keep // 2
    tail = keep - head
    return f"{text[:head]}{marker}{text[-tail:]}"


def _append_prompt_suffix(messages: list[dict], suffix: str) -> None:
    if not suffix:
        return
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content", "")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    block["text"] = f"{block.get('text') or ''}{suffix}"
                    return
            content.append({"type": "text", "text": suffix.strip()})
        else:
            message["content"] = f"{content or ''}{suffix}"
        return


def _find_safe_split(messages: list[dict], target_keep: int) -> int:
    """Find a safe split index that doesn't break tool call pairs.

    Returns the index where keep_zone starts. Ensures:
    - Prefer starting compacted history on a user turn
    - Never splits between an assistant tool_call and its tool result
    - keep_zone doesn't start with a tool result message
    - At least 2 messages are kept
    """
    n = len(messages)
    split = min(max(2, n - target_keep), max(0, n - 2))

    for idx in range(split, n - 1):
        if messages[idx].get("role") == "user":
            return idx

    # Don't start keep_zone with a tool result; keep its assistant call too.
    while split > 0 and messages[split].get("role") == "tool":
        split -= 1

    return split


def _summary_checkpoint_message_id(keep_zone: list[dict], fallback: str) -> str:
    for message in keep_zone:
        if message.get("role") == "user" and message.get("id"):
            return message["id"]
    for message in keep_zone:
        if message.get("id"):
            return message["id"]
    return fallback


# ── Connection resolution ───────────────────────────────────


def build_artifact_item(tool_name: str, arguments: dict, result: str) -> dict | None:
    """Build an artifact output item if the tool call produced an artifact.

    Works for both create_artifact and create_file with artifact_type.
    Returns None if no artifact card should be shown.
    """
    artifact_type = arguments.get("artifact_type", "")
    if tool_name == "create_artifact":
        artifact_type = artifact_type or "implementation_plan"
    if not artifact_type:
        return None

    try:
        meta = json.loads(result)
    except (json.JSONDecodeError, TypeError):
        meta = {}

    return {
        "type": "artifact",
        "artifact_type": artifact_type,
        "title": meta.get("title")
        or arguments.get("title")
        or artifact_type.replace("_", " ").title(),
        "content": arguments.get("content", ""),
        "path": meta.get("path") or arguments.get("path", ""),
    }


def _default_base_url(provider: str) -> str:
    return {
        "anthropic": "https://api.anthropic.com/v1",
        "openai": "https://api.openai.com/v1",
    }.get(provider, "https://api.openai.com/v1")


# ── The agentic loop ────────────────────────────────────────


async def run_chat_task(
    request,
    *,
    message_id: str,
    chat_id: str,
    user_id: str,
    target: ModelTarget,
    workspace: str,
    regeneration_prompt: str | None = None,
    output_queue: asyncio.Queue | None = None,
):
    """Plain async function. Makes raw API calls in a loop."""
    if request is None:
        try:
            from cptr.app import app as cptr_app
            from cptr.utils.identity import internal_request_for_user

            request = await internal_request_for_user(cptr_app, user_id)
        except Exception:
            logger.debug(
                "[task %s] internal request creation failed", message_id[:8], exc_info=True
            )

    async def emit(**data):
        """Stream an output delta to the user."""
        try:
            await emit_to_user(user_id, {"chat_id": chat_id, "message_id": message_id, **data})
        except Exception:
            # Socket failure must not prevent the queue push below,
            # otherwise the gateway SSE stream hangs forever.
            logger.debug("[task %s] emit_to_user failed", message_id[:8], exc_info=True)
        # Push to gateway queue if present
        if output_queue is not None:
            if "delta" in data:
                await output_queue.put({"type": "delta", "content": data["delta"]})
            elif "output" in data:
                await output_queue.put({"type": "output", "item": data["output"]})
            elif data.get("done"):
                if "error" in data:
                    await output_queue.put({"type": "error", "message": data["error"]})
                else:
                    await output_queue.put({"type": "done", "finish_reason": "stop"})

    async def _emit_done():
        """Emit done=True enriched with chat title and content preview."""
        try:
            chat_obj = await Chat.get_by_id(chat_id)
            title = chat_obj.title if chat_obj else "Chat"
        except Exception:
            title = "Chat"
        preview = content[:300] if content else ""
        ws_name = workspace.rstrip("/").rsplit("/", 1)[-1] if workspace else ""
        try:
            await clear_active_tasks(chat_id, user_id, message_id)
        except Exception:
            logger.debug("[task %s] clear_active_tasks failed", message_id[:8], exc_info=True)
        await emit(
            done=True,
            title=title,
            content=preview,
            workspace=workspace,
            workspace_name=ws_name,
        )

    event_workspace = (
        {"id": workspace, "name": workspace.rstrip("/").rsplit("/", 1)[-1]} if workspace else None
    )

    # Load existing state so continuations don't overwrite previous output
    msg = await ChatMessage.get_by_id(message_id)
    summary_message_id = message_id
    if msg and msg.parent_id:
        parent_msg = await ChatMessage.get_by_id(msg.parent_id)
        if parent_msg and parent_msg.role == "user":
            summary_message_id = parent_msg.id
    content = (msg.content or "") if msg else ""
    output_items: list[dict] = list(msg.output or []) if msg else []
    text_buffer = ""  # Accumulates text between tool calls
    task_completed_success = False
    review_messages: list[dict] = []
    review_model_connection: dict | None = None
    review_model_name = ""
    review_chat_params: dict = {}
    review_builtin_tools: dict | None = None
    loaded_skill_names: set[str] = set()
    tool_names_used: set[str] = set()
    skill_create_requested = False
    skill_authoring_allowed = False

    logger.info(
        "[task %s] start: existing content=%d chars, output=%d items",
        message_id[:8],
        len(content),
        len(output_items),
    )

    def _flush_text() -> dict | None:
        """Flush accumulated text into a message output item."""
        nonlocal text_buffer
        if not text_buffer:
            return None
        if not text_buffer.strip():
            text_buffer = ""
            return None
        logger.info(
            "[task %s] flush_text: %d chars into message item", message_id[:8], len(text_buffer)
        )
        item = {
            "type": "message",
            "id": str(uuid.uuid4()),
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text_buffer}],
        }
        output_items.append(item)
        text_buffer = ""
        _sync_state()
        return item

    def _sync_state():
        """Update in-memory state so API can serve it on refresh."""
        _task_state[message_id] = {"content": content, "output": output_items}

    async def _save_message(save_reason: str, **kwargs) -> bool:
        """Persist a message update and log enough detail to debug skipped saves."""
        saved_output = kwargs.get("output", output_items)
        saved_content = kwargs.get("content", content)
        counts, reasoning_count, reasoning_chars = _output_debug_stats(saved_output)
        logger.info(
            "[task %s] db save (%s) begin: done=%s content=%d chars output=%d items reasoning=%d items/%d chars types=%s",
            message_id[:8],
            save_reason,
            kwargs.get("done", "<unchanged>"),
            len(saved_content or ""),
            len(saved_output or []),
            reasoning_count,
            reasoning_chars,
            counts,
        )
        saved = await ChatMessage.update(message_id, **kwargs)
        log = logger.info if saved else logger.warning
        log("[task %s] db save (%s) result: updated=%s", message_id[:8], save_reason, saved)
        return saved

    async def save_session(agent_target: AgentModelTarget, resume_state: dict | None):
        if not resume_state:
            return
        chat = await Chat.get_by_id(chat_id)
        if not chat:
            return
        meta = dict(chat.meta or {})
        sessions = dict(meta.get("agent_sessions") or {})
        sessions[agent_target.profile_id] = {
            **resume_state,
            "profile_id": agent_target.profile_id,
            "agent": agent_target.agent,
            "model": agent_target.model,
            "workspace": workspace,
            "updated_at": now_ms(),
        }
        meta["agent_sessions"] = sessions
        await Chat.update_meta(chat_id, meta, now_ms())

    async def _run_agent_target(agent_target: AgentModelTarget):
        nonlocal content, text_buffer, task_completed_success, skill_authoring_allowed
        nonlocal skill_create_requested
        from cptr.utils.agents.claude_code import run_claude_code_agent
        from cptr.utils.agents.cline import run_cline_agent
        from cptr.utils.agents.codex import run_codex_agent
        from cptr.utils.agents.cursor import run_cursor_agent
        from cptr.utils.agents.gemini import run_gemini_agent
        from cptr.utils.agents.grok import run_grok_agent
        from cptr.utils.agents.opencode import run_opencode_agent
        from cptr.utils.agents.pi import run_pi_agent

        chat_obj = await Chat.get_by_id(chat_id)
        identity = await identity_for_context({"request": request, "user_id": user_id})
        chat_params = (chat_obj.meta or {}).get("params", {}) if chat_obj else {}
        agent_workspace = workspace or str(Path.home())
        messages, loaded_summary = await _load_message_history(chat_id, message_id)
        skill_settings = await get_skill_settings()
        skill_authoring_allowed = _has_prior_real_chat_content(messages, loaded_summary)
        skill_create_denial_reason = "empty_chat"
        if not skill_settings["enabled"] or not skill_settings["tool_enabled"]:
            skill_authoring_allowed = False
            skill_create_denial_reason = "disabled"
        skill_create_requested = _apply_skills_create_prompt(
            messages, allowed=skill_authoring_allowed, denial_reason=skill_create_denial_reason
        )
        memory_message, memory_files = _memory_recall_inputs(messages, regeneration_prompt)
        system = await _load_system_prompt(
            request,
            agent_workspace,
            agent_target.full_model_id,
            user_id=user_id,
            current_message=memory_message,
            recent_messages=messages,
            mentioned_files=memory_files,
        )
        if loaded_summary:
            system += f"\n\n[CONVERSATION SUMMARY]\n{loaded_summary}"
        current_user_files = []
        if msg and msg.parent_id:
            parent_msg = await ChatMessage.get_by_id(msg.parent_id)
            meta_files = (parent_msg.meta or {}).get("files") if parent_msg else None
            if isinstance(meta_files, list):
                current_user_files = meta_files
        agent_attachments = await prepare_agent_attachments(
            request,
            workspace=agent_workspace,
            user_id=user_id,
            chat_id=chat_id,
            message_id=(msg.parent_id if msg and msg.parent_id else message_id),
            files=current_user_files,
        )
        _append_prompt_suffix(messages, agent_attachments.prompt_suffix)
        if regeneration_prompt:
            messages.append({"role": "user", "content": regeneration_prompt})
        if chat_params.get("plan_mode", False):
            messages.append({"role": "user", "content": PLAN_MODE_PROMPT})
        system = await _apply_voice_mode_system_prompt(system, chat_params)

        resume_state = None
        if chat_obj:
            sessions = (chat_obj.meta or {}).get("agent_sessions") or {}
            if isinstance(sessions, dict):
                candidate = sessions.get(agent_target.profile_id)
                if isinstance(candidate, dict):
                    resume_state = candidate

        runners = {
            "codex": run_codex_agent,
            "claude_code": run_claude_code_agent,
            "cursor": run_cursor_agent,
            "grok": run_grok_agent,
            "opencode": run_opencode_agent,
            "cline": run_cline_agent,
            "gemini": run_gemini_agent,
            "pi": run_pi_agent,
        }
        runner = runners.get(agent_target.agent)
        if runner is None:
            raise RuntimeError(f"Unsupported agent type: {agent_target.agent}")

        def _active_reasoning_item():
            return next(
                (
                    item
                    for item in reversed(output_items)
                    if item.get("type") == "reasoning" and item.get("status") == "in_progress"
                ),
                None,
            )

        async def _finish_reasoning_item():
            existing = _active_reasoning_item()
            if not existing:
                return
            item = {**existing, "status": "completed"}
            _upsert_output_item(output_items, item)
            await emit(output=item)
            _sync_state()

        async for event in runner(
            profile=agent_target.config,
            model=agent_target.model,
            workspace=agent_workspace,
            messages=messages,
            system_prompt=system,
            chat_params=chat_params,
            resume_state=resume_state,
            attachments=agent_attachments,
            identity=identity,
        ):
            if isinstance(event, AgentTextDelta):
                await _finish_reasoning_item()
                content += event.text
                text_buffer += event.text
                await emit(delta=event.text)
                _sync_state()
            elif isinstance(event, AgentReasoningDelta):
                existing = _active_reasoning_item()
                if existing:
                    blocks = existing.get("content") or []
                    text = next(
                        (
                            block.get("text", "")
                            for block in blocks
                            if isinstance(block, dict) and block.get("type") == "reasoning_text"
                        ),
                        "",
                    )
                    item = {
                        **existing,
                        "content": [{"type": "reasoning_text", "text": f"{text}{event.text}"}],
                    }
                else:
                    section = sum(item.get("type") == "reasoning" for item in output_items) + 1
                    item = {
                        "type": "reasoning",
                        "id": f"reasoning-{message_id}-{section}",
                        "status": "in_progress",
                        "content": [{"type": "reasoning_text", "text": event.text}],
                    }
                _upsert_output_item(output_items, item)
                await emit(output=item)
                _sync_state()
            elif isinstance(event, AgentToolUpdate):
                await _finish_reasoning_item()
                flushed_item = _flush_text()
                if flushed_item:
                    await emit(output=flushed_item)
                existing = next(
                    (
                        item
                        for item in output_items
                        if item.get("type") == "function_call"
                        and item.get("call_id") == event.call_id
                    ),
                    {},
                )
                call_item = {
                    "type": "function_call",
                    "id": existing.get("id") or f"agent-{event.call_id}",
                    "call_id": event.call_id,
                    "name": _safe_tool_name(event.name or existing.get("name")),
                    "native_agent": True,
                    "arguments": {
                        **(existing.get("arguments") or {}),
                        **(event.arguments or {}),
                    },
                    "status": event.status,
                }
                _upsert_output_item(output_items, call_item)
                await emit(output=call_item)
                if event.output is not None:
                    capped_event_output = None
                    output_limit = (
                        CHAT_TOOL_COMMAND_MAX_CHARS
                        if call_item["name"] == "run_command"
                        else CHAT_TOOL_MAX_CHARS
                    )
                    capped_event_output = _append_capped_output("", event.output, output_limit)
                    existing_output = next(
                        (
                            item
                            for item in output_items
                            if item.get("type") == "function_call_output"
                            and item.get("call_id") == event.call_id
                        ),
                        None,
                    )
                    if (
                        existing_output
                        and existing_output.get("output")
                        and capped_event_output is not None
                    ):
                        _sync_state()
                        continue
                    output_item = {
                        "type": "function_call_output",
                        "call_id": event.call_id,
                        "native_agent": True,
                        "output": capped_event_output or "",
                    }
                    _upsert_output_item(output_items, output_item)
                    await emit(output=output_item)
                _sync_state()
            elif isinstance(event, AgentToolOutputDelta):
                await _finish_reasoning_item()
                flushed_item = _flush_text()
                if flushed_item:
                    await emit(output=flushed_item)
                existing_call = next(
                    (
                        item
                        for item in output_items
                        if item.get("type") == "function_call"
                        and item.get("call_id") == event.call_id
                    ),
                    None,
                )
                if existing_call is None:
                    existing_call = {
                        "type": "function_call",
                        "id": f"agent-{event.call_id}",
                        "call_id": event.call_id,
                        "name": "run_command"
                        if event.stream_kind == "command_output"
                        else "agent_tool",
                        "native_agent": True,
                        "arguments": {},
                        "status": "in_progress",
                    }
                    _upsert_output_item(output_items, existing_call)
                    await emit(output=existing_call)
                existing_output = next(
                    (
                        item
                        for item in output_items
                        if item.get("type") == "function_call_output"
                        and item.get("call_id") == event.call_id
                    ),
                    {},
                )
                max_chars = (
                    CHAT_TOOL_COMMAND_MAX_CHARS
                    if event.stream_kind == "command_output"
                    else CHAT_TOOL_MAX_CHARS
                )
                output_item = {
                    "type": "function_call_output",
                    "call_id": event.call_id,
                    "native_agent": True,
                    "output": _append_capped_output(
                        "" if event.replace else str(existing_output.get("output") or ""),
                        event.delta,
                        max_chars,
                    ),
                }
                _upsert_output_item(output_items, output_item)
                await emit(output=output_item)
                _sync_state()
            elif isinstance(event, AgentError):
                raise RuntimeError(event.message)
            elif isinstance(event, AgentDone):
                await _finish_reasoning_item()
                flushed_item = _flush_text()
                if flushed_item:
                    await emit(output=flushed_item)
                await save_session(agent_target, event.resume_state)
                await _save_message(
                    "agent done",
                    content=content,
                    output=output_items,
                    usage=event.usage,
                    done=True,
                )
                _task_state.pop(message_id, None)
                await _emit_done()
                preview = content[:300] if content else ""
                await publish_event(
                    EVENTS.CHAT_FINISHED,
                    actor={"id": user_id},
                    subject_id=chat_id,
                    subject_type="chat",
                    source="chat_task",
                    data={"workspace": event_workspace, "preview": preview},
                    message=preview,
                )
                task_completed_success = True
                return

        flushed_item = _flush_text()
        if flushed_item:
            await emit(output=flushed_item)
        await _finish_reasoning_item()
        await _save_message("agent stream ended", content=content, output=output_items, done=True)
        _task_state.pop(message_id, None)
        await _emit_done()
        preview = content[:300] if content else ""
        await publish_event(
            EVENTS.CHAT_FINISHED,
            actor={"id": user_id},
            subject_id=chat_id,
            subject_type="chat",
            source="chat_task",
            data={"workspace": event_workspace, "preview": preview},
            message=preview,
        )
        return

    try:
        if isinstance(target, AgentModelTarget):
            await _run_agent_target(target)
            return

        connection = target.connection
        model = target.runtime_model
        provider = connection["provider"]
        api_key = decrypt_key(connection.get("api_key", ""), _get_jwt_secret())
        base_url = connection.get("base_url") or _default_base_url(provider)
        api_type = connection.get("api_type", "chat_completions")

        chat_obj = await Chat.get_by_id(chat_id)
        chat_params = (chat_obj.meta or {}).get("params", {}) if chat_obj else {}
        configured_model = (msg.model if msg else None) or model
        try:
            chat_models_config = await Config.get("chat.models") or {}
        except Exception:
            chat_models_config = {}
        builtin_tools = resolve_builtin_tools_config(chat_models_config, model, configured_model)
        provider_type = (
            "llama.cpp"
            if provider == "openai" and connection.get("provider_type") == "llama.cpp"
            else "default"
        )
        messages, loaded_summary = await _load_message_history(chat_id, message_id)
        skill_settings = await get_skill_settings()
        skill_authoring_allowed = _has_prior_real_chat_content(messages, loaded_summary)
        skill_create_denial_reason = "empty_chat"
        if not skill_settings["enabled"] or not skill_settings["tool_enabled"]:
            skill_authoring_allowed = False
            skill_create_denial_reason = "disabled"
        skill_create_requested = _apply_skills_create_prompt(
            messages, allowed=skill_authoring_allowed, denial_reason=skill_create_denial_reason
        )
        memory_message, memory_files = _memory_recall_inputs(messages, regeneration_prompt)
        system = await _load_system_prompt(
            request,
            workspace,
            model,
            user_id=user_id,
            current_message=memory_message,
            recent_messages=messages,
            mentioned_files=memory_files,
        )
        if loaded_summary:
            system += f"\n\n[CONVERSATION SUMMARY]\n{loaded_summary}"
        if regeneration_prompt:
            messages.append({"role": "user", "content": regeneration_prompt})
        tools = await get_tool_list(builtin_tools=builtin_tools, workspace=workspace)
        if not skill_authoring_allowed:
            tools = [t for t in tools if t["name"] != "manage_skill"]

        # Remove view_skill tool if no skills are available
        skills = discover_skills(workspace) if skill_settings["enabled"] else []
        if not skills:
            tools = [t for t in tools if t["name"] != "view_skill"]

        # Strip delegate_task from sub-agent chats (depth limit = 1)
        if chat_obj and is_internal_chat(chat_obj.meta):
            tools = [t for t in tools if t["name"] not in {"delegate_task", "update_memory"}]

        # Parse $skill-name mentions from the user message to auto-activate skills
        attached_skill_ids: list[str] = []
        if skills and messages:
            # Find the last user message
            last_user = next((m for m in reversed(messages) if m["role"] == "user"), None)
            if last_user:
                import re as _re

                mentioned = _re.findall(
                    r"\$([a-z0-9](?:[a-z0-9-]*[a-z0-9])?)",
                    _plain_message_text(last_user.get("content")),
                )
                skill_names = {s.name for s in skills}
                attached_skill_ids = [m for m in mentioned if m in skill_names]
        if attached_skill_ids:
            from cptr.utils.tools import _activated_skills

            skill_blocks = []
            for sid in attached_skill_ids:
                skill = load_skill(workspace, sid)
                if skill:
                    skill_blocks.append(format_skill_content(skill))
                    _activated_skills.add(sid)  # mark as activated for dedup
                    loaded_skill_names.add(sid)
                    bump_skill_view(workspace, sid, skill.source)
            if skill_blocks:
                system += "\n\n" + "\n\n".join(skill_blocks)

        system = await _apply_voice_mode_system_prompt(system, chat_params)

        # Plan mode: strip write tools, inject prompt as user message (not system, to preserve cache)
        plan_mode = chat_params.get("plan_mode", False)
        if plan_mode:
            tools = [t for t in tools if await resolve_builtin_tool_approval(t["name"]) == "allow"]
            tools = [t for t in tools if t["name"] not in {"delegate_task", "update_memory"}]
            tools.append(_fn_to_schema("create_artifact", create_artifact))
            tools.append(ASK_USER_SCHEMA)
            messages.append({"role": "user", "content": PLAN_MODE_PROMPT})
            logger.info(
                "[task %s] plan mode active, %d tools available", message_id[:8], len(tools)
            )
        review_messages = messages
        review_model_connection = connection
        review_model_name = model
        review_chat_params = {
            **chat_params,
            "subagent": bool(
                chat_obj
                and (
                    (chat_obj.meta or {}).get("subagent")
                    or (
                        (chat_obj.meta or {}).get("internal") is True
                        and (chat_obj.meta or {}).get("type") == "subagent"
                    )
                )
            ),
        }
        review_builtin_tools = builtin_tools

        async def run_queued_tool_calls(tool_ctx: dict) -> str:
            """Run queued tool calls until approval is needed or the queue is empty.

            Returns: "idle", "completed", or "approval_required".
            """
            processed_any = False
            for item in output_items:
                if item.get("type") != "function_call":
                    continue
                if item.get("name") == ASK_USER_NAME or _is_native_agent_tool_item(item):
                    continue
                if item.get("status") in {"completed", "rejected", "failed"}:
                    continue
                call_id = item.get("call_id")
                existing_result = next(
                    (
                        result
                        for result in output_items
                        if result.get("type") == "function_call_output"
                        and result.get("call_id") == call_id
                    ),
                    None,
                )
                if call_id and existing_result:
                    item["status"] = existing_result.get("status") or (
                        "failed"
                        if _is_tool_result_error(existing_result.get("output", ""))
                        else "completed"
                    )
                    continue

                name = item.get("name", "")
                tool = ALL_TOOLS.get(name)
                tool_approval = await resolve_builtin_tool_approval(name) if tool else "review"
                current_chat = await Chat.get_by_id(chat_id)
                current_params = (
                    (current_chat.meta or {}).get("params", {}) if current_chat else chat_params
                )
                current_params = current_params if isinstance(current_params, dict) else {}
                approval_mode = current_params.get("tool_approval_mode")
                if approval_mode not in {"ask", "auto", "full"}:
                    approval_mode = (
                        "full"
                        if "tool_approval_mode" not in current_params
                        and current_params.get("auto_approve_tools")
                        else "auto"
                    )
                should_auto = approval_mode == "full" or (
                    approval_mode == "auto" and tool and tool_approval == "allow"
                )
                if (
                    not should_auto
                    and approval_mode == "auto"
                    and not item.get("approved")
                    and tool
                    and await review_tool_approval(
                        messages=messages,
                        assistant_text=content,
                        connection=connection,
                        model=model,
                        tool_name=name,
                        arguments=item.get("arguments") or {},
                        approval_policy=tool_approval,
                    )
                ):
                    item["approved"] = True
                    should_auto = True
                needs_approval = not should_auto
                if needs_approval and not item.get("approved"):
                    item["status"] = "pending"
                    await _save_message(
                        "pending approval",
                        content=content,
                        output=output_items,
                        done=False,
                    )
                    await emit(output=item)
                    _task_state.pop(message_id, None)
                    await emit(
                        done=True,
                        status="approval_required",
                        title=chat_obj.title if chat_obj else None,
                        tool_name=name,
                        workspace=workspace,
                        workspace_name=workspace.rstrip("/").rsplit("/", 1)[-1]
                        if workspace
                        else "",
                    )
                    return "approval_required"

                item["status"] = "in_progress"
                await emit(output=item)
                _sync_state()
                await _save_message("tool call in progress", content=content, output=output_items)

                arguments = item.get("arguments") or {}
                if name == "create_artifact":
                    args = dict(arguments)
                    args.pop("workspace", None)
                    result = await create_artifact(**args, workspace=workspace)
                else:
                    result = await execute_tool(
                        name,
                        arguments,
                        {**tool_ctx, "call_id": item["call_id"]},
                    )

                result_item = {
                    "type": "function_call_output",
                    "call_id": item["call_id"],
                    "output": result,
                    "status": "failed" if _is_tool_result_error(result) else "completed",
                }
                output_items.append(result_item)
                item["status"] = result_item["status"]
                await emit(output=item)
                await emit(output=result_item)
                _sync_state()

                artifact_item = build_artifact_item(name, arguments, result)
                if artifact_item:
                    output_items.append(artifact_item)
                    await emit(output=artifact_item)
                    _sync_state()

                if name == "display_file":
                    try:
                        file_item = json.loads(result)
                    except (json.JSONDecodeError, TypeError):
                        file_item = None
                    if isinstance(file_item, dict) and file_item.get("type") == "file":
                        output_items.append(file_item)
                        await emit(output=file_item)
                        _sync_state()

                await _save_message("tool call complete", content=content, output=output_items)
                processed_any = True

            return "completed" if processed_any else "idle"

        tool_ctx = {
            "workspace": workspace,
            "user_id": user_id,
            "request": request,
            "model_id": model,
            "full_model_id": ((chat_obj.meta or {}).get("last_model") if chat_obj else None)
            or model,
            "chat_id": chat_id,
            "message_id": message_id,
            "connection": connection,
            "builtin_tools": builtin_tools,
        }

        resumed_calls = await run_queued_tool_calls(tool_ctx)
        if resumed_calls == "approval_required":
            return
        if resumed_calls == "completed":
            messages, loaded_summary = await _load_message_history(chat_id, message_id)

        last_usage: dict | None = None  # real usage from last API call
        new_messages_since: int = 0  # messages appended since last API call

        # Request params: arbitrary key-value pairs merged into the API request body
        # Merge order: global ("*") → per-model → chat overrides (chat wins)
        chat_request_params = chat_params.get("request_params") or {}
        global_rp = {}
        model_rp = {}
        compact_token_threshold = None
        global_params = chat_models_config.get("*", {}).get("params", {})
        if not isinstance(global_params, dict):
            global_params = {}
        model_params = chat_models_config.get(model, {}).get("params", {})
        if not isinstance(model_params, dict):
            model_params = {}
        global_rp = global_params.get("request_params") or {}
        model_rp = model_params.get("request_params") or {}
        compact_token_threshold = resolve_compact_token_threshold(
            configured_model, chat_models_config=chat_models_config
        )
        compact_token_threshold = compact_token_threshold or resolve_compact_token_threshold()
        request_params = {**global_rp, **model_rp, **chat_request_params} or None

        for _iteration in range(CHAT_MAX_ITERATIONS):
            # ── Context compaction: summarize older messages if too large ──
            if should_compact(
                messages,
                system,
                last_usage,
                new_messages_since,
                threshold=compact_token_threshold,
            ):
                target_keep = max(2, len(messages) * 2 // 5)
                split_idx = _find_safe_split(messages, target_keep)
                drop_zone = messages[:split_idx]
                keep_zone = messages[split_idx:]

                summary = await summarize_messages(
                    drop_zone,
                    loaded_summary,
                    connection,
                    model,
                )

                checkpoint_message_id = _summary_checkpoint_message_id(
                    keep_zone, summary_message_id
                )
                await ChatMessage.update(checkpoint_message_id, chat_summary=summary)
                loaded_summary = summary

                # Append summary to system prompt (works for all providers)
                memory_message, memory_files = _memory_recall_inputs(keep_zone, regeneration_prompt)
                system = await _load_system_prompt(
                    request,
                    workspace,
                    model,
                    user_id=user_id,
                    current_message=memory_message,
                    recent_messages=keep_zone,
                    mentioned_files=memory_files,
                )
                system += f"\n\n[CONVERSATION SUMMARY]\n{summary}"
                # Re-inject attached skills after compaction (protect from pruning)
                if attached_skill_ids:
                    skill_blocks = []
                    for sid in attached_skill_ids:
                        skill = load_skill(workspace, sid)
                        if skill:
                            skill_blocks.append(format_skill_content(skill))
                    if skill_blocks:
                        system += "\n\n" + "\n\n".join(skill_blocks)
                system = await _apply_voice_mode_system_prompt(system, chat_params)
                messages = keep_zone
                last_usage = None  # reset after compaction
                new_messages_since = 0

                logger.info(
                    "[task %s] compacted: checkpoint=%s dropped %d msgs, kept %d, summary=%d chars",
                    message_id[:8],
                    checkpoint_message_id[:8],
                    len(drop_zone),
                    len(keep_zone),
                    len(summary),
                )

            # Anthropic supports images natively in tool_result content blocks.
            # Chat Completions and Responses API don't support multimodal tool messages,
            # so extract images into a follow-up user message.
            api_messages = messages
            if provider != "anthropic":
                image_blocks = []
                normalized_messages = []
                for m in api_messages:
                    if m.get("role") == "tool" and isinstance(m.get("content"), list):
                        text_parts = []
                        for part in m["content"]:
                            if part.get("type") == "text":
                                text_parts.append(part.get("text", ""))
                            elif part.get("type") == "image":
                                image_blocks.append(part)
                        normalized_messages.append({**m, "content": "\n".join(text_parts)})
                    else:
                        normalized_messages.append(m)
                api_messages = normalized_messages
                if image_blocks:
                    api_messages.append(
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "text",
                                    "text": "Here are the images from the tool results above.",
                                },
                                *image_blocks,
                            ],
                        }
                    )
            api_messages = [{k: v for k, v in m.items() if k != "id"} for m in api_messages]

            form_data = ChatCompletionForm(
                model=model,
                messages=api_messages,
                instructions=system,
                tools=tools,
            )
            if provider == "anthropic":
                stream = stream_anthropic(
                    form_data, base_url, api_key, request_params=request_params
                )
            elif api_type == "responses":
                stream = stream_openai_responses(
                    form_data,
                    base_url,
                    api_key,
                    request_params=request_params,
                    provider_type=provider_type,
                )
            else:
                stream = stream_openai_completions(
                    form_data,
                    base_url,
                    api_key,
                    request_params=request_params,
                    provider_type=provider_type,
                )

            restart = False
            pending_calls: list[dict] = []  # Collect tool calls from this response
            pending_call_ids: set[str] = set()
            response_reasoning_items: list[dict] = []  # Pair with tool outputs on the next request
            streamed_reasoning_chars = 0
            estimated_prompt_tokens = 0
            last_emitted_context_tokens = 0
            if provider_type == "llama.cpp":
                estimated_prompt_tokens = estimate_tokens(system) + estimate_messages_tokens(
                    api_messages
                )
                last_emitted_context_tokens = estimated_prompt_tokens
                await emit(
                    context_usage=build_context_usage(
                        estimated_prompt_tokens,
                        threshold=compact_token_threshold,
                    )
                )

            async for event in stream:
                if event["type"] == "text_delta":
                    content += event["content"]
                    text_buffer += event["content"]
                    await emit(delta=event["content"])
                    if provider_type == "llama.cpp" and usage_context_tokens(last_usage) <= 0:
                        estimated_context_tokens = estimated_prompt_tokens + estimate_tokens(
                            content
                        )
                        if estimated_context_tokens - last_emitted_context_tokens >= 256:
                            last_emitted_context_tokens = estimated_context_tokens
                            await emit(
                                context_usage=build_context_usage(
                                    estimated_context_tokens,
                                    threshold=compact_token_threshold,
                                )
                            )
                    _sync_state()

                elif event["type"] == "tool_call":
                    # Collect tool call — don't execute yet
                    call_id = event["call_id"]
                    if call_id in pending_call_ids:
                        logger.warning(
                            "[task %s] ignoring duplicate tool call id=%s name=%s",
                            message_id[:8],
                            call_id,
                            event["name"],
                        )
                        continue
                    pending_call_ids.add(call_id)
                    pending_calls.append(event)

                elif event["type"] in ("output", "reasoning"):
                    # Providers stream normalized Responses-style output items.
                    # "reasoning" is kept temporarily for compatibility with older
                    # provider adapters.
                    item = event["item"]
                    _upsert_output_item(output_items, item)
                    if item.get("type") == "reasoning":
                        streamed_reasoning_chars = max(
                            streamed_reasoning_chars,
                            _reasoning_text_len(item),
                        )
                        if item.get("status") in (None, "completed"):
                            _upsert_output_item(response_reasoning_items, item)
                    logger.info(
                        "[task %s] output item: type=%s status=%s output=%d items reasoning_chars=%d response_reasoning_items=%d",
                        message_id[:8],
                        item.get("type"),
                        item.get("status"),
                        len(output_items),
                        streamed_reasoning_chars,
                        len(response_reasoning_items),
                    )
                    _sync_state()
                    await emit(output=item)

                elif event["type"] == "usage":
                    usage = normalize_usage({k: v for k, v in event.items() if k != "type"})
                    last_usage = usage
                    new_messages_since = 0
                    tokens = usage_context_tokens(usage)
                    if tokens > 0:
                        await emit(
                            context_usage=build_context_usage(
                                tokens, threshold=compact_token_threshold
                            )
                        )

                elif event["type"] == "done":
                    # Stream ended. Usage may have arrived earlier, multiple times, or never.
                    if not pending_calls:
                        _flush_text()
                        if streamed_reasoning_chars and not response_reasoning_items:
                            logger.warning(
                                "[task %s] reasoning output streamed (%d chars) but no completed reasoning item arrived before done; DB output may contain only in-progress reasoning",
                                message_id[:8],
                                streamed_reasoning_chars,
                            )
                        if provider_type == "llama.cpp" and usage_context_tokens(last_usage) <= 0:
                            estimated_context_tokens = estimated_prompt_tokens + estimate_tokens(
                                content
                            )
                            await emit(
                                context_usage=build_context_usage(
                                    estimated_context_tokens,
                                    threshold=compact_token_threshold,
                                )
                            )
                        await _save_message(
                            "done",
                            content=content,
                            output=output_items,
                            usage=last_usage,
                            done=True,
                        )
                        _task_state.pop(message_id, None)
                        await _emit_done()
                        preview = content[:300] if content else ""
                        await publish_event(
                            EVENTS.CHAT_FINISHED,
                            actor={"id": user_id},
                            subject_id=chat_id,
                            subject_type="chat",
                            source="chat_task",
                            data={"workspace": event_workspace, "preview": preview},
                            message=preview,
                        )
                        task_completed_success = True
                        return

            # ── Process collected tool calls ────────────────────
            if pending_calls:
                if streamed_reasoning_chars and not response_reasoning_items:
                    logger.warning(
                        "[task %s] reasoning output streamed (%d chars) before tool calls but no completed reasoning item arrived; model continuation will not include reasoning items",
                        message_id[:8],
                        streamed_reasoning_chars,
                    )
                flushed_item = _flush_text()

                ask_user_calls = [tc for tc in pending_calls if tc["name"] == ASK_USER_NAME]
                if ask_user_calls:
                    tc = ask_user_calls[0]
                    error = None
                    if len(pending_calls) != 1:
                        error = "Error: ask_user must be called by itself after research."
                    elif len(ask_user_calls) != 1:
                        error = "Error: only one ask_user call is allowed per turn."
                    else:
                        try:
                            arguments = validate_ask_user_request(tc["arguments"])
                        except ValueError as exc:
                            error = f"Error: {exc}"

                    if error:
                        item = {
                            "type": "function_call",
                            "id": str(uuid.uuid4()),
                            "call_id": tc["call_id"],
                            "fc_id": tc.get("id", ""),
                            "name": ASK_USER_NAME,
                            "arguments": tc["arguments"],
                            "status": "completed",
                        }
                        result_item = {
                            "type": "function_call_output",
                            "call_id": tc["call_id"],
                            "output": error,
                        }
                        output_items.extend((item, result_item))
                        if flushed_item:
                            await emit(output=flushed_item)
                        await emit(output=item)
                        await emit(output=result_item)
                        _sync_state()
                        new_messages = _output_items_to_messages(
                            [
                                *response_reasoning_items,
                                *([flushed_item] if flushed_item else []),
                                item,
                                result_item,
                            ]
                        )
                        messages.extend(new_messages)
                        new_messages_since += len(new_messages)
                        await _save_message(
                            "invalid ask_user", content=content, output=output_items
                        )
                        continue

                    expires_at = now_ms() + arguments["autoResolutionMs"]
                    item = {
                        "type": "function_call",
                        "id": str(uuid.uuid4()),
                        "call_id": tc["call_id"],
                        "fc_id": tc.get("id", ""),
                        "name": ASK_USER_NAME,
                        "arguments": arguments,
                        "status": "pending",
                        "expires_at": expires_at,
                    }
                    output_items.append(item)
                    await _save_message(
                        "pending ask_user",
                        content=content,
                        output=output_items,
                        done=False,
                    )
                    if flushed_item:
                        await emit(output=flushed_item)
                    await emit(output=item)
                    _task_state.pop(message_id, None)

                    async def auto_answer():
                        from cptr.socket.main import is_chat_visible

                        remaining = arguments["autoResolutionMs"] / 1000
                        while remaining > 0:
                            await asyncio.sleep(1)
                            if not is_chat_visible(user_id, chat_id):
                                remaining -= 1
                        from cptr.app import app
                        from cptr.routers.chat import AskUserNotPendingError, resolve_ask_user

                        try:
                            await resolve_ask_user(
                                app, chat_id, message_id, tc["call_id"], None, True
                            )
                        except AskUserNotPendingError:
                            pass

                    asyncio.create_task(auto_answer())
                    await emit(
                        done=True,
                        status="input_required",
                        title=chat_obj.title if chat_obj else None,
                        workspace=workspace,
                        workspace_name=workspace.rstrip("/").rsplit("/", 1)[-1]
                        if workspace
                        else "",
                    )
                    return

                # Check if any call needs approval
                needs_approval = None
                for tc in pending_calls:
                    name = tc["name"]
                    tool_names_used.add(name)
                    if name == "view_skill":
                        skill_name = str(tc.get("arguments", {}).get("skill_name") or "").strip()
                        if skill_name:
                            loaded_skill_names.add(skill_name)
                    tool = ALL_TOOLS.get(name)
                    tool_approval = await resolve_builtin_tool_approval(name) if tool else "review"
                    current_chat = await Chat.get_by_id(chat_id)
                    current_params = (
                        (current_chat.meta or {}).get("params", {}) if current_chat else chat_params
                    )
                    current_params = current_params if isinstance(current_params, dict) else {}
                    approval_mode = current_params.get("tool_approval_mode")
                    if approval_mode not in {"ask", "auto", "full"}:
                        approval_mode = (
                            "full"
                            if "tool_approval_mode" not in current_params
                            and current_params.get("auto_approve_tools")
                            else "auto"
                        )
                    should_auto = approval_mode == "full" or (
                        approval_mode == "auto" and tool and tool_approval == "allow"
                    )
                    if not should_auto:
                        needs_approval = tc
                        break

                if needs_approval:
                    call_items = []
                    for tc in pending_calls:
                        item = {
                            "type": "function_call",
                            "id": str(uuid.uuid4()),
                            "call_id": tc["call_id"],
                            "fc_id": tc.get("id", ""),
                            "name": tc["name"],
                            "arguments": tc["arguments"],
                            "status": "queued",
                        }
                        output_items.append(item)
                        call_items.append(item)

                    await _save_message(
                        "queued tool calls",
                        content=content,
                        output=output_items,
                        done=False,
                    )
                    if flushed_item:
                        await emit(output=flushed_item)
                    for item in call_items:
                        await emit(output=item)

                    queue_result = await run_queued_tool_calls(tool_ctx)
                    if queue_result == "approval_required":
                        return
                    if queue_result == "completed":
                        messages, loaded_summary = await _load_message_history(chat_id, message_id)
                        new_messages_since = 0
                        restart = True
                        continue
                    raise RuntimeError("tool-call queue did not make progress")

                # All calls are auto-approved — build UI items
                call_items: list[tuple[dict, dict]] = []  # (event, ui_item)
                for tc in pending_calls:
                    item = {
                        "type": "function_call",
                        "id": str(uuid.uuid4()),
                        "call_id": tc["call_id"],
                        "fc_id": tc.get("id", ""),
                        "name": tc["name"],
                        "arguments": tc["arguments"],
                        "status": "in_progress",
                    }
                    output_items.append(item)
                    call_items.append((tc, item))

                # Emit all as in_progress
                if flushed_item:
                    await emit(output=flushed_item)
                for _, item in call_items:
                    await emit(output=item)
                _sync_state()

                # Separate delegate_task (concurrent) from others (sequential)
                delegate_indices = [
                    i for i, (tc, _) in enumerate(call_items) if tc["name"] == "delegate_task"
                ]
                other_indices = [
                    i for i, (tc, _) in enumerate(call_items) if tc["name"] != "delegate_task"
                ]

                # Execute non-delegate tools sequentially first
                # Collect results for batch message construction
                sequential_result_items: list[dict] = []
                for idx in other_indices:
                    tc, item = call_items[idx]
                    if tc["name"] == "create_artifact":
                        args = dict(tc["arguments"])
                        args.pop("workspace", None)
                        result = await create_artifact(**args, workspace=workspace)
                    else:
                        result = await execute_tool(
                            tc["name"],
                            tc["arguments"],
                            {**tool_ctx, "call_id": tc["call_id"]},
                        )

                    # Append output BEFORE marking completed — if anything
                    # between here and persist fails, the call stays
                    # "in_progress" (safely skipped on reload) rather than
                    # "completed" without an output (corrupts history).
                    result_item = {
                        "type": "function_call_output",
                        "call_id": tc["call_id"],
                        "output": result,
                        "status": "failed" if _is_tool_result_error(result) else "completed",
                    }
                    output_items.append(result_item)
                    item["status"] = result_item["status"]
                    await emit(output=item)
                    await emit(output=result_item)
                    _sync_state()
                    sequential_result_items.append(result_item)

                    artifact_item = build_artifact_item(tc["name"], tc["arguments"], result)
                    if artifact_item:
                        output_items.append(artifact_item)
                        await emit(output=artifact_item)
                        _sync_state()

                    if tc["name"] == "display_file":
                        try:
                            file_item = json.loads(result)
                        except (json.JSONDecodeError, TypeError):
                            file_item = None
                        if isinstance(file_item, dict) and file_item.get("type") == "file":
                            output_items.append(file_item)
                            await emit(output=file_item)
                            _sync_state()
                # Execute delegate_task calls concurrently, emit each as it completes
                delegate_result_items: list[dict] = []
                if delegate_indices:
                    # Create tasks, mapping task → index
                    inflight: dict[asyncio.Task, int] = {}
                    for idx in delegate_indices:
                        tc, _ = call_items[idx]
                        task = asyncio.create_task(
                            execute_tool(
                                tc["name"],
                                tc["arguments"],
                                {**tool_ctx, "call_id": tc["call_id"]},
                            )
                        )
                        inflight[task] = idx
                    while inflight:
                        done_set, _ = await asyncio.wait(
                            inflight.keys(), return_when=asyncio.FIRST_COMPLETED
                        )
                        for task in done_set:
                            idx = inflight.pop(task)
                            tc, item = call_items[idx]
                            try:
                                result = task.result()
                            except Exception as e:
                                result = f"Error: {e}"

                            # Append output BEFORE marking completed (same
                            # ordering fix as sequential path above).
                            result_item = {
                                "type": "function_call_output",
                                "call_id": tc["call_id"],
                                "output": result,
                                "status": "failed"
                                if _is_tool_result_error(result)
                                else "completed",
                            }
                            output_items.append(result_item)
                            item["status"] = result_item["status"]
                            await emit(output=item)
                            await emit(output=result_item)
                            _sync_state()
                            delegate_result_items.append(result_item)

                new_messages = _output_items_to_messages(
                    [
                        *response_reasoning_items,
                        *([flushed_item] if flushed_item else []),
                        *(
                            item
                            for _, item in call_items
                            if item.get("status") in {"completed", "failed"}
                        ),
                        *sequential_result_items,
                        *delegate_result_items,
                    ]
                )
                messages.extend(new_messages)
                new_messages_since += len(new_messages)

                # Persist after all tool calls
                await _save_message("tool calls complete", content=content, output=output_items)
                restart = True

            if not restart:
                flushed_item = _flush_text()
                if flushed_item:
                    await emit(output=flushed_item)
                await _save_message(
                    "end",
                    content=content,
                    output=output_items,
                    usage=last_usage,
                    done=True,
                )
                _task_state.pop(message_id, None)
                await _emit_done()
                preview = content[:300] if content else ""
                await publish_event(
                    EVENTS.CHAT_FINISHED,
                    actor={"id": user_id},
                    subject_id=chat_id,
                    subject_type="chat",
                    source="chat_task",
                    data={"workspace": event_workspace, "preview": preview},
                    message=preview,
                )
                task_completed_success = True
                return

        # Max iterations reached
        await _save_message(
            "max iterations",
            content=content,
            output=output_items,
            done=True,
            meta={"error": "max iterations reached"},
        )
        _task_state.pop(message_id, None)
        await _emit_done()
        await publish_event(
            EVENTS.CHAT_FAILED,
            actor={"id": user_id},
            subject_id=chat_id,
            subject_type="chat",
            source="chat_task",
            data={"workspace": event_workspace, "preview": "Max iterations reached."},
            message="Max iterations reached.",
        )

    except asyncio.CancelledError:
        _flush_text()
        _scrub_incomplete_items(output_items)
        await _save_message("cancelled", content=content, output=output_items, done=True)
        _task_state.pop(message_id, None)
        await _emit_done()
    except Exception as e:
        logger.exception(f"Chat task error for message {message_id}")
        _flush_text()
        error_msg = str(e)
        # Try to extract API error body for more detail
        if hasattr(e, "response"):
            try:
                body = e.response.text or ""
                if body:
                    import json as _json

                    err_data = _json.loads(body)
                    api_msg = err_data.get("error", {}).get("message", "")
                    if api_msg:
                        error_msg = api_msg
            except Exception:
                pass
        # Append error to content so it's visible in the chat
        error_block = f"\n\n> **Error:** {error_msg}"
        content += error_block
        text_buffer += error_block
        flushed_item = _flush_text()
        if flushed_item:
            await emit(output=flushed_item)
        _scrub_incomplete_items(output_items)
        await _save_message(
            "error",
            content=content,
            output=output_items,
            done=True,
            meta={"error": error_msg},
        )
        _task_state.pop(message_id, None)
        await emit(done=True, error=error_msg)
        await publish_event(
            EVENTS.CHAT_FAILED,
            actor={"id": user_id},
            subject_id=chat_id,
            subject_type="chat",
            source="chat_task",
            data={"workspace": event_workspace, "preview": error_msg[:300] if error_msg else ""},
            message=error_msg[:300] if error_msg else "",
        )
    finally:
        # Guarantee the gateway SSE stream terminates.  If emit()
        # already pushed a done/error event the sentinel is harmless
        # (_stream checks for None separately).  Without this, a
        # crash in emit_to_user or an unexpected exit path leaves
        # the SSE generator hanging for up to 5 minutes.
        if output_queue is not None:
            try:
                await output_queue.put(None)
            except Exception:
                pass
        _tasks.pop(message_id, None)
        _task_state.pop(message_id, None)
        _task_chat.pop(message_id, None)
        if chat_id not in get_active_chat_ids():
            try:
                await Chat.touch(chat_id, now_ms())
                chat = await Chat.get_by_id(chat_id)
                unread_counts = await Chat.unread_counts_by_workspace(
                    user_id, [workspace], get_active_chat_ids()
                )
                await emit_to_user(
                    user_id,
                    {
                        "type": "chat:active",
                        "chat_id": chat_id,
                        "workspace": workspace,
                        "active": False,
                        "updated_at": chat.updated_at if chat else None,
                        "workspace_unread_count": unread_counts.get(workspace, 0),
                    },
                )
            except Exception:
                logger.debug("[task %s] active-state emit failed", message_id[:8], exc_info=True)
        try:
            await export_chat_to_file(request, chat_id)
        except Exception:
            logger.exception(f"Failed to export chat {chat_id}")
        # Generate a proper title if the chat still has the auto-truncated fallback
        try:
            chat_obj = await Chat.get_by_id(chat_id)
            if chat_obj:
                all_msgs = await ChatMessage.get_all_by_chat(chat_id)
                first_user = next(
                    (
                        m
                        for m in all_msgs
                        if m.role == "user" and not (m.meta and m.meta.get("queued"))
                    ),
                    None,
                )
                if first_user:
                    # The router sets title = content[:50] on creation.
                    # Only generate if the title still matches that fallback.
                    fallback = first_user.content[:50].strip() or "New Chat"
                    if chat_obj.title == fallback:
                        await generate_chat_title(
                            chat_id,
                            user_id,
                            connection,
                            model,
                            first_user.content,
                        )
        except Exception:
            logger.debug(
                "[title] Error in title generation for chat %s", chat_id[:8], exc_info=True
            )
        # Best-effort post-turn memory review. Runs detached and never competes
        # with queued user input processing.
        try:
            from cptr.utils.memory import review_memory_after_turn

            await review_memory_after_turn(
                request,
                user_id=user_id,
                message_id=message_id,
                workspace=workspace,
                conversation_messages=messages,
                assistant_reply=content,
                model_connection=connection,
                model=model,
            )
        except Exception:
            logger.debug("[memory] Failed to review conversation", exc_info=True)
        try:
            from cptr.utils.skills import review_skills_after_turn

            await review_skills_after_turn(
                workspace=workspace,
                conversation_messages=review_messages,
                assistant_reply=content,
                model_connection=review_model_connection,
                model=review_model_name,
                loaded_skill_names=loaded_skill_names,
                tool_names=tool_names_used,
                skill_create_requested=skill_create_requested,
                plan_mode=bool(review_chat_params.get("plan_mode")),
                subagent=bool(review_chat_params.get("subagent")),
                skills_enabled=task_completed_success
                and skill_authoring_allowed
                and "manage_skill" not in disabled_builtin_tool_names(review_builtin_tools),
            )
        except Exception:
            logger.debug("[skills] Failed to review conversation", exc_info=True)
        # Process any pending user prompts or internal subagent results.
        try:
            await process_pending_chat_inputs(request, chat_id, user_id, workspace)
        except Exception:
            logger.exception(f"Failed to process pending inputs for chat {chat_id}")
