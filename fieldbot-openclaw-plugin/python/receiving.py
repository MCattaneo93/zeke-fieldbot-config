"""Vendor delivery-slip serial entry for purchase-order receipts.

The other half of the pipeline from deliveries.py: serials arrive on a
vendor's packing slip when hardware is RECEIVED (a purchase order's
incoming stock.picking), not assigned by Matthew to a store. These serials
are brand new to Odoo - no stock.lot exists for them yet.

Writing `lot_name` (a plain text field), never `lot_id`, is what makes
"save without creating inventory or validating" true: Odoo only turns
`lot_name` into a real stock.lot record when a human validates the
receipt. `button_validate` is never whitelisted here, for the same reason
it never is in deliveries.py - a human always validates a receipt in Odoo.
"""
from __future__ import annotations

import json
import logging
import re
import time

import config
from odoo_client import client

log = logging.getLogger(__name__)

_SERIAL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-]{4,}")


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()


def _normalize_po_ref(text: str) -> str:
    """"PO00559" / "P00559" / "00559" / "559" all name the same order -
    compare by digits only, zeros stripped."""
    digits = re.sub(r"[^0-9]", "", text or "")
    return digits.lstrip("0") or digits


# ---- parsing the vendor's serial list ---------------------------------


def parse_serial_list(raw_text: str) -> dict:
    """One serial per line, or comma/pipe separated. Normalizes case,
    drops exact duplicates, and reports (never silently drops) anything
    that doesn't look like a serial - the same "never retype/guess" rule
    as deliveries.py's parser."""
    serials: list[str] = []
    warnings: list[str] = []
    seen: set[str] = set()
    for raw_line in (raw_text or "").splitlines():
        for token in re.split(r"[,\|]", raw_line):
            token = token.strip()
            if not token:
                continue
            if not _SERIAL_RE.fullmatch(token):
                warnings.append(f"Not recognized as a serial, skipped: {token!r}")
                continue
            serial = token.upper()
            if serial in seen:
                warnings.append(f"Duplicate in your list, counted once: {serial}")
                continue
            seen.add(serial)
            serials.append(serial)
    return {"serials": serials, "warnings": warnings}


# ---- finding the receipt a delivery slip refers to ---------------------


def _open_incoming_receipts(extra_domain: list) -> list[dict]:
    return client.search_read(
        "stock.picking",
        [["picking_type_id", "like", "Receipts"], ["state", "not in", ["done", "cancel"]], *extra_domain],
        ["id", "name", "origin", "partner_id", "state"],
    )


def _unconfirmed_po_status(po_reference: str | None, vendor_name: str | None) -> dict:
    """Called only once no open receipt matched. Tells "no such PO/vendor"
    apart from "it exists but nobody has confirmed it yet" - a receipt
    doesn't exist in Odoo at all until confirmation, so this is the only
    way to give that distinction rather than a bare not_found."""
    domain = []
    if po_reference:
        # Odoo can't compare digit-normalized text server-side; pull
        # candidates broadly by state and match the reference in Python.
        candidates = client.search_read(
            "purchase.order", [["state", "in", ["draft", "sent"]]], ["id", "name", "partner_id", "state"]
        )
        ref = _normalize_po_ref(po_reference)
        matches = [p for p in candidates if _normalize_po_ref(p["name"]) == ref]
        if matches:
            return {
                "status": "not_confirmed",
                "pos": [{"po_name": p["name"], "vendor_name": p["partner_id"][1] if p["partner_id"] else ""} for p in matches],
            }
        return {"status": "not_found", "query": po_reference}
    if vendor_name:
        matches = client.search_read(
            "purchase.order",
            [["state", "in", ["draft", "sent"]], ["partner_id", "like", vendor_name]],
            ["id", "name", "partner_id", "state"],
        )
        if matches:
            return {
                "status": "not_confirmed",
                "pos": [{"po_name": p["name"], "vendor_name": p["partner_id"][1] if p["partner_id"] else ""} for p in matches],
            }
        return {"status": "not_found", "query": vendor_name}
    return {"status": "not_found", "query": ""}


def resolve_receipt(
    po_reference: str | None = None,
    vendor_name: str | None = None,
    override_picking_id: int | None = None,
) -> dict:
    """Find the one open incoming receipt a vendor delivery slip refers to.

    Returns exactly one of:
      {"status": "matched", "picking_id", "picking_name", "po_name", "vendor_name"}
      {"status": "ambiguous", "candidates": [{"picking_id", "picking_name", "po_name", "vendor_name"}, ...]}
      {"status": "not_confirmed", "pos": [{"po_name", "vendor_name"}, ...]}
        -- a matching purchase order exists but is still draft/sent; confirm
           it in Odoo first, then the receipt (and this lookup) will exist.
      {"status": "not_found", "query": str}
    Never guesses between two open receipts for the same vendor, and never
    confirms a purchase order itself.
    """
    if override_picking_id is not None:
        pickings = client.search_read(
            "stock.picking", [["id", "=", override_picking_id]],
            ["id", "name", "origin", "partner_id"],
        )
        if not pickings:
            return {"status": "not_found", "query": f"picking #{override_picking_id}"}
        p = pickings[0]
        return {
            "status": "matched", "picking_id": p["id"], "picking_name": p["name"],
            "po_name": p.get("origin") or "", "vendor_name": p["partner_id"][1] if p.get("partner_id") else "",
        }

    if not po_reference and not vendor_name:
        return {"status": "not_found", "query": ""}

    if po_reference:
        candidates = _open_incoming_receipts([])
        ref = _normalize_po_ref(po_reference)
        hits = [p for p in candidates if p.get("origin") and _normalize_po_ref(p["origin"]) == ref]
    else:
        hits = _open_incoming_receipts([["partner_id", "like", vendor_name]])

    if len(hits) == 1:
        p = hits[0]
        return {
            "status": "matched", "picking_id": p["id"], "picking_name": p["name"],
            "po_name": p.get("origin") or "", "vendor_name": p["partner_id"][1] if p.get("partner_id") else "",
        }
    if len(hits) > 1:
        return {
            "status": "ambiguous",
            "candidates": [
                {"picking_id": p["id"], "picking_name": p["name"], "po_name": p.get("origin") or "",
                 "vendor_name": p["partner_id"][1] if p.get("partner_id") else ""}
                for p in hits
            ],
        }
    return _unconfirmed_po_status(po_reference, vendor_name)


# ---- reconciling the slip against the receipt's lines -------------------


def preview_receiving(
    raw_serials: str,
    po_reference: str | None = None,
    vendor_name: str | None = None,
    product_hint: str | None = None,
    override_picking_id: int | None = None,
) -> dict:
    """Read-only. Resolves the receipt, then checks the given serials
    against its open (not-yet-serialized) lines for one product. Never
    writes; `fill_receiving_serials` re-runs this itself before writing."""
    parsed = parse_serial_list(raw_serials)
    resolved = resolve_receipt(po_reference, vendor_name, override_picking_id)
    report = {"resolution": resolved, "warnings": parsed["warnings"], "serials": parsed["serials"]}
    if resolved["status"] != "matched":
        return report

    picking_id = resolved["picking_id"]
    lines = client.search_read(
        "stock.move.line", [["picking_id", "=", picking_id]],
        ["id", "product_id", "lot_id", "lot_name"],
    )
    by_product: dict[int, list[dict]] = {}
    for line in lines:
        if line.get("product_id"):
            by_product.setdefault(line["product_id"][0], []).append(line)

    candidate_pids = list(by_product)
    if product_hint:
        hint = _normalize(product_hint)
        candidate_pids = [pid for pid in candidate_pids if hint in _normalize(by_product[pid][0]["product_id"][1])]

    fillable = {
        pid: [l for l in by_product[pid] if not l.get("lot_id") and not l.get("lot_name")]
        for pid in candidate_pids
    }
    fillable = {pid: lines_ for pid, lines_ in fillable.items() if lines_}

    report["products"] = [
        {
            "product_id": pid,
            "product_name": by_product[pid][0]["product_id"][1],
            "total_lines": len(by_product[pid]),
            "empty_lines": sum(1 for l in by_product[pid] if not l.get("lot_id") and not l.get("lot_name")),
        }
        for pid in by_product
    ]

    if not fillable:
        report["status"] = "no_open_lines_for_hint" if product_hint else "no_open_lines"
        return report
    if len(fillable) > 1:
        report["status"] = "needs_product_hint"
        report["candidates"] = [
            {"product_id": pid, "product_name": by_product[pid][0]["product_id"][1], "empty_lines": len(lines_)}
            for pid, lines_ in fillable.items()
        ]
        return report

    (pid, empty_lines), = fillable.items()
    serials = parsed["serials"]

    existing_lots = client.search_read("stock.lot", [["name", "in", serials]], ["id", "name"]) if serials else []
    existing_line_names = client.search_read(
        "stock.move.line", [["lot_name", "in", serials]], ["id", "lot_name", "picking_id"]
    ) if serials else []
    used_elsewhere = sorted(
        {l["name"] for l in existing_lots}
        | {l["lot_name"] for l in existing_line_names if l["picking_id"][0] != picking_id}
    )

    report["product"] = {"product_id": pid, "product_name": by_product[pid][0]["product_id"][1]}
    report["empty_line_count"] = len(empty_lines)
    report["serial_count"] = len(serials)

    if used_elsewhere:
        report["status"] = "duplicate_serial"
        report["already_used_elsewhere"] = used_elsewhere
        return report
    if len(serials) > len(empty_lines):
        report["status"] = "too_many_serials"
        return report

    report["status"] = "ready"
    report["line_ids_to_fill"] = [l["id"] for l in sorted(empty_lines, key=lambda l: l["id"])][: len(serials)]
    return report


# ---- the write: enter serials onto the receipt's open lines --------------

_JOURNAL_PATH = config.STATE_DIR / "receiving_serial_journal.jsonl"


def _append_journal(changes: list[dict], picking_id: int) -> None:
    _JOURNAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _JOURNAL_PATH.open("a", encoding="utf-8") as f:
        for change in changes:
            f.write(json.dumps({**change, "picking_id": picking_id, "ts": time.time()}) + "\n")


def fill_receiving_serials(
    raw_serials: str,
    po_reference: str | None = None,
    vendor_name: str | None = None,
    product_hint: str | None = None,
    override_picking_id: int | None = None,
) -> dict:
    """Writes the given serials onto the receipt's still-open lines for one
    product, journal first. Only acts when the receipt resolved
    unambiguously, the PO is confirmed (a receipt exists at all), there's
    no count mismatch, and none of the serials are already used elsewhere.
    Never writes more lines than serials given - fewer arrived than
    ordered is a normal, expected outcome, not an error.
    """
    report = preview_receiving(raw_serials, po_reference, vendor_name, product_hint, override_picking_id)
    if report.get("status") != "ready":
        return report

    picking_id = report["resolution"]["picking_id"]
    line_ids = report["line_ids_to_fill"]
    serials = report["serials"]
    changes = [{"line_id": line_id, "serial": serial} for line_id, serial in zip(line_ids, serials)]

    _append_journal(changes, picking_id)

    for change in changes:
        client.execute("stock.move.line", "write", [[change["line_id"]], {"lot_name": change["serial"]}])

    written = client.search_read("stock.move.line", [["id", "in", line_ids]], ["id", "lot_name"])
    written_by_id = {w["id"]: w for w in written}
    for change in changes:
        w = written_by_id.get(change["line_id"])
        change["verified"] = bool(w and w.get("lot_name") == change["serial"])

    remaining_empty = report["empty_line_count"] - len(changes)
    note_items = "".join(f"<li>{c['serial']}</li>" for c in changes)
    po_name = report["resolution"].get("po_name")
    tail = f" for {po_name}" if po_name else ""
    remainder_note = (
        f"{remaining_empty} line(s) left open for the rest of the shipment." if remaining_empty
        else "All lines for this product now have a serial."
    )
    client.execute(
        "stock.picking", "message_post", [[picking_id]],
        {"body": (
            f"<p>Serial numbers entered from vendor delivery slip{tail} "
            f"({len(changes)} of {report['empty_line_count']} open lines), via Zeke:</p>"
            f"<ul>{note_items}</ul>"
            f"<p>{remainder_note} Not validated - verify against the delivery slip, then validate.</p>"
        )},
    )

    report["status"] = "filled"
    report["changes"] = changes
    report["remaining_empty_lines"] = remaining_empty
    return report


# ---- rendering for Telegram -------------------------------------------------


def render_report(report: dict) -> str:
    lines: list[str] = []
    for w in report.get("warnings") or []:
        lines.append(f"⚠ {w}")
    if report.get("warnings"):
        lines.append("")

    resolution = report["resolution"]
    status = resolution["status"]

    if status == "ambiguous":
        lines.append("More than one open receipt matches - which one?")
        for c in resolution["candidates"]:
            lines.append(f"  {c['picking_name']} - {c['po_name'] or '(no PO ref)'} - {c['vendor_name']}")
        return "\n".join(lines).strip()

    if status == "not_confirmed":
        lines.append("Found the order, but it isn't confirmed yet - no receipt exists to put serials on.")
        for p in resolution["pos"]:
            lines.append(f"  {p['po_name']} - {p['vendor_name']}")
        lines.append("Confirm it in Odoo, then send the serials again.")
        return "\n".join(lines).strip()

    if status == "not_found":
        lines.append(f"Couldn't find a matching order or vendor for {resolution.get('query') or '(nothing given)'}.")
        return "\n".join(lines).strip()

    # matched
    lines.append(f"{resolution['picking_name']} - {resolution.get('po_name') or '(no PO ref)'} - {resolution['vendor_name']}")

    report_status = report.get("status")
    if report_status in ("no_open_lines", "no_open_lines_for_hint"):
        lines.append("No open (unfilled) lines" + (" for that product." if report_status.endswith("hint") else "."))
    elif report_status == "needs_product_hint":
        lines.append("More than one product on this receipt still needs serials - which one?")
        for c in report["candidates"]:
            lines.append(f"  {c['product_name']} ({c['empty_lines']} open)")
    elif report_status == "duplicate_serial":
        lines.append("Stopping - some of these serials are already on record elsewhere:")
        for s in report["already_used_elsewhere"]:
            lines.append(f"  {s}")
    elif report_status == "too_many_serials":
        lines.append(
            f"You gave {report['serial_count']} serials but only {report['empty_line_count']} "
            f"open line(s) for {report['product']['product_name']}. Check the list before I write anything."
        )
    elif report_status in ("ready", "filled"):
        product = report.get("product", {})
        if report_status == "ready":
            lines.append(f"{report['serial_count']} serial(s) ready to write for {product.get('product_name')}.")
        else:
            for c in report["changes"]:
                mark = "✓" if c["verified"] else "✗ FAILED - check manually"
                lines.append(f"  {mark} {c['serial']}")
            remaining = report.get("remaining_empty_lines", 0)
            lines.append(
                f"{remaining} line(s) still open for the rest of the shipment." if remaining
                else "All lines for this product now have a serial."
            )
            lines.append("Not validated - verify against the delivery slip, then validate.")

    return "\n".join(lines).strip()
