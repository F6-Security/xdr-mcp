# Copyright 2026 Future Joint-Stock Company
# SPDX-License-Identifier: Apache-2.0

"""MCP server for the F6 XDR platform API."""

import asyncio
import fnmatch
import json
import os
import re
import shutil
import ssl
import sys
import uuid
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

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

# Starting a collection acts on a host, as SYSTEM or root and with whatever
# commands the chosen custom artifacts carry. That is a different
# thing from marking alerts, so it has a switch of its own: a setup that lets the
# model resolve alerts has not thereby agreed to it acting on hosts.
ALLOW_COLLECT = os.environ.get("XDR_ALLOW_COLLECT", "").strip().lower() in ("1", "true", "yes")

# With XDR_PASSWORD_PROMPT the second factor a protected collection needs goes
# from the person at this machine straight to the API: when the API turns the
# collection down, the server asks for it in a window of its own. The model never
# sees it, since it is not an argument, not in a result and not in an error.
PASSWORD_PROMPT = os.environ.get("XDR_PASSWORD_PROMPT", "").strip().lower() in ("1", "true", "yes")
PASSWORD_PROMPT_SECONDS = 120

# Collected artifacts run from a few kilobytes of logs to memory dumps of many
# gigabytes, so a download is capped unless the caller asks for more.
DEFAULT_ARTIFACT_PAGE_SIZE = 20
DEFAULT_DOWNLOAD_MB = 2048
MAX_DOWNLOAD_MB = 100 * 1024


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


def _download_dir() -> str:
    """Resolve the folder xdr_download_artifact saves into.

    The tool is opt-in, like xdr_mark_event: without XDR_DOWNLOAD_DIR it is not
    advertised. Artifacts can hold a host's memory, credentials or shell
    history, so where they land is the operator's choice rather than a default
    the server picks. A folder that does not exist fails the server on startup,
    not on the first download.
    """
    raw = os.environ.get("XDR_DOWNLOAD_DIR", "").strip()
    if not raw:
        return ""
    path = os.path.abspath(os.path.expanduser(raw))
    if not os.path.isdir(path):
        raise RuntimeError(f"XDR_DOWNLOAD_DIR is not a directory: {raw}")
    return path


BASE_URL = _validated_base_url()
API_KEY = (os.environ.get("XDR_API_KEY") or "").strip()
VERIFY = _verification()
DOWNLOAD_DIR = _download_dir()


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


# Collected artifacts, "forensic reports" in the API: what an analyst gathers
# from a host — logs, configs, a bug report, a memory dump — kept by the
# platform as one zip per collection. Fields kept from a list row; the rest is
# bookkeeping the model has no use for.
ARTIFACT_FIELDS = (
    "id", "internal_id", "ts_created", "asset_id", "asset_name", "machine_id", "asset_status",
    "asset_version", "parts", "command_status", "upload_progress", "size", "user", "error",
)
ARTIFACT_ORDERINGS = ("-ts_created", "ts_created")

# What the download endpoint answers for a path that is not in the report: a
# 200 with an empty zip, the end-of-central-directory record alone, and no file
# name. Verified against a live installation.
EMPTY_ZIP_BYTES = 22


def as_uuid(name: str, value: Any) -> str:
    """Validate a UUID argument; it goes into a URL path, so nothing else may."""
    try:
        return str(uuid.UUID(str(value)))
    except ValueError as error:
        raise ValueError(f"{name} must be a UUID, got {value!r}") from error


def artifact_path(value: Any) -> str:
    """Normalise a path inside an artifact to '/a/b' form, '/' for all of it."""
    if value is None:
        return "/"
    if not isinstance(value, str):
        raise ValueError("path must be a string")
    parts = [part for part in value.replace("\\", "/").split("/") if part]
    if ".." in parts:
        raise ValueError("path must not contain '..'")
    return "/" + "/".join(parts)


def artifact_file_name(internal_id: str, disposition: str) -> str:
    """Build a safe local file name from Content-Disposition.

    The API names a part after its full path inside the report
    ("var/log/x.log.zip") and the whole report after a timestamp with colons;
    neither is usable as a file name on every platform as it stands, and the
    header is the server's to choose, so nothing in it may steer the path.
    """
    match = re.search(r'filename="([^"]*)"', disposition)
    name = match.group(1) if match else ""
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._") or "artifact.zip"
    return f"{internal_id[:8]}_{name}"


async def list_artifacts(args: dict[str, Any]) -> dict[str, Any]:
    asset_id = args.get("asset_id")
    if asset_id is not None:
        asset_id = as_int("asset_id", asset_id, 1)
    elif args.get("machine_id"):
        machine_id = as_uuid("machine_id", args["machine_id"])
        # The list ignores a machine_id filter and returns every report, so the
        # asset card turns it into the asset id the list does filter on.
        asset = await xdr_request("GET", f"/api/assets/{machine_id}/")
        asset_id = (asset.get("data") or {}).get("id")
        if not asset_id:
            raise LookupError(f"no asset with machine_id {machine_id}")

    page = as_int("page", args.get("page", 1), 1)
    page_size = as_int("page_size", args.get("page_size", DEFAULT_ARTIFACT_PAGE_SIZE), 1, MAX_PAGE_SIZE)
    ordering = args.get("ordering") or "-ts_created"
    if ordering not in ARTIFACT_ORDERINGS:
        raise ValueError(f"ordering must be one of: {', '.join(ARTIFACT_ORDERINGS)}")

    # This endpoint pages by page number under the name "cursor"; "page" and
    # "offset" are ignored and bring back the first page again.
    qs = build_query_string(
        {"asset_id": asset_id, "ordering": ordering, "cursor": page, "page_size": page_size}
    )
    result = await xdr_request("GET", f"/api/assets/forensic-reports/{qs}")
    data = result.get("data") or {}
    return {
        "count": data.get("count"),
        "page": page,
        "next_page": data.get("next"),
        "results": [{key: row.get(key) for key in ARTIFACT_FIELDS} for row in data.get("results") or []],
    }


async def get_artifact_structure(args: dict[str, Any]) -> dict[str, Any]:
    report_id = as_int("report_id", args.get("report_id"), 1)
    path = artifact_path(args.get("path"))
    qs = build_query_string({"path": path if path != "/" else None})
    result = await xdr_request(
        "GET", f"/api/assets/forensic-reports/{report_id}/forensic-report/structure/{qs}"
    )
    return {"report_id": report_id, "path": path, "entries": result.get("data")}


async def download_artifact(args: dict[str, Any]) -> dict[str, Any]:
    """Stream an artifact, or one part of it, as a zip into DOWNLOAD_DIR.

    Only the saved path and size go back to the model: the content itself stays
    on disk, out of the context.
    """
    if not BASE_URL or not API_KEY:
        raise RuntimeError("XDR_BASE_URL and XDR_API_KEY environment variables must be set")

    internal_id = as_uuid("internal_id", args.get("internal_id"))
    path = artifact_path(args.get("path"))
    max_mb = as_int("max_mb", args.get("max_mb", DEFAULT_DOWNLOAD_MB), 1, MAX_DOWNLOAD_MB)
    limit = max_mb * 1024 * 1024

    # The path travels as a single URL segment, slashes included ("/" becomes
    # %2F) — the way the console's own download link spells it.
    url = f"{BASE_URL}/api/assets/forensic-reports/{internal_id}/download/{quote(path, safe='')}/"
    headers = {"x-api-key": API_KEY, "User-Agent": USER_AGENT}

    # The read timeout applies per chunk rather than to the whole transfer, so a
    # large report that keeps streaming is not cut off.
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(60.0, read=600.0), trust_env=False, follow_redirects=False, verify=VERIFY
    ) as client:
        async with client.stream("GET", url, headers=headers) as response:
            content_type = response.headers.get("content-type", "")
            # An unknown route is answered with the console's HTML page and a
            # 200, so the content type is checked as well as the status.
            if not response.is_success or not content_type.startswith("application/octet-stream"):
                detail = ""
                if content_type.startswith("application/json"):
                    detail = (await response.aread()).decode("utf-8", "replace")[:MAX_ERROR_CHARS]
                suffix = f": {detail}" if detail else ""
                raise RuntimeError(
                    f"API error {response.status_code} ({content_type or 'no content type'}){suffix}"
                )

            declared = response.headers.get("content-length", "")
            if declared.isdigit() and int(declared) > limit:
                raise RuntimeError(
                    f"the artifact is {int(declared)} bytes, over max_mb={max_mb}; raise max_mb or "
                    f"pick a smaller path with xdr_get_artifact_structure"
                )

            disposition = response.headers.get("content-disposition", "")
            target = os.path.join(DOWNLOAD_DIR, artifact_file_name(internal_id, disposition))
            partial = target + ".part"
            size = 0
            try:
                with open(partial, "wb") as file:
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > limit:
                            raise RuntimeError(
                                f"the download passed max_mb={max_mb} and was stopped; raise max_mb "
                                f"or pick a smaller path with xdr_get_artifact_structure"
                            )
                        file.write(chunk)
            except BaseException:
                if os.path.exists(partial):
                    os.remove(partial)
                raise

    if size == EMPTY_ZIP_BYTES and 'filename=""' in disposition:
        os.remove(partial)
        raise FileNotFoundError(
            f"nothing at {path} in artifact {internal_id}; see xdr_get_artifact_structure"
        )

    os.replace(partial, target)
    return {"internal_id": internal_id, "path": path, "file": target, "bytes": size}


# Starting a collection, the console's Response → Collect artifacts. The agent
# type goes to the API as unit_type and comes from the host, never from the
# caller. The asset card does not carry unit_type, but its forensic_options lists
# what the host's agent type offers, and each type's list has a name no other one
# has: the marker below. An artifact name the API does not know would be dropped
# without a word, so names are checked here rather than collected as nothing.
COLLECT_TARGETS: dict[str, dict[str, Any]] = {
    "huntpoint": {
        "os": "Windows",
        "marker": "files_to_upload",
        "paths_field": "files_to_upload",
        "commands_field": "commands_to_execute",
        "artifacts": (
            "browser_history", "bugreport", "certutil", "crash_dumps", "diagnostics", "event_log",
            "handles", "hiber_file", "log_file", "master_file_table", "memory_dump", "page_file",
            "perf_logs", "powershell_console_history", "prefetch", "recent_file_cache", "recent_files",
            "registry", "scheduler_tasks", "srum", "sum", "temp_dir", "usn_journal",
        ),
    },
    "edr_linux": {
        "os": "Linux",
        "marker": "dpkg_packages",
        "paths_field": "custom_paths",
        "commands_field": "executable_commands",
        "artifacts": (
            "apparmor_configs", "bugreport", "cron_jobs", "devices", "dns_resolvers", "dpkg_packages",
            "fstab", "groups", "hosts", "init_subsystem", "ip_routes", "kernel_modules", "locks",
            "logged_users", "memory_info", "mounts", "network_interfaces", "network_tables",
            "pacman_packages", "rpm_packages", "se_linux_configs", "shadow", "shell_history",
            "ssh_configs", "sudoers_configs", "system_controls", "unix_sockets",
        ),
    },
    "edr_macos": {
        "os": "macOS",
        "marker": "provision_profiles",
        "paths_field": "custom_paths",
        "commands_field": "executable_commands",
        "artifacts": (
            "agent_logs", "bugreport", "etc", "logs", "provision_profiles", "shell", "ssh", "startup_items",
        ),
    },
}
COLLECT_ARGUMENTS = {"machine_id", "artifacts", "paths", "preset_ids", "otp"}

# Parts the API guards with the user's second factor, the code from their
# authenticator app. It waives the code for a personal token whose user has an
# IP allowlist and asks for it otherwise.
PROTECTED_PARTS = frozenset({
    "bugreport", "custom_paths", "files_to_upload", "hiber_file", "memory_dump", "network_tables",
    "page_file", "preset_ids", "shadow", "shell_history", "ssh_configs", "sudoers_configs",
})

# A collection in one of these is still under way, and a host takes one at a time.
COLLECTION_IN_FLIGHT = ("New", "Queued", "Pending", "Uploading")

# Limits and path rules for one collection. A path outside them fails the whole
# collection, not just that path.
MAX_COLLECT_ITEMS = 256
MAX_PRESETS = 100
WINDOWS_LENGTH_LIMIT = 8191
WINDOWS_LENGTH_RESERVED = 500
WINDOWS_PATH = re.compile(r"[A-Za-z]:\\[^/]*")
POSIX_UNSAFE_CHARS = frozenset('$"`\\')
LASTLOG = ("var", "log", "lastlog")


def windows_path_problem(path: str) -> str | None:
    if any(ord(char) < 32 for char in path):
        return "must not contain control characters"
    if not WINDOWS_PATH.fullmatch(path):
        return r"must be absolute and use backslashes, like C:\Windows\Prefetch"
    if path.endswith("\\"):
        return "must not end with a backslash"
    if '"' in path or "%" in path:
        return 'must not contain " or %'
    if "[" in path or "]" in path:
        return "must not contain [ or ]; take the folder instead"
    seen_star = False
    for part in path.split("\\"):
        if "?" in part and not seen_star and "*" not in part.split("?")[0]:
            return "may use ? only after a *"
        seen_star = seen_star or "*" in part
    return None


def posix_path_problem(path: str, linux: bool = True) -> str | None:
    if any(ord(char) < 32 for char in path):
        return "must not contain control characters"
    if not path.startswith("/"):
        return "must be absolute"
    bad = sorted(set(path) & POSIX_UNSAFE_CHARS)
    if bad:
        return f"must not contain {' '.join(bad)}"
    parts = [part for part in path.split("/") if part]
    if linux and len(parts) <= len(LASTLOG) and all(
        fnmatch.fnmatchcase(name, part) for part, name in zip(parts, LASTLOG, strict=False)
    ):
        # lastlog is a sparse file and can be very large.
        return "takes in /var/log/lastlog, which can be very large; name the logs instead"
    return None


def string_list(name: str, value: Any, limit: int) -> list[str]:
    """A list of non-empty strings, trimmed, with repeats dropped."""
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise ValueError(f"{name} must be an array of non-empty strings")
    items = list(dict.fromkeys(item.strip() for item in value))
    if len(items) > limit:
        raise ValueError(f"{name} takes at most {limit} entries, got {len(items)}")
    return items


async def artifact_presets() -> list[dict[str, Any]]:
    """Custom artifacts the token's user can see: stored lists of paths or commands."""
    result = await xdr_request("GET", "/api/assets/forensic-reports/presets/")
    rows = result.get("data") if isinstance(result, dict) else result
    presets = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        params = row.get("params") or {}
        presets.append({
            "id": row.get("id"),
            "name": row.get("name"),
            "description": row.get("description"),
            "commands": params.get("commands") or [],
            "paths": params.get("paths") or [],
            "company_ids": row.get("company_ids") or [],
        })
    return presets


async def list_artifact_presets(args: dict[str, Any]) -> dict[str, Any]:
    search = args.get("search")
    if search is not None and not isinstance(search, str):
        raise ValueError("search must be a string")
    presets = await artifact_presets()
    if search and search.strip():
        needle = search.strip().casefold()
        presets = [
            preset for preset in presets
            if needle in f"{preset['name'] or ''}\n{preset['description'] or ''}".casefold()
        ]
    return {"count": len(presets), "presets": presets}


def api_message(error: Exception) -> str:
    """The console's own message out of an "API error ...: {json}" text, or ''."""
    text = str(error)
    start = text.find("{")
    try:
        body = json.loads(text[start:]) if start >= 0 else {}
    except ValueError:
        return ""
    messages = body.get("messages") if isinstance(body, dict) else None
    return "; ".join(
        str(item.get("message") or "") for item in messages or [] if isinstance(item, dict)
    ).strip()


async def ask_password(prompt: str) -> str | None:
    """Ask the person at this machine for a secret in a window of its own.

    The prompt goes in as an argument, never into the script itself, so nothing
    in it can run. The window's own process has no stdin and its stdout is a
    pipe: this server's stdio is the MCP channel. None if they cancel, or let
    the window time out.
    """
    if sys.platform == "darwin":
        argv = [
            "/usr/bin/osascript",
            "-e", "on run argv",
            "-e", (
                'set answer to display dialog (item 1 of argv) default answer "" with hidden answer '
                'with title "F6 XDR" buttons {"Отмена", "Отправить"} default button "Отправить" '
                f'cancel button "Отмена" with icon caution giving up after {PASSWORD_PROMPT_SECONDS}'
            ),
            "-e", "if gave up of answer then error number -128",
            "-e", "return text returned of answer",
            "-e", "end run",
            prompt,
        ]
    elif shutil.which("zenity"):
        # zenity reads Pango markup in --text
        text = prompt.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        argv = [
            "zenity", "--entry", "--hide-text", "--title=F6 XDR", f"--text={text}",
            f"--timeout={PASSWORD_PROMPT_SECONDS}",
        ]
    else:
        raise RuntimeError("XDR_PASSWORD_PROMPT needs macOS or zenity to show a password window")

    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        out, _ = await asyncio.wait_for(process.communicate(), PASSWORD_PROMPT_SECONDS + 15)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        return None
    if process.returncode != 0:
        return None
    secret = out.decode("utf-8", "replace")
    secret = secret[:-1] if secret.endswith("\n") else secret
    return secret or None


async def post_collection(body: dict[str, Any], what: str) -> dict[str, Any]:
    """Create the collection; with XDR_PASSWORD_PROMPT, get the second factor from a window if asked.

    The first request goes without it. The API checks the second factor before
    it looks the host up, so a refused request has created nothing, and a token
    that needs no code is never asked for one.
    """
    try:
        return await xdr_request("POST", "/api/assets/forensic-reports/", body)
    except RuntimeError as error:
        protected = sorted(PROTECTED_PARTS & set(body))
        if not protected or not str(error).startswith("API error 400"):
            raise
        if PASSWORD_PROMPT and "password" not in body:
            message = api_message(error)
            secret = await ask_password(
                f"Сбор артефактов: {what}.\n\n"
                + (f"Консоль: «{message}».\n\n" if message else "")
                + "Введите одноразовый код или пароль, который просит консоль. "
                "Он уйдёт прямо в консоль, модель его не увидит."
            )
            if not secret:
                raise RuntimeError(
                    f"{error}. The user closed the password window, so nothing was collected"
                ) from None
            try:
                retry = {**body, "password": secret}
                return await xdr_request("POST", "/api/assets/forensic-reports/", retry)
            except RuntimeError as retry_error:
                if api_message(retry_error) != message:
                    raise  # the second factor was taken; this is some other refusal
                raise RuntimeError(
                    f"{retry_error}. The console turned down what the user typed in the window, so nothing "
                    f"was collected. Calling again asks once more; three wrong codes lock the user out for "
                    f"five minutes"
                ) from None
        raise RuntimeError(
            f"{error}. This collection includes parts the platform guards with the user's second factor "
            f"({', '.join(protected)}). If the message is about the code, ask the user for the current "
            f"code from their authenticator app and call again with otp; three wrong codes lock them out "
            f"for five minutes"
        ) from error


async def recent_collections(asset_id: int) -> list[dict[str, Any]]:
    qs = build_query_string({"asset_id": asset_id, "ordering": "-ts_created", "cursor": 1, "page_size": 10})
    result = await xdr_request("GET", f"/api/assets/forensic-reports/{qs}")
    return (result.get("data") or {}).get("results") or []


async def collect_artifacts(args: dict[str, Any]) -> dict[str, Any]:
    """Start a collection on one host after checking it against the host's card.

    Everything that would make the collection fail quietly is caught before
    the request: a wrong agent type, an artifact the host does not offer, a
    path outside the rules, a second collection while one is under way.
    """
    unknown = sorted(set(args) - COLLECT_ARGUMENTS)
    if unknown:
        raise ValueError(
            f"unsupported argument(s): {', '.join(unknown)}. Allowed: "
            f"{', '.join(sorted(COLLECT_ARGUMENTS))}. Commands are not taken directly: run them through "
            f"a custom artifact (preset_ids)"
        )
    machine_id = as_uuid("machine_id", args.get("machine_id"))
    artifacts = string_list("artifacts", args.get("artifacts"), MAX_COLLECT_ITEMS)
    paths = string_list("paths", args.get("paths"), MAX_COLLECT_ITEMS)
    preset_ids = string_list("preset_ids", args.get("preset_ids"), MAX_PRESETS)
    if not (artifacts or paths or preset_ids):
        raise ValueError("nothing to collect: name at least one artifact, path or preset")
    otp = args.get("otp")
    if otp is not None and (not isinstance(otp, str) or not re.fullmatch(r"\d{6,8}", otp)):
        raise ValueError("otp must be the 6-8 digit code from the user's authenticator app")

    card = (await xdr_request("GET", f"/api/assets/{machine_id}/")).get("data") or {}
    asset_id = card.get("id")
    if not asset_id:
        raise LookupError(f"no asset with machine_id {machine_id}")
    offered = card.get("forensic_options")
    if not isinstance(offered, list):
        offered = []
    unit_type, target = next(
        ((name, spec) for name, spec in COLLECT_TARGETS.items() if spec["marker"] in offered), (None, None)
    )
    if target is None:
        raise ValueError(f"asset {machine_id} has no EDR agent to collect artifacts from")
    if card.get("archived"):
        raise ValueError(f"asset {machine_id} is archived, and the platform does not collect from it")
    features = card.get("features") if isinstance(card.get("features"), dict) else {}
    if features.get("collect_artifacts") is False:
        raise ValueError(f"collecting artifacts is turned off for the agent on {machine_id}")
    os_name = target["os"]

    wrong = [name for name in artifacts if name not in target["artifacts"]]
    if wrong:
        raise ValueError(
            f"not a {os_name} artifact: {', '.join(wrong)}. {os_name} has: {', '.join(target['artifacts'])}"
        )
    for path in paths:
        if os_name == "Windows":
            problem = windows_path_problem(path)
        else:
            problem = posix_path_problem(path, linux=os_name == "Linux")
        if problem:
            raise ValueError(f"path {path!r} {problem}")

    presets: list[dict[str, Any]] = []
    if preset_ids:
        by_id = {preset["id"]: preset for preset in await artifact_presets()}
        missing = [preset_id for preset_id in preset_ids if preset_id not in by_id]
        if missing:
            raise ValueError(
                f"no custom artifact {', '.join(missing)} visible to this token; "
                f"see xdr_list_artifact_presets"
            )
        presets = [by_id[preset_id] for preset_id in preset_ids]

    # The platform merges presets into the request, repeats dropped; the limits
    # apply to the merged lists.
    commands = list(dict.fromkeys(command for preset in presets for command in preset["commands"]))
    all_paths = list(dict.fromkeys(paths + [path for preset in presets for path in preset["paths"]]))
    for name, items in (("paths", all_paths), ("commands", commands)):
        if len(items) > MAX_COLLECT_ITEMS:
            raise ValueError(
                f"{len(items)} {name} with the presets merged in, over the {MAX_COLLECT_ITEMS} "
                f"one collection takes"
            )
    if os_name == "Windows":
        # Roughly the size of the paths and commands together.
        length = WINDOWS_LENGTH_RESERVED + sum(len(path) + 3 for path in all_paths) + sum(
            len(command) + command.count('"') + 3 for command in commands
        )
        if length > WINDOWS_LENGTH_LIMIT:
            raise ValueError(
                f"the paths and commands come to about {length} characters, over the "
                f"{WINDOWS_LENGTH_LIMIT} one Windows collection takes; split it into several"
            )

    needed = list(artifacts)
    if all_paths:
        needed.append(target["paths_field"])
    if commands:
        needed.append(target["commands_field"])
    lacking = [name for name in needed if name not in offered]
    if lacking:
        raise ValueError(
            f"the agent on this host does not offer {', '.join(lacking)}; it offers: {', '.join(offered)}"
        )
    if commands and features.get("triage_execute_commands") is False:
        raise ValueError("the agent on this host is not allowed to run commands, and the presets carry some")

    busy = next((row for row in await recent_collections(asset_id)
                 if row.get("command_status") in COLLECTION_IN_FLIGHT), None)
    if busy:
        raise RuntimeError(
            f"collection {busy.get('id')} on this host is still {busy.get('command_status')} (started "
            f"{busy.get('ts_created')} by {busy.get('user')}), and a host takes one at a time. "
            f"Wait for it, or stop it with xdr_abort_artifact_collection"
        )

    # The card's own spelling of the machine id: the API looks it up as an exact term.
    body: dict[str, Any] = {"unit_type": unit_type, "asset": card.get("machine_id") or machine_id}
    body.update(dict.fromkeys(artifacts, True))
    if paths:
        body[target["paths_field"]] = paths
    if preset_ids:
        body["preset_ids"] = preset_ids
    if otp:
        body["password"] = otp

    host = card.get("alias") or card.get("current_name") or body["asset"]
    what = list(artifacts)
    if paths:
        what.append("пути " + ", ".join(paths[:5]) + (f" и ещё {len(paths) - 5}" if len(paths) > 5 else ""))
    if presets:
        what.append("пользовательские артефакты " + ", ".join(f"«{preset['name']}»" for preset in presets))
    created = await post_collection(body, f"{host} ({os_name}) — {'; '.join(what)}")

    report_id = (created.get("data") or {}).get("id")
    row = next((row for row in await recent_collections(asset_id) if row.get("id") == report_id), None)
    return {
        "report_id": report_id,
        "machine_id": body["asset"],
        "host": card.get("alias") or card.get("current_name"),
        "os": os_name,
        "host_status": card.get("status"),
        "requested": {
            "artifacts": artifacts,
            "paths": paths,
            "presets": [preset["name"] for preset in presets],
        },
        "collection": {key: row.get(key) for key in ARTIFACT_FIELDS} if row else None,
        "next": (
            "Follow it with xdr_list_artifacts by machine_id; command_status goes New, Queued or Pending, "
            "Uploading, then Done or Failed. A host that is offline picks the collection up when it is back."
        ),
    }


async def abort_collection(args: dict[str, Any]) -> dict[str, Any]:
    """Stop a collection, only one of the given host's and only while it is under way.

    The API also takes an abort for a collection that has finished and answers
    that it was stopped. It stops nothing, and afterwards the archive's
    structure can no longer be read while the list still shows Done (seen on a
    live installation). A mistyped id would do that to someone's collection.
    """
    machine_id = as_uuid("machine_id", args.get("machine_id"))
    report_id = as_int("report_id", args.get("report_id"), 1)
    asset_id = ((await xdr_request("GET", f"/api/assets/{machine_id}/")).get("data") or {}).get("id")
    if not asset_id:
        raise LookupError(f"no asset with machine_id {machine_id}")
    row = next((row for row in await recent_collections(asset_id) if row.get("id") == report_id), None)
    if row is None:
        raise LookupError(f"collection {report_id} is not among the recent collections of {machine_id}")
    if row.get("command_status") not in COLLECTION_IN_FLIGHT:
        raise ValueError(
            f"collection {report_id} is {row.get('command_status')}, not under way, so there is nothing to "
            f"stop; an abort would only leave its archive unreadable"
        )
    result = await xdr_request("POST", f"/api/assets/forensic-reports/{report_id}/forensic-report/abort/", {})
    return {"report_id": report_id, "result": result}


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

DOWNLOAD_ARTIFACT_TOOL = types.Tool(
    name="xdr_download_artifact",
    description=(
        "Download a collected artifact, or one file or folder inside it, as a zip into the folder "
        "set by XDR_DOWNLOAD_DIR, and return where it was saved and its size. The content is not "
        "returned. Takes 'internal_id' (UUID) from xdr_list_artifacts; 'path' is a file or folder "
        "inside the artifact as xdr_get_artifact_structure shows it (e.g. /var/log/syslog), and the "
        "default '/' is the whole artifact. Artifacts can be many gigabytes: the download is refused "
        "or stopped above max_mb, so pick the part you need instead of raising it. Reads from XDR "
        "only; the one thing it writes is the local file."
    ),
    annotations=types.ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "internal_id": {
                "type": "string",
                "description": "Artifact UUID ('internal_id' in xdr_list_artifacts)",
            },
            "path": {
                "type": "string",
                "description": "File or folder inside the artifact; default '/' (all of it)",
            },
            "max_mb": {
                "type": "integer", "minimum": 1, "maximum": MAX_DOWNLOAD_MB,
                "description": f"Size cap in MiB (default {DEFAULT_DOWNLOAD_MB})",
            },
        },
        "required": ["internal_id"],
    },
)

COLLECT_ARTIFACTS_TOOL = types.Tool(
    name="xdr_collect_artifacts",
    description=(
        "Start collecting artifacts from one host — the console's Response → Collect artifacts. The "
        "agent gathers them with SYSTEM or root rights and uploads one zip, which xdr_list_artifacts, "
        "xdr_get_artifact_structure and xdr_download_artifact then reach. Start one only when the user "
        "has asked for this host: it loads the host and goes into the audit log under the token's user. "
        "Takes the host's 'machine_id' and at least one of: "
        "'artifacts', named sets per OS — "
        + "; ".join(f"{t['os']}: {', '.join(t['artifacts'])}" for t in COLLECT_TARGETS.values())
        + " (an older agent offers fewer; the host's card is checked); "
        "'paths', files or folders to take (Windows: absolute with backslashes, no trailing backslash, "
        "[ and ] are not allowed, ? only after a *; Linux and macOS: absolute, without $ \" ` or "
        "backslash; * works within one path component and a folder is taken whole); 'preset_ids', "
        "custom artifacts from xdr_list_artifact_presets, stored lists of paths or commands made in the "
        "console — their commands run on the host. Arbitrary commands are not taken. memory_dump, "
        "page_file and hiber_file run to gigabytes. The platform asks for the user's one-time code for "
        + ", ".join(sorted(PROTECTED_PARTS - {"custom_paths", "files_to_upload", "preset_ids"}))
        + ", paths and presets, unless the token's user has an IP allowlist. "
        + (
            "When it asks, this server asks the user for it in a window of its own and sends it straight "
            "to the console. Do not ask for it in the chat and do not pass otp; if the user closes the "
            "window, nothing is collected. "
            if PASSWORD_PROMPT else
            "When it asks, ask the user for the current code and pass it as 'otp'. Never guess it: three "
            "wrong codes lock the user out for five minutes. "
        )
        + "A host takes one collection at a time, so "
        "this refuses while one is still under way there. Returns the new collection's id."
    ),
    annotations=types.ToolAnnotations(
        readOnlyHint=False,
        # A preset's commands can change the host.
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=True,
    ),
    inputSchema={
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "machine_id": {
                "type": "string",
                "description": "Agent machine id (UUID) of the host, as in the assets section",
            },
            "artifacts": {
                "type": "array",
                "uniqueItems": True,
                "items": {
                    "type": "string",
                    "enum": sorted({name for t in COLLECT_TARGETS.values() for name in t["artifacts"]}),
                },
                "description": "Named artifacts; only the host's OS's names apply",
            },
            "paths": {
                "type": "array",
                "maxItems": MAX_COLLECT_ITEMS,
                "items": {"type": "string", "minLength": 1},
                "description": r"Files or folders on the host, e.g. C:\Windows\Tasks or /etc/cron.d",
            },
            "preset_ids": {
                "type": "array",
                "maxItems": MAX_PRESETS,
                "items": {"type": "string", "minLength": 1},
                "description": "Custom artifact ids from xdr_list_artifact_presets",
            },
            "otp": {
                "type": "string",
                "pattern": "^[0-9]{6,8}$",
                "description": "The user's current one-time code, only when the platform asks for it",
            },
        },
        "required": ["machine_id"],
    },
)

LIST_ARTIFACT_PRESETS_TOOL = types.Tool(
    name="xdr_list_artifact_presets",
    description=(
        "List custom artifacts for xdr_collect_artifacts: named lists of paths to take or commands to "
        "run on a host, made in the console. Each has 'id', 'name', 'description', 'commands' and "
        "'paths'. A custom artifact has no OS field, so tell the OS by its name and contents. Several "
        "chosen together are merged into one collection."
    ),
    annotations=READ_ONLY_ANNOTATIONS,
    inputSchema={
        "type": "object",
        "properties": {
            "search": {
                "type": "string",
                "description": "Keep only those whose name or description contains this (any case)",
            },
        },
    },
)

ABORT_COLLECTION_TOOL = types.Tool(
    name="xdr_abort_artifact_collection",
    description=(
        "Stop a collection that is still under way (command_status New, Queued, Pending or Uploading). "
        "Takes the host's 'machine_id' and the collection's numeric 'id' from xdr_list_artifacts or "
        "xdr_collect_artifacts; it refuses a collection of another host or one that has finished, since "
        "the platform would take that abort too and leave the finished archive unreadable. A small "
        "collection can finish within a minute, and one that finishes as the abort arrives may lose its "
        "archive the same way. What the agent has not yet uploaded is lost, so stop only a collection "
        "the user wants stopped."
    ),
    annotations=types.ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=True,
        openWorldHint=True,
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "machine_id": {
                "type": "string",
                "description": "Agent machine id (UUID) of the host the collection runs on",
            },
            "report_id": {
                "type": "integer", "minimum": 1,
                "description": "Numeric collection id ('id' in xdr_list_artifacts)",
            },
        },
        "required": ["machine_id", "report_id"],
    },
)

COLLECT_TOOLS = [LIST_ARTIFACT_PRESETS_TOOL, COLLECT_ARTIFACTS_TOOL, ABORT_COLLECTION_TOOL]

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
    types.Tool(
        name="xdr_list_artifacts",
        description=(
            "List artifacts collected from hosts — what an analyst gathered through the asset's "
            "Response → Artifacts: bug reports, logs, configs, memory dumps. Filter by host with "
            "asset_id (the asset's numeric id) or machine_id; with neither, lists artifacts across "
            "all hosts. Newest first by default. Each row has 'id' (for xdr_get_artifact_structure), "
            "'internal_id' (for xdr_download_artifact), 'parts' (what was collected), "
            "'command_status' (New, Queued, Pending, Uploading, Uploaded, Done, Failed, Aborted) and 'size' "
            "in bytes."
        ),
        annotations=READ_ONLY_ANNOTATIONS,
        inputSchema={
            "type": "object",
            "properties": {
                "asset_id": {"type": "integer", "minimum": 1, "description": "Numeric asset id"},
                "machine_id": {
                    "type": "string",
                    "description": "Agent machine id (UUID); looked up to the asset id first",
                },
                "ordering": {
                    "type": "string",
                    "enum": list(ARTIFACT_ORDERINGS),
                    "description": "By creation time; default -ts_created (newest first)",
                },
                "page": {"type": "integer", "minimum": 1, "description": "1-based page number"},
                "page_size": {
                    "type": "integer", "minimum": 1, "maximum": MAX_PAGE_SIZE,
                    "description": f"Rows per page (1-{MAX_PAGE_SIZE}, default {DEFAULT_ARTIFACT_PAGE_SIZE})",
                },
            },
        },
    ),
    types.Tool(
        name="xdr_get_artifact_structure",
        description=(
            "List the files and folders inside a collected artifact, one level at a time, with their "
            "sizes. Takes the artifact's numeric 'id' from xdr_list_artifacts and an optional folder "
            "inside it (e.g. /var/log). Use it to find the part worth downloading rather than "
            "fetching a whole artifact."
        ),
        annotations=READ_ONLY_ANNOTATIONS,
        inputSchema={
            "type": "object",
            "properties": {
                "report_id": {
                    "type": "integer", "minimum": 1,
                    "description": "Numeric artifact id ('id' in xdr_list_artifacts)",
                },
                "path": {"type": "string", "description": "Folder inside the artifact; omit for the top"},
            },
            "required": ["report_id"],
        },
    ),
]

if DOWNLOAD_DIR:
    TOOLS.append(DOWNLOAD_ARTIFACT_TOOL)

if ALLOW_COLLECT:
    TOOLS.extend(COLLECT_TOOLS)

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

        elif name == "xdr_list_artifacts":
            result = await list_artifacts(args)

        elif name == "xdr_get_artifact_structure":
            result = await get_artifact_structure(args)

        elif name == "xdr_download_artifact":
            if not DOWNLOAD_DIR:
                raise PermissionError(
                    "xdr_download_artifact is disabled. Set XDR_DOWNLOAD_DIR to a folder to enable it."
                )
            result = await download_artifact(args)

        elif name in (tool.name for tool in COLLECT_TOOLS):
            if not ALLOW_COLLECT:
                raise PermissionError(
                    f"{name} is disabled. Set XDR_ALLOW_COLLECT=1 to enable collecting artifacts from hosts."
                )
            if name == "xdr_list_artifact_presets":
                result = await list_artifact_presets(args)
            elif name == "xdr_collect_artifacts":
                result = await collect_artifacts(args)
            else:
                result = await abort_collection(args)

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
