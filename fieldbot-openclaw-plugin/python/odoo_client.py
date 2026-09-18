"""Thin XML-RPC wrapper around Odoo, restricted to a hardcoded model whitelist.

This is the safety boundary: the bot authenticates with a real user's API key,
so the ONLY thing standing between a misparsed message and the rest of Odoo is
this whitelist. Nothing outside it can be read or written, period.
"""
import logging
import xmlrpc.client

import config

log = logging.getLogger(__name__)

# model -> operations the bot may perform on it
WHITELIST: dict[str, set[str]] = {
    "project.task": {"search_read", "read", "write", "message_post", "create"},
    "project.task.type": {"search_read", "read"},
    "project.project": {"search_read", "read"},
    "account.analytic.line": {"search_read", "create", "write"},
    "res.users": {"search_read"},
    "hr.employee": {"search_read"},
    "res.partner": {"search_read", "create"},
    "ir.attachment": {"create", "search_count"},
    "mail.message": {"search_read"},  # read-only: ticket chatter for ticket_details
    # activities = "next steps" on tickets; action_feedback marks one done
    # (logs to chatter + deletes the activity, same as the GUI checkmark)
    "mail.activity": {"search_read", "search_count", "create", "write", "action_feedback"},
    "mail.activity.type": {"search_read"},
    "mail.message.subtype": {"search_read"},  # find the "Activities" subtype for recaps
    "ir.model": {"search_read"},  # res_model_id lookup for activity creation
    # Knowledge base: read-only. actions.py redacts credentials before any
    # text leaves the bridge; see redaction.py.
    "knowledge.article": {"search_read"},
    # ACME store delivery reconciliation (deliveries.py). NO "button_validate"
    # anywhere below - that omission is the entire safety property of this
    # feature. A human always finalizes a delivery in Odoo; do not add it.
    "stock.picking": {"search_read", "message_post"},
    "stock.move.line": {"search_read", "write"},  # deliveries.py only ever writes {"lot_id": ...}
    "stock.lot": {"search_read"},
    "stock.quant": {"search_read"},
    "sale.order": {"search_read"},  # diagnostics only: "no delivery - SO is still draft/cancelled"
    "product.product": {"search_read"},  # display names only
}


class OdooError(Exception):
    pass


class _TimeoutTransport(xmlrpc.client.SafeTransport):
    """SafeTransport with a socket timeout so a dead connection can't hang the bot."""

    def __init__(self, timeout: float = 30.0):
        super().__init__()
        self._timeout = timeout

    def make_connection(self, host):
        conn = super().make_connection(host)
        conn.timeout = self._timeout
        return conn


def _proxy(path: str) -> xmlrpc.client.ServerProxy:
    return xmlrpc.client.ServerProxy(
        f"{config.ODOO_URL}{path}", transport=_TimeoutTransport(), allow_none=True
    )


class OdooClient:
    def __init__(self):
        self._uid = None
        self._models = None

    def _connect(self):
        common = _proxy("/xmlrpc/2/common")
        uid = common.authenticate(
            config.ODOO_DB, config.ODOO_LOGIN, config.ODOO_API_KEY, {}
        )
        if not uid:
            raise OdooError(
                "Odoo authentication failed - check ODOO_LOGIN and the API key"
            )
        self._uid = uid
        self._models = _proxy("/xmlrpc/2/object")
        log.info("Authenticated to Odoo as uid=%s", uid)

    def execute(self, model: str, method: str, args: list, kwargs: dict | None = None):
        if model not in WHITELIST or method not in WHITELIST[model]:
            raise OdooError(f"Blocked by whitelist: {model}.{method}")
        if method == "message_post":
            # HARD GUARDRAIL: every chatter post is an INTERNAL log note.
            # mail.mt_note never notifies followers, so nothing the bot writes
            # can ever reach a customer. Recipient params are stripped so no
            # code path can add external recipients. Do not weaken this.
            kwargs = dict(kwargs or {})
            kwargs["subtype_xmlid"] = "mail.mt_note"
            for banned in ("partner_ids", "subtype_id", "email_from",
                           "email_layout_xmlid", "notify_author"):
                kwargs.pop(banned, None)
        if self._models is None:
            self._connect()
        try:
            return self._models.execute_kw(
                config.ODOO_DB, self._uid, config.ODOO_API_KEY,
                model, method, args, kwargs or {},
            )
        except xmlrpc.client.Fault as e:
            # session/uid never expires for API keys, but reconnect once on transport faults
            log.warning("Odoo fault: %s", e.faultString[:200])
            raise OdooError(e.faultString.splitlines()[-1][:300]) from e
        except (ConnectionError, xmlrpc.client.ProtocolError) as e:
            self._models = None
            raise OdooError(f"Odoo connection error: {e}") from e

    # convenience shorthands ------------------------------------------------
    def search_read(self, model, domain, fields, **kw):
        return self.execute(model, "search_read", [domain], {"fields": fields, **kw})


client = OdooClient()
