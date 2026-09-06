# Copyright 2026 Future Joint-Stock Company
# SPDX-License-Identifier: Apache-2.0

"""Tests for the XDR MCP server.

Run from the repository root:

    ./venv/bin/python -m unittest discover -s tests -v
"""

import importlib
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

BASE_ENV = {"XDR_BASE_URL": "https://xdr.example.com", "XDR_API_KEY": "test-key"}


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
        self.assertEqual(len(s.TOOLS), 4)

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
    def test_only_the_write_tool_is_marked_destructive(self):
        s = load_server(XDR_ALLOW_WRITE="1")
        for tool in s.TOOLS:
            with self.subTest(tool=tool.name):
                if tool.name == "xdr_mark_event":
                    self.assertFalse(tool.annotations.read_only_hint)
                    self.assertTrue(tool.annotations.destructive_hint)
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

    def test_mapping_and_filters(self):
        request, _ = self.call("xdr_get_mapping", {"section": "applications"})
        request.assert_awaited_once_with("GET", "/api/software/mapping/")

        request, _ = self.call("xdr_get_filters", {"section": "audit"})
        request.assert_awaited_once_with("GET", "/api/system_logs/filters/")

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


if __name__ == "__main__":
    unittest.main()
