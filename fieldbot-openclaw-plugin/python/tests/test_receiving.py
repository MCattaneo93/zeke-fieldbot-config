"""Offline tests for vendor delivery-slip serial entry on PO receipts.

All data here is synthetic. `button_validate` never appearing anywhere in
this file, and purchase.order staying read-only, are the properties this
whole feature depends on.
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

bridge = importlib.import_module("bridge")
receiving = importlib.import_module("receiving")
odoo_client = importlib.import_module("odoo_client")


class FakeOdoo:
    def __init__(self):
        self.receipts: list[dict] = []          # stock.picking rows (open incoming receipts)
        self.move_lines: list[dict] = []
        self.lots: dict[str, dict] = {}          # existing stock.lot rows, by name
        self.purchase_orders: list[dict] = []    # draft/sent POs
        self.calls: list[tuple] = []
        self.notes: list[tuple] = []

    def add_line(self, line_id, picking_id, product_id, product_name, lot_name=False, lot_id=False):
        self.move_lines.append({
            "id": line_id, "picking_id": [picking_id, "?"],
            "product_id": [product_id, product_name],
            "lot_id": lot_id, "lot_name": lot_name,
        })

    def execute(self, model, method, args, kwargs=None):
        kwargs = kwargs or {}
        self.calls.append((model, method, args, kwargs))
        if method == "search_read":
            return self._search_read(model, args[0], kwargs)
        if model == "stock.move.line" and method == "write":
            ids, vals = args
            for line in self.move_lines:
                if line["id"] in ids:
                    line["lot_name"] = vals.get("lot_name", line["lot_name"])
            return True
        if model == "stock.picking" and method == "message_post":
            self.notes.append((args[0][0], kwargs.get("body", "")))
            return True
        raise AssertionError(f"Unexpected fake Odoo call: {model}.{method}")

    def _search_read(self, model, domain, kwargs):
        if model == "stock.picking":
            if domain and domain[0][0] == "id":
                return [p for p in self.receipts if p["id"] == domain[0][2]]
            partner_clause = next((c for c in domain if c[0] == "partner_id"), None)
            out = list(self.receipts)
            if partner_clause:
                out = [p for p in out if partner_clause[2].lower() in p["partner_id"][1].lower()]
            return out
        if model == "stock.move.line":
            if domain[0][0] == "id":
                return [l for l in self.move_lines if l["id"] in domain[0][2]]
            if domain[0][0] == "lot_name":
                names = domain[0][2]
                return [l for l in self.move_lines if l.get("lot_name") and l["lot_name"] in names]
            picking_id = domain[0][2]
            return [l for l in self.move_lines if l["picking_id"][0] == picking_id]
        if model == "stock.lot":
            names = domain[0][2]
            return [self.lots[n] for n in names if n in self.lots]
        if model == "purchase.order":
            partner_clause = next((c for c in domain if c[0] == "partner_id"), None)
            out = list(self.purchase_orders)
            if partner_clause:
                out = [p for p in out if partner_clause[2].lower() in p["partner_id"][1].lower()]
            return out
        raise AssertionError(f"Unexpected fake search_read model: {model}")


def patched(fake: FakeOdoo):
    return patch.object(receiving.client, "execute", side_effect=fake.execute)


class WhitelistTests(unittest.TestCase):
    def test_purchase_order_is_read_only(self):
        self.assertEqual(odoo_client.WHITELIST["purchase.order"], {"search_read"})
        for method in ("write", "create", "unlink", "button_confirm"):
            with self.subTest(method=method):
                with self.assertRaisesRegex(odoo_client.OdooError, "Blocked by whitelist"):
                    odoo_client.client.execute("purchase.order", method, [[1]])

    def test_validate_is_never_whitelisted(self):
        with self.assertRaisesRegex(odoo_client.OdooError, "Blocked by whitelist"):
            odoo_client.client.execute("stock.picking", "button_validate", [[1]])


class ParseSerialListTests(unittest.TestCase):
    def test_basic_list(self):
        out = receiving.parse_serial_list("SN0001\nSN0002\nSN0003")
        self.assertEqual(out["serials"], ["SN0001", "SN0002", "SN0003"])
        self.assertEqual(out["warnings"], [])

    def test_comma_and_pipe_separated(self):
        out = receiving.parse_serial_list("SN0001, SN0002 | SN0003")
        self.assertEqual(out["serials"], ["SN0001", "SN0002", "SN0003"])

    def test_case_normalized(self):
        out = receiving.parse_serial_list("sn0001")
        self.assertEqual(out["serials"], ["SN0001"])

    def test_duplicate_is_counted_once_and_warned(self):
        out = receiving.parse_serial_list("SN0001\nSN0001")
        self.assertEqual(out["serials"], ["SN0001"])
        self.assertTrue(any("Duplicate" in w for w in out["warnings"]))

    def test_junk_token_is_reported_not_dropped_silently(self):
        out = receiving.parse_serial_list("SN0001\nsee attached photo")
        self.assertEqual(out["serials"], ["SN0001"])
        self.assertTrue(any("see attached photo" in w or "photo" in w for w in out["warnings"]))


class NormalizePoRefTests(unittest.TestCase):
    def test_variants_are_equivalent(self):
        variants = ["P90001", "PO90001", "090001", "90001"]
        refs = {receiving._normalize_po_ref(v) for v in variants}
        self.assertEqual(len(refs), 1)


class ResolveReceiptTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeOdoo()

    def test_matched_by_po_reference(self):
        self.fake.receipts = [
            {"id": 1534, "name": "POS.N/IN/00306", "origin": "P90001", "partner_id": [5366, "Acme Hardware Supply"]},
        ]
        with patched(self.fake):
            result = receiving.resolve_receipt(po_reference="PO90001")
        self.assertEqual(result, {
            "status": "matched", "picking_id": 1534, "picking_name": "POS.N/IN/00306",
            "po_name": "P90001", "vendor_name": "Acme Hardware Supply",
        })

    def test_matched_by_vendor_name_when_only_one_open_receipt(self):
        self.fake.receipts = [
            {"id": 1534, "name": "POS.N/IN/00306", "origin": "P90001", "partner_id": [5366, "Acme Hardware Supply"]},
        ]
        with patched(self.fake):
            result = receiving.resolve_receipt(vendor_name="Acme Hardware")
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["picking_id"], 1534)

    def test_ambiguous_when_vendor_has_two_open_receipts(self):
        self.fake.receipts = [
            {"id": 645, "name": "POS.N/IN/00112", "origin": "P90002", "partner_id": [5366, "Acme Hardware Supply"]},
            {"id": 1534, "name": "POS.N/IN/00306", "origin": "P90001", "partner_id": [5366, "Acme Hardware Supply"]},
        ]
        with patched(self.fake):
            result = receiving.resolve_receipt(vendor_name="Acme Hardware")
        self.assertEqual(result["status"], "ambiguous")
        self.assertEqual({c["picking_id"] for c in result["candidates"]}, {645, 1534})

    def test_not_confirmed_when_po_exists_but_no_receipt(self):
        self.fake.purchase_orders = [{"id": 88, "name": "P90003", "partner_id": [5366, "Acme Hardware Supply"], "state": "draft"}]
        with patched(self.fake):
            result = receiving.resolve_receipt(po_reference="P90003")
        self.assertEqual(result["status"], "not_confirmed")
        self.assertEqual(result["pos"], [{"po_name": "P90003", "vendor_name": "Acme Hardware Supply"}])

    def test_not_confirmed_by_vendor_lookup(self):
        self.fake.purchase_orders = [{"id": 88, "name": "P90003", "partner_id": [9, "Some Vendor Inc"], "state": "sent"}]
        with patched(self.fake):
            result = receiving.resolve_receipt(vendor_name="Some Vendor")
        self.assertEqual(result["status"], "not_confirmed")

    def test_not_found_when_nothing_matches_at_all(self):
        with patched(self.fake):
            result = receiving.resolve_receipt(po_reference="P99999")
        self.assertEqual(result, {"status": "not_found", "query": "P99999"})

    def test_override_picking_id_bypasses_search(self):
        self.fake.receipts = [
            {"id": 1534, "name": "POS.N/IN/00306", "origin": "P90001", "partner_id": [5366, "Acme Hardware Supply"]},
        ]
        with patched(self.fake):
            result = receiving.resolve_receipt(override_picking_id=1534)
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["picking_id"], 1534)


class PreviewAndFillTests(unittest.TestCase):
    def _po00559_fixture(self):
        fake = FakeOdoo()
        fake.receipts = [
            {"id": 1534, "name": "POS.N/IN/00306", "origin": "P90001", "partner_id": [5366, "Acme Hardware Supply"]},
        ]
        for i in range(8):
            fake.add_line(10725 + i, 1534, 838, "Epson TM-M30III-UE Thermal Receipt Printer")
        return fake

    def test_real_world_six_of_eight_printers(self):
        fake = self._po00559_fixture()
        serials = "\n".join(["SN0001", "SN0002", "SN0003", "SN0004", "SN0005", "SN0006"])
        with patched(fake):
            report = receiving.fill_receiving_serials(serials, po_reference="P90001")

        self.assertEqual(report["status"], "filled")
        self.assertEqual(len(report["changes"]), 6)
        self.assertTrue(all(c["verified"] for c in report["changes"]))
        self.assertEqual(report["remaining_empty_lines"], 2)
        filled_lines = [l for l in fake.move_lines if l["lot_name"]]
        empty_lines = [l for l in fake.move_lines if not l["lot_name"]]
        self.assertEqual(len(filled_lines), 6)
        self.assertEqual(len(empty_lines), 2)
        self.assertEqual(len(fake.notes), 1)
        self.assertIn("SN0001", fake.notes[0][1])

    def test_preview_does_not_write(self):
        fake = self._po00559_fixture()
        with patched(fake):
            report = receiving.preview_receiving("SN0001\nSN0002", po_reference="P90001")
        self.assertEqual(report["status"], "ready")
        self.assertTrue(all(call[1] == "search_read" for call in fake.calls))
        self.assertEqual(fake.notes, [])

    def test_too_many_serials_writes_nothing(self):
        fake = FakeOdoo()
        fake.receipts = [{"id": 1, "name": "POS.N/IN/00001", "origin": "P00001", "partner_id": [1, "Vendor"]}]
        fake.add_line(1, 1, 1, "Widget")
        with patched(fake):
            report = receiving.fill_receiving_serials("SERIAL01\nSERIAL02", po_reference="P00001")
        self.assertEqual(report["status"], "too_many_serials")
        self.assertEqual([c for c in fake.calls if c[1] == "write"], [])

    def test_duplicate_serial_elsewhere_blocks_write(self):
        fake = self._po00559_fixture()
        fake.lots["SN0001"] = {"id": 1, "name": "SN0001"}
        with patched(fake):
            report = receiving.fill_receiving_serials("SN0001", po_reference="P90001")
        self.assertEqual(report["status"], "duplicate_serial")
        self.assertEqual(report["already_used_elsewhere"], ["SN0001"])
        self.assertEqual([c for c in fake.calls if c[1] == "write"], [])

    def test_serial_already_on_a_line_in_the_same_picking_is_not_a_duplicate(self):
        """Re-running with a serial already written to THIS receipt (e.g. a
        retry) must not be treated as a cross-picking duplicate error."""
        fake = self._po00559_fixture()
        fake.move_lines[0]["lot_name"] = "SN0001"  # already filled on this same receipt
        with patched(fake):
            report = receiving.preview_receiving("SN0003", po_reference="P90001")
        self.assertEqual(report["status"], "ready")

    def test_needs_product_hint_when_multiple_products_have_open_lines(self):
        fake = FakeOdoo()
        fake.receipts = [{"id": 1, "name": "POS.N/IN/00001", "origin": "P00001", "partner_id": [1, "Vendor"]}]
        fake.add_line(1, 1, 1, "Printer")
        fake.add_line(2, 1, 2, "Cash Drawer")
        with patched(fake):
            report = receiving.preview_receiving("SERIAL01", po_reference="P00001")
        self.assertEqual(report["status"], "needs_product_hint")
        self.assertEqual({c["product_name"] for c in report["candidates"]}, {"Printer", "Cash Drawer"})

    def test_product_hint_resolves_the_ambiguity(self):
        fake = FakeOdoo()
        fake.receipts = [{"id": 1, "name": "POS.N/IN/00001", "origin": "P00001", "partner_id": [1, "Vendor"]}]
        fake.add_line(1, 1, 1, "Printer")
        fake.add_line(2, 1, 2, "Cash Drawer")
        with patched(fake):
            report = receiving.preview_receiving("SERIAL01", po_reference="P00001", product_hint="printer")
        self.assertEqual(report["status"], "ready")
        self.assertEqual(report["product"]["product_name"], "Printer")

    def test_no_open_lines_when_everything_already_filled(self):
        fake = FakeOdoo()
        fake.receipts = [{"id": 1, "name": "POS.N/IN/00001", "origin": "P00001", "partner_id": [1, "Vendor"]}]
        fake.add_line(1, 1, 1, "Printer", lot_name="SN_OLD")
        with patched(fake):
            report = receiving.preview_receiving("SN_NEW", po_reference="P00001")
        self.assertEqual(report["status"], "no_open_lines")

    def test_journal_written_before_any_write_call(self):
        fake = self._po00559_fixture()
        with patched(fake):
            receiving.fill_receiving_serials("SN0001", po_reference="P90001")
        journal_text = receiving._JOURNAL_PATH.read_text(encoding="utf-8")
        self.assertIn("SN0001", journal_text)

    def test_write_payload_is_only_lot_name(self):
        fake = self._po00559_fixture()
        with patched(fake):
            receiving.fill_receiving_serials("SN0001", po_reference="P90001")
        write_calls = [c for c in fake.calls if c[0] == "stock.move.line" and c[1] == "write"]
        self.assertEqual(len(write_calls), 1)
        self.assertEqual(set(write_calls[0][2][1].keys()), {"lot_name"})

    def test_no_stock_lot_is_ever_created(self):
        fake = self._po00559_fixture()
        with patched(fake):
            receiving.fill_receiving_serials("SN0001", po_reference="P90001")
        self.assertFalse(any(c[0] == "stock.lot" and c[1] != "search_read" for c in fake.calls))

    def test_button_validate_never_attempted(self):
        fake = self._po00559_fixture()
        with patched(fake):
            receiving.fill_receiving_serials("SN0001", po_reference="P90001")
        self.assertFalse(any(c[1] == "button_validate" for c in fake.calls))

    def test_not_confirmed_writes_nothing(self):
        fake = FakeOdoo()
        fake.purchase_orders = [{"id": 1, "name": "P90003", "partner_id": [1, "Vendor"], "state": "draft"}]
        with patched(fake):
            report = receiving.fill_receiving_serials("SERIAL01", po_reference="P90003")
        self.assertEqual(report["resolution"]["status"], "not_confirmed")
        self.assertEqual([c for c in fake.calls if c[1] == "write"], [])

    def test_ambiguous_writes_nothing(self):
        fake = FakeOdoo()
        fake.receipts = [
            {"id": 1, "name": "POS.N/IN/00001", "origin": "P00001", "partner_id": [1, "Vendor"]},
            {"id": 2, "name": "POS.N/IN/00002", "origin": "P00002", "partner_id": [1, "Vendor"]},
        ]
        with patched(fake):
            report = receiving.fill_receiving_serials("SERIAL01", vendor_name="Vendor")
        self.assertEqual(report["resolution"]["status"], "ambiguous")
        self.assertEqual([c for c in fake.calls if c[1] == "write"], [])


class DispatchTests(unittest.TestCase):
    def test_dispatch_routes_both_operations(self):
        with patch.object(bridge.receiving, "preview_receiving", return_value={"resolution": {"status": "not_found", "query": ""}}) as preview, \
             patch.object(bridge.receiving, "fill_receiving_serials", return_value={"resolution": {"status": "not_found", "query": ""}}) as fill:
            out = bridge.dispatch("receiving_reconcile", {"raw_list": "SN1", "po_reference": "P1"}, {})
            self.assertIn("message", out)
            preview.assert_called_once()
            out = bridge.dispatch("receiving_fill_serials", {"raw_list": "SN1", "po_reference": "P1"}, {})
            self.assertIn("message", out)
            fill.assert_called_once()

    def test_requires_a_list(self):
        with self.assertRaisesRegex(ValueError, "raw_list or file_path"):
            bridge.dispatch("receiving_reconcile", {"po_reference": "P1"}, {})

    def test_rejects_both_inputs_at_once(self):
        with self.assertRaisesRegex(ValueError, "not both"):
            bridge.dispatch(
                "receiving_reconcile",
                {"raw_list": "SN1", "file_path": "/tmp/x.txt", "po_reference": "P1"},
                {},
            )


if __name__ == "__main__":
    unittest.main()
