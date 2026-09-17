"""Offline tests for the text the command surface sends straight to Telegram.

These replies bypass the model, so whatever this renders is exactly what a
technician reads. Untrimmed output is not a cosmetic problem: a full plate runs
past 19k characters, which Telegram breaks into a wall of separate messages.
"""

from __future__ import annotations

import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path


PYTHON_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PYTHON_DIR))

_state_temp = tempfile.TemporaryDirectory()
os.environ.setdefault("FIELDBOT_ODOO_URL", "https://example.invalid")
os.environ.setdefault("FIELDBOT_ODOO_DB", "test")
os.environ.setdefault("FIELDBOT_ODOO_LOGIN", "fieldbot@example.invalid")
os.environ.setdefault("FIELDBOT_ODOO_API_KEY", "test-only-key")
os.environ.setdefault("FIELDBOT_USER_MAP", '{"123":"tech@example.invalid"}')
os.environ.setdefault("FIELDBOT_STATE_DIR", _state_temp.name)

bridge = importlib.import_module("bridge")


def ticket(task_id: int, priority: str = "0", last_update: str = "2026-01-01") -> dict:
    return {
        "task_id": task_id,
        "title": f"Ticket {task_id}",
        "customer": "",
        "project": "",
        "stage": "Assigned",
        "assigned_to": ["Matthew Cattaneo"],
        "priority": priority,
        "last_update": last_update,
        "deadline": None,
        "next_step": None,
    }


class CappedListTests(unittest.TestCase):
    def test_no_limit_renders_everything(self):
        tickets = [ticket(i) for i in range(30)]
        message = bridge.capped_list(tickets, "Head", "Empty", None)
        self.assertEqual(message.count("\n• "), 30)
        self.assertNotIn("more.", message)

    def test_limit_trims_and_says_how_many_were_hidden(self):
        tickets = [ticket(i) for i in range(30)]
        message = bridge.capped_list(tickets, "Head", "Empty", 15)
        self.assertEqual(message.count("\n• "), 15)
        self.assertIn("…and 15 more.", message)

    def test_a_short_list_is_never_annotated(self):
        tickets = [ticket(i) for i in range(3)]
        message = bridge.capped_list(tickets, "Head", "Empty", 15)
        self.assertEqual(message.count("\n• "), 3)
        self.assertNotIn("more.", message)

    def test_trimming_keeps_the_urgent_tickets(self):
        # One hot ticket buried at the end of a long list must survive the cut.
        tickets = [ticket(i, last_update="2026-06-01") for i in range(30)]
        tickets.append(ticket(999, priority="1", last_update="2026-06-02"))
        message = bridge.capped_list(tickets, "Head", "Empty", 5)
        self.assertIn("#999", message)

    def test_an_empty_list_uses_the_empty_text(self):
        self.assertEqual(bridge.capped_list([], "Head", "Nothing here.", 15), "Nothing here.")

    def test_a_junk_limit_is_treated_as_no_limit_not_as_zero(self):
        tickets = [ticket(i) for i in range(4)]
        for junk in (None, "", "abc", -1, 0, [1]):
            message = bridge.capped_list(tickets, "Head", "Empty", junk)
            self.assertEqual(message.count("\n• "), 4, f"limit={junk!r} dropped tickets")


if __name__ == "__main__":
    unittest.main()
