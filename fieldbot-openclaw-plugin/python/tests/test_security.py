"""Offline regression tests for Fieldbot's security boundaries."""

from __future__ import annotations

import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PYTHON_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PYTHON_DIR))

_state_temp = tempfile.TemporaryDirectory()
os.environ.update(
    {
        "FIELDBOT_ODOO_URL": "https://example.invalid",
        "FIELDBOT_ODOO_DB": "test",
        "FIELDBOT_ODOO_LOGIN": "fieldbot@example.invalid",
        "FIELDBOT_ODOO_API_KEY": "test-only-key",
        "FIELDBOT_USER_MAP": '{"123":"tech@example.invalid"}',
        "FIELDBOT_STATE_DIR": _state_temp.name,
    }
)

bridge = importlib.import_module("bridge")
odoo_client = importlib.import_module("odoo_client")


class FakeModels:
    def __init__(self):
        self.calls = []

    def execute_kw(self, *args):
        self.calls.append(args)
        return True


class SecurityBoundaryTests(unittest.TestCase):
    def test_whitelist_blocks_customer_communication_models(self):
        client = odoo_client.OdooClient()
        with self.assertRaisesRegex(odoo_client.OdooError, "Blocked by whitelist"):
            client.execute("mail.mail", "create", [{}])
        with self.assertRaisesRegex(odoo_client.OdooError, "Blocked by whitelist"):
            client.execute("sms.sms", "create", [{}])

    def test_message_post_is_forced_to_internal_note(self):
        client = odoo_client.OdooClient()
        fake = FakeModels()
        client._uid = 7
        client._models = fake
        client.execute(
            "project.task",
            "message_post",
            [[42]],
            {
                "body": "Internal status",
                "partner_ids": [999],
                "email_from": "attacker@example.invalid",
                "subtype_xmlid": "mail.mt_comment",
            },
        )
        kwargs = fake.calls[0][-1]
        self.assertEqual(kwargs["subtype_xmlid"], "mail.mt_note")
        self.assertNotIn("partner_ids", kwargs)
        self.assertNotIn("email_from", kwargs)

    def test_sender_identity_is_parsed_from_runtime_context(self):
        self.assertEqual(bridge.sender_id("telegram:123"), 123)
        self.assertEqual(bridge.sender_id("tg:123"), 123)
        self.assertIsNone(bridge.sender_id("telegram:not-a-number"))

    def test_media_path_must_stay_inside_workspace(self):
        with tempfile.TemporaryDirectory() as workspace, tempfile.NamedTemporaryFile(
            suffix=".jpg"
        ) as outside:
            with self.assertRaisesRegex(PermissionError, "inside the active workspace"):
                bridge.validated_media_path(outside.name, workspace)

    def test_assignment_works_without_caller_identity_when_owner(self):
        """Group ticket-card buttons name their target, so they need no caller.

        The group chat gives OpenClaw no per-person identity, but the button
        payload fully specifies the change, and the runtime owner bit proves the
        requester is one of the team.
        """
        with patch.object(bridge.actions, "reassign_ticket") as reassign:
            reassign.return_value = "assigned"
            bridge.dispatch(
                "reassign",
                {"task_id": 6230, "mode": "set", "names": ["mike"]},
                {"requester_sender_id": None, "sender_is_owner": True},
            )
            reassign.assert_called_once()
            # No caller identity available, so nothing is attributed to a person.
            self.assertIsNone(reassign.call_args[0][0])

    def test_assignment_is_refused_without_owner_or_identity(self):
        with patch.object(bridge.actions, "reassign_ticket") as reassign:
            with self.assertRaisesRegex(PermissionError, "field-service team"):
                bridge.dispatch(
                    "reassign",
                    {"task_id": 6230, "mode": "set", "names": ["mike"]},
                    {"requester_sender_id": None, "sender_is_owner": False},
                )
            reassign.assert_not_called()

    def test_owner_bit_does_not_unlock_claiming(self):
        """Claiming says who owns the work, so it still needs a real identity."""
        with self.assertRaises(PermissionError):
            bridge.dispatch(
                "claim",
                {"task_id": 6230},
                {"requester_sender_id": None, "sender_is_owner": True},
            )

    def test_owner_bit_does_not_unlock_an_unattributable_timesheet(self):
        """Closing reads whose work it is off the ticket, not off the owner bit.

        A ticket that nobody has claimed and that is not assigned to exactly one
        registered technician cannot answer the question, and the owner bit must
        not paper over that - logging one technician's hours against another is
        the mistake this guards.
        """
        with patch.object(bridge.state, "claim_owner", return_value=None), \
             patch.object(
                 bridge.actions.client, "execute", return_value=[{"user_ids": []}]
             ), \
             patch.object(bridge.actions, "get_tech", return_value=None), \
             patch.object(bridge.actions, "task_label", return_value="#6230"):
            for operation, params in (
                ("close", {"task_id": 6230, "hours": 1}),
                ("log_update", {"task_id": 6230, "comment": "x", "hours": None}),
            ):
                with self.subTest(operation=operation):
                    with self.assertRaisesRegex(PermissionError, "whose time"):
                        bridge.dispatch(
                            operation,
                            params,
                            {"requester_sender_id": None, "sender_is_owner": True},
                        )

    def test_owner_bit_is_never_taken_from_tool_arguments(self):
        """A model-supplied param must not be able to forge authorization."""
        with patch.object(bridge.actions, "reassign_ticket") as reassign:
            with self.assertRaises(PermissionError):
                bridge.dispatch(
                    "reassign",
                    {
                        "task_id": 6230,
                        "mode": "set",
                        "names": ["mike"],
                        "sender_is_owner": True,
                    },
                    {"requester_sender_id": None},
                )
            reassign.assert_not_called()

    def test_name_matching_is_exact_never_substring(self):
        """A partial name must never resolve to a technician.

        Assignment states who owns a customer commitment. Prefix matching made
        "matt" work only by luck, and would have picked the wrong person as soon
        as a second similar name existed in Odoo.
        """
        actions = importlib.import_module("actions")

        class FakeTech:
            def __init__(self, name, login, uid):
                self.name = name
                self.odoo_login = login
                self.uid = uid

        techs = {123: FakeTech("Matthew Cattaneo", "matthew@pos.com", 263)}
        with patch.object(actions, "get_tech", side_effect=lambda tg: techs.get(tg)):
            with patch.object(actions.config, "TECH_ALIASES", {"matt": "matthew@pos.com"}):
                resolves = ("Matthew", "matthew", "MATTHEW", "Matthew Cattaneo",
                            "matthew@pos.com", "matt", "MATT")
                for name in resolves:
                    with self.subTest(name=name):
                        self.assertIsNotNone(
                            actions.find_tech_by_first_name(name),
                            f"{name!r} should resolve exactly",
                        )
                # Partials, and a different person who merely shares a prefix.
                for name in ("mat", "matth", "cattaneo", "matthew c", "matty",
                             "matt jones", "", "  "):
                    with self.subTest(name=name):
                        self.assertIsNone(
                            actions.find_tech_by_first_name(name),
                            f"{name!r} must NOT resolve to a technician",
                        )

    def test_unknown_alias_does_not_resolve(self):
        actions = importlib.import_module("actions")
        with patch.object(actions, "get_tech", side_effect=lambda tg: None):
            with patch.object(actions.config, "TECH_ALIASES", {"matt": "matthew@pos.com"}):
                self.assertIsNone(actions.find_tech_by_first_name("matt"))

    def test_direct_user_cannot_run_new_ticket_poll(self):
        with patch.object(bridge.reports, "poll_new_tickets") as poll:
            with self.assertRaisesRegex(PermissionError, "restricted to automations"):
                bridge.dispatch(
                    "poll_new_tickets",
                    {},
                    {"requester_sender_id": "telegram:123"},
                )
            poll.assert_not_called()


if __name__ == "__main__":
    unittest.main()

