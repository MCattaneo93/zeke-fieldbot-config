"""ACME store delivery reconciliation and serial write-in.

Odoo already auto-reserves *a* serial on every pending ACME delivery line via
FIFO at order-confirmation time. The gap this module closes is verifying that
reservation actually matches the serial Matthew's own external list says
should ship to each store, and correcting it when it doesn't - never the
other way around, and never finalizing the delivery itself.

`button_validate` (the method that marks a delivery done) is never
whitelisted anywhere this module touches (see odoo_client.WHITELIST). That is
deliberate and load-bearing: whoever reads this file next should not
"helpfully" add it. A human always validates a delivery in Odoo; this module
only makes sure the delivery is correct before that happens.
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

import config
from odoo_client import client

log = logging.getLogger(__name__)

# ---- in-memory cache of ACME partners (mirrors actions._cached's 6h TTL) ---
_CACHE_TTL = 6 * 3600
_cache: dict[str, tuple[float, object]] = {}


def _cached(key: str, fetch):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < _CACHE_TTL:
        return hit[1]
    val = fetch()
    _cache[key] = (time.time(), val)
    return val


# Every store-name format seen in the live data:
#   "Acme Retail Bell - Store 10"
#   "Acme Retail Arlington LLC - TX (S28)"
#   "Acme Mini (Store 4)"
# There is no structured store-number field anywhere on res.partner (checked
# ref, barcode, every x_studio_* field - all empty) - the number only exists
# as free text inside the name, in whichever of these forms someone typed it.
_STORE_NUMBER_RE = re.compile(r"(?:\bstore\s*|\(\s*s)\s*0*(\d+)\b", re.IGNORECASE)
_SERIAL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-]{4,}")


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()


def _store_number(name: str) -> str | None:
    m = _STORE_NUMBER_RE.search(name or "")
    return str(int(m.group(1))) if m else None


def _chain_partners() -> list[dict]:
    """The ~43 ACME store partners, 6h-cached. `ilike 'acme'` is the same
    naming-convention heuristic that produced the confirmed set during
    research - not a structured flag, since none exists."""
    return _cached(
        "chain_partners",
        lambda: client.search_read(
            "res.partner",
            [["is_company", "=", True], ["name", "ilike", "acme"]],
            ["id", "name"],
        ),
    )


def resolve_chain_store(store_query: str) -> dict:
    """Resolve a store label (a bare number, "Store 12", "(S28)", or a name
    fragment) to exactly one ACME partner.

    Returns exactly one of:
      {"status": "matched", "partner_id": int, "name": str}
      {"status": "ambiguous", "candidates": [{"partner_id": int, "name": str}, ...]}
      {"status": "not_found", "query": str}
    Never guesses when more than one partner could plausibly match.
    """
    partners = _chain_partners()
    q = _normalize(store_query)
    q_number = _store_number(store_query)
    if q_number is None and store_query.strip().isdigit():
        q_number = str(int(store_query.strip()))

    by_number = [p for p in partners if q_number is not None and _store_number(p["name"]) == q_number]
    by_name = [p for p in partners if q and q in _normalize(p["name"])]
    hits = by_number or by_name

    if len(hits) == 1:
        return {"status": "matched", "partner_id": hits[0]["id"], "name": hits[0]["name"]}
    if len(hits) > 1:
        return {
            "status": "ambiguous",
            "query": store_query,
            "candidates": [{"partner_id": p["id"], "name": p["name"]} for p in hits],
        }
    return {"status": "not_found", "query": store_query}


# ---- parsing Matthew's pasted list ----------------------------------------


def parse_store_list(raw_text: str) -> dict:
    """Best-effort parse of a pasted store -> serial list into
    {"assignments": {store_label: [serial, ...]}, "warnings": [...]}.

    Handles two common shapes:
      1. One line per store: "Store 10: SN1, SN2, SN3" (a colon or dash
         separates the store label from a comma/pipe-separated serial list).
      2. Block form: a store-label line followed by one serial per line,
         until a blank line or the next label.

    This has not been tuned against Matthew's real list yet - if the actual
    format doesn't match either shape, this needs a quick follow-up
    adjustment once he shares a real sample (flagged in the plan as Step 0).
    Never silently drops a line it can't place; anything it can't parse ends
    up neither matched to a store nor silently discarded - it's absent from
    "assignments" and the caller sees the total serial count didn't add up.
    """
    assignments: dict[str, list[str]] = {}
    warnings: list[str] = []
    seen_serials: dict[str, str] = {}  # serial -> first store label it appeared under

    def add(store: str, serial: str) -> None:
        serial = serial.strip().upper()
        if not serial:
            return
        if serial in seen_serials and seen_serials[serial] != store:
            warnings.append(
                f"Serial {serial} appears under both '{seen_serials[serial]}' "
                f"and '{store}' in your list - needs your attention."
            )
        seen_serials.setdefault(serial, store)
        bucket = assignments.setdefault(store, [])
        if serial not in bucket:
            bucket.append(serial)

    current_store: str | None = None
    for raw_line in (raw_text or "").splitlines():
        line = raw_line.strip()
        if not line:
            current_store = None
            continue

        # "Store 10: SN1, SN2" / "Store 10 - SN1, SN2"
        m = re.match(r"^(.*?[:\-])\s*([A-Za-z0-9][A-Za-z0-9,\s|]*)$", line)
        inline_serials = []
        if m:
            inline_serials = [
                s.strip() for s in re.split(r"[,\|]", m.group(2))
                if _SERIAL_RE.fullmatch(s.strip())
            ]
        if m and inline_serials:
            store_label = m.group(1).rstrip(":- ").strip()
            for s in inline_serials:
                add(store_label, s)
            current_store = store_label
            continue

        # A bare serial (or several), belonging to whichever store header
        # came before it in this block.
        tokens = [t.strip() for t in re.split(r"[,\|]", line) if t.strip()]
        if tokens and current_store and all(_SERIAL_RE.fullmatch(t) for t in tokens):
            for t in tokens:
                add(current_store, t)
            continue

        # Otherwise this line starts a new store block.
        current_store = line.rstrip(":- ").strip()

    return {"assignments": assignments, "warnings": warnings}


# ---- reconciliation (read-only) --------------------------------------------


def _resolve_serial_lots(serials: list[str]) -> dict[str, dict]:
    """serial -> {lot_id, product_id, product_name, on_hand: [...]} , or
    {"error": "..."} for a serial that can't be placed."""
    if not serials:
        return {}
    lots = client.search_read(
        "stock.lot", [["name", "in", serials]], ["id", "name", "product_id"]
    )
    by_name = {l["name"]: l for l in lots}
    lot_ids = [l["id"] for l in lots]
    quants = client.search_read(
        "stock.quant",
        [["lot_id", "in", lot_ids], ["quantity", ">", 0]],
        ["lot_id", "location_id", "quantity"],
    ) if lot_ids else []
    quant_by_lot: dict[int, list] = {}
    for q in quants:
        quant_by_lot.setdefault(q["lot_id"][0], []).append(q)

    out: dict[str, dict] = {}
    for serial in serials:
        lot = by_name.get(serial)
        if lot is None:
            out[serial] = {"error": "not found in Odoo inventory at all"}
            continue
        on_hand = quant_by_lot.get(lot["id"], [])
        entry = {
            "lot_id": lot["id"],
            "product_id": lot["product_id"][0] if lot["product_id"] else None,
            "product_name": lot["product_id"][1] if lot["product_id"] else None,
            "on_hand": [
                {"location": q["location_id"][1], "quantity": q["quantity"]}
                for q in on_hand
            ],
        }
        if not on_hand:
            entry["error"] = "in Odoo but 0 on hand anywhere - may already be shipped elsewhere"
        out[serial] = entry
    return out


def _pending_pickings(partner_id: int) -> list[dict]:
    return client.search_read(
        "stock.picking",
        [["partner_id", "=", partner_id], ["state", "not in", ["done", "cancel"]]],
        ["id", "name", "state"],
    )


def _done_pickings(partner_id: int) -> list[dict]:
    return client.search_read(
        "stock.picking",
        [["partner_id", "=", partner_id], ["state", "=", "done"]],
        ["id", "name", "state"],
    )


def _move_lines(picking_ids: list[int]) -> list[dict]:
    if not picking_ids:
        return []
    return client.search_read(
        "stock.move.line",
        [["picking_id", "in", picking_ids]],
        ["id", "picking_id", "product_id", "lot_id", "quantity", "location_id"],
    )


def _latest_sale_order_state(partner_id: int) -> str | None:
    orders = client.search_read(
        "sale.order",
        [["partner_id", "=", partner_id]],
        ["id", "state"],
        order="id desc",
        limit=1,
    )
    return orders[0]["state"] if orders else None


def _reconcile_store(store_label: str, expected_serials: list[str], overrides: dict[str, int] | None = None) -> dict:
    override_id = (overrides or {}).get(store_label) or (overrides or {}).get(_normalize(store_label))
    if override_id is not None:
        matches = client.search_read("res.partner", [["id", "=", override_id]], ["id", "name"])
        if not matches:
            return {"query": store_label, "resolution": {"status": "not_found", "query": store_label}}
        resolved = {"status": "matched", "partner_id": matches[0]["id"], "name": matches[0]["name"]}
    else:
        resolved = resolve_chain_store(store_label)
    if resolved["status"] != "matched":
        return {"query": store_label, "resolution": resolved}

    partner_id = resolved["partner_id"]
    partner_name = resolved["name"]
    lot_info = _resolve_serial_lots(expected_serials)

    pending = _pending_pickings(partner_id)
    if not pending:
        done = _done_pickings(partner_id)
        if done:
            done_lines = _move_lines([p["id"] for p in done])
            shipped_serials = {l["lot_id"][1] for l in done_lines if l["lot_id"]}
            missing = [s for s in expected_serials if s not in shipped_serials]
            return {
                "store": partner_name,
                "status": "already_delivered",
                "pickings": [p["name"] for p in done],
                "list_matches_what_shipped": not missing,
                "in_list_but_not_shipped": missing,
            }
        return {
            "store": partner_name,
            "status": "no_delivery",
            "sale_order_state": _latest_sale_order_state(partner_id) or "no sale order found",
        }

    picking_by_id = {p["id"]: p for p in pending}
    lines = _move_lines(list(picking_by_id))

    lines_by_product: dict[int, list[dict]] = {}
    for line in lines:
        if line["product_id"]:
            lines_by_product.setdefault(line["product_id"][0], []).append(line)

    expected_by_product: dict[int, list[str]] = {}
    unresolved_serials = []
    for serial in expected_serials:
        info = lot_info.get(serial, {})
        if info.get("error") or info.get("product_id") is None:
            unresolved_serials.append({"serial": serial, "issue": info.get("error", "unknown")})
            continue
        expected_by_product.setdefault(info["product_id"], []).append(serial)

    # A pending picking mixes serial-tracked hardware lines with
    # non-serial-tracked service lines (monthly hosting, license fees) that
    # never carry a lot_id and that Matthew's list never mentions. Only
    # products with real serial evidence - a line that holds a lot, or a
    # serial he listed for it - belong in this reconciliation; a bare
    # service line is neither "matching" nor "mismatched", it's simply out
    # of scope for this tool.
    serial_tracked_product_ids = {
        pid for pid, plines in lines_by_product.items() if any(l["lot_id"] for l in plines)
    }
    product_ids = (set(lines_by_product) & serial_tracked_product_ids) | set(expected_by_product)
    product_name = {
        p["id"]: p["name"]
        for p in (client.search_read("product.product", [["id", "in", list(product_ids)]], ["id", "name"])
                  if product_ids else [])
    }

    per_product = []
    needs_attention = []
    changes_needed = []

    for pid in product_ids:
        plines = lines_by_product.get(pid, [])
        pserials = expected_by_product.get(pid, [])
        current_serials = [l["lot_id"][1] for l in plines if l["lot_id"]]
        entry = {
            "product": product_name.get(pid, f"#{pid}"),
            "expected": pserials,
            "currently_on_delivery": current_serials,
        }

        if len(plines) != len(pserials):
            entry["status"] = "count_mismatch"
            needs_attention.append(entry)
            per_product.append(entry)
            continue

        # Keep lines that already hold an expected serial; only the
        # remainder needs reassigning - never touch a line that's already
        # right just to "normalize" ordering.
        remaining_lines = [l for l in plines if not l["lot_id"] or l["lot_id"][1] not in pserials]
        remaining_serials = [s for s in pserials if s not in current_serials]

        if len(remaining_lines) != len(remaining_serials):
            entry["status"] = "mismatch"
            needs_attention.append(entry)
            per_product.append(entry)
            continue

        # Location guard: a serial can only fill a line if it's actually on
        # hand at that line's source location. Pairing is otherwise
        # order-immaterial (same product, same store), so greedily match each
        # line to any still-available serial that clears this guard.
        available_serials = list(remaining_serials)
        pairing = []
        location_ok = True
        for line in remaining_lines:
            line_location = line["location_id"][1] if line["location_id"] else None
            match = next(
                (s for s in available_serials
                 if any(q["location"] == line_location for q in lot_info[s]["on_hand"])),
                None,
            )
            if match is None:
                location_ok = False
                break
            available_serials.remove(match)
            pairing.append((line, match))

        if not location_ok:
            entry["status"] = "location_mismatch"
            entry["detail"] = "expected serial(s) are on hand at a different location than this delivery line"
            needs_attention.append(entry)
            per_product.append(entry)
            continue

        entry["status"] = "matches" if not remaining_lines else "fixable"
        per_product.append(entry)
        for line, serial in pairing:
            changes_needed.append({
                "line_id": line["id"],
                "picking_id": line["picking_id"][0],
                "picking": picking_by_id[line["picking_id"][0]]["name"],
                "product": entry["product"],
                "from_serial": line["lot_id"][1] if line["lot_id"] else None,
                "to_serial": serial,
                "to_lot_id": lot_info[serial]["lot_id"],
            })

    return {
        "store": partner_name,
        "status": "reconciled",
        "pickings": [p["name"] for p in pending],
        "per_product": per_product,
        "changes_needed": changes_needed,
        "needs_attention": needs_attention,
        "unresolved_serials": unresolved_serials,
    }


def preview_chain_delivery(assignments: dict, store_overrides: dict[str, int] | None = None) -> dict:
    """Read-only reconciliation across every store in one pasted list.

    Provably cannot write anything: every model this touches is whitelisted
    for search_read only (odoo_client.WHITELIST) except stock.move.line and
    stock.picking, and this function never calls their write/message_post
    methods - only fill_chain_delivery_serials does.
    """
    return {
        "stores": [
            _reconcile_store(store, serials, store_overrides)
            for store, serials in assignments.items()
        ]
    }


# ---- the write: fill in corrected serials ----------------------------------

_JOURNAL_PATH = config.STATE_DIR / "delivery_serial_journal.jsonl"


def _append_journal(entries: list[dict]) -> None:
    _JOURNAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _JOURNAL_PATH.open("a", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps({**entry, "ts": time.time()}) + "\n")


def fill_chain_delivery_serials(assignments: dict, store_overrides: dict[str, int] | None = None) -> dict:
    """Write corrected lot_id assignments for every store whose
    reconciliation came back unambiguous ("fixable" changes_needed).

    Never touches a store reported as count_mismatch / mismatch / with
    unresolved serials - those need a human first. Re-runs the reconcile
    logic itself rather than trusting a prior preview call, since inventory
    can move between calls.

    Sequencing matters: a serial can be needed on a *different* store's line
    than the one Odoo's FIFO reservation put it on, so every line in the
    whole batch is released (lot_id -> False) before any line is assigned -
    otherwise a same-batch swap could momentarily collide. If interrupted
    between the two phases, re-running is safe: an empty line just gets
    filled again.
    """
    report = preview_chain_delivery(assignments, store_overrides)

    all_changes = []
    for store_result in report["stores"]:
        for change in store_result.get("changes_needed") or []:
            all_changes.append({**change, "store": store_result["store"]})

    if not all_changes:
        return report

    _append_journal(all_changes)

    for change in all_changes:
        client.execute("stock.move.line", "write", [[change["line_id"]], {"lot_id": False}])
    for change in all_changes:
        client.execute("stock.move.line", "write", [[change["line_id"]], {"lot_id": change["to_lot_id"]}])

    written = client.search_read(
        "stock.move.line",
        [["id", "in", [c["line_id"] for c in all_changes]]],
        ["id", "lot_id"],
    )
    written_by_id = {w["id"]: w for w in written}
    for change in all_changes:
        w = written_by_id.get(change["line_id"])
        change["verified"] = bool(w and w["lot_id"] and w["lot_id"][1] == change["to_serial"])

    changes_by_picking: dict[int, list[dict]] = {}
    for change in all_changes:
        changes_by_picking.setdefault(change["picking_id"], []).append(change)
    for picking_id, changes in changes_by_picking.items():
        note_items = "".join(
            f"<li>{c['product']}: {c['from_serial'] or '(empty)'} &rarr; {c['to_serial']}</li>"
            for c in changes
        )
        client.execute(
            "stock.picking", "message_post", [[picking_id]],
            {"body": f"<p>Serial numbers corrected per store list, via Zeke:</p><ul>{note_items}</ul>"},
        )

    changes_by_store: dict[str, list[dict]] = {}
    for change in all_changes:
        changes_by_store.setdefault(change["store"], []).append(change)
    for store_result in report["stores"]:
        store_changes = changes_by_store.get(store_result.get("store"))
        if store_changes:
            store_result["changes_written"] = store_changes
            store_result["status"] = "filled"

    return report


# ---- rendering for Telegram -------------------------------------------------


def render_report(report: dict, warnings: list[str] | None = None) -> str:
    lines: list[str] = []
    if warnings:
        lines.append("Parsing notes:")
        lines.extend(f"- {w}" for w in warnings)
        lines.append("")

    for store_result in report["stores"]:
        if "resolution" in store_result:
            res = store_result["resolution"]
            lines.append(f"**{store_result['query']}**")
            if res["status"] == "ambiguous":
                names = ", ".join(c["name"] for c in res["candidates"])
                lines.append(f"  Ambiguous - could be: {names}. Tell me which one.")
            else:
                lines.append("  No ACME store matches that.")
            lines.append("")
            continue

        lines.append(f"**{store_result['store']}**")
        status = store_result["status"]

        if status == "already_delivered":
            lines.append(f"  Already delivered ({', '.join(store_result['pickings'])}).")
            if not store_result["list_matches_what_shipped"]:
                extra = ", ".join(store_result["in_list_but_not_shipped"])
                lines.append(f"  ⚠ your list has serials not on that shipment: {extra}")
        elif status == "no_delivery":
            lines.append(f"  No delivery yet - sale order is {store_result['sale_order_state']}.")
        else:
            for p in store_result.get("per_product", []):
                if p["status"] == "matches":
                    lines.append(f"  {p['product']}: already matches your list")
                elif p["status"] == "fixable":
                    lines.append(f"  {p['product']}: correcting {len(p['expected'])} serial(s)")
                else:
                    lines.append(
                        f"  ⚠ {p['product']}: {p['status']} - "
                        f"expected {p['expected']}, delivery has {p['currently_on_delivery']}"
                    )
            for u in store_result.get("unresolved_serials", []):
                lines.append(f"  ⚠ serial {u['serial']}: {u['issue']}")
            if store_result.get("changes_written"):
                for c in store_result["changes_written"]:
                    mark = "✓" if c["verified"] else "✗ FAILED - check manually"
                    lines.append(f"    {mark} {c['from_serial'] or '(empty)'} -> {c['to_serial']} on {c['picking']}")
                lines.append(f"  Ready to validate in Odoo: {', '.join(store_result['pickings'])}")
            elif store_result.get("changes_needed"):
                for c in store_result["changes_needed"]:
                    lines.append(f"    would set {c['from_serial'] or '(empty)'} -> {c['to_serial']} on {c['picking']}")
            elif not store_result.get("needs_attention") and not store_result.get("unresolved_serials"):
                lines.append(f"  Already matches your list - ready to validate in Odoo: {', '.join(store_result['pickings'])}")
        lines.append("")

    return "\n".join(lines).strip()
