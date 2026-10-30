"""Detection helpers for coding agent profiles."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import re
import shlex
import shutil
import socket
from contextlib import suppress
from dataclasses import dataclass, asdict
from typing import Any

import httpx

from cptr.utils.agents.models import (
    default_agent_profiles,
    get_raw_agent_profiles,
    model_id_for_profile,
    normalize_agent_profiles,
)
from cptr.utils.agents.opencode import opencode_server_url_candidates

DETECTION_TTL_SECONDS = 30
CLAUDE_MODEL_FALLBACKS = [
    "claude-fable-5",
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-opus-4-6",
    "claude-opus-4-5",
    "claude-sonnet-4-6",
    "claude-haiku-4-5",
]
MIN_CLAUDE_FABLE_5 = (2, 1, 169)
MIN_CLAUDE_OPUS_4_8 = (2, 1, 154)
MIN_CLAUDE_OPUS_4_7 = (2, 1, 111)


@dataclass
class AgentDetection:
    status: str
    command: str | None = None
    version: str | None = None
    message: str | None = None
    models: list[str] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _resolve_command(command: str) -> str | None:
    command = command.strip()
    if not command:
        return None
    expanded = os.path.expanduser(command)
    if os.path.isabs(expanded):
        return expanded if os.access(expanded, os.X_OK) else None
    return shutil.which(command)


def _find_claude_desktop_command() -> str | None:
    if os.name == "nt":
        roots = []
        appdata = os.environ.get("APPDATA")
        if appdata:
            roots.append(os.path.join(appdata, "Claude", "claude-code"))
        localappdata = os.environ.get("LOCALAPPDATA")
        packages_dir = os.path.join(localappdata, "Packages") if localappdata else None
        if packages_dir and os.path.isdir(packages_dir):
            with suppress(OSError):
                for name in os.listdir(packages_dir):
                    if name.startswith("Claude_"):
                        roots.append(
                            os.path.join(
                                packages_dir,
                                name,
                                "LocalCache",
                                "Roaming",
                                "Claude",
                                "claude-code",
                            )
                        )
        relative_paths = (("claude.exe",),)
    else:
        roots = [
            os.path.join(
                os.path.expanduser("~"),
                "Library",
                "Application Support",
                "Claude",
                "claude-code",
            )
        ]
        relative_paths = (
            ("claude.app", "Contents", "MacOS", "claude"),
            ("claude",),
        )

    candidates: list[tuple[tuple[int, int, int], str]] = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        with suppress(OSError):
            for name in os.listdir(root):
                version_dir = os.path.join(root, name)
                version = _parse_version_tuple(name)
                if version is None or not os.path.isdir(version_dir):
                    continue
                for relative_path in relative_paths:
                    candidate = os.path.join(version_dir, *relative_path)
                    if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                        candidates.append((version, candidate))
                        break
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


async def _run_probe(
    argv: list[str],
    timeout: float = 3.0,
    env: dict[str, str] | None = None,
) -> tuple[int, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        return 124, "probe timed out"
    except Exception as exc:  # noqa: BLE001 - surfaced as detection status.
        return 1, str(exc)

    text = (stdout or stderr).decode(errors="replace").strip()
    return proc.returncode or 0, text


async def detect_profile(profile: dict[str, Any]) -> AgentDetection:
    raw_command = str(profile.get("command") or "").strip()
    command = _resolve_command(raw_command)
    default_claude_command = profile.get("agent") == "claude_code" and raw_command == "claude"
    if profile.get("agent") == "opencode" and str(profile.get("server_url") or "").strip():
        version = None
        if command is not None:
            code, version_text = await _run_probe([command, "--version"])
            version = version_text.splitlines()[0] if code == 0 and version_text else None
        models = await _probe_opencode_models(command or raw_command or "opencode", profile)
        if not models:
            return AgentDetection(
                "auth_unknown",
                command,
                version,
                "Could not discover OpenCode models. Check connected OpenCode providers.",
                [],
            )
        return AgentDetection("ready", command, version, None, models)

    if command is None and default_claude_command:
        command = _find_claude_desktop_command()
    if command is None:
        return AgentDetection("not_found", None, None, "Command not found")

    code, version_text = await _run_probe([command, "--version"])
    if code != 0 and default_claude_command:
        desktop_command = _find_claude_desktop_command()
        if desktop_command and desktop_command != command:
            desktop_code, desktop_version_text = await _run_probe([desktop_command, "--version"])
            if desktop_code == 0:
                command = desktop_command
                code = desktop_code
                version_text = desktop_version_text
    version = version_text.splitlines()[0] if code == 0 and version_text else None

    if profile.get("agent") == "claude_code":
        models = _claude_models_for_version(version)
        if code != 0:
            return AgentDetection(
                "error",
                command,
                version,
                version_text or "Failed to run Claude Code health check.",
                models,
            )
        if importlib.util.find_spec("claude_agent_sdk") is None:
            return AgentDetection(
                "missing_dependency",
                command,
                version,
                "Python package claude-agent-sdk is not installed",
                models,
            )
        models = await _probe_claude_models(command, profile) or models
        return AgentDetection("ready", command, version, None, models)

    if profile.get("agent") == "codex":
        help_code, help_text = await _run_probe([command, "app-server", "--help"])
        if help_code != 0:
            return AgentDetection(
                "error",
                command,
                version,
                help_text or "codex app-server is unavailable",
            )
        models = await _probe_codex_models(command, profile)
        return AgentDetection("ready", command, version, None, models)

    if profile.get("agent") == "cursor":
        about = await _probe_cursor_about(command, profile)
        if about["status"] != "ready":
            return AgentDetection(
                about["status"],
                command,
                about.get("version"),
                about.get("message"),
                [],
            )
        models = await _probe_cursor_models(command, profile)
        if not models:
            return AgentDetection(
                "auth_unknown",
                command,
                about.get("version"),
                "Could not discover Cursor models. Check Cursor Agent login and CLI setup.",
                [],
            )
        return AgentDetection("ready", command, about.get("version"), None, models)

    if profile.get("agent") == "grok":
        models = await _probe_grok_models(command, profile)
        if not models:
            return AgentDetection(
                "auth_unknown",
                command,
                version,
                "Could not discover Grok models. Check Grok login or XAI_API_KEY.",
                [],
            )
        return AgentDetection("ready", command, version, None, models)

    if profile.get("agent") == "opencode":
        models = await _probe_opencode_models(command, profile)
        if not models:
            return AgentDetection(
                "auth_unknown",
                command,
                version,
                "Could not discover OpenCode models. Check connected OpenCode providers.",
                [],
            )
        return AgentDetection("ready", command, version, None, models)

    if profile.get("agent") == "cline":
        models = await _probe_acp_models(command, profile)
        if not models:
            return AgentDetection(
                "auth_unknown",
                command,
                version,
                "Could not discover Cline models. Check `cline auth` or add models manually.",
                [],
            )
        return AgentDetection("ready", command, version, None, models)

    if profile.get("agent") == "gemini":
        models = await _probe_acp_models(command, profile, auth_method_id="oauth-personal")
        if not models:
            return AgentDetection(
                "auth_unknown",
                command,
                version,
                "Could not discover Gemini models. Check Gemini CLI login or add models manually.",
                [],
            )
        return AgentDetection("ready", command, version, None, models)

    if profile.get("agent") == "pi":
        models = await _probe_pi_models(command, profile)
        if models is None:
            return AgentDetection(
                "error",
                command,
                version,
                "Pi RPC is unavailable; the resolved command may not be Pi.",
                [],
            )
        if not models:
            return AgentDetection(
                "auth_unknown",
                command,
                version,
                "Could not discover Pi models. Check Pi provider configuration.",
                [],
            )
        return AgentDetection("ready", command, version, None, models)

    return AgentDetection("error", command, version, "Unknown agent")


def _parse_version_tuple(version: str | None) -> tuple[int, int, int] | None:
    if not version:
        return None
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", version)
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def _version_at_least(version: tuple[int, int, int] | None, minimum: tuple[int, int, int]) -> bool:
    return version is not None and version >= minimum


def _claude_models_for_version(version: str | None) -> list[str]:
    parsed = _parse_version_tuple(version)
    models = []
    for model in CLAUDE_MODEL_FALLBACKS:
        if model == "claude-fable-5" and not _version_at_least(parsed, MIN_CLAUDE_FABLE_5):
            continue
        if model == "claude-opus-4-8" and not _version_at_least(parsed, MIN_CLAUDE_OPUS_4_8):
            continue
        if model == "claude-opus-4-7" and not _version_at_least(parsed, MIN_CLAUDE_OPUS_4_7):
            continue
        models.append(model)
    return models


async def _probe_claude_models(command: str, profile: dict[str, Any]) -> list[str] | None:
    async def probe() -> list[str]:
        from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

        env = os.environ.copy()
        if profile.get("home"):
            env["HOME"] = os.path.expanduser(str(profile["home"]))
        launch_args = shlex.split(str(profile.get("launch_args") or ""))
        options = ClaudeAgentOptions(
            cli_path=command,
            cwd=os.getcwd(),
            env=env,
            extra_args={arg: None for arg in launch_args},
        )
        client = ClaudeSDKClient(options)
        try:
            await client.connect()
            info = await client.get_server_info()
        finally:
            await client.disconnect()

        data = info.get("models") if isinstance(info, dict) else None
        models: list[str] = []
        for item in data if isinstance(data, list) else []:
            if not isinstance(item, dict):
                continue
            model = item.get("resolvedModel")
            if not isinstance(model, str) or not model.strip():
                model = item.get("value")
            if not isinstance(model, str) or not model.strip():
                continue
            model = model.strip()
            if model not in models:
                models.append(model)
        return models

    try:
        # The SDK's AnyIO task group must connect and disconnect in the same task.
        return await asyncio.wait_for(probe(), timeout=5) or None
    except Exception:
        return None


async def _probe_codex_models(command: str, profile: dict[str, Any]) -> list[str] | None:
    from cptr.utils.agents.codex import CodexAppServer

    env = os.environ.copy()
    if profile.get("home"):
        env["CODEX_HOME"] = os.path.expanduser(str(profile["home"]))

    client = CodexAppServer(command, os.getcwd(), env)
    try:
        await asyncio.wait_for(client.start(), timeout=5)
        models: list[str] = []
        cursor: str | None = None
        while True:
            response = await asyncio.wait_for(
                client.request("model/list", {"cursor": cursor} if cursor else {}),
                timeout=5,
            )
            result = response.get("result") if isinstance(response.get("result"), dict) else {}
            data = result.get("data") if isinstance(result, dict) else None
            if not isinstance(data, list):
                break
            for item in data:
                if isinstance(item, dict) and isinstance(item.get("model"), str):
                    models.append(item["model"])
            next_cursor = result.get("nextCursor") if isinstance(result, dict) else None
            cursor = next_cursor if isinstance(next_cursor, str) and next_cursor else None
            if not cursor:
                break
        return models or None
    except Exception:
        return None
    finally:
        await client.close()


async def _probe_pi_models(command: str, profile: dict[str, Any]) -> list[str] | None:
    from cptr.utils.agents.pi import PiRpcClient

    env = os.environ.copy()
    if profile.get("home"):
        env["HOME"] = os.path.expanduser(str(profile["home"]))
    client = PiRpcClient(command, os.getcwd(), env)
    try:
        await asyncio.wait_for(client.start(), timeout=5)
        response = await asyncio.wait_for(client.request("get_available_models"), timeout=5)
        data = response.get("data") if isinstance(response.get("data"), dict) else {}
        models = []
        for item in data.get("models", []):
            if not isinstance(item, dict):
                continue
            provider, model = item.get("provider"), item.get("id")
            if isinstance(provider, str) and provider and isinstance(model, str) and model:
                models.append(f"{provider}/{model}")
        return models
    except Exception:
        return None
    finally:
        await client.close()


async def _probe_cursor_about(command: str, profile: dict[str, Any]) -> dict[str, Any]:
    env = os.environ.copy()
    if profile.get("home"):
        env["HOME"] = os.path.expanduser(str(profile["home"]))
    code, text = await _run_probe([command, "about", "--format", "json"], timeout=8, env=env)
    if code != 0:
        code, text = await _run_probe([command, "about"], timeout=8, env=env)
    version = None
    if text:
        with suppress(Exception):
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                raw_version = parsed.get("cliVersion")
                version = raw_version.strip() if isinstance(raw_version, str) else None
                if parsed.get("userEmail") is None:
                    return {
                        "status": "auth_unknown",
                        "version": version,
                        "message": "Cursor Agent is not authenticated. Run `agent login`.",
                    }
                email = parsed.get("userEmail")
                if isinstance(email, str) and email.strip():
                    lower = email.strip().lower()
                    if "not logged in" in lower or "login required" in lower:
                        return {
                            "status": "auth_unknown",
                            "version": version,
                            "message": "Cursor Agent is not authenticated. Run `agent login`.",
                        }
                    return {"status": "ready", "version": version, "message": None}
        version_match = re.search(r"CLI Version\s+([^\n]+)", text)
        if version_match:
            version = version_match.group(1).strip()
        email_match = re.search(r"User Email\s+([^\n]+)", text)
        if email_match:
            email = email_match.group(1).strip().lower()
            if "not logged in" in email or "login required" in email:
                return {
                    "status": "auth_unknown",
                    "version": version,
                    "message": "Cursor Agent is not authenticated. Run `agent login`.",
                }
    if code != 0:
        return {
            "status": "error",
            "version": version,
            "message": text or "Failed to run Cursor Agent health check.",
        }
    return {"status": "ready", "version": version, "message": None}


async def _probe_cursor_models(command: str, profile: dict[str, Any]) -> list[str] | None:
    from cptr.utils.agents.acp import AcpClient

    env = os.environ.copy()
    if profile.get("home"):
        env["HOME"] = os.path.expanduser(str(profile["home"]))
    args = []
    api_endpoint = str(profile.get("api_endpoint") or "").strip()
    if api_endpoint:
        args.extend(["-e", api_endpoint])
    args.append("acp")
    client = AcpClient(
        command=command,
        args=args,
        cwd=os.getcwd(),
        env=env,
        auth_method_id="cursor_login",
        client_capabilities={"_meta": {"parameterizedModelPicker": True}},
    )
    try:
        await asyncio.wait_for(client.start(), timeout=10)
        result = await asyncio.wait_for(
            client.request("cursor/list_available_models", {}), timeout=5
        )
        models = []
        for item in result.get("models") or []:
            if isinstance(item, dict) and isinstance(item.get("value"), str):
                models.append(item["value"].strip())
        return [model for model in models if model] or None
    except Exception:
        return None
    finally:
        await client.close()


async def _probe_grok_models(command: str, profile: dict[str, Any]) -> list[str] | None:
    from cptr.utils.agents.acp import AcpClient, acp_models_from_setup

    env = os.environ.copy()
    if profile.get("home"):
        env["HOME"] = os.path.expanduser(str(profile["home"]))
    env["GROK_OAUTH2_REFERRER"] = "cptr"
    auth_method_id = "xai.api_key" if env.get("XAI_API_KEY", "").strip() else "cached_token"
    client = AcpClient(
        command=command,
        args=["agent", "stdio"],
        cwd=os.getcwd(),
        env=env,
        auth_method_id=auth_method_id,
    )
    try:
        await asyncio.wait_for(client.start(), timeout=10)
        return acp_models_from_setup(client.setup_result) or None
    except Exception:
        return None
    finally:
        await client.close()


async def _probe_opencode_models(command: str, profile: dict[str, Any]) -> list[str] | None:
    env = os.environ.copy()
    if profile.get("home"):
        env["HOME"] = os.path.expanduser(str(profile["home"]))
    server_url = str(profile.get("server_url") or "").strip()
    password = str(profile.get("server_password") or "").strip()
    proc: asyncio.subprocess.Process | None = None
    try:
        if not server_url:
            port = _free_port()
            proc = await asyncio.create_subprocess_exec(
                command,
                "serve",
                "--hostname=127.0.0.1",
                f"--port={port}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**env, "OPENCODE_CONFIG_CONTENT": "{}"},
            )
            server_url = await _read_opencode_server_url(proc, port)
        headers = {}
        if password:
            import base64

            token = base64.b64encode(f"opencode:{password}".encode()).decode()
            headers["Authorization"] = f"Basic {token}"
        for candidate_url in opencode_server_url_candidates(server_url):
            try:
                async with httpx.AsyncClient(base_url=candidate_url, timeout=5) as client:
                    providers = await _opencode_json(
                        client, ["provider.list", "provider/list", "provider"], headers
                    )
                    provider_list = (
                        providers.get("data")
                        if isinstance(providers.get("data"), dict)
                        else providers
                    )
                    connected = set(provider_list.get("connected") or [])
                    all_providers = provider_list.get("all") or []
                    models: list[str] = []
                    for provider in all_providers:
                        if not isinstance(provider, dict):
                            continue
                        provider_id = provider.get("id")
                        if not isinstance(provider_id, str) or provider_id not in connected:
                            continue
                        raw_models = provider.get("models")
                        if isinstance(raw_models, dict):
                            for model_id in raw_models:
                                if isinstance(model_id, str) and model_id.strip():
                                    models.append(f"{provider_id}/{model_id.strip()}")
                    if models:
                        return models
            except Exception:
                continue
        return None
    except Exception:
        return None
    finally:
        if proc and proc.returncode is None:
            proc.terminate()
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=2)
            if proc.returncode is None:
                proc.kill()
                await proc.wait()


async def _probe_acp_models(
    command: str,
    profile: dict[str, Any],
    auth_method_id: str | None = None,
) -> list[str] | None:
    from cptr.utils.agents.acp import AcpClient, acp_models_from_setup

    env = os.environ.copy()
    if profile.get("home"):
        env["HOME"] = os.path.expanduser(str(profile["home"]))
    client = AcpClient(
        command=command,
        args=["--acp"],
        cwd=os.getcwd(),
        env=env,
        auth_method_id=auth_method_id,
    )
    try:
        await asyncio.wait_for(client.start(), timeout=10)
        return acp_models_from_setup(client.setup_result) or None
    except Exception:
        return None
    finally:
        await client.close()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _read_opencode_server_url(proc: asyncio.subprocess.Process, port: int) -> str:
    assert proc.stdout is not None
    fallback = f"http://127.0.0.1:{port}"
    deadline = asyncio.get_running_loop().time() + 5
    while asyncio.get_running_loop().time() < deadline:
        line = await asyncio.wait_for(proc.stdout.readline(), timeout=1)
        if not line:
            break
        text = line.decode(errors="replace").strip()
        match = re.search(r"(https?://[^\s]+)", text)
        if match:
            return match.group(1)
    return fallback


async def _opencode_json(
    client: httpx.AsyncClient,
    paths: list[str],
    headers: dict[str, str],
) -> dict[str, Any]:
    last_error = None
    for path in paths:
        try:
            response = await client.get(f"/{path}", headers=headers)
            if response.status_code == 405:
                response = await client.post(f"/{path}", headers=headers, json={})
            response.raise_for_status()
            data = response.json()
            return data if isinstance(data, dict) else {}
        except Exception as exc:  # noqa: BLE001 - try alternate generated route names.
            last_error = exc
    if last_error:
        raise last_error
    return {}


async def get_agent_status(app_state=None, refresh: bool = False) -> dict[str, Any]:
    now = asyncio.get_running_loop().time()
    cache = getattr(app_state, "AGENTS", None) if app_state is not None else None
    if not refresh and isinstance(cache, dict):
        cached_at = cache.get("cached_at")
        if isinstance(cached_at, (int, float)) and now - cached_at < DETECTION_TTL_SECONDS:
            return cache["payload"]

    raw_profiles = await get_raw_agent_profiles()
    implicit_defaults = raw_profiles is None
    profiles = [] if implicit_defaults else normalize_agent_profiles(raw_profiles)
    entries = []
    candidates = profiles if not implicit_defaults else default_agent_profiles()
    for profile in candidates:
        detected = await detect_profile(profile)
        if implicit_defaults and detected.status in {"not_found", "error"}:
            continue
        mode = profile.get("mode", "auto")
        models = list(dict.fromkeys([*(detected.models or []), *(profile.get("models") or [])]))
        available = mode != "disabled" and (mode != "auto" or detected.status == "ready")
        effective_profile = dict(profile)
        resolved_profile_command = _resolve_command(str(profile.get("command") or ""))
        if (
            detected.command
            and profile.get("agent") == "claude_code"
            and detected.command != resolved_profile_command
        ):
            effective_profile["command"] = detected.command
        effective_profile["models"] = models
        if models and effective_profile.get("default_model") not in models:
            effective_profile["default_model"] = models[0]
        entries.append(
            {
                "id": effective_profile["id"],
                "agent": effective_profile["agent"],
                "name": effective_profile["name"],
                "config": effective_profile,
                "detected": detected.to_dict(),
                "available": available,
                "implicit": implicit_defaults,
                "model_ids": [model_id_for_profile(effective_profile, model) for model in models],
            }
        )

    payload = {"profiles": entries}
    if app_state is not None:
        app_state.AGENTS = {"cached_at": now, "payload": payload}
    return payload


def invalidate_agent_detection_cache(app_state) -> None:
    if app_state is not None and hasattr(app_state, "AGENTS"):
        delattr(app_state, "AGENTS")


async def get_available_agent_model_entries(app_state=None) -> list[dict[str, str]]:
    status = await get_agent_status(app_state)
    entries: list[dict[str, str]] = []
    for profile in status["profiles"]:
        if not profile["available"]:
            continue
        config = profile["config"]
        for model in config.get("models") or []:
            model_id = model_id_for_profile(config, model)
            entries.append(
                {
                    "id": model_id,
                    "name": model if config["agent"] == "claude_code" else model_id,
                    "provider": "agent",
                    "connection_id": f"agent:{config['id']}",
                    "agent_id": config["agent"],
                    "profile_id": config["id"],
                }
            )
    return entries
