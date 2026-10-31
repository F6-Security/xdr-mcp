# Copyright 2026 Future Joint-Stock Company
# SPDX-License-Identifier: Apache-2.0

"""Tests for the XDR MCP server.

Run from the repository root:

    ./venv/bin/python -m unittest discover -s tests -v
"""

import importlib
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

BASE_ENV = {"XDR_BASE_URL": "https://xdr.example.com", "XDR_API_KEY": "test-key"}
ARTIFACT_ID = "6f1c2e3d-4b5a-4c6d-8e7f-90a1b2c3d4e5"


def load_server(**env):
    """Import server.py under a controlled environment.

    Most module state (BASE_URL, ALLOW_WRITE, the tool list) is decided at import
    time, so the environment-dependent tests reload the module.
    """
    merged = {**BASE_ENV, **env}
    with mock.patch.dict(os.environ, merged, clear=True):
        sys.modules.pop("xdr_mcp.server", None)
        return importlib.import_module("xdr_mcp.server")


server = load_server()


class TestWriteGate(unittest.TestCase):
    """xdr_mark_event is opt-in: it must not be advertised unless asked for."""

    def test_disabled_by_default(self):
        s = load_server()
        self.assertFalse(s.ALLOW_WRITE)
        self.assertNotIn("xdr_mark_event", [t.name for t in s.TOOLS])
        self.assertEqual(len(s.TOOLS), 7)

    def test_enabled_by_flag(self):
        for value in ("1", "true", "TRUE", "yes"):
            with self.subTest(value=value):
                s = load_server(XDR_ALLOW_WRITE=value)
                self.assertTrue(s.ALLOW_WRITE)
                self.assertIn("xdr_mark_event", [t.name for t in s.TOOLS])

    def test_other_values_leave_it_off(self):
        for value in ("0", "false", "no", "", "on"):
            with self.subTest(value=value):
                s = load_server(XDR_ALLOW_WRITE=value)
                self.assertFalse(s.ALLOW_WRITE)

    def test_calling_it_while_disabled_is_refused(self):
        s = load_server()
        params = s.types.CallToolRequestParams(name="xdr_mark_event", arguments={"payload": {}})
        with mock.patch.object(s, "xdr_request") as request:
            result = run(s.on_call_tool(None, params))
        request.assert_not_called()
        self.assertTrue(result.is_error)
        self.assertIn("XDR_ALLOW_WRITE", result.content[0].text)


class TestDownloadGate(unittest.TestCase):
    """xdr_download_artifact writes host artifacts to disk, so it is opt-in too."""

    def test_disabled_by_default(self):
        s = load_server()
        self.assertEqual(s.DOWNLOAD_DIR, "")
        self.assertNotIn("xdr_download_artifact", [t.name for t in s.TOOLS])

    def test_enabled_by_a_folder(self):
        with tempfile.TemporaryDirectory() as folder:
            s = load_server(XDR_DOWNLOAD_DIR=folder)
        self.assertEqual(s.DOWNLOAD_DIR, os.path.abspath(folder))
        self.assertIn("xdr_download_artifact", [t.name for t in s.TOOLS])

    def test_a_missing_folder_fails_at_startup(self):
        with self.assertRaisesRegex(RuntimeError, "not a directory"):
            load_server(XDR_DOWNLOAD_DIR="/nonexistent/downloads")

    def test_calling_it_while_disabled_is_refused(self):
        s = load_server()
        params = s.types.CallToolRequestParams(
            name="xdr_download_artifact", arguments={"internal_id": str(ARTIFACT_ID)}
        )
        with mock.patch.object(s, "download_artifact") as download:
            result = run(s.on_call_tool(None, params))
        download.assert_not_called()
        self.assertTrue(result.is_error)
        self.assertIn("XDR_DOWNLOAD_DIR", result.content[0].text)


class TestCollectGate(unittest.TestCase):
    """Collecting acts on hosts, so it has a switch of its own, apart from writes."""

    NAMES = ["xdr_list_artifact_presets", "xdr_collect_artifacts", "xdr_abort_artifact_collection"]

    def test_disabled_by_default(self):
        s = load_server()
        self.assertFalse(s.ALLOW_COLLECT)
        for name in self.NAMES:
            self.assertNotIn(name, [t.name for t in s.TOOLS])

    def test_allowing_writes_does_not_allow_collecting(self):
        s = load_server(XDR_ALLOW_WRITE="1")
        self.assertNotIn("xdr_collect_artifacts", [t.name for t in s.TOOLS])

    def test_enabled_by_flag(self):
        for value in ("1", "true", "yes"):
            with self.subTest(value=value):
                s = load_server(XDR_ALLOW_COLLECT=value)
                self.assertEqual([t.name for t in s.TOOLS][-3:], self.NAMES)

    def test_calling_them_while_disabled_is_refused(self):
        s = load_server()
        for name in self.NAMES:
            with self.subTest(name=name):
                arguments = {"machine_id": ARTIFACT_ID, "artifacts": ["prefetch"], "report_id": 1}
                params = s.types.CallToolRequestParams(name=name, arguments=arguments)
                with mock.patch.object(s, "xdr_request") as request:
                    result = run(s.on_call_tool(None, params))
                request.assert_not_called()
                self.assertTrue(result.is_error)
                self.assertIn("XDR_ALLOW_COLLECT", result.content[0].text)


class TestBaseUrlValidation(unittest.TestCase):
    """A bad base URL must fail the import, not leak the API key on first use."""

    def test_https_accepted_and_trailing_slash_trimmed(self):
        s = load_server(XDR_BASE_URL="https://xdr.example.com/")
        self.assertEqual(s.BASE_URL, "https://xdr.example.com")

    def test_rejected(self):
        cases = {
            "http://xdr.example.com": "https",
            "xdr.example.com": "https",
            "ftp://xdr.example.com": "https",
            "https://xdr.example.com?x=1": "query string",
            "https://xdr.example.com#frag": "fragment",
            "https://": "host",
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                with self.assertRaises(RuntimeError) as ctx:
                    load_server(XDR_BASE_URL=url)
                self.assertIn(expected, str(ctx.exception))

    def test_unset_is_tolerated_until_a_request_is_made(self):
        s = load_server(XDR_BASE_URL="")
        self.assertEqual(s.BASE_URL, "")

    def test_api_key_is_stripped(self):
        s = load_server(XDR_API_KEY="  abc\r\n")
        self.assertEqual(s.API_KEY, "abc")


class TestCaBundle(unittest.TestCase):
    """trust_env=False ignores SSL_CERT_FILE, so XDR_CA_BUNDLE is the way in
    for an installation behind an internal CA."""

    def test_default_is_plain_verification(self):
        s = load_server()
        self.assertIs(s.VERIFY, True)

    def test_a_bundle_becomes_an_ssl_context(self):
        import ssl
        import tempfile

        # A context needs a parsable PEM, so reuse the bundle certifi ships.
        import certifi

        s = load_server(XDR_CA_BUNDLE=certifi.where())
        self.assertIsInstance(s.VERIFY, ssl.SSLContext)
        self.assertTrue(s.VERIFY.check_hostname)
        self.assertEqual(s.VERIFY.verify_mode, ssl.CERT_REQUIRED)

        with tempfile.NamedTemporaryFile(suffix=".pem") as empty:
            with self.assertRaisesRegex(RuntimeError, "could not be loaded"):
                load_server(XDR_CA_BUNDLE=empty.name)

    def test_a_missing_file_fails_at_startup(self):
        with self.assertRaisesRegex(RuntimeError, "not a file"):
            load_server(XDR_CA_BUNDLE="/nonexistent/ca.pem")

    def test_verification_is_never_disabled(self):
        # There is deliberately no switch that turns certificate checking off.
        # Parsed rather than grepped so a mention in a comment does not match.
        import ast

        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "src", "xdr_mcp", "server.py",
        )
        tree = ast.parse(open(path).read())
        disabled = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.keyword)
            and node.arg == "verify"
            and isinstance(node.value, ast.Constant)
            and node.value.value is False
        ]
        self.assertEqual(disabled, [], "verify must never be hard-coded to False")


class TestAsInt(unittest.TestCase):
    def test_accepts_integers(self):
        self.assertEqual(server.as_int("page_size", 50, 1, 200), 50)

    def test_accepts_integral_floats(self):
        # JSON has no integer type; 10.0 must not reach the API as "10.0",
        # which its pagination silently discards in favour of the default.
        self.assertEqual(server.as_int("page_size", 10.0, 1, 200), 10)

    def test_rejects_fractional_floats(self):
        with self.assertRaisesRegex(ValueError, "whole number"):
            server.as_int("page_size", 10.5, 1, 200)

    def test_rejects_out_of_range(self):
        with self.assertRaisesRegex(ValueError, "<= 200"):
            server.as_int("page_size", 100000, 1, 200)
        with self.assertRaisesRegex(ValueError, ">= 0"):
            server.as_int("offset", -1, 0)

    def test_rejects_non_numbers(self):
        for value in ("50", None, [], {}, True):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "must be an integer"):
                    server.as_int("page_size", value, 1, 200)


class TestGetSection(unittest.TestCase):
    def test_missing_section(self):
        with self.assertRaisesRegex(ValueError, "section is required"):
            server.get_section(None)

    def test_unknown_section(self):
        with self.assertRaisesRegex(ValueError, 'Invalid section "nope"'):
            server.get_section("nope")

    def test_unhashable_section_does_not_crash(self):
        with self.assertRaisesRegex(ValueError, "Invalid section"):
            server.get_section(["alerts"])

    def test_every_section_can_be_counted(self):
        # xdr_count reads section["countPath"]; a missing key would be a KeyError
        # surfaced only at call time.
        for name in server.ALL_SECTIONS:
            with self.subTest(section=name):
                self.assertIn("countPath", server.get_section(name))

    def test_alerts_count_uses_the_same_viewset_as_search(self):
        alerts = server.get_section("alerts")
        self.assertEqual(alerts["countPath"], alerts["path"])


class TestBuildPagingOffset(unittest.TestCase):
    """The nine Elastic-backed sections page by offset."""

    def setUp(self):
        self.section = server.SECTIONS["alerts"]

    def test_offset_and_page_size(self):
        self.assertEqual(
            server.build_paging(self.section, {"offset": 50, "page_size": 25}),
            "?page_size=25&offset=50",
        )

    def test_no_arguments(self):
        self.assertEqual(server.build_paging(self.section, {}), "")

    def test_integral_float_is_normalised(self):
        self.assertEqual(server.build_paging(self.section, {"page_size": 20.0}), "?page_size=20")

    def test_values_are_url_escaped(self):
        self.assertEqual(
            server.build_paging(self.section, {"ordering": '-ts&page_size=999#x'}),
            "?ordering=-ts%26page_size%3D999%23x",
        )

    def test_page_size_is_capped(self):
        with self.assertRaisesRegex(ValueError, "<= 200"):
            server.build_paging(self.section, {"page_size": 100000})


class TestBuildPagingCursor(unittest.TestCase):
    """The modules section pages by 1-based page number, not offset."""

    def setUp(self):
        self.section = server.SECTIONS["modules"]

    def test_only_modules_pages_by_cursor(self):
        cursor_sections = [n for n, s in server.SECTIONS.items() if s.get("paging") == "cursor"]
        self.assertEqual(cursor_sections, ["modules"])

    def test_offset_is_translated_to_a_page_number(self):
        self.assertEqual(server.build_paging(self.section, {"offset": 0}), "?cursor=1")
        self.assertEqual(
            server.build_paging(self.section, {"offset": 50, "page_size": 25}),
            "?page_size=25&cursor=3",
        )

    def test_default_page_size_is_used_when_omitted(self):
        self.assertEqual(server.build_paging(self.section, {"offset": 20}), "?cursor=3")

    def test_misaligned_offset_is_refused(self):
        with self.assertRaisesRegex(ValueError, "multiple of page_size"):
            server.build_paging(self.section, {"offset": 15, "page_size": 25})

    def test_no_offset_means_no_cursor(self):
        self.assertEqual(server.build_paging(self.section, {"page_size": 25}), "?page_size=25")


class TestValidateMatcher(unittest.TestCase):
    def setUp(self):
        self.payload = {
            "expression": 'id: "abc"',
            "basename": "alert",
            "action": {"resolved": True},
            "comment": "resolved via API",
        }

    def test_valid_payload_passes(self):
        self.assertEqual(server.validate_matcher(dict(self.payload)), self.payload)

    def test_payload_must_be_an_object(self):
        for value in ("string", None, [], 5):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "must be an object"):
                    server.validate_matcher(value)

    def test_unknown_fields_are_refused(self):
        with self.assertRaisesRegex(ValueError, "unsupported field"):
            server.validate_matcher({**self.payload, "target_company": 7})

    def test_expression_is_required(self):
        for value in ("", "   ", None, 5):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "expression is required"):
                    server.validate_matcher({**self.payload, "expression": value})

    def test_only_matcher_basenames_are_accepted(self):
        # The API defines matcher actions for alert/envelope/attach only;
        # incident and asset are rejected with an opaque error server-side.
        for basename in ("incident", "asset", "", None):
            with self.subTest(basename=basename):
                with self.assertRaisesRegex(ValueError, "basename must be one of"):
                    server.validate_matcher({**self.payload, "basename": basename})

    def test_action_must_have_exactly_one_field(self):
        for action in ({}, {"resolved": True, "false_positive": True}, "resolved", None):
            with self.subTest(action=action):
                with self.assertRaisesRegex(ValueError, "exactly one field"):
                    server.validate_matcher({**self.payload, "action": action})

    def test_action_must_be_allowed_for_the_basename(self):
        cases = [
            ("alert", "false_negative"),
            ("envelope", "on_hold"),
            ("envelope", "related"),
            ("attach", "on_hold"),
            ("attach", "false_negative"),
        ]
        for basename, action in cases:
            with self.subTest(basename=basename, action=action):
                payload = {**self.payload, "basename": basename, "action": {action: True}}
                with self.assertRaisesRegex(ValueError, "not allowed for basename"):
                    server.validate_matcher(payload)

    def test_allowed_actions_per_basename(self):
        for basename, actions in server.MATCHER_ACTIONS.items():
            for action in actions:
                with self.subTest(basename=basename, action=action):
                    payload = {**self.payload, "basename": basename, "action": {action: True}}
                    self.assertEqual(server.validate_matcher(payload)["basename"], basename)

    def test_comment_is_required(self):
        payload = {k: v for k, v in self.payload.items() if k != "comment"}
        with self.assertRaisesRegex(ValueError, "comment is required"):
            server.validate_matcher(payload)
        with self.assertRaisesRegex(ValueError, "comment is required"):
            server.validate_matcher({**self.payload, "comment": "  "})

    def test_on_hold_needs_no_comment(self):
        payload = {
            "expression": 'id: "abc"',
            "basename": "alert",
            "action": {"on_hold": "2026-01-01T00:00:00Z"},
        }
        self.assertEqual(server.validate_matcher(payload), payload)

    def test_resolve_reason_must_be_known(self):
        # An unknown reason is silently rewritten to threat_eliminated by the API,
        # which still answers 201 — so it is caught here instead.
        with self.assertRaisesRegex(ValueError, "resolve_reason must be one of"):
            server.validate_matcher({**self.payload, "resolve_reason": "typo"})
        for reason in server.RESOLVE_REASONS:
            with self.subTest(reason=reason):
                server.validate_matcher({**self.payload, "resolve_reason": reason})

    def test_owner_only_flags_are_still_accepted(self):
        # is_global is a supported capability for an owner of their own instance;
        # the API enforces the permission.
        payload = {**self.payload, "is_global": True, "release_from_quarantine": True}
        self.assertEqual(server.validate_matcher(payload), payload)

    def test_child_matchers_are_validated(self):
        child = {"expression": 'id: "b"', "basename": "attach", "action": {"resolved": True}}
        self.assertIn("child_matchers", server.validate_matcher({**self.payload, "child_matchers": [child]}))

        with self.assertRaisesRegex(ValueError, "basename must be one of"):
            server.validate_matcher({**self.payload, "child_matchers": [{**child, "basename": "incident"}]})

        with self.assertRaisesRegex(ValueError, "must be an array"):
            server.validate_matcher({**self.payload, "child_matchers": child})

    def test_child_matchers_reject_top_level_only_fields(self):
        child = {
            "expression": 'id: "b"', "basename": "attach",
            "action": {"resolved": True}, "is_global": True,
        }
        with self.assertRaisesRegex(ValueError, "unsupported field"):
            server.validate_matcher({**self.payload, "child_matchers": [child]})


class TestToolDeclarations(unittest.TestCase):
    def test_only_the_tools_that_act_are_marked_destructive(self):
        with tempfile.TemporaryDirectory() as folder:
            s = load_server(XDR_ALLOW_WRITE="1", XDR_DOWNLOAD_DIR=folder, XDR_ALLOW_COLLECT="1")
        for tool in s.TOOLS:
            with self.subTest(tool=tool.name):
                if tool.name in ("xdr_mark_event", "xdr_collect_artifacts", "xdr_abort_artifact_collection"):
                    self.assertFalse(tool.annotations.read_only_hint)
                    self.assertTrue(tool.annotations.destructive_hint)
                elif tool.name == "xdr_download_artifact":
                    # Reads XDR only, but writes a local file: not read-only,
                    # and nothing it does destroys anything.
                    self.assertFalse(tool.annotations.read_only_hint)
                    self.assertFalse(tool.annotations.destructive_hint)
                else:
                    self.assertTrue(tool.annotations.read_only_hint)

    def test_ordering_description_lists_every_section(self):
        for name, section in server.SECTIONS.items():
            with self.subTest(section=name):
                self.assertIn(f"{name}: {', '.join(section['ordering'])}", server.ORDERING_DESCRIPTION)

    def test_alerts_order_by_created_at_not_ts_created(self):
        # ts_created does not exist for alerts or incidents; the API drops an
        # unknown ordering field silently and falls back to its default.
        self.assertEqual(
            server.SECTIONS["alerts"]["ordering"],
            ("created_at", "last_event", "last_updated", "severity"),
        )
        self.assertEqual(server.SECTIONS["incidents"]["ordering"], ("created_at", "updated_at"))
        self.assertNotIn("alerts: ts_created", server.ORDERING_DESCRIPTION)

    def test_query_description_covers_wildcards(self):
        # Without this the model has no way to know * is available, and a search
        # by name, path or domain is close to unusable.
        self.assertIn("wildcard", server.QUERY_DESCRIPTION)
        self.assertIn("xdr_get_suggestions", server.QUERY_DESCRIPTION)

    def test_query_description_does_not_teach_values_that_return_nothing(self):
        # Both were wrong in the shipped description and fail silently against the
        # API: severity has no "high"/"medium", and the calendar-rounding forms
        # come back empty. Verified against a live installation.
        self.assertIn('severity : "critical"', server.QUERY_DESCRIPTION)
        self.assertNotIn('severity : "high"', server.QUERY_DESCRIPTION)
        self.assertNotIn("now/d (today)", server.QUERY_DESCRIPTION)
        self.assertIn("now-1d", server.QUERY_DESCRIPTION)

    def test_matcher_schema_is_closed(self):
        s = load_server(XDR_ALLOW_WRITE="1")
        mark = next(t for t in s.TOOLS if t.name == "xdr_mark_event")
        payload = mark.input_schema["properties"]["payload"]
        self.assertFalse(payload["additionalProperties"])
        self.assertEqual(payload["required"], ["expression", "basename", "action"])
        self.assertEqual(payload["properties"]["basename"]["enum"], sorted(s.MATCHER_ACTIONS))
        self.assertEqual(payload["properties"]["action"]["maxProperties"], 1)

    def test_tools_serialise_with_camel_case_aliases(self):
        # The wire format is camelCase; the models store snake_case internally.
        wire = server.TOOLS[0].model_dump(by_alias=True, exclude_none=True)
        self.assertIn("inputSchema", wire)
        self.assertIs(wire["annotations"]["readOnlyHint"], True)


def run(coro):
    import asyncio

    return asyncio.run(coro)


class TestToolDispatch(unittest.TestCase):
    """Pin the mapping from tool call to HTTP method, path and body."""

    def setUp(self):
        self.server = load_server(XDR_ALLOW_WRITE="1")

    def call(self, name, arguments):
        params = self.server.types.CallToolRequestParams(name=name, arguments=arguments)
        with mock.patch.object(self.server, "xdr_request", new_callable=mock.AsyncMock) as request:
            request.return_value = {"ok": True}
            result = run(self.server.on_call_tool(None, params))
        return request, result

    def test_search(self):
        request, result = self.call("xdr_search", {"section": "emails", "query": 'id: "x"'})
        request.assert_awaited_once_with("POST", "/api/mailing/check/", {"search": 'id: "x"'})
        self.assertFalse(result.is_error)

    def test_search_passes_paging(self):
        request, _ = self.call(
            "xdr_search", {"section": "alerts", "query": "a", "page_size": 25, "offset": 50}
        )
        self.assertEqual(request.await_args.args[1], "/api/v1/alerts/check/?page_size=25&offset=50")

    def test_an_empty_query_lists_the_section(self):
        # check/ is the list endpoint with the search taken from the body, so an
        # empty search is how the whole section is requested.
        request, _ = self.call("xdr_search", {"section": "assets", "query": ""})
        request.assert_awaited_once_with("POST", "/api/assets/check/", {"search": ""})

    def test_search_of_modules_uses_a_cursor(self):
        request, _ = self.call(
            "xdr_search", {"section": "modules", "query": "", "offset": 20, "page_size": 10}
        )
        self.assertEqual(request.await_args.args[1], "/api/appliances/check/?page_size=10&cursor=3")

    def test_there_is_no_separate_list_tool(self):
        self.assertNotIn("xdr_list", [t.name for t in self.server.TOOLS])
        request, result = self.call("xdr_list", {"section": "assets"})
        request.assert_not_awaited()
        self.assertTrue(result.is_error)

    def test_count_without_query_is_a_get(self):
        request, _ = self.call("xdr_count", {"section": "alerts"})
        request.assert_awaited_once_with("GET", "/api/v1/alerts/count/")

    def test_count_with_query_is_a_post(self):
        request, _ = self.call("xdr_count", {"section": "alerts", "query": "sev"})
        request.assert_awaited_once_with("POST", "/api/v1/alerts/count/", {"search": "sev"})

    def test_suggestions(self):
        request, _ = self.call(
            "xdr_get_suggestions",
            {"section": "assets", "field": "company_name", "prefix": "ЛЕГ"},
        )
        request.assert_awaited_once_with(
            "GET", "/api/assets/search_help/?field=company_name&prefix=%D0%9B%D0%95%D0%93"
        )

    def test_suggestions_without_a_prefix(self):
        request, _ = self.call("xdr_get_suggestions", {"section": "assets", "field": "status"})
        request.assert_awaited_once_with("GET", "/api/assets/search_help/?field=status")

    def test_suggestions_need_a_field(self):
        for field in (None, "", "   ", 5):
            with self.subTest(field=field):
                request, result = self.call(
                    "xdr_get_suggestions", {"section": "assets", "field": field}
                )
                request.assert_not_awaited()
                self.assertTrue(result.is_error)

    def test_mapping_and_filters(self):
        request, _ = self.call("xdr_get_mapping", {"section": "applications"})
        request.assert_awaited_once_with("GET", "/api/software/mapping/")

        request, _ = self.call("xdr_get_filters", {"section": "audit"})
        request.assert_awaited_once_with("GET", "/api/system_logs/filters/")

    def test_list_artifacts_of_an_asset(self):
        request, result = self.call("xdr_list_artifacts", {"asset_id": 7})
        request.assert_awaited_once_with(
            "GET", "/api/assets/forensic-reports/?asset_id=7&ordering=-ts_created&cursor=1&page_size=20"
        )
        self.assertFalse(result.is_error)

    def test_list_artifacts_pages_by_cursor(self):
        # The endpoint takes the page number as "cursor"; "page" and "offset"
        # are ignored and return the first page again.
        request, _ = self.call(
            "xdr_list_artifacts", {"page": 3, "page_size": 5, "ordering": "ts_created"}
        )
        request.assert_awaited_once_with(
            "GET", "/api/assets/forensic-reports/?ordering=ts_created&cursor=3&page_size=5"
        )

    def test_list_artifacts_by_machine_id_looks_the_asset_up_first(self):
        # The list ignores a machine_id filter, so it is turned into the asset id.
        params = self.server.types.CallToolRequestParams(
            name="xdr_list_artifacts", arguments={"machine_id": ARTIFACT_ID.upper()}
        )
        with mock.patch.object(self.server, "xdr_request", new_callable=mock.AsyncMock) as request:
            request.side_effect = [{"data": {"id": 42}}, {"data": {"count": 0, "results": []}}]
            result = run(self.server.on_call_tool(None, params))
        self.assertEqual(
            request.await_args_list,
            [
                mock.call("GET", f"/api/assets/{ARTIFACT_ID}/"),
                mock.call(
                    "GET",
                    "/api/assets/forensic-reports/?asset_id=42&ordering=-ts_created&cursor=1&page_size=20",
                ),
            ],
        )
        self.assertFalse(result.is_error)

    def test_list_artifacts_keeps_only_the_useful_fields(self):
        row = {field: field for field in self.server.ARTIFACT_FIELDS}
        params = self.server.types.CallToolRequestParams(name="xdr_list_artifacts", arguments={})
        with mock.patch.object(self.server, "xdr_request", new_callable=mock.AsyncMock) as request:
            request.return_value = {
                "data": {"count": 1, "next": None, "results": [{**row, "need_rotation": True}]}
            }
            result = run(self.server.on_call_tool(None, params))
        self.assertIn('"internal_id": "internal_id"', result.content[0].text)
        self.assertNotIn("need_rotation", result.content[0].text)

    def test_list_artifacts_rejects_bad_arguments_before_the_request(self):
        for arguments in (
            {"ordering": "-size"},
            {"machine_id": "not-a-uuid"},
            {"asset_id": 0},
            {"page_size": 100000},
        ):
            with self.subTest(arguments=arguments):
                request, result = self.call("xdr_list_artifacts", arguments)
                request.assert_not_awaited()
                self.assertTrue(result.is_error)

    def test_artifact_structure(self):
        request, _ = self.call("xdr_get_artifact_structure", {"report_id": 5})
        request.assert_awaited_once_with("GET", "/api/assets/forensic-reports/5/forensic-report/structure/")

        request, _ = self.call("xdr_get_artifact_structure", {"report_id": 5, "path": "var//log/"})
        request.assert_awaited_once_with(
            "GET", "/api/assets/forensic-reports/5/forensic-report/structure/?path=%2Fvar%2Flog"
        )

    def test_artifact_structure_refuses_a_path_out_of_the_artifact(self):
        request, result = self.call("xdr_get_artifact_structure", {"report_id": 5, "path": "/var/../etc"})
        request.assert_not_awaited()
        self.assertTrue(result.is_error)

    def test_mark_event_sends_the_validated_payload(self):
        payload = {
            "expression": 'id: "abc"',
            "basename": "alert",
            "action": {"resolved": True},
            "comment": "c",
        }
        request, result = self.call("xdr_mark_event", {"payload": payload})
        request.assert_awaited_once_with("POST", "/api/matchers/", payload)
        self.assertFalse(result.is_error)

    def test_mark_event_rejects_a_bad_payload_before_the_request(self):
        request, result = self.call("xdr_mark_event", {"payload": {"expression": "x"}})
        request.assert_not_awaited()
        self.assertTrue(result.is_error)

    def test_unknown_tool(self):
        request, result = self.call("xdr_nope", {})
        request.assert_not_awaited()
        self.assertTrue(result.is_error)
        self.assertIn("Unknown tool", result.content[0].text)

    def test_errors_are_reported_without_a_traceback(self):
        request, result = self.call("xdr_search", {"section": "nope", "query": ""})
        request.assert_not_awaited()
        self.assertTrue(result.is_error)
        self.assertIn("ValueError", result.content[0].text)


class TestRequestErrorHandling(unittest.IsolatedAsyncioTestCase):
    """xdr_request must not hand the model an unbounded API response."""

    def setUp(self):
        self.server = load_server()

    async def run_request(self, response):
        client = mock.AsyncMock()
        client.request.return_value = response
        context = mock.MagicMock()
        context.__aenter__ = mock.AsyncMock(return_value=client)
        context.__aexit__ = mock.AsyncMock(return_value=False)
        with mock.patch.object(self.server.httpx, "AsyncClient", return_value=context):
            return await self.server.xdr_request("GET", "/api/assets/")

    @staticmethod
    def response(status=200, content_type="application/json", text='{"ok":true}', json_value=None):
        r = mock.MagicMock()
        r.is_success = 200 <= status < 300
        r.status_code = status
        r.reason_phrase = "OK" if r.is_success else "Error"
        r.headers = {"content-type": content_type}
        r.text = text
        r.content = text.encode()
        r.json.return_value = json_value if json_value is not None else {"ok": True}
        return r

    async def test_success_returns_parsed_json(self):
        self.assertEqual(await self.run_request(self.response()), {"ok": True})

    async def test_html_error_body_is_not_echoed(self):
        # A Django debug page or an SSO login page must not reach the model.
        html = "<html>Traceback: SECRET_KEY=hunter2</html>"
        with self.assertRaises(RuntimeError) as ctx:
            await self.run_request(self.response(status=500, content_type="text/html", text=html))
        self.assertNotIn("hunter2", str(ctx.exception))
        self.assertIn("API error 500", str(ctx.exception))

    async def test_json_error_body_is_truncated(self):
        body = '{"detail": "' + "x" * 5000 + '"}'
        with self.assertRaises(RuntimeError) as ctx:
            await self.run_request(self.response(status=400, text=body))
        self.assertLess(len(str(ctx.exception)), self.server.MAX_ERROR_CHARS + 100)

    async def test_oversized_response_is_refused(self):
        big = '{"d":"' + "x" * (self.server.MAX_RESPONSE_BYTES + 10) + '"}'
        with self.assertRaisesRegex(RuntimeError, "over the"):
            await self.run_request(self.response(text=big))

    async def test_non_json_success_body_is_reported_clearly(self):
        r = self.response(content_type="text/html", text="<html>hi</html>")
        r.json.side_effect = ValueError("Expecting value")
        with self.assertRaisesRegex(RuntimeError, "expected JSON"):
            await self.run_request(r)

    async def test_missing_configuration_is_reported(self):
        s = load_server(XDR_BASE_URL="")
        with self.assertRaisesRegex(RuntimeError, "must be set"):
            await s.xdr_request("GET", "/api/assets/")


class TestArtifactHelpers(unittest.TestCase):
    def test_uuid_is_normalised(self):
        self.assertEqual(server.as_uuid("internal_id", ARTIFACT_ID.upper()), ARTIFACT_ID)

    def test_anything_but_a_uuid_is_refused(self):
        # It goes into the URL path, so nothing else may.
        for value in ("../../assets", "", None, 5, f"{ARTIFACT_ID}/x"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "must be a UUID"):
                    server.as_uuid("internal_id", value)

    def test_paths_are_normalised(self):
        cases = {None: "/", "": "/", "/": "/", "var/log/": "/var/log", "\\var\\\\log": "/var/log"}
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(server.artifact_path(value), expected)

    def test_paths_cannot_climb_out(self):
        for value in ("..", "/var/../etc", "a/../../b"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, r"must not contain '\.\.'"):
                    server.artifact_path(value)

    def test_file_names_cannot_steer_the_path(self):
        cases = {
            'attachment; filename="2026-01-02T03:04:05.000001.zip"': "2026-01-02T03_04_05.000001.zip",
            'attachment; filename="var/log/syslog.zip"': "syslog.zip",
            'attachment; filename="..\\..\\evil.zip"': "evil.zip",
            'attachment; filename="../.."': "artifact.zip",
            'attachment; filename=""': "artifact.zip",
            "": "artifact.zip",
        }
        for disposition, expected in cases.items():
            with self.subTest(disposition=disposition):
                self.assertEqual(
                    server.artifact_file_name(ARTIFACT_ID, disposition), f"{ARTIFACT_ID[:8]}_{expected}"
                )


class TestDownloadArtifact(unittest.IsolatedAsyncioTestCase):
    """The download streams to disk, and leaves nothing behind when it fails."""

    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.server = load_server(XDR_DOWNLOAD_DIR=self.folder)
        self.requests = []

    async def download(self, response, **arguments):
        real_client = httpx.AsyncClient

        def handler(request):
            self.requests.append(request)
            return response

        def client(**kwargs):
            kwargs.pop("verify")
            return real_client(transport=httpx.MockTransport(handler), **kwargs)

        with mock.patch.object(self.server.httpx, "AsyncClient", side_effect=client):
            return await self.server.download_artifact({"internal_id": ARTIFACT_ID, **arguments})

    @staticmethod
    def zip_response(content, file_name):
        return httpx.Response(
            200,
            headers={
                "content-type": "application/octet-stream",
                "content-disposition": f'attachment; filename="{file_name}"',
            },
            content=content,
        )

    def saved(self):
        return sorted(os.listdir(self.folder))

    async def test_the_whole_artifact(self):
        result = await self.download(self.zip_response(b"PK zip", "2026-01-02T03:04:05.000001.zip"))

        request = self.requests[0]
        self.assertEqual(
            request.url.raw_path, f"/api/assets/forensic-reports/{ARTIFACT_ID}/download/%2F/".encode()
        )
        self.assertEqual(request.headers["x-api-key"], "test-key")

        name = f"{ARTIFACT_ID[:8]}_2026-01-02T03_04_05.000001.zip"
        self.assertEqual(result, {
            "internal_id": ARTIFACT_ID, "path": "/", "file": os.path.join(self.folder, name), "bytes": 6,
        })
        with open(result["file"], "rb") as file:
            self.assertEqual(file.read(), b"PK zip")
        self.assertEqual(self.saved(), [name])

    async def test_one_file_inside_it(self):
        result = await self.download(
            self.zip_response(b"PK", "var/log/syslog.zip"), path="var/log/syslog"
        )
        self.assertTrue(
            self.requests[0].url.raw_path.endswith(b"/download/%2Fvar%2Flog%2Fsyslog/")
        )
        self.assertEqual(os.path.basename(result["file"]), f"{ARTIFACT_ID[:8]}_syslog.zip")

    async def test_a_path_not_in_the_artifact(self):
        # The API answers it with a 200 and an empty zip rather than an error.
        empty = b"PK\x05\x06" + b"\x00" * 18
        with self.assertRaisesRegex(FileNotFoundError, "nothing at /nope"):
            await self.download(self.zip_response(empty, ""), path="/nope")
        self.assertEqual(self.saved(), [])

    async def test_the_console_page_is_not_saved_as_an_artifact(self):
        # Unknown routes come back as the console's HTML with a 200.
        page = httpx.Response(200, headers={"content-type": "text/html"}, content=b"<html>secret</html>")
        with self.assertRaisesRegex(RuntimeError, "API error 200") as ctx:
            await self.download(page)
        self.assertNotIn("secret", str(ctx.exception))
        self.assertEqual(self.saved(), [])

    async def test_a_json_error_is_reported_bounded(self):
        denied = httpx.Response(403, json={"detail": "x" * 5000})
        with self.assertRaisesRegex(RuntimeError, "API error 403") as ctx:
            await self.download(denied)
        self.assertLess(len(str(ctx.exception)), self.server.MAX_ERROR_CHARS + 100)
        self.assertEqual(self.saved(), [])

    async def test_a_declared_size_over_the_cap_is_refused(self):
        big = self.zip_response(b"x" * (1024 * 1024 + 1), "big.zip")
        with self.assertRaisesRegex(RuntimeError, "over max_mb=1"):
            await self.download(big, max_mb=1)
        self.assertEqual(self.saved(), [])

    async def test_a_stream_over_the_cap_is_stopped_and_removed(self):
        async def chunks():
            for _ in range(3):
                yield b"x" * (512 * 1024)

        # No Content-Length: the cap can only be enforced while streaming.
        streamed = httpx.Response(
            200,
            headers={"content-type": "application/octet-stream", "content-disposition": 'filename="b.zip"'},
            content=chunks(),
        )
        with self.assertRaisesRegex(RuntimeError, "was stopped"):
            await self.download(streamed, max_mb=1)
        self.assertEqual(self.saved(), [])

    async def test_bad_arguments_are_refused_before_the_request(self):
        for arguments in ({"internal_id": "../x"}, {"path": "/a/../b"}, {"max_mb": 0}):
            with self.subTest(arguments=arguments):
                with self.assertRaises(ValueError):
                    await self.download(self.zip_response(b"PK", "a.zip"), **arguments)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.saved(), [])


class TestCollectPaths(unittest.TestCase):
    """A path outside the rules fails the whole collection."""

    def test_windows_paths_that_work(self):
        for path in (
            r"C:\Windows\Prefetch",
            r"C:\Users\*\AppData\Roaming\*\*.exe",
            r"C:\ProgramData\*-????????-????",
            r"D:\inetpub\logs",
        ):
            with self.subTest(path=path):
                self.assertIsNone(server.windows_path_problem(path))

    def test_windows_paths_that_break_it(self):
        cases = {
            "C:/Windows/Prefetch": "backslashes",
            r"Windows\Prefetch": "absolute",
            "C:\\Windows\\Prefetch\\": "backslash",
            "C:\\": "backslash",
            r'C:\Program Files\x" -o "C:\evil': '"',
            r"C:\%TEMP%\x": "%",
            r"C:\INetCache\update_e[1].jpg": "[",
            r"C:\ProgramData\????????-*": "only after a *",
            "C:\\Windows\\Tasks\nC:\\x": "control characters",
        }
        for path, expected in cases.items():
            with self.subTest(path=path):
                self.assertIn(expected, server.windows_path_problem(path))

    def test_posix_paths_that_work(self):
        for path in ("/etc/cron.d", "/var/log/*.log", "/home/*/.bash_history", "/var/log/auth.log"):
            with self.subTest(path=path):
                self.assertIsNone(server.posix_path_problem(path))

    def test_posix_paths_that_break_it(self):
        cases = {
            "etc/cron.d": "absolute",
            "/srv/$HOME": "$",
            '/srv/a"b': '"',
            "/srv/`id`": "`",
            "/srv/a\\b": "\\",
            "/srv/a\tb": "control characters",
        }
        for path, expected in cases.items():
            with self.subTest(path=path):
                self.assertIn(expected, server.posix_path_problem(path))

    def test_nothing_that_takes_in_lastlog(self):
        # Sparse, and can be very large.
        for path in ("/var/log/lastlog", "/var/log", "/var/log/", "/var/log/*", "/var", "/", "/*/log/l*"):
            with self.subTest(path=path):
                self.assertIn("lastlog", server.posix_path_problem(path))
        # macOS has no such file, and its /var/log is worth taking.
        self.assertIsNone(server.posix_path_problem("/var/log", linux=False))

    def test_string_lists(self):
        self.assertEqual(server.string_list("paths", None, 5), [])
        self.assertEqual(server.string_list("paths", [" /a ", "/b", "/a"], 5), ["/a", "/b"])
        for value in ("/a", ["/a", ""], ["/a", " "], [1], {"a": 1}):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "array of non-empty strings"):
                    server.string_list("paths", value, 5)
        with self.assertRaisesRegex(ValueError, "at most 2"):
            server.string_list("paths", ["/a", "/b", "/c"], 2)


MACHINE_ID = "0f2b6c1e-58a4-4c3e-9d7a-1b2c3d4e5f60"
# forensic_options as live cards carry them, per agent type.
WINDOWS_OPTIONS = [
    "browser_history", "commands_to_execute", "crash_dumps", "diagnostics", "event_log", "files_to_upload",
    "hiber_file", "log_file", "master_file_table", "memory_dump", "page_file", "perf_logs", "prefetch",
    "registry", "temp_dir", "usn_journal", "bugreport",
]
LINUX_OPTIONS = [
    "apparmor_configs", "cron_jobs", "custom_paths", "devices", "dns_resolvers", "dpkg_packages",
    "executable_commands", "fstab", "groups", "hosts", "init_subsystem", "ip_routes", "kernel_modules",
    "logged_users", "locks", "memory_info", "mounts", "network_interfaces", "network_tables",
    "pacman_packages", "rpm_packages", "se_linux_configs", "shadow", "shell_history", "ssh_configs",
    "sudoers_configs", "system_controls", "unix_sockets", "bugreport",
]
MACOS_OPTIONS = [
    "agent_logs", "custom_paths", "etc", "executable_commands", "logs", "provision_profiles", "shell", "ssh",
    "startup_items",
]
FEATURES = {"collect_artifacts": True, "triage_execute_commands": True, "remote_shell": True}


def card(options=WINDOWS_OPTIONS, **fields):
    # The live card has no unit_type: the type shows only through forensic_options.
    data = {
        "id": 42, "machine_id": MACHINE_ID.upper(), "type": "agent", "archived": False, "status": "Online",
        "current_name": "WS-1", "forensic_options": options, "features": FEATURES,
    }
    return {"data": {**data, **fields}}


class FakeApi:
    """Answers the calls a collection makes, and records them."""

    def __init__(self, asset, before=(), after=(), presets=(), post_error=None, post_errors=()):
        self.asset = asset
        self.reports = [list(before), list(after)]
        self.presets = list(presets)
        self.post_error = post_error
        # one outcome per create request, in order; then success
        self.post_errors = list(post_errors)
        self.calls = []

    async def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        if path == "/api/assets/forensic-reports/presets/":
            return {"data": self.presets}
        if path.startswith("/api/assets/forensic-reports/?"):
            # the host's collections before the new one is made, then after
            rows = self.reports.pop(0) if len(self.reports) > 1 else self.reports[0]
            return {"data": {"count": len(rows), "results": rows}}
        if method == "GET" and path.startswith("/api/assets/"):
            return self.asset
        if method == "POST" and path == "/api/assets/forensic-reports/":
            error = self.post_errors.pop(0) if self.post_errors else self.post_error
            if error:
                raise error
            return {"data": {"id": 501}}
        if method == "POST" and path.endswith("/forensic-report/abort/"):
            return {"messages": ["aborted"]}
        raise AssertionError(f"unexpected request {method} {path}")

    def posts(self):
        return [body for method, path, body in self.calls if method == "POST"]


class TestCollectArtifacts(unittest.TestCase):
    """Everything that would make the API or the agent fail quietly is caught before the request."""

    def setUp(self):
        self.server = load_server(XDR_ALLOW_COLLECT="1")

    def call(self, api, name="xdr_collect_artifacts", **arguments):
        params = self.server.types.CallToolRequestParams(name=name, arguments=arguments)
        with mock.patch.object(self.server, "xdr_request", new=api):
            return run(self.server.on_call_tool(None, params))

    def collect(self, api, **arguments):
        return self.call(api, machine_id=MACHINE_ID, **arguments)

    def refused(self, api, expected, **arguments):
        result = self.collect(api, **arguments)
        self.assertTrue(result.is_error, result.content[0].text)
        self.assertIn(expected, result.content[0].text)
        self.assertEqual(api.posts(), [])
        return result.content[0].text

    def test_a_windows_collection(self):
        row = {"id": 501, "internal_id": ARTIFACT_ID, "command_status": "New", "need_rotation": False}
        api = FakeApi(card(), before=[], after=[row])
        result = self.collect(api, artifacts=["prefetch", "event_log"], paths=[r"C:\Windows\Tasks"])
        self.assertFalse(result.is_error, result.content[0].text)
        self.assertEqual(api.calls[0], ("GET", f"/api/assets/{MACHINE_ID}/", None))
        self.assertEqual(api.posts(), [{
            "unit_type": "huntpoint",
            # the card's spelling: the API looks the machine id up as an exact term
            "asset": MACHINE_ID.upper(),
            "prefetch": True,
            "event_log": True,
            "files_to_upload": [r"C:\Windows\Tasks"],
        }])
        self.assertIn('"report_id": 501', result.content[0].text)
        self.assertIn(f'"internal_id": "{ARTIFACT_ID}"', result.content[0].text)
        self.assertNotIn("need_rotation", result.content[0].text)

    def test_the_agent_type_comes_from_the_host(self):
        # A Linux host goes to the API as edr_linux, and its paths as custom_paths.
        api = FakeApi(card(LINUX_OPTIONS))
        result = self.collect(api, artifacts=["hosts"], paths=["/etc/cron.d"])
        self.assertFalse(result.is_error, result.content[0].text)
        self.assertEqual(api.posts(), [{
            "unit_type": "edr_linux", "asset": MACHINE_ID.upper(), "hosts": True,
            "custom_paths": ["/etc/cron.d"],
        }])

        api = FakeApi(card(MACOS_OPTIONS))
        self.assertFalse(self.collect(api, artifacts=["etc"]).is_error)
        self.assertEqual(api.posts()[0]["unit_type"], "edr_macos")

    def test_each_agent_type_has_a_marker_only_it_offers(self):
        lists = {"huntpoint": WINDOWS_OPTIONS, "edr_linux": LINUX_OPTIONS, "edr_macos": MACOS_OPTIONS}
        for unit_type, spec in self.server.COLLECT_TARGETS.items():
            with self.subTest(unit_type=unit_type):
                holders = [name for name, options in lists.items() if spec["marker"] in options]
                self.assertEqual(holders, [unit_type])

    def test_a_host_without_an_agent_is_refused(self):
        # A network host's card has no forensic options at all.
        for options in (None, [], ["something_else"]):
            with self.subTest(options=options):
                self.refused(FakeApi(card(options)), "no EDR agent", artifacts=["prefetch"])

    def test_a_host_with_collection_turned_off_is_refused(self):
        features = {**FEATURES, "collect_artifacts": False}
        self.refused(FakeApi(card(features=features)), "turned off", artifacts=["prefetch"])

    def test_an_archived_host_is_refused(self):
        self.refused(FakeApi(card(archived=True)), "archived", artifacts=["prefetch"])

    def test_an_artifact_of_another_os_is_refused(self):
        # The API would drop the unknown field and start a collection of nothing.
        text = self.refused(FakeApi(card()), "not a Windows artifact: shadow", artifacts=["shadow"])
        self.assertIn("prefetch", text)

    def test_an_artifact_the_agent_does_not_offer_is_refused(self):
        older = [name for name in WINDOWS_OPTIONS if name != "bugreport"]
        self.refused(FakeApi(card(options=older)), "does not offer bugreport", artifacts=["bugreport"])

    def test_paths_need_the_agent_to_take_paths(self):
        options = [name for name in LINUX_OPTIONS if name != "custom_paths"]
        self.refused(FakeApi(card(options)), "custom_paths", paths=["/etc/cron.d"])

    def test_a_path_outside_the_rules_is_refused(self):
        self.refused(FakeApi(card()), "backslash", paths=["C:\\Windows\\Tasks\\"])
        self.refused(FakeApi(card(LINUX_OPTIONS)), "lastlog", paths=["/var/log"])

    def test_a_collection_under_way_blocks_another(self):
        busy = {"id": 7, "command_status": "Pending", "ts_created": "2026-10-31T10:00:00Z", "user": "A. B."}
        text = self.refused(FakeApi(card(), before=[busy]), "collection 7", artifacts=["prefetch"])
        self.assertIn("xdr_abort_artifact_collection", text)

    def test_finished_collections_do_not_block(self):
        done = [{"id": n, "command_status": status} for n, status in enumerate(("Done", "Failed", "Aborted"))]
        api = FakeApi(card(), before=done)
        self.assertFalse(self.collect(api, artifacts=["prefetch"]).is_error)
        self.assertEqual(len(api.posts()), 1)

    def test_presets_are_looked_up_and_sent(self):
        presets = [
            {"id": "p1", "name": "Win · Tasks", "params": {"paths": [r"C:\Windows\Tasks"]}},
            {"id": "p2", "name": "Win · Hives", "params": {"commands": [r"reg save HKLM\SAM C:\f6ir\SAM"]}},
        ]
        api = FakeApi(card(), presets=presets)
        result = self.collect(api, preset_ids=["p2", "p1"])
        self.assertFalse(result.is_error, result.content[0].text)
        self.assertEqual(api.posts()[0]["preset_ids"], ["p2", "p1"])
        self.assertIn("Win · Hives", result.content[0].text)

    def test_an_unknown_preset_is_refused(self):
        self.refused(FakeApi(card(), presets=[{"id": "p1"}]), "no custom artifact p9", preset_ids=["p9"])

    def test_preset_commands_need_the_agent_to_take_commands(self):
        options = [name for name in WINDOWS_OPTIONS if name != "commands_to_execute"]
        presets = [{"id": "p1", "params": {"commands": ["whoami"]}}]
        self.refused(
            FakeApi(card(options=options), presets=presets), "commands_to_execute", preset_ids=["p1"]
        )
        # The console hides commands from a host whose agent may not run them.
        features = {**FEATURES, "triage_execute_commands": False}
        api = FakeApi(card(features=features), presets=presets)
        self.refused(api, "not allowed to run commands", preset_ids=["p1"])

    def test_too_long_a_windows_collection_is_refused(self):
        paths = [rf"C:\Users\*\AppData\Local\Vendor{n:03}\{'x' * 80}" for n in range(80)]
        self.refused(FakeApi(card()), "over the 8191", paths=paths)

    def test_the_code_is_sent_as_the_password(self):
        api = FakeApi(card(LINUX_OPTIONS))
        self.assertFalse(self.collect(api, artifacts=["shell_history"], otp="123456").is_error)
        self.assertEqual(api.posts()[0]["password"], "123456")

    def test_a_malformed_code_is_refused_before_any_request(self):
        for otp in ("12ab56", "123", 123456, ""):
            with self.subTest(otp=otp):
                api = FakeApi(card())
                result = self.collect(api, artifacts=["prefetch"], otp=otp)
                self.assertTrue(result.is_error)
                self.assertEqual(api.calls, [])

    def test_commands_are_not_taken_directly(self):
        api = FakeApi(card())
        result = self.collect(api, commands=["whoami"])
        self.assertTrue(result.is_error)
        self.assertIn("preset_ids", result.content[0].text)
        self.assertEqual(api.calls, [])

    def test_nothing_to_collect_is_refused(self):
        api = FakeApi(card())
        result = self.collect(api, artifacts=[], paths=[])
        self.assertIn("nothing to collect", result.content[0].text)
        self.assertEqual(api.calls, [])

    def test_a_refused_protected_collection_explains_the_code(self):
        error = RuntimeError(
            'API error 400 Bad Request: {"data":[],"errors":[],"messages":[{"message":"wrong code"}]}'
        )
        api = FakeApi(card(LINUX_OPTIONS), post_error=error)
        result = self.collect(api, artifacts=["shell_history"])
        self.assertTrue(result.is_error)
        self.assertIn("shell_history", result.content[0].text)
        self.assertIn("otp", result.content[0].text)

    def test_other_refusals_pass_through_unchanged(self):
        error = RuntimeError("API error 400 Bad Request: {}")
        result = self.collect(FakeApi(card(), post_error=error), artifacts=["prefetch"])
        self.assertTrue(result.is_error)
        self.assertNotIn("otp", result.content[0].text)

    def abort(self, api, **arguments):
        return self.call(api, name="xdr_abort_artifact_collection", **{"machine_id": MACHINE_ID, **arguments})

    def test_abort_a_collection_under_way(self):
        api = FakeApi(card(), before=[{"id": 5, "command_status": "Pending"}])
        result = self.abort(api, report_id=5)
        self.assertFalse(result.is_error, result.content[0].text)
        self.assertEqual(api.posts(), [{}])
        self.assertEqual(api.calls[-1][1], "/api/assets/forensic-reports/5/forensic-report/abort/")

    def test_a_finished_collection_is_not_aborted(self):
        # The API takes it and answers that it stopped, and the finished archive
        # can no longer be read afterwards. Seen on a live installation.
        for status in ("Done", "Failed", "Aborted"):
            with self.subTest(status=status):
                api = FakeApi(card(), before=[{"id": 5, "command_status": status}])
                result = self.abort(api, report_id=5)
                self.assertTrue(result.is_error)
                self.assertIn("not under way", result.content[0].text)
                self.assertEqual(api.posts(), [])

    def test_a_collection_of_another_host_is_not_aborted(self):
        api = FakeApi(card(), before=[{"id": 6, "command_status": "Pending"}])
        result = self.abort(api, report_id=5)
        self.assertIn("not among the recent collections", result.content[0].text)
        self.assertEqual(api.posts(), [])

    def test_abort_needs_a_host_and_a_report_id(self):
        for arguments in ({"report_id": None}, {"report_id": 0}, {"report_id": "5/../x"},
                          {"report_id": 5, "machine_id": "nope"}):
            with self.subTest(arguments=arguments):
                api = FakeApi(card())
                result = self.abort(api, **arguments)
                self.assertTrue(result.is_error)
                self.assertEqual(api.calls, [])

    def test_presets_list_keeps_what_matters_and_filters(self):
        presets = [
            {"id": "p1", "name": "Win · Tasks", "description": "", "params": {"paths": ["C:\\x"]},
             "creator_id": 3, "can_manage": True},
            {"id": "p2", "name": "Linux · Logs", "description": "auth and syslog", "params": {}},
        ]
        result = self.call(FakeApi(card(), presets=presets), name="xdr_list_artifact_presets", search="LOGS")
        text = result.content[0].text
        self.assertIn('"count": 1', text)
        self.assertIn("Linux · Logs", text)
        self.assertNotIn("creator_id", text)


CODE_REFUSAL ='API error 400 Bad Request: {"data":[],"errors":[],"messages":[{"message":"Неверный код"}]}'


class TestPasswordPrompt(unittest.TestCase):
    """With XDR_PASSWORD_PROMPT the second factor comes from a window, never through the model."""

    def setUp(self):
        self.server = load_server(XDR_ALLOW_COLLECT="1", XDR_PASSWORD_PROMPT="1")

    def collect(self, api, typed, **arguments):
        params = self.server.types.CallToolRequestParams(
            name="xdr_collect_artifacts", arguments={"machine_id": MACHINE_ID, **arguments}
        )
        ask = mock.AsyncMock(return_value=typed)
        with mock.patch.object(self.server, "xdr_request", new=api), \
                mock.patch.object(self.server, "ask_password", new=ask):
            result = run(self.server.on_call_tool(None, params))
        return result, ask

    def test_off_by_default(self):
        s = load_server(XDR_ALLOW_COLLECT="1")
        self.assertFalse(s.PASSWORD_PROMPT)
        self.assertIn("pass it as 'otp'", s.COLLECT_ARTIFACTS_TOOL.description)
        self.assertIn("window of its own", self.server.COLLECT_ARTIFACTS_TOOL.description)
        self.assertNotIn("pass it as 'otp'", self.server.COLLECT_ARTIFACTS_TOOL.description)

    def test_the_window_supplies_the_second_factor(self):
        api = FakeApi(card(LINUX_OPTIONS), post_errors=[RuntimeError(CODE_REFUSAL)])
        result, ask = self.collect(api, "s3cret pass", artifacts=["shell_history"], paths=["/etc/hosts"])
        self.assertFalse(result.is_error, result.content[0].text)
        first, second = api.posts()
        self.assertNotIn("password", first)
        self.assertEqual(second["password"], "s3cret pass")
        # The person sees what they are letting through, and what the console said.
        prompt = ask.await_args.args[0]
        for expected in ("WS-1 (Linux)", "shell_history", "/etc/hosts", "Неверный код"):
            self.assertIn(expected, prompt)
        self.assertNotIn("s3cret", result.content[0].text)

    def test_closing_the_window_collects_nothing(self):
        api = FakeApi(card(LINUX_OPTIONS), post_errors=[RuntimeError(CODE_REFUSAL)])
        result, _ = self.collect(api, None, artifacts=["shell_history"])
        self.assertTrue(result.is_error)
        self.assertIn("closed the password window", result.content[0].text)
        self.assertEqual(len(api.posts()), 1)

    def test_a_wrong_second_factor_is_reported_without_it(self):
        api = FakeApi(card(LINUX_OPTIONS), post_errors=[RuntimeError(CODE_REFUSAL)] * 2)
        result, _ = self.collect(api, "hunter2-xyz", artifacts=["shell_history"])
        self.assertTrue(result.is_error)
        self.assertIn("turned down what the user typed", result.content[0].text)
        self.assertNotIn("hunter2-xyz", result.content[0].text)
        self.assertEqual(len(api.posts()), 2)

    def test_another_refusal_after_the_second_factor_is_passed_on(self):
        other = RuntimeError('API error 400 Bad Request: {"messages":[{"message":"service unavailable"}]}')
        api = FakeApi(card(LINUX_OPTIONS), post_errors=[RuntimeError(CODE_REFUSAL), other])
        result, _ = self.collect(api, "s3cret", artifacts=["shell_history"])
        self.assertIn("service unavailable", result.content[0].text)
        self.assertNotIn("turned down", result.content[0].text)

    def test_a_one_time_code_is_asked_for_when_that_is_missing(self):
        api = FakeApi(card(LINUX_OPTIONS), post_errors=[RuntimeError(CODE_REFUSAL)])
        result, ask = self.collect(api, "123456", artifacts=["shell_history"])
        self.assertFalse(result.is_error, result.content[0].text)
        self.assertIn("одноразовый код", ask.await_args.args[0])
        self.assertEqual(api.posts()[1]["password"], "123456")

    def test_unprotected_refusals_open_no_window(self):
        api = FakeApi(card(LINUX_OPTIONS), post_error=RuntimeError(CODE_REFUSAL))
        result, ask = self.collect(api, "x", artifacts=["hosts"])
        self.assertTrue(result.is_error)
        ask.assert_not_awaited()

    def test_a_code_from_the_model_is_used_as_is(self):
        api = FakeApi(card(LINUX_OPTIONS), post_error=RuntimeError(CODE_REFUSAL))
        result, ask = self.collect(api, "x", artifacts=["shell_history"], otp="123456")
        ask.assert_not_awaited()
        self.assertEqual([body["password"] for body in api.posts()], ["123456"])
        self.assertIn("call again with otp", result.content[0].text)


class TestAskPassword(unittest.IsolatedAsyncioTestCase):
    """The window: its text cannot run, and it stays off the MCP channel."""

    def setUp(self):
        self.server = load_server()

    async def ask(self, platform, returncode=0, out=b"pa ss\n"):
        process = mock.MagicMock(returncode=returncode)
        process.communicate = mock.AsyncMock(return_value=(out, b""))
        spawn = mock.AsyncMock(return_value=process)
        with mock.patch.object(self.server, "sys", mock.MagicMock(platform=platform)), \
                mock.patch.object(self.server.asyncio, "create_subprocess_exec", new=spawn):
            answer = await self.server.ask_password('x" & do shell script "touch /tmp/pwned" & "')
        return answer, spawn

    async def test_macos_dialog(self):
        answer, spawn = await self.ask("darwin")
        self.assertEqual(answer, "pa ss")
        argv, kwargs = spawn.await_args.args, spawn.await_args.kwargs
        self.assertEqual(argv[0], "/usr/bin/osascript")
        # The prompt is an argument of the script, not a part of it.
        self.assertEqual(argv[-1], 'x" & do shell script "touch /tmp/pwned" & "')
        self.assertFalse(any("touch" in part for part in argv[:-1]))
        self.assertIn("with hidden answer", " ".join(argv))
        # stdin and stdout of this server are the MCP channel.
        self.assertEqual(kwargs["stdin"], self.server.asyncio.subprocess.DEVNULL)
        self.assertEqual(kwargs["stdout"], self.server.asyncio.subprocess.PIPE)

    async def test_cancel_and_empty_answers_are_none(self):
        self.assertIsNone((await self.ask("darwin", returncode=1))[0])
        self.assertIsNone((await self.ask("darwin", out=b"\n"))[0])

    async def test_no_window_without_a_way_to_show_one(self):
        with mock.patch.object(self.server.shutil, "which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "macOS or zenity"):
                await self.ask("linux")


if __name__ == "__main__":
    unittest.main()
