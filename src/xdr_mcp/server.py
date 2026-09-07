# Copyright 2026 Future Joint-Stock Company
# SPDX-License-Identifier: Apache-2.0

"""MCP server for the F6 XDR platform API."""

import asyncio
import json
import os
import ssl
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx
import mcp_types as types
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from xdr_mcp import __version__

# explicit so the UA doesn't silently change with the httpx version
USER_AGENT = f"f6-xdr-mcp/{__version__}"

# The API caps page_size at 1000; this lower cap keeps a single tool call from
# flooding the model's context with records.
DEFAULT_PAGE_SIZE = 10  # what the API uses when page_size is omitted
MAX_PAGE_SIZE = 200
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_ERROR_CHARS = 512

# xdr_mark_event writes to the platform, so it is opt-in: without XDR_ALLOW_WRITE
# the server advertises the read-only tools only.
ALLOW_WRITE = os.environ.get("XDR_ALLOW_WRITE", "").strip().lower() in ("1", "true", "yes")


def _validated_base_url() -> str:
    """Reject a base URL that would send the API key somewhere unintended.

    Runs at import so a bad value fails the server on startup rather than
    leaking the key on the first tool call.
    """
    raw = os.environ.get("XDR_BASE_URL", "").strip().rstrip("/")
    if not raw:
        return ""

    parts = urlsplit(raw)
    if parts.scheme != "https":
        raise RuntimeError(f"XDR_BASE_URL must use https:// (got {parts.scheme or 'no'} scheme)")
    if not parts.hostname:
        raise RuntimeError("XDR_BASE_URL must contain a host")
    if parts.query or parts.fragment:
        raise RuntimeError("XDR_BASE_URL must not contain a query string or fragment")
    return raw


def _verification() -> ssl.SSLContext | bool:
    """Build the TLS verification setting.

    trust_env=False means SSL_CERT_FILE and SSL_CERT_DIR are ignored, so an
    installation behind an internal CA needs a way in that is not verify=False.
    """
    bundle = os.environ.get("XDR_CA_BUNDLE", "").strip()
    if not bundle:
        return True
    if not os.path.isfile(bundle):
        raise RuntimeError(f"XDR_CA_BUNDLE is not a file: {bundle}")
    try:
        return ssl.create_default_context(cafile=bundle)
    except ssl.SSLError as error:
        raise RuntimeError(f"XDR_CA_BUNDLE could not be loaded: {error}") from error


BASE_URL = _validated_base_url()
API_KEY = (os.environ.get("XDR_API_KEY") or "").strip()
VERIFY = _verification()


async def xdr_request(method: str, path: str, body: Any = None) -> Any:
    if not BASE_URL or not API_KEY:
        raise RuntimeError("XDR_BASE_URL and XDR_API_KEY environment variables must be set")

    url = f"{BASE_URL}{path}"
    headers = {
        "x-api-key": API_KEY,
        # Matched exactly: the API's 403/404/500 handlers compare this header by
        # equality and fall back to HTML for anything else.
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }

    # separators/ensure_ascii chosen so the wire bytes match JSON.stringify()
    content = json.dumps(body, separators=(",", ":"), ensure_ascii=False) if body is not None else None

    # trust_env=False keeps HTTP_PROXY/HTTPS_PROXY out of the picture; certificate
    # verification stays on. follow_redirects is pinned so an upgrade cannot start
    # forwarding the API key to a redirect target.
    async with httpx.AsyncClient(
        timeout=60.0, trust_env=False, follow_redirects=False, verify=VERIFY
    ) as client:
        response = await client.request(method, url, headers=headers, content=content)

    if not response.is_success:
        # Only echo a JSON error body, and only a bounded slice of it: an HTML
        # error page can carry tracebacks, settings and upstream names.
        detail = ""
        if response.headers.get("content-type", "").startswith("application/json"):
            detail = response.text[:MAX_ERROR_CHARS]
        suffix = f": {detail}" if detail else ""
        raise RuntimeError(f"API error {response.status_code} {response.reason_phrase}{suffix}")

    if len(response.content) > MAX_RESPONSE_BYTES:
        raise RuntimeError(
            f"response is {len(response.content)} bytes, over the {MAX_RESPONSE_BYTES} byte limit "
            f"— narrow the query or lower page_size"
        )

    try:
        return response.json()
    except ValueError as error:
        content_type = response.headers.get("content-type", "unknown")
        raise RuntimeError(
            f"expected JSON from {path}, got content-type '{content_type}'"
        ) from error


def build_query_string(params: dict[str, Any]) -> str:
    pairs = [(k, str(v)) for k, v in params.items() if v is not None]
    s = urlencode(pairs)
    return f"?{s}" if s else ""


def as_int(name: str, value: Any, minimum: int, maximum: int | None = None) -> int:
    """Coerce a JSON number to int. str(10.0) would reach the API as "10.0",
    which its pagination silently discards in favour of the default."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be an integer")
    if isinstance(value, float) and not value.is_integer():
        raise ValueError(f"{name} must be a whole number, got {value}")
    number = int(value)
    if number < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {number}")
    if maximum is not None and number > maximum:
        raise ValueError(f"{name} must be <= {maximum}, got {number}")
    return number


# Base paths are per-section; they do not follow a single scheme, so each one is
# spelled out. "paging" marks the sections whose API pages by 1-based page number
# ("cursor") instead of "offset". "basename" is set only where matchers apply.
SECTIONS: dict[str, dict[str, Any]] = {
    "alerts": {
        "path": "v1/alerts", "countPath": "v1/alerts", "basename": "alert",
        "ordering": ("created_at", "last_event", "last_updated", "severity"),
    },
    "incidents": {
        "path": "v1/incidents", "countPath": "v1/incidents",
        "ordering": ("created_at", "updated_at"),
    },
    "emails": {
        "path": "mailing", "countPath": "mailing", "basename": "envelope",
        "ordering": ("ts_created", "delay"),
    },
    "files": {
        "path": "attaches", "countPath": "attaches", "basename": "attach",
        "ordering": ("ts_created", "delay"),
    },
    "events": {
        "path": "eclipse_events", "countPath": "eclipse_events",
        "ordering": ("Header.Timestamp",),
    },
    "connections": {
        "path": "network", "countPath": "network",
        "ordering": ("ts_parsed", "ts_last_usage_parsed", "service_count", "duration"),
    },
    "assets": {
        "path": "assets", "countPath": "assets",
        "ordering": ("last_activity", "first_activity"),
    },
    "modules": {
        "path": "appliances", "countPath": "appliances", "paging": "cursor",
        "ordering": ("ts_created", "name"),
    },
    "audit": {
        "path": "system_logs", "countPath": "system_logs",
        "ordering": ("timestamp",),
    },
    "applications": {
        "path": "software", "countPath": "software",
        "ordering": ("name", "ts_created", "ts_updated"),
    },
}

ALL_SECTIONS = list(SECTIONS.keys())


def get_section(name: Any) -> dict[str, Any]:
    if name is None:
        raise ValueError(f"section is required. Must be one of: {', '.join(ALL_SECTIONS)}")
    if not isinstance(name, str) or name not in SECTIONS:
        raise ValueError(f'Invalid section "{name}". Must be one of: {", ".join(ALL_SECTIONS)}')
    return SECTIONS[name]


def build_paging(section: dict[str, Any], args: dict[str, Any]) -> str:
    params: dict[str, Any] = {"ordering": args.get("ordering")}

    page_size = args.get("page_size")
    if page_size is not None:
        page_size = as_int("page_size", page_size, 1, MAX_PAGE_SIZE)
        params["page_size"] = page_size

    offset = args.get("offset")
    if offset is not None:
        offset = as_int("offset", offset, 0)
        if section.get("paging") == "cursor":
            # This section pages by page number, so an offset only maps cleanly
            # when it lands on a page boundary.
            size = page_size if page_size is not None else DEFAULT_PAGE_SIZE
            if offset % size:
                raise ValueError(
                    f"this section pages in fixed steps: offset must be a multiple of page_size "
                    f"({size}), got {offset}"
                )
            params["cursor"] = offset // size + 1
        else:
            params["offset"] = offset

    return build_query_string(params)


SECTION_DESCRIPTION = f"Section name, one of: {', '.join(ALL_SECTIONS)}"

QUERY_DESCRIPTION = (
    'Search query string using field:value syntax, e.g. \'resolved : "false" AND severity : "critical"\'. '
    'Pass an empty string to list the whole section unfiltered — deliberately, since that can be '
    "a very large number of records. "
    "Fields can be combined with AND, OR, AND NOT, OR NOT and grouped with parentheses. "
    "A string value may contain * as a wildcard, in any position: name : \"VDI-5*\", "
    'file_name : "*.exe", domain : "*corp*". Without a wildcard a string value must match exactly. '
    "Numeric and date fields do not take wildcards. "
    "Call xdr_get_mapping for the fields a section accepts, xdr_get_filters for the values an "
    "enumerated field takes, and xdr_get_suggestions to look up the exact spelling of a value — a "
    "value outside the accepted set returns zero results rather than an error. "
    "Time filtering uses the timestamp field, relative (timestamp >= now-1d, now-6h) or absolute "
    '(timestamp >= "2024-01-01T00:00:00.000+03:00"). The calendar-rounding forms now/d, now/w and '
    "now/M return nothing, so use now-1d and the like. Note that timestamp is a search field: the "
    "ordering parameter takes an entirely different set of names. "
    'For the "applications" section, filter by asset, e.g. (asset: "<machine_id>"). '
    "A query the API cannot parse is not rejected — it is treated as free text, so verify that "
    "the results match what you asked for."
)

ORDERING_DESCRIPTION = (
    "Sort field with optional - prefix for descending, e.g. -created_at (newest first). "
    "An unknown field is silently ignored: the API reports no error and the results come back "
    "in some other order, so use only the fields listed here. Per section — "
    + "; ".join(f"{name}: {', '.join(s['ordering'])}" for name, s in SECTIONS.items())
    + "."
)

PAGE_SIZE_DESCRIPTION = f"Number of results per page (1-{MAX_PAGE_SIZE}, default {DEFAULT_PAGE_SIZE})"
OFFSET_DESCRIPTION = (
    'Number of results to skip. For the "modules" section the API pages in fixed steps, '
    "so offset must be a multiple of page_size there."
)

# Action fields the API accepts per basename; anything else is rejected server-side.
MATCHER_ACTIONS: dict[str, tuple[str, ...]] = {
    "alert": ("false_positive", "resolved", "on_hold", "related"),
    "envelope": ("false_positive", "resolved", "false_negative"),
    "attach": ("false_positive", "resolved"),
}
RESOLVE_REASONS = ("threat_eliminated", "not_confirmed", "clients_request", "api_request")
MATCHER_FIELDS = {
    "expression", "basename", "action", "comment", "resolve_reason",
    "is_global", "is_one_timer", "release_from_quarantine", "child_matchers",
}
CHILD_MATCHER_FIELDS = {"expression", "basename", "action", "release_from_quarantine", "resolve_reason"}


def validate_matcher(payload: Any, fields: set[str] = MATCHER_FIELDS, require_comment: bool = True) -> Any:
    """Check the payload before it reaches the API.

    The tool schema alone is not a control — a client is free to ignore it — and
    this endpoint takes a rule that is applied to everything the expression
    matches, so the payload is checked here rather than passed through.
    """
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")

    unknown = sorted(set(payload) - fields)
    if unknown:
        raise ValueError(f"unsupported field(s): {', '.join(unknown)}. Allowed: {', '.join(sorted(fields))}")

    expression = payload.get("expression")
    if not isinstance(expression, str) or not expression.strip():
        raise ValueError("expression is required and must be a non-empty string")

    basename = payload.get("basename")
    if basename not in MATCHER_ACTIONS:
        raise ValueError(
            f'basename must be one of: {", ".join(sorted(MATCHER_ACTIONS))} '
            f"(the API supports matchers for these only)"
        )

    action = payload.get("action")
    if not isinstance(action, dict) or len(action) != 1:
        raise ValueError("action must be an object with exactly one field")
    action_field = next(iter(action))
    if action_field not in MATCHER_ACTIONS[basename]:
        raise ValueError(
            f'action "{action_field}" is not allowed for basename "{basename}". '
            f'Allowed: {", ".join(MATCHER_ACTIONS[basename])}'
        )

    reason = payload.get("resolve_reason")
    if reason is not None and reason not in RESOLVE_REASONS:
        # The API silently rewrites an unknown reason to threat_eliminated and
        # still returns 201, so catch it here instead.
        raise ValueError(f'resolve_reason must be one of: {", ".join(RESOLVE_REASONS)}')

    if require_comment and action_field != "on_hold" and not str(payload.get("comment") or "").strip():
        raise ValueError('comment is required unless the action is "on_hold"')

    children = payload.get("child_matchers")
    if children is not None:
        if not isinstance(children, list):
            raise ValueError("child_matchers must be an array")
        for child in children:
            validate_matcher(child, fields=CHILD_MATCHER_FIELDS, require_comment=False)

    return payload


READ_ONLY_ANNOTATIONS = types.ToolAnnotations(readOnlyHint=True, openWorldHint=True)

SECTION_PROPERTY = {"type": "string", "description": SECTION_DESCRIPTION, "enum": ALL_SECTIONS}
PAGING_PROPERTIES = {
    "ordering": {"type": "string", "description": ORDERING_DESCRIPTION},
    "page_size": {
        "type": "integer", "minimum": 1, "maximum": MAX_PAGE_SIZE,
        "description": PAGE_SIZE_DESCRIPTION,
    },
    "offset": {"type": "integer", "minimum": 0, "description": OFFSET_DESCRIPTION},
}

MARK_EVENT_TOOL = types.Tool(
    name="xdr_mark_event",
    description=(
        "Create a matcher (POST /api/matchers/) in XDR — a rule that marks objects, e.g. resolve "
        "an alert or mark it as a false positive. Note what this does: 'expression' is a search "
        "expression, not an identifier, so the rule applies to EVERY object it matches, now and "
        "(unless is_one_timer is true) in future. It is applied asynchronously, the response does "
        "not contain the result, and this tool cannot undo it. Target a single object with "
        "'id: \"<event-id>\"' unless a broad rule is what you intend. "
        "Required: 'expression'; 'basename' — alert, envelope (emails) or attach (files); "
        "'action' — an object with exactly one field, allowed per basename: "
        "alert → false_positive, resolved, on_hold, related; envelope → false_positive, resolved, "
        "false_negative; attach → false_positive, resolved. 'comment' is required too, except when "
        "the action is on_hold. Incidents and assets cannot be marked through this endpoint. "
        "Optional: resolve_reason (threat_eliminated | not_confirmed | clients_request | "
        "api_request), is_one_timer (default true), release_from_quarantine (takes effect for "
        "attach and envelope only), child_matchers, is_global (applies the rule across all "
        "companies; the API accepts it from an instance owner only)."
    ),
    annotations=types.ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=True,
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "payload": {
                "type": "object",
                "additionalProperties": False,
                "required": ["expression", "basename", "action"],
                "properties": {
                    "expression": {
                        "type": "string",
                        "minLength": 1,
                        "description": (
                            'Search expression identifying the target(s), e.g. \'id: "<event-id>"\''
                        ),
                    },
                    "basename": {"type": "string", "enum": sorted(MATCHER_ACTIONS)},
                    "action": {
                        "type": "object",
                        "additionalProperties": False,
                        "minProperties": 1,
                        "maxProperties": 1,
                        "description": "Exactly one field, valid for the chosen basename",
                        "properties": {
                            "false_positive": {"type": "boolean"},
                            "false_negative": {"type": "boolean"},
                            "resolved": {"type": "boolean"},
                            "on_hold": {"type": "string", "description": "ISO-8601 datetime"},
                            "related": {"type": "string"},
                        },
                    },
                    "comment": {"type": "string", "minLength": 1},
                    "resolve_reason": {"type": "string", "enum": list(RESOLVE_REASONS)},
                    "is_global": {"type": "boolean"},
                    "is_one_timer": {"type": "boolean"},
                    "release_from_quarantine": {"type": "boolean"},
                    "child_matchers": {"type": "array", "items": {"type": "object"}},
                },
            },
        },
        "required": ["payload"],
    },
)

TOOLS = [
    types.Tool(
        name="xdr_search",
        description=(
            "Search for events/objects in an XDR section. Returns matching records; an empty "
            "query returns the section unfiltered. Use xdr_get_mapping first to discover "
            "available fields."
        ),
        annotations=READ_ONLY_ANNOTATIONS,
        inputSchema={
            "type": "object",
            "properties": {
                "section": SECTION_PROPERTY,
                "query": {"type": "string", "description": QUERY_DESCRIPTION},
                **PAGING_PROPERTIES,
            },
            "required": ["section", "query"],
        },
    ),
    types.Tool(
        name="xdr_count",
        description=(
            "Get the total count of records in an XDR section. "
            "Optionally filter by query to count matching records only."
        ),
        annotations=READ_ONLY_ANNOTATIONS,
        inputSchema={
            "type": "object",
            "properties": {
                "section": SECTION_PROPERTY,
                "query": {"type": "string", "description": QUERY_DESCRIPTION + " (optional)"},
            },
            "required": ["section"],
        },
    ),
    types.Tool(
        name="xdr_get_mapping",
        description=(
            "Get all available search fields for an XDR section. "
            "Returns field names, labels, categories, and supported operators. "
            "Call this first to understand what you can search on."
        ),
        annotations=READ_ONLY_ANNOTATIONS,
        inputSchema={
            "type": "object",
            "properties": {"section": SECTION_PROPERTY},
            "required": ["section"],
        },
    ),
    types.Tool(
        name="xdr_get_suggestions",
        description=(
            "Look up real values of a field, optionally narrowed by a prefix. Use it when a query "
            "returns nothing and the value may simply be spelled differently — an unmatched value "
            "is answered with zero records rather than an error, so a guess is indistinguishable "
            "from a genuine absence. Each entry has a value to put in a query and a label for "
            "display; search on the value. Returns up to about ten entries, so narrow the prefix "
            "to see more. Field names come from xdr_get_mapping, where the ones worth asking "
            "about carry show_suggestions; an unknown field name comes back empty rather than as "
            "an error."
        ),
        annotations=READ_ONLY_ANNOTATIONS,
        inputSchema={
            "type": "object",
            "properties": {
                "section": SECTION_PROPERTY,
                "field": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Field to list values of, as named by xdr_get_mapping",
                },
                "prefix": {
                    "type": "string",
                    "description": (
                        "Return only values starting with this. Omit for the field's first values."
                    ),
                },
            },
            "required": ["section", "field"],
        },
    ),
    types.Tool(
        name="xdr_get_filters",
        description="Get available filter options for an XDR section.",
        annotations=READ_ONLY_ANNOTATIONS,
        inputSchema={
            "type": "object",
            "properties": {"section": SECTION_PROPERTY},
            "required": ["section"],
        },
    ),
]

if ALLOW_WRITE:
    TOOLS.append(MARK_EVENT_TOOL)


async def on_list_tools(
    ctx: ServerRequestContext[Any],
    params: types.PaginatedRequestParams | None,
) -> types.ListToolsResult:
    return types.ListToolsResult(tools=TOOLS)


async def on_call_tool(
    ctx: ServerRequestContext[Any],
    params: types.CallToolRequestParams,
) -> types.CallToolResult:
    name = params.name
    args: dict[str, Any] = params.arguments or {}

    try:
        if name == "xdr_search":
            section = get_section(args.get("section"))
            qs = build_paging(section, args)
            # check/ is the list endpoint with the search taken from the body, so
            # an empty search returns the section unfiltered. Verified against a
            # live installation: identical rows, and the count only moves by the
            # ingest that happens between two calls.
            result = await xdr_request(
                "POST",
                f"/api/{section['path']}/check/{qs}",
                {"search": args.get("query") or ""},
            )

        elif name == "xdr_count":
            section = get_section(args.get("section"))
            if args.get("query"):
                result = await xdr_request(
                    "POST",
                    f"/api/{section['countPath']}/count/",
                    {"search": args.get("query")},
                )
            else:
                result = await xdr_request("GET", f"/api/{section['countPath']}/count/")

        elif name == "xdr_get_mapping":
            section = get_section(args.get("section"))
            result = await xdr_request("GET", f"/api/{section['path']}/mapping/")

        elif name == "xdr_get_suggestions":
            section = get_section(args.get("section"))
            field = args.get("field")
            if not isinstance(field, str) or not field.strip():
                raise ValueError("field is required and must be a non-empty string")
            qs = build_query_string({"field": field, "prefix": args.get("prefix") or None})
            result = await xdr_request("GET", f"/api/{section['path']}/search_help/{qs}")

        elif name == "xdr_get_filters":
            section = get_section(args.get("section"))
            result = await xdr_request("GET", f"/api/{section['path']}/filters/")

        elif name == "xdr_mark_event":
            if not ALLOW_WRITE:
                raise PermissionError(
                    "xdr_mark_event is disabled. Set XDR_ALLOW_WRITE=1 to enable writes to XDR."
                )
            payload = validate_matcher(args.get("payload"))
            result = await xdr_request("POST", "/api/matchers/", payload)

        else:
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=f"Unknown tool: {name}")],
                isError=True,
            )

        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]
        )
    except Exception as error:
        # Bound the text: an error can carry an API response body, and some
        # transport errors carry host names.
        detail = str(error)[:MAX_ERROR_CHARS]
        message = f"Error: {type(error).__name__}: {detail}" if detail else f"Error: {type(error).__name__}"
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=message)],
            isError=True,
        )


server = Server(
    "xdr-mcp",
    version=__version__,
    on_list_tools=on_list_tools,
    on_call_tool=on_call_tool,
)


async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def cli() -> None:
    """Console-script entry point."""
    asyncio.run(main())


if __name__ == "__main__":
    cli()
