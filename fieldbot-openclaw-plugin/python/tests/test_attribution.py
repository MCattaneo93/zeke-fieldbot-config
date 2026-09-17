"""Whose time gets logged, and when fieldbot refuses to guess.

The rule: the ticket decides. An explicit claim or a single registered assignee
is a better answer than whoever happened to type the message, so the asker's
identity is only needed when the ticket cannot answer.
"""

from __future__ import annotations

import importlib
import os
import sys
import tempfile
import unittest
from unittest.mock import patch
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
actions = importlib.import_module("actions")
config = importlib.import_module("config")

# Patched per-test rather than assigned: these modules are shared with the other
# suites, and mutating USER_MAP at import time silently breaks them.
TEST_USER_MAP = {"111": "matt@example.invalid", "222": "mike@example.invalid"}

class FakeTech:
    """A stand-in for actions.Tech, whose real constructor queries Odoo."""

    def __init__(self, uid: int, name: str):
        self.uid = uid
        self.name = name


MATT = FakeTech(uid=11, name="Matthew Cattaneo")
MIKE = FakeTech(uid=22, name="Mike Huang")
TECHS = {111: MATT, 222: MIKE}


class TimesheetActorTests(unittest.TestCase):
    def actor(self, raw_sender, *, claim_owner=None, assigned_uids=()):
        """Run timesheet_actor against a fake ticket."""

        def fake_execute(model, method, args, kwargs=None):
            return [{"user_ids": list(assigned_uids)}]

        with patch.object(config, "USER_MAP", TEST_USER_MAP), \
             patch.object(bridge.config, "USER_MAP", TEST_USER_MAP), \
             patch.object(bridge.state, "claim_owner", return_value=claim_owner), \
             patch.object(actions, "get_tech", side_effect=lambda tid: TECHS.get(int(tid))), \
             patch.object(actions.client, "execute", side_effect=fake_execute), \
             patch.object(actions, "task_label", return_value="#6129"):
            return bridge.timesheet_actor(raw_sender, 6129, "close")

    def test_a_sole_assignee_is_enough_with_no_asker(self):
        _, tech, telegram_id = self.actor(None, assigned_uids=[MIKE.uid])
        self.assertEqual(tech.name, "Mike Huang")
        self.assertEqual(telegram_id, 222)

    def test_an_explicit_claim_wins_over_assignment(self):
        _, tech, _ = self.actor(None, claim_owner=111, assigned_uids=[MIKE.uid])
        self.assertEqual(tech.name, "Matthew Cattaneo")

    def test_the_asker_is_used_when_the_ticket_cannot_answer(self):
        _, tech, telegram_id = self.actor("111", assigned_uids=[])
        self.assertEqual(tech.name, "Matthew Cattaneo")
        self.assertEqual(telegram_id, 111)

    def test_an_unassigned_ticket_with_no_asker_is_refused(self):
        with self.assertRaisesRegex(PermissionError, "can't tell whose time"):
            self.actor(None, assigned_uids=[])

    def test_two_registered_assignees_with_no_asker_is_refused(self):
        # Ambiguous on purpose: logging one technician's hours against the
        # other is exactly the mistake this refusal exists to prevent.
        with self.assertRaisesRegex(PermissionError, "can't tell whose time"):
            self.actor(None, assigned_uids=[MATT.uid, MIKE.uid])

    def test_an_unregistered_sender_is_refused_even_when_the_ticket_is_clear(self):
        # A stranger must not be able to close tickets just because the ticket
        # itself would have answered the attribution question.
        with self.assertRaisesRegex(PermissionError, "registered Telegram technician"):
            self.actor("999999", assigned_uids=[MIKE.uid])

    def test_an_unrelated_assignee_does_not_count(self):
        # Someone assigned in Odoo who is not a registered fieldbot technician
        # cannot be the attribution target.
        with self.assertRaisesRegex(PermissionError, "can't tell whose time"):
            self.actor(None, assigned_uids=[9999])

    def test_the_refusal_names_the_ticket_and_a_way_forward(self):
        with self.assertRaises(PermissionError) as caught:
            self.actor(None, assigned_uids=[])
        message = str(caught.exception)
        self.assertIn("#6129", message)
        self.assertIn("/assign", message)


if __name__ == "__main__":
    unittest.main()
