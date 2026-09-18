"""Offline tests for ACME store delivery reconciliation and serial write-in.

All data here is synthetic (fake store names, fake serials) - none of it is
real inventory or a real customer record. `button_validate` never appearing
anywhere in this file, and staying unreachable through the whitelist, is the
single most important property under test.
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
deliveries = importlib.import_module("deliveries")
odoo_client = importlib.import_module("odoo_client")


class FakeOdoo:
    """Records every execute() call and answers only the query shapes
    deliveries.py actually sends, so an unexpected call shape fails loudly."""

    def __init__(self):
        self.partners: list[dict] = []
        self.lots: dict[str, dict] = {}
        self.quants: dict[int, list[dict]] = {}
        self.pickings: dict[int, list[dict]] = {}
        self.move_lines: list[dict] = []
        self.sale_orders: dict[int, list[dict]] = {}
        self.products: dict[int, str] = {}
        self.calls: list[tuple] = []
        self.notes: list[tuple] = []

    def add_lot(self, serial: str, lot_id: int, product_id: int, product_name: str,
                on_hand: list[tuple[str, float]]):
        self.lots[serial] = {"id": lot_id, "name": serial, "product_id": [product_id, product_name]}
        self.quants[lot_id] = [
            {"lot_id": [lot_id, serial], "location_id": [i, loc], "quantity": qty}
            for i, (loc, qty) in enumerate(on_hand, start=900)
        ]
        self.products[product_id] = product_name

    def add_line(self, line_id, picking_id, picking_name, product_id, product_name,
                 lot_serial, location):
        self.move_lines.append({
            "id": line_id,
            "picking_id": [picking_id, picking_name],
            "product_id": [product_id, product_name],
            "lot_id": [self.lots[lot_serial]["id"], lot_serial] if lot_serial else False,
            "quantity": 1,
            "location_id": [1, location],
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
                    if vals.get("lot_id"):
                        lid = vals["lot_id"]
                        name = next((s for s, l in self.lots.items() if l["id"] == lid), None)
                        line["lot_id"] = [lid, name]
                    else:
                        line["lot_id"] = False
            return True
        if model == "stock.picking" and method == "message_post":
            self.notes.append((args[0][0], kwargs.get("body", "")))
            return True
        raise AssertionError(f"Unexpected fake Odoo call: {model}.{method}")

    def _search_read(self, model, domain, kwargs):
        if model == "res.partner":
            if domain and domain[0][0] == "id":
                return [p for p in self.partners if p["id"] == domain[0][2]]
            return list(self.partners)
        if model == "stock.lot":
            names = domain[0][2]
            return [self.lots[n] for n in names if n in self.lots]
        if model == "stock.quant":
            lot_ids = domain[0][2]
            out = []
            for lid in lot_ids:
                out.extend(self.quants.get(lid, []))
            return out
        if model == "stock.picking":
            partner_id = domain[0][2]
            state_clause = domain[1]
            pickings = self.pickings.get(partner_id, [])
            if state_clause[1] == "not in":
                return [p for p in pickings if p["state"] not in state_clause[2]]
            return [p for p in pickings if p["state"] == state_clause[2]]
        if model == "stock.move.line":
            if domain[0][0] == "id":
                ids = domain[0][2]
                return [l for l in self.move_lines if l["id"] in ids]
            picking_ids = domain[0][2]
            return [l for l in self.move_lines if l["picking_id"][0] in picking_ids]
        if model == "sale.order":
            partner_id = domain[0][2]
            orders = self.sale_orders.get(partner_id, [])
            limit = kwargs.get("limit")
            return orders[:limit] if limit else orders
        if model == "product.product":
            ids = domain[0][2]
            return [{"id": i, "name": self.products[i]} for i in ids if i in self.products]
        raise AssertionError(f"Unexpected fake search_read model: {model}")


def patched(fake: FakeOdoo):
    return patch.object(deliveries.client, "execute", side_effect=fake.execute)


class WhitelistTests(unittest.TestCase):
    def test_new_models_are_pinned_exactly(self):
        self.assertEqual(odoo_client.WHITELIST["stock.picking"], {"search_read", "message_post"})
        self.assertEqual(odoo_client.WHITELIST["stock.move.line"], {"search_read", "write"})
        self.assertEqual(odoo_client.WHITELIST["stock.lot"], {"search_read"})
        self.assertEqual(odoo_client.WHITELIST["stock.quant"], {"search_read"})
        self.assertEqual(odoo_client.WHITELIST["sale.order"], {"search_read"})
        self.assertEqual(odoo_client.WHITELIST["product.product"], {"search_read"})

    def test_validate_is_never_whitelisted(self):
        with self.assertRaisesRegex(odoo_client.OdooError, "Blocked by whitelist"):
            odoo_client.client.execute("stock.picking", "button_validate", [[1]])

    def test_sale_order_is_read_only(self):
        for method in ("write", "create", "unlink"):
            with self.subTest(method=method):
                with self.assertRaisesRegex(odoo_client.OdooError, "Blocked by whitelist"):
                    odoo_client.client.execute("sale.order", method, [[1]])

    def test_product_product_is_read_only(self):
        for method in ("write", "create", "unlink"):
            with self.subTest(method=method):
                with self.assertRaisesRegex(odoo_client.OdooError, "Blocked by whitelist"):
                    odoo_client.client.execute("product.product", method, [[1]])

    def test_neighbouring_models_are_unreachable(self):
        for model in ("stock.move", "stock.picking.type", "account.move", "sale.order.line"):
            with self.subTest(model=model):
                with self.assertRaisesRegex(odoo_client.OdooError, "Blocked by whitelist"):
                    odoo_client.client.execute(model, "search_read", [[]])


class ParserTests(unittest.TestCase):
    def test_colon_format(self):
        out = deliveries.parse_store_list("Store 10: SN0001, SN0002\nStore 11: SN0003")
        self.assertEqual(out["assignments"], {
            "Store 10": ["SN0001", "SN0002"],
            "Store 11": ["SN0003"],
        })
        self.assertEqual(out["warnings"], [])

    def test_block_format(self):
        text = "Store 4\nSN0010\nSN0011\n\nStore 5\nSN0012"
        out = deliveries.parse_store_list(text)
        self.assertEqual(out["assignments"], {
            "Store 4": ["SN0010", "SN0011"],
            "Store 5": ["SN0012"],
        })

    def test_serials_are_normalized(self):
        out = deliveries.parse_store_list("Store 4: sn0010 ,  SN0011  ")
        self.assertEqual(out["assignments"]["Store 4"], ["SN0010", "SN0011"])

    def test_duplicate_serial_across_stores_is_flagged(self):
        out = deliveries.parse_store_list("Store 4: SN0010\nStore 5: SN0010")
        self.assertTrue(any("SN0010" in w for w in out["warnings"]))

    def test_duplicate_serial_within_one_store_is_not_repeated(self):
        out = deliveries.parse_store_list("Store 4: SN0010, SN0010")
        self.assertEqual(out["assignments"]["Store 4"], ["SN0010"])
        self.assertEqual(out["warnings"], [])


class StoreResolutionTests(unittest.TestCase):
    def setUp(self):
        deliveries._cache.clear()
        self.partners = [
            {"id": 1, "name": "Acme Retail Bell - Store 10"},
            {"id": 2, "name": "Acme Distributors LLC - TX (S28)"},
            {"id": 3, "name": "Acme Mini (Store 4)"},
            {"id": 4, "name": "Acme Express - Store 4"},
        ]

    def test_dash_store_number_format(self):
        with patch.object(deliveries, "_chain_partners", return_value=self.partners):
            result = deliveries.resolve_chain_store("Store 10")
        self.assertEqual(result, {"status": "matched", "partner_id": 1, "name": self.partners[0]["name"]})

    def test_parenthetical_s_number_format(self):
        with patch.object(deliveries, "_chain_partners", return_value=self.partners):
            result = deliveries.resolve_chain_store("S28")
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["partner_id"], 2)

    def test_bare_number_matches_store_number(self):
        with patch.object(deliveries, "_chain_partners", return_value=self.partners):
            result = deliveries.resolve_chain_store("28")
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["partner_id"], 2)

    def test_ambiguous_number_returns_candidates(self):
        with patch.object(deliveries, "_chain_partners", return_value=self.partners):
            result = deliveries.resolve_chain_store("Store 4")
        self.assertEqual(result["status"], "ambiguous")
        self.assertEqual({c["partner_id"] for c in result["candidates"]}, {3, 4})

    def test_unmatched_query_returns_not_found(self):
        with patch.object(deliveries, "_chain_partners", return_value=self.partners):
            result = deliveries.resolve_chain_store("Store 999")
        self.assertEqual(result, {"status": "not_found", "query": "Store 999"})


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        deliveries._cache.clear()

    def test_store_already_matches_nothing_to_write(self):
        fake = FakeOdoo()
        fake.partners = [{"id": 1, "name": "Acme Store 10"}]
        fake.add_lot("SN0001", lot_id=11, product_id=1, product_name="Widget A", on_hand=[("WH/Stock", 1)])
        fake.pickings[1] = [{"id": 500, "name": "WH/OUT/00500", "state": "assigned"}]
        fake.add_line(1, 500, "WH/OUT/00500", 1, "Widget A", "SN0001", "WH/Stock")

        with patched(fake):
            report = deliveries.preview_chain_delivery({"Store 10": ["SN0001"]})

        store = report["stores"][0]
        self.assertEqual(store["status"], "reconciled")
        self.assertEqual(store["changes_needed"], [])
        self.assertEqual(store["per_product"][0]["status"], "matches")
        # Only ever search_read: nothing here can write.
        self.assertTrue(all(call[1] == "search_read" for call in fake.calls))

    def test_store_needs_a_correction(self):
        fake = FakeOdoo()
        fake.partners = [{"id": 1, "name": "Acme Store 10"}]
        fake.add_lot("SN_OLD", lot_id=11, product_id=1, product_name="Widget A", on_hand=[("WH/Stock", 1)])
        fake.add_lot("SN_NEW", lot_id=12, product_id=1, product_name="Widget A", on_hand=[("WH/Stock", 1)])
        fake.pickings[1] = [{"id": 500, "name": "WH/OUT/00500", "state": "assigned"}]
        fake.add_line(1, 500, "WH/OUT/00500", 1, "Widget A", "SN_OLD", "WH/Stock")

        with patched(fake):
            report = deliveries.preview_chain_delivery({"Store 10": ["SN_NEW"]})

        store = report["stores"][0]
        self.assertEqual(store["per_product"][0]["status"], "fixable")
        self.assertEqual(len(store["changes_needed"]), 1)
        change = store["changes_needed"][0]
        self.assertEqual(change["from_serial"], "SN_OLD")
        self.assertEqual(change["to_serial"], "SN_NEW")
        self.assertEqual(change["to_lot_id"], 12)

    def test_count_mismatch_is_reported_not_touched(self):
        fake = FakeOdoo()
        fake.partners = [{"id": 1, "name": "Acme Store 10"}]
        fake.add_lot("SN1", lot_id=11, product_id=1, product_name="Widget A", on_hand=[("WH/Stock", 1)])
        fake.add_lot("SN2", lot_id=12, product_id=1, product_name="Widget A", on_hand=[("WH/Stock", 1)])
        fake.pickings[1] = [{"id": 500, "name": "WH/OUT/00500", "state": "assigned"}]
        fake.add_line(1, 500, "WH/OUT/00500", 1, "Widget A", "SN1", "WH/Stock")

        with patched(fake):
            report = deliveries.preview_chain_delivery({"Store 10": ["SN1", "SN2"]})

        store = report["stores"][0]
        self.assertEqual(store["per_product"][0]["status"], "count_mismatch")
        self.assertEqual(store["changes_needed"], [])

    def test_serial_held_outside_batch_is_reported_never_in_change_set(self):
        """A serial that resolves to a different product than the delivery's
        line is unresolved for this store, not force-fit into a change."""
        fake = FakeOdoo()
        fake.partners = [{"id": 1, "name": "Acme Store 10"}]
        fake.add_lot("SN_ELSEWHERE", lot_id=99, product_id=2, product_name="Widget B", on_hand=[("WH/Stock", 1)])
        fake.add_lot("SN_HELD", lot_id=11, product_id=1, product_name="Widget A", on_hand=[("WH/Stock", 1)])
        fake.pickings[1] = [{"id": 500, "name": "WH/OUT/00500", "state": "assigned"}]
        fake.add_line(1, 500, "WH/OUT/00500", 1, "Widget A", "SN_HELD", "WH/Stock")

        with patched(fake):
            report = deliveries.preview_chain_delivery({"Store 10": ["SN_ELSEWHERE"]})

        store = report["stores"][0]
        all_changes = store["changes_needed"]
        self.assertEqual(all_changes, [])
        statuses = [p["status"] for p in store["per_product"]]
        self.assertIn("count_mismatch", statuses)

    def test_location_mismatch_is_reported_not_written(self):
        fake = FakeOdoo()
        fake.partners = [{"id": 1, "name": "Acme Store 10"}]
        fake.add_lot("SN_OLD", lot_id=11, product_id=1, product_name="Widget A", on_hand=[("WH/Stock", 1)])
        # The expected serial exists and is on hand, but at a different
        # location than this delivery line draws from - the anomaly the plan
        # flagged should surface here, not get silently written.
        fake.add_lot("SN_WRONG_LOC", lot_id=12, product_id=1, product_name="Widget A",
                     on_hand=[("Partners/Customers", 1)])
        fake.pickings[1] = [{"id": 500, "name": "WH/OUT/00500", "state": "assigned"}]
        fake.add_line(1, 500, "WH/OUT/00500", 1, "Widget A", "SN_OLD", "WH/Stock")

        with patched(fake):
            report = deliveries.preview_chain_delivery({"Store 10": ["SN_WRONG_LOC"]})

        store = report["stores"][0]
        self.assertEqual(store["per_product"][0]["status"], "location_mismatch")
        self.assertEqual(store["changes_needed"], [])

    def test_already_delivered_matches_list(self):
        fake = FakeOdoo()
        fake.partners = [{"id": 1, "name": "Acme Store 10"}]
        fake.add_lot("SN1", lot_id=11, product_id=1, product_name="Widget A", on_hand=[])
        fake.pickings[1] = []
        fake.move_lines.append({
            "id": 1, "picking_id": [777, "WH/OUT/00777"], "product_id": [1, "Widget A"],
            "lot_id": [11, "SN1"], "quantity": 1, "location_id": [1, "WH/Stock"],
        })

        class DoneOdoo(FakeOdoo):
            def _search_read(self, model, domain, kwargs):
                if model == "stock.picking" and domain[1][1] == "=":
                    return [{"id": 777, "name": "WH/OUT/00777", "state": "done"}]
                return super()._search_read(model, domain, kwargs)

        done_fake = DoneOdoo()
        done_fake.__dict__.update(fake.__dict__)

        with patched(done_fake):
            report = deliveries.preview_chain_delivery({"Store 10": ["SN1"]})

        store = report["stores"][0]
        self.assertEqual(store["status"], "already_delivered")
        self.assertTrue(store["list_matches_what_shipped"])

    def test_non_serial_tracked_lines_are_ignored(self):
        """A pending picking can mix a serial-tracked hardware line with a
        service line (monthly hosting, license fee) that never carries a
        lot_id. The service line must never show up as a mismatch just
        because Matthew's list doesn't mention it."""
        fake = FakeOdoo()
        fake.partners = [{"id": 1, "name": "Acme Store 10"}]
        fake.add_lot("SN0001", lot_id=11, product_id=1, product_name="Widget A", on_hand=[("WH/Stock", 1)])
        fake.products[2] = "Monthly Hosting"
        fake.pickings[1] = [{"id": 500, "name": "WH/OUT/00500", "state": "assigned"}]
        fake.add_line(1, 500, "WH/OUT/00500", 1, "Widget A", "SN0001", "WH/Stock")
        fake.move_lines.append({
            "id": 2, "picking_id": [500, "WH/OUT/00500"], "product_id": [2, "Monthly Hosting"],
            "lot_id": False, "quantity": 1, "location_id": [1, "WH/Stock"],
        })

        with patched(fake):
            report = deliveries.preview_chain_delivery({"Store 10": ["SN0001"]})

        store = report["stores"][0]
        products_seen = {p["product"] for p in store["per_product"]}
        self.assertNotIn("Monthly Hosting", products_seen)
        self.assertEqual(store["per_product"], [{
            "product": "Widget A",
            "expected": ["SN0001"],
            "currently_on_delivery": ["SN0001"],
            "status": "matches",
        }])

    def test_no_delivery_reports_sale_order_state(self):
        fake = FakeOdoo()
        fake.partners = [{"id": 1, "name": "Acme Store 10"}]
        fake.pickings[1] = []
        fake.sale_orders[1] = [{"id": 55, "state": "draft"}]

        with patched(fake):
            report = deliveries.preview_chain_delivery({"Store 10": ["SN1"]})

        store = report["stores"][0]
        self.assertEqual(store["status"], "no_delivery")
        self.assertEqual(store["sale_order_state"], "draft")

    def test_unresolvable_store_label_is_reported(self):
        fake = FakeOdoo()
        fake.partners = []
        with patched(fake):
            report = deliveries.preview_chain_delivery({"Store 999": ["SN1"]})
        store = report["stores"][0]
        self.assertEqual(store["resolution"]["status"], "not_found")


class FillTests(unittest.TestCase):
    def setUp(self):
        deliveries._cache.clear()

    def _cross_store_fixture(self):
        """Two stores; the serial each one needs currently sits on the
        other's delivery. This is the scenario the whole-batch design exists
        for - a per-store tool cannot resolve it."""
        fake = FakeOdoo()
        fake.partners = [
            {"id": 1, "name": "Acme Store 10"},
            {"id": 2, "name": "Acme Store 11"},
        ]
        fake.add_lot("SN_A", lot_id=11, product_id=1, product_name="Widget A", on_hand=[("WH/Stock", 1)])
        fake.add_lot("SN_B", lot_id=12, product_id=1, product_name="Widget A", on_hand=[("WH/Stock", 1)])
        fake.pickings[1] = [{"id": 500, "name": "WH/OUT/00500", "state": "assigned"}]
        fake.pickings[2] = [{"id": 600, "name": "WH/OUT/00600", "state": "assigned"}]
        fake.add_line(1, 500, "WH/OUT/00500", 1, "Widget A", "SN_B", "WH/Stock")  # wrong: store 10 has B
        fake.add_line(2, 600, "WH/OUT/00600", 1, "Widget A", "SN_A", "WH/Stock")  # wrong: store 11 has A
        return fake

    def test_cross_store_swap_is_release_then_assign(self):
        fake = self._cross_store_fixture()
        assignments = {"Store 10": ["SN_A"], "Store 11": ["SN_B"]}

        with patched(fake):
            report = deliveries.fill_chain_delivery_serials(assignments)

        write_calls = [c for c in fake.calls if c[0] == "stock.move.line" and c[1] == "write"]
        self.assertEqual(len(write_calls), 4)  # 2 releases + 2 assigns
        release_calls = write_calls[:2]
        assign_calls = write_calls[2:]
        for call in release_calls:
            self.assertEqual(call[2][1], {"lot_id": False})
        for call in assign_calls:
            self.assertEqual(set(call[2][1].keys()), {"lot_id"})
            self.assertTrue(call[2][1]["lot_id"])

        for store in report["stores"]:
            self.assertEqual(store["status"], "filled")
            for change in store["changes_written"]:
                self.assertTrue(change["verified"])

    def test_payload_is_only_lot_id(self):
        fake = self._cross_store_fixture()
        with patched(fake):
            deliveries.fill_chain_delivery_serials({"Store 10": ["SN_A"], "Store 11": ["SN_B"]})
        for call in fake.calls:
            if call[0] == "stock.move.line" and call[1] == "write":
                self.assertEqual(set(call[2][1].keys()), {"lot_id"})

    def test_count_mismatch_store_is_not_written(self):
        fake = FakeOdoo()
        fake.partners = [{"id": 1, "name": "Acme Store 10"}]
        fake.add_lot("SN1", lot_id=11, product_id=1, product_name="Widget A", on_hand=[("WH/Stock", 1)])
        fake.add_lot("SN2", lot_id=12, product_id=1, product_name="Widget A", on_hand=[("WH/Stock", 1)])
        fake.pickings[1] = [{"id": 500, "name": "WH/OUT/00500", "state": "assigned"}]
        fake.add_line(1, 500, "WH/OUT/00500", 1, "Widget A", "SN1", "WH/Stock")

        with patched(fake):
            report = deliveries.fill_chain_delivery_serials({"Store 10": ["SN1", "SN2"]})

        self.assertEqual(len([c for c in fake.calls if c[1] == "write"]), 0)
        self.assertEqual(report["stores"][0].get("status"), "reconciled")

    def test_journal_is_written_before_any_write_call(self):
        fake = self._cross_store_fixture()
        with patched(fake):
            deliveries.fill_chain_delivery_serials({"Store 10": ["SN_A"], "Store 11": ["SN_B"]})
        journal_text = deliveries._JOURNAL_PATH.read_text(encoding="utf-8")
        self.assertIn("SN_A", journal_text)
        self.assertIn("SN_B", journal_text)

    def test_one_note_per_touched_picking(self):
        fake = self._cross_store_fixture()
        with patched(fake):
            deliveries.fill_chain_delivery_serials({"Store 10": ["SN_A"], "Store 11": ["SN_B"]})
        self.assertEqual(len(fake.notes), 2)
        picking_ids = {n[0] for n in fake.notes}
        self.assertEqual(picking_ids, {500, 600})

    def test_idempotent_rerun_is_a_no_op(self):
        fake = self._cross_store_fixture()
        assignments = {"Store 10": ["SN_A"], "Store 11": ["SN_B"]}
        with patched(fake):
            deliveries.fill_chain_delivery_serials(assignments)
            fake.notes.clear()
            journal_len_before = len(deliveries._JOURNAL_PATH.read_text(encoding="utf-8").splitlines())
            second = deliveries.fill_chain_delivery_serials(assignments)
        self.assertEqual(fake.notes, [])
        journal_len_after = len(deliveries._JOURNAL_PATH.read_text(encoding="utf-8").splitlines())
        self.assertEqual(journal_len_before, journal_len_after)
        for store in second["stores"]:
            self.assertNotEqual(store.get("status"), "filled")

    def test_read_back_failure_is_reported_not_hidden(self):
        fake = self._cross_store_fixture()

        class FlakyOdoo(FakeOdoo):
            def execute(self, model, method, args, kwargs=None):
                if model == "stock.move.line" and method == "write" and args[1].get("lot_id") == 12:
                    return True  # pretend the write silently failed to apply
                return super().execute(model, method, args, kwargs)

        flaky = FlakyOdoo()
        flaky.__dict__.update(fake.__dict__)

        with patched(flaky):
            report = deliveries.fill_chain_delivery_serials({"Store 10": ["SN_A"], "Store 11": ["SN_B"]})

        failures = [
            c for store in report["stores"]
            for c in store.get("changes_written", [])
            if not c["verified"]
        ]
        self.assertTrue(failures)

    def test_button_validate_never_attempted(self):
        fake = self._cross_store_fixture()
        with patched(fake):
            deliveries.fill_chain_delivery_serials({"Store 10": ["SN_A"], "Store 11": ["SN_B"]})
        self.assertFalse(any(call[1] == "button_validate" for call in fake.calls))


class DispatchTests(unittest.TestCase):
    def test_dispatch_routes_reconcile_and_fill(self):
        with patch.object(bridge.deliveries, "preview_chain_delivery", return_value={"stores": []}) as preview, \
             patch.object(bridge.deliveries, "fill_chain_delivery_serials", return_value={"stores": []}) as fill, \
             patch.object(bridge.deliveries, "parse_store_list",
                          return_value={"assignments": {"Store 1": ["SN1"]}, "warnings": []}):
            out = bridge.dispatch("delivery_reconcile", {"raw_list": "Store 1: SN1"}, {})
            self.assertIn("message", out)
            preview.assert_called_once()
            out = bridge.dispatch("delivery_fill_serials", {"raw_list": "Store 1: SN1"}, {})
            self.assertIn("message", out)
            fill.assert_called_once()

    def test_reconcile_requires_a_list(self):
        with self.assertRaisesRegex(ValueError, "raw_list or file_path"):
            bridge.dispatch("delivery_reconcile", {}, {})

    def test_reconcile_rejects_both_inputs_at_once(self):
        with self.assertRaisesRegex(ValueError, "not both"):
            bridge.dispatch(
                "delivery_reconcile",
                {"raw_list": "Store 1: SN1", "file_path": "/tmp/x.txt"},
                {},
            )


if __name__ == "__main__":
    unittest.main()
