"""OpenAPI tool server client.

Handles fetching OpenAPI specs, converting them to LLM-compatible tool schemas,
and executing tool calls against OpenAPI servers.
"""

from __future__ import annotations

import copy
import json
import logging

import httpx

logger = logging.getLogger(__name__)

# Valid HTTP methods per OpenAPI 3.x
_HTTP_METHODS = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}

_TIMEOUT = 15  # seconds


# ── Spec fetching ───────────────────────────────────────────


async def fetch_openapi_spec(
    url: str, headers: dict | None = None, timeout: float = _TIMEOUT
) -> dict:
    """Fetch and parse an OpenAPI spec from a URL.

    Supports JSON and YAML (if PyYAML is installed). Falls back to YAML
    parsing for non-JSON content.

    Args:
        url: Full URL to the OpenAPI spec endpoint.
        headers: Optional HTTP headers (e.g. Authorization).
        timeout: Request timeout in seconds.

    Returns:
        The parsed OpenAPI spec as a dict.
    """
    _headers = {"Accept": "application/json"}
    if headers:
        _headers.update(headers)

    async with httpx.AsyncClient(timeout=timeout, verify=True) as client:
        resp = await client.get(url, headers=_headers)
        resp.raise_for_status()

        text = resp.text
        if url.lower().endswith((".yaml", ".yml")):
            import yaml

            return yaml.safe_load(text)

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # Fallback to YAML for non-.yml URLs that aren't valid JSON
            import yaml

            return yaml.safe_load(text)


# ── Schema conversion ──────────────────────────────────────


def _resolve_schema(schema: dict, components: dict, resolved: set | None = None) -> dict:
    """Recursively resolve $ref references in a JSON schema."""
    if not schema:
        return {}

    if resolved is None:
        resolved = set()

    if "$ref" in schema:
        ref_path = schema["$ref"]
        schema_name = ref_path.split("/")[-1]

        if schema_name in resolved:
            return {}  # Avoid infinite recursion

        resolved.add(schema_name)

        ref_parts = ref_path.strip("#/").split("/")
        target = components
        for part in ref_parts[1:]:  # Skip 'components'
            target = target.get(part, {})
        return _resolve_schema(target, components, resolved)

    result = copy.deepcopy(schema)

    if "properties" in result:
        for prop, prop_schema in result["properties"].items():
            result["properties"][prop] = _resolve_schema(prop_schema, components)

    if "items" in result:
        result["items"] = _resolve_schema(result["items"], components)

    for keyword in ("oneOf", "anyOf", "allOf"):
        if keyword in result and isinstance(result[keyword], list):
            result[keyword] = [
                _resolve_schema(inner, components, resolved) for inner in result[keyword]
            ]

    return result


def convert_openapi_to_tool_specs(openapi_spec: dict) -> list[dict]:
    """Convert an OpenAPI specification into tool schemas for the LLM.

    Each operation with an ``operationId`` becomes a tool. Parameters and
    request body properties are flattened into a single ``parameters`` object.

    Args:
        openapi_spec: The parsed OpenAPI spec dict.

    Returns:
        A list of tool spec dicts with name, description, and parameters.
    """
    specs = []
    components = openapi_spec.get("components", {})

    for path, methods in openapi_spec.get("paths", {}).items():
        if not isinstance(methods, dict):
            continue

        path_params = methods.get("parameters", [])
        if not isinstance(path_params, list):
            path_params = []

        for method, operation in methods.items():
            if method not in _HTTP_METHODS:
                continue
            if not isinstance(operation, dict):
                continue
            if not operation.get("operationId"):
                continue

            tool = {
                "name": operation["operationId"],
                "description": operation.get(
                    "description", operation.get("summary", "No description available.")
                ),
                "parameters": {"type": "object", "properties": {}, "required": []},
            }

            # Merge path-level and operation-level params
            op_params = operation.get("parameters", [])
            if not isinstance(op_params, list):
                op_params = []

            merged = {}
            for param in path_params:
                if isinstance(param, dict) and param.get("name"):
                    merged[(param["name"], param.get("in", ""))] = param
            for param in op_params:
                if isinstance(param, dict) and param.get("name"):
                    merged[(param["name"], param.get("in", ""))] = param

            for param in merged.values():
                pname = param.get("name")
                if not pname:
                    continue
                pschema = param.get("schema", {})
                desc = pschema.get("description", "") or param.get("description", "")
                if pschema.get("enum") and isinstance(pschema["enum"], list):
                    desc += f". Possible values: {', '.join(str(v) for v in pschema['enum'])}"

                prop = {
                    "type": pschema.get("type") or "string",
                    "description": desc,
                }
                if pschema.get("type") == "array" and "items" in pschema:
                    prop["items"] = pschema["items"]

                prop = {k: v for k, v in prop.items() if v is not None}
                tool["parameters"]["properties"][pname] = prop
                if param.get("required"):
                    tool["parameters"]["required"].append(pname)

            # Extract requestBody
            request_body = operation.get("requestBody")
            if request_body:
                content = request_body.get("content", {})
                json_schema = content.get("application/json", {}).get("schema")
                if json_schema:
                    resolved = _resolve_schema(json_schema, components)
                    if resolved.get("properties"):
                        tool["parameters"]["properties"].update(resolved["properties"])
                        if "required" in resolved:
                            tool["parameters"]["required"] = list(
                                set(tool["parameters"]["required"] + resolved["required"])
                            )
                    elif resolved.get("type") == "array":
                        tool["parameters"] = resolved

            specs.append(tool)

    return specs


# ── Tool execution ──────────────────────────────────────────


async def execute_openapi_tool(
    server_url: str,
    openapi_spec: dict,
    tool_name: str,
    args: dict,
    headers: dict | None = None,
    timeout: float = 60,
) -> str:
    """Execute a tool call against an OpenAPI server.

    Resolves the route by operationId, separates path/query/body params,
    and makes the appropriate HTTP request.

    Args:
        server_url: Base URL of the OpenAPI server.
        openapi_spec: The full parsed OpenAPI spec.
        tool_name: The operationId to call.
        args: Arguments from the LLM tool call.
        headers: Optional HTTP headers.
        timeout: Request timeout in seconds.

    Returns:
        The response body as a string (JSON-serialized if structured).
    """
    paths = openapi_spec.get("paths", {})

    # Find matching route
    route_path = None
    http_method = None
    operation = None

    for rpath, methods in paths.items():
        if not isinstance(methods, dict):
            continue
        for method, op in methods.items():
            if method not in _HTTP_METHODS:
                continue
            if isinstance(op, dict) and op.get("operationId") == tool_name:
                route_path = rpath
                http_method = method
                operation = op
                break
        if route_path:
            break

    if not route_path or not operation:
        return json.dumps({"error": f"Operation '{tool_name}' not found in OpenAPI spec"})

    # Classify parameters
    path_params = {}
    query_params = {}
    body_params = dict(args)

    all_params = (operation.get("parameters") or []) + (
        paths.get(route_path, {}).get("parameters") or []
    )

    for param in all_params:
        pname = param.get("name", "")
        pin = param.get("in", "")
        if pname in body_params:
            if pin == "path":
                path_params[pname] = body_params.pop(pname)
            elif pin == "query":
                query_params[pname] = body_params.pop(pname)
            elif pin == "header":
                body_params.pop(pname)  # skip header params for now

    # Build URL with path params
    url = server_url.rstrip("/") + route_path
    for pname, pval in path_params.items():
        url = url.replace(f"{{{pname}}}", str(pval))

    _headers = {"Content-Type": "application/json"}
    if headers:
        _headers.update(headers)

    try:
        async with httpx.AsyncClient(timeout=timeout, verify=True) as client:
            if http_method in ("get", "head", "options"):
                resp = await client.request(
                    http_method.upper(), url, headers=_headers, params=query_params
                )
            else:
                resp = await client.request(
                    http_method.upper(),
                    url,
                    headers=_headers,
                    params=query_params,
                    json=body_params if body_params else None,
                )

            if resp.status_code >= 400:
                body = resp.text.strip()
                try:
                    body = json.dumps(resp.json(), indent=2, ensure_ascii=False)
                except Exception:
                    pass
                return json.dumps(
                    {
                        "error": f"HTTP {resp.status_code} from tool server",
                        "status_code": resp.status_code,
                        "body": body[:4000],
                    },
                    ensure_ascii=False,
                )

            # Try to parse as JSON
            try:
                data = resp.json()
                return json.dumps(data, indent=2, ensure_ascii=False)
            except Exception:
                return resp.text

    except httpx.TimeoutException:
        return json.dumps({"error": f"Request to {url} timed out after {timeout}s"})
    except Exception as e:
        return json.dumps({"error": str(e)})
