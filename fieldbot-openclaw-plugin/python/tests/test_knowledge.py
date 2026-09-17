"""Offline tests for read-only Knowledge base access.

The KB holds technicians' working notes, including logins and PINs. These tests
pin the three properties that make exposing it to an agent acceptable: Odoo is
reachable read-only, templates are never returned, and no credential survives
into the text the agent receives.
"""

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
os.environ.setdefault("FIELDBOT_ODOO_URL", "https://example.invalid")
os.environ.setdefault("FIELDBOT_ODOO_DB", "test")
os.environ.setdefault("FIELDBOT_ODOO_LOGIN", "fieldbot@example.invalid")
os.environ.setdefault("FIELDBOT_ODOO_API_KEY", "test-only-key")
os.environ.setdefault("FIELDBOT_USER_MAP", '{"123":"tech@example.invalid"}')
os.environ.setdefault("FIELDBOT_STATE_DIR", _state_temp.name)

actions = importlib.import_module("actions")
bridge = importlib.import_module("bridge")
odoo_client = importlib.import_module("odoo_client")
redaction = importlib.import_module("redaction")


def article(article_id: int, name: str, body: str, parent: str | None = None) -> dict:
    return {
        "id": article_id,
        "name": name,
        "body": body,
        "parent_id": [99, parent] if parent else False,
        "write_date": "2026-09-01 12:00:00",
    }


class WhitelistTests(unittest.TestCase):
    def test_knowledge_is_search_read_only(self):
        # Exact set: widening this must be a deliberate, test-breaking change.
        self.assertEqual(odoo_client.WHITELIST["knowledge.article"], {"search_read"})

    def test_knowledge_writes_are_blocked(self):
        for method in ("write", "create", "unlink", "read", "message_post"):
            with self.subTest(method=method):
                with self.assertRaisesRegex(odoo_client.OdooError, "Blocked by whitelist"):
                    odoo_client.client.execute("knowledge.article", method, [[1]])

    def test_neighbouring_knowledge_models_are_unreachable(self):
        for model in ("knowledge.article.member", "knowledge.invite", "knowledge.article.thread"):
            with self.subTest(model=model):
                with self.assertRaisesRegex(odoo_client.OdooError, "Blocked by whitelist"):
                    odoo_client.client.execute(model, "search_read", [[]])


class RedactionTests(unittest.TestCase):
    def assertRedacted(self, text: str, secret: str):
        out, count = redaction.redact(text)
        self.assertNotIn(secret, out, f"{secret!r} leaked from {text!r}")
        self.assertGreaterEqual(count, 1)

    def test_labelled_passwords(self):
        self.assertRedacted("Password: hunter2", "hunter2")
        self.assertRedacted("admin pwd = S3cret!", "S3cret!")
        self.assertRedacted("the wifi key - CoffeeShop99", "CoffeeShop99")
        self.assertRedacted("The password is Tr0ub4dor", "Tr0ub4dor")

    def test_multi_word_passphrase_is_fully_removed(self):
        out, _ = redaction.redact("Passphrase: Summer 2024 Blue!")
        for part in ("Summer", "2024", "Blue"):
            self.assertNotIn(part, out)

    def test_table_cell_value(self):
        self.assertRedacted("Username | admin | Password | Z9x!qq |", "Z9x!qq")

    def test_keys_tokens_and_pins(self):
        self.assertRedacted("API key: sk_live_abc123", "sk_live_abc123")
        self.assertRedacted("token = eyJhbGciOi", "eyJhbGciOi")
        self.assertRedacted("Manager PIN 4821", "4821")

    def test_payment_data(self):
        self.assertRedacted("test card 4111 1111 1111 1111", "4111 1111 1111 1111")
        self.assertRedacted("CVV 123", "123")
        self.assertRedacted("exp 12/29", "12/29")
        self.assertRedacted("SSN 123-45-6789", "123-45-6789")

    def test_label_alone_with_value_on_next_line(self):
        self.assertRedacted("Office router\nPassword\nhunter2\nModel AX3000", "hunter2")
        self.assertRedacted("• PIN\n4821", "4821")
        out, _ = redaction.redact("Password\nhunter2\nModel AX3000")
        self.assertIn("Model AX3000", out)  # only the value's own line goes

    def test_a_value_is_counted_once(self):
        self.assertEqual(redaction.redact("Password:\nhunter2"), ("Password:\n[REDACTED]", 1))

    def test_prose_about_passwords_survives(self):
        text = "Reset the password in Settings, then reboot the terminal."
        self.assertEqual(redaction.redact(text), (text, 0))

    def test_identifiers_survive(self):
        text = "Router 192.168.1.1, login admin, 3 printers"
        self.assertEqual(redaction.redact(text)[0], text)


class WithholdingTests(unittest.TestCase):
    """Shapes taken from the real KB, where value-level redaction leaked."""

    def assertWithheld(self, text: str, secret: str):
        out, n = redaction.withhold_credential_lines(text)
        self.assertNotIn(secret, out, f"{secret!r} survived in {out!r}")
        self.assertIn(redaction.WITHHELD, out)
        self.assertGreaterEqual(n, 1)

    def test_value_in_running_prose(self):
        self.assertWithheld("In some cases the manager password would be 8812.", "8812")
        self.assertWithheld("Terminal Password for Admin X9-22 Plus", "X9-22")
        self.assertWithheld("Log in with the admin PASSWORD Qx7! then open EMV Secure", "Qx7!")

    def test_heading_followed_by_a_bare_list(self):
        text = ("Pin Pad Passwords\n\nPax A920 abc123\nIngenico 9981\n\n"
                "To reboot the pin pad, hold the red cancel key for about five seconds until it restarts.")
        out, _ = redaction.withhold_credential_lines(text)
        for secret in ("abc123", "9981"):
            self.assertNotIn(secret, out)
        self.assertIn("hold the red cancel key", out)  # prose after the block survives

    def test_sentence_running_onto_the_next_line(self):
        self.assertWithheld("The username would be tech with a password\nof Sunny99", "Sunny99")

    def test_login_info_on_the_next_line(self):
        self.assertWithheld("Here is the login info\nadmin / Qq7!", "Qq7!")

    def test_block_stops_when_prose_resumes(self):
        text = ("Passwords\nadmin x1\nuser y2\n"
                "This long sentence explains how to configure the terminal once login is complete.")
        out, _ = redaction.withhold_credential_lines(text)
        self.assertNotIn("x1", out)
        self.assertIn("configure the terminal", out)

    def test_pin_pads_are_hardware_not_pins(self):
        text = "Ingenico pin pad reboot steps are below."
        self.assertEqual(redaction.withhold_credential_lines(text), (text, 0))

    def test_harmless_mentions_are_withheld_too(self):
        # The accepted cost of failing closed.
        out, n = redaction.withhold_credential_lines("Reset the password in Settings.")
        self.assertEqual((out, n), (redaction.WITHHELD, 1))

    def test_adjacent_withheld_lines_collapse_to_one_marker(self):
        out, n = redaction.withhold_credential_lines(
            "Admin password is on the card.\nUser password is on the back.")
        self.assertEqual(out.count(redaction.WITHHELD), 1)
        self.assertEqual(n, 2)


class SearchKnowledgeTests(unittest.TestCase):
    def test_templates_are_excluded(self):
        with patch.object(actions.client, "search_read", return_value=[]) as sr:
            actions.search_knowledge("clover")
        domain = sr.call_args[0][1]
        self.assertIn(["is_template", "=", False], domain)

    def test_short_query_is_refused_without_calling_odoo(self):
        with patch.object(actions.client, "search_read") as sr:
            self.assertIn("word or phrase", actions.search_knowledge(" a "))
        sr.assert_not_called()

    def test_search_cannot_confirm_a_redacted_secret(self):
        # Searching for a guessed password must not reveal which article holds it.
        body = "<p>" + ("filler text " * 40) + "</p><p>Password: hunter2</p>"
        rows = [article(7, "Office router", body)]
        with patch.object(actions.client, "search_read", return_value=rows):
            out = actions.search_knowledge("hunter2")
        self.assertNotIn("#7", out)
        self.assertNotIn("hunter2", "\n".join(out.split("\n")[1:]))

    def test_snippet_near_a_secret_shows_it_withheld(self):
        body = "<p>Configure the router first.</p><p>Password: hunter2</p>"
        rows = [article(7, "Office network", body)]
        with patch.object(actions.client, "search_read", return_value=rows):
            out = actions.search_knowledge("router")
        self.assertIn("#7", out)
        self.assertNotIn("hunter2", out)
        self.assertIn("withheld", out)

    def test_withheld_marker_text_is_not_searchable(self):
        rows = [article(8, "Router", "<p>Password: hunter2</p>")]
        with patch.object(actions.client, "search_read", return_value=rows):
            out = actions.search_knowledge("odoo")
        self.assertNotIn("#8", out)

    def test_table_credentials_are_redacted_in_results(self):
        body = "<table><tr><td>Password</td><td>Z9x!qq</td></tr></table>"
        rows = [article(3, "Clover login", body)]
        with patch.object(actions.client, "search_read", return_value=rows):
            out = actions.search_knowledge("clover")
        self.assertNotIn("Z9x!qq", out)
        self.assertIn("credentials protected", out.lower())

    def test_no_results(self):
        with patch.object(actions.client, "search_read", return_value=[]):
            self.assertIn("Nothing in the knowledge base", actions.search_knowledge("zzz"))


class ReadArticleTests(unittest.TestCase):
    def test_templates_are_excluded(self):
        with patch.object(actions.client, "search_read", return_value=[]) as sr:
            actions.read_article(5)
        domain = sr.call_args[0][1]
        self.assertIn(["is_template", "=", False], domain)
        self.assertIn(["id", "=", 5], domain)

    def test_missing_article(self):
        with patch.object(actions.client, "search_read", return_value=[]):
            self.assertIn("No knowledge article #5", actions.read_article(5))

    def test_full_text_is_redacted_and_linked(self):
        body = "<h1>Setup</h1><ul><li>Step one</li></ul><p>PIN: 4821</p>"
        rows = [article(12, "Terminal setup", body, parent="Clover")]
        with patch.object(actions.client, "search_read", return_value=rows):
            out = actions.read_article(12)
        self.assertNotIn("4821", out)
        self.assertIn("• Step one", out)
        self.assertIn("/knowledge/article/12", out)
        self.assertIn("Clover", out)

    def test_long_articles_are_truncated_after_redaction(self):
        body = "<p>" + ("x" * (actions.KB_READ_MAX_CHARS + 500)) + "</p>"
        rows = [article(9, "Huge", body)]
        with patch.object(actions.client, "search_read", return_value=rows):
            out = actions.read_article(9)
        self.assertIn("truncated", out)
        self.assertLess(len(out), actions.KB_READ_MAX_CHARS + 600)

    def test_images_do_not_dump_base64(self):
        body = '<p>See <img src="data:image/png;base64,' + "A" * 4000 + '"> here</p>'
        rows = [article(4, "Screenshot", body)]
        with patch.object(actions.client, "search_read", return_value=rows):
            out = actions.read_article(4)
        self.assertNotIn("AAAA", out)
        self.assertIn("[image]", out)


class DispatchTests(unittest.TestCase):
    def test_bridge_routes_both_operations(self):
        with patch.object(actions, "search_knowledge", return_value="S") as s, \
             patch.object(actions, "read_article", return_value="R") as r:
            self.assertEqual(bridge.dispatch("search_knowledge", {"query": "q"}, {}), {"message": "S"})
            self.assertEqual(bridge.dispatch("read_article", {"article_id": "12"}, {}), {"message": "R"})
        s.assert_called_once_with("q")
        r.assert_called_once_with(12)


if __name__ == "__main__":
    unittest.main()
