"""Tests for server.py's opt-in write-tool registration.

The same server runs as a local stdio connector and as a shared --http
endpoint, so the Google Sheets / Apps Script write tools must appear ONLY when
WAREHOUSE_MCP_ENABLE_WRITES is exactly "1", must carry write annotations
(readOnlyHint=False, destructiveHint=True) so clients prompt before calling
them, and must never change the read-only tool set. Hermetic: no network, no
Google credentials (registration only imports google_sheets_script; nothing is
called).
"""
from __future__ import annotations

import asyncio
import os
import unittest
from unittest import mock

import server

WRITE_TOOL_NAMES = {
    "sheets_add_tabs", "sheets_rename_tab", "sheets_write_values",
    "sheets_set_checkboxes", "script_create_project", "script_push_content",
    "script_deploy",
}


def _tools(srv):
    return {t.name: t for t in asyncio.run(srv.list_tools())}


class WriteToolRegistrationTests(unittest.TestCase):
    def _build(self, env_value):
        with mock.patch.dict("os.environ"):
            os.environ.pop(server.WAREHOUSE_MCP_ENABLE_WRITES_ENV, None)
            if env_value is not None:
                os.environ[server.WAREHOUSE_MCP_ENABLE_WRITES_ENV] = env_value
            return server.build_server()

    def test_write_tools_absent_by_default(self):
        tools = _tools(self._build(None))
        self.assertFalse(WRITE_TOOL_NAMES & set(tools))

    def test_only_the_exact_value_1_enables_them(self):
        for value in ("0", "true", "yes", "", " 1"):
            tools = _tools(self._build(value))
            self.assertFalse(WRITE_TOOL_NAMES & set(tools), f"enabled by {value!r}")

    def test_flag_registers_all_write_tools_with_write_annotations(self):
        tools = _tools(self._build("1"))
        self.assertTrue(WRITE_TOOL_NAMES <= set(tools))
        for name in WRITE_TOOL_NAMES:
            ann = tools[name].annotations
            self.assertFalse(ann.readOnlyHint, name)
            self.assertTrue(ann.destructiveHint, name)

    def test_flag_leaves_the_read_only_tools_unchanged(self):
        base = _tools(self._build(None))
        enabled = _tools(self._build("1"))
        for name, tool in base.items():
            self.assertIn(name, enabled)
            self.assertTrue(enabled[name].annotations.readOnlyHint, name)
        self.assertEqual(set(enabled) - set(base), WRITE_TOOL_NAMES)

    def test_register_write_tools_reports_its_decision(self):
        srv = self._build(None)
        with mock.patch.dict("os.environ", {server.WAREHOUSE_MCP_ENABLE_WRITES_ENV: "0"}):
            self.assertFalse(server.register_write_tools(srv))


if __name__ == "__main__":
    unittest.main()
