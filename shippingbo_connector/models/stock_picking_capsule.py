import logging
from urllib.parse import quote

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

SHIPPINGBO_CAPSULE_STATE_MAP = {
    "uploading":          "transmitted",
    "draft":              "transmitted",
    "waiting":            "transmitted",
    "sent_to_logistics":  "transmitted",
    "dispatched":         "transmitted",
    "ongoing":            "in_progress",
    "received":           "received",
    "in_trouble":         "error",
    "canceled":           "cancelled",
}

# Reception has started on the Shippingbo side: the capsule can no longer be replaced.
SHIPPINGBO_CAPSULE_LOCKED_STATES = ("in_progress", "received", "cancelled")


class StockPicking(models.Model):
    _inherit = "stock.picking"

    shippingbo_capsule_id = fields.Integer(
        string="Shippingbo Supply Capsule ID",
        copy=False,
        index=True,
    )

    def action_shippingbo_resend_capsule(self):
        self._shippingbo_resend_capsule()

    def _send_capsule_to_shippingbo(self):
        self.ensure_one()
        if self.shippingbo_capsule_id or self.state in ("done", "cancel"):
            return

        payload, missing = self._build_supply_capsule_payload()
        if missing:
            self.message_post(
                body=self.env._("Shippingbo: products without internal reference not sent: %s")
                     % ", ".join(missing.mapped("display_name"))
            )
        if not payload["supply_capsule"]["supply_capsule_items_attributes"]:
            self.message_post(body=self.env._("Shippingbo: no line to send."))
            return

        api_client = self.env["shippingbo.api"]
        status, res = api_client._shippingbo_call("POST", "/supply_capsules", payload)
        capsule_id = self._shippingbo_extract_capsule_id(res)
        if not capsule_id and status == 409:
            capsule_id = self._shippingbo_find_capsule_id(api_client, payload["supply_capsule"])

        if capsule_id:
            self.write({"shippingbo_capsule_id": capsule_id, "shippingbo_state": "transmitted"})
            self.message_post(body=self.env._("Receipt sent to Shippingbo. supply_capsule_id=%s") % capsule_id)
        else:
            self.write({"shippingbo_state": "error"})
            self.message_post(body=self.env._("Shippingbo: receipt sending failed (%s) — %s") % (status, res))

    def _build_supply_capsule_payload(self):
        """Return (payload, products skipped for lack of default_code)."""
        partner = (self.purchase_id.partner_id or self.partner_id).commercial_partner_id
        moves = self.move_ids.filtered(lambda m: m.state != "cancel" and m.product_qty > 0)
        missing = moves.filtered(lambda m: not m.product_id.default_code).product_id
        items = [
            {
                "source_ref":  str(move.id),
                "product_ref": move.product_id.default_code,
                "quantity":    int(move.product_qty),
            }
            for move in moves if move.product_id.default_code
        ]
        payload = {
            "supply_capsule": {
                "source_ref":    self.name,
                "external_ref":  self.purchase_id.name or None,
                "supplier_name": partner.name,
                "supplier_code": partner.ref or partner.name,
                "expected_delivery_date": (
                    self.scheduled_date.date().isoformat() if self.scheduled_date else None
                ),
                "supply_capsule_items_attributes": items,
            }
        }
        return payload, missing

    @staticmethod
    def _shippingbo_extract_capsule_id(res):
        if not isinstance(res, dict):
            return False
        return res.get("id") or (res.get("supply_capsule") or {}).get("id")

    def _shippingbo_find_capsule_id(self, api_client, capsule_vals):
        """source_ref + supplier_code is unique on the Shippingbo side."""
        res = api_client._shippingbo_request(
            "GET", "/supply_capsules?search[source_ref__eq]=%s" % quote(capsule_vals["source_ref"], safe="")
        )
        for capsule in (res or {}).get("supply_capsules") or []:
            if capsule.get("supplier_code") == capsule_vals["supplier_code"]:
                return capsule.get("id")
        return False

    def _shippingbo_delete_capsule(self):
        self.ensure_one()
        status, res = self.env["shippingbo.api"]._shippingbo_call(
            "DELETE", "/supply_capsules/%s" % self.shippingbo_capsule_id
        )
        if status < 300 or status == 404:
            return True
        self.message_post(
            body=self.env._("Shippingbo: deletion of capsule %s refused (%s) — %s")
                 % (self.shippingbo_capsule_id, status, res)
        )
        return False

    def _shippingbo_resend_capsule(self):
        """Shippingbo has no update on capsule quantities: delete and recreate."""
        for picking in self.filtered(lambda p: p.shippingbo_capsule_id and p.state not in ("done", "cancel")):
            if picking.shippingbo_state in SHIPPINGBO_CAPSULE_LOCKED_STATES:
                picking.message_post(
                    body=self.env._("Shippingbo: reception already started, the change is not sent.")
                )
                continue
            if picking._shippingbo_delete_capsule():
                picking.write({"shippingbo_capsule_id": 0, "shippingbo_state": False})
                picking._send_capsule_to_shippingbo()

    def _shippingbo_cancel_capsule(self):
        for picking in self.filtered(lambda p: p.shippingbo_capsule_id and p.shippingbo_state != "cancelled"):
            if picking.shippingbo_state in SHIPPINGBO_CAPSULE_LOCKED_STATES:
                picking.message_post(
                    body=self.env._("Shippingbo: reception already started, cancel it in Shippingbo.")
                )
                continue
            if picking._shippingbo_delete_capsule():
                picking.write({"shippingbo_state": "cancelled"})
                picking.message_post(body=self.env._("Shippingbo: receipt cancelled."))

    def action_cancel(self):
        res = super().action_cancel()
        self.filtered(lambda p: p.picking_type_code == "incoming")._shippingbo_cancel_capsule()
        return res

    @api.model
    def _shippingbo_dispatch_supply_capsule(self, capsule):
        capsule_id = capsule.get("id")
        remote_state = capsule.get("state")
        if not capsule_id:
            return
        pickings = self.search([("shippingbo_capsule_id", "=", capsule_id)], order="id")
        if not pickings:
            _logger.warning("ShippingBo webhook: no picking for supply capsule %s", capsule_id)
            return
        open_picking = pickings.filtered(lambda p: p.state not in ("done", "cancel"))[:1]
        picking = open_picking or pickings[-1]

        # Only post on change: the polling cron replays the same capsule repeatedly.
        state = SHIPPINGBO_CAPSULE_STATE_MAP.get(remote_state)
        if state and picking.shippingbo_state != state:
            picking.write({"shippingbo_state": state})
            if remote_state == "in_trouble":
                picking.message_post(body=self.env._("⚠️ Shippingbo: receipt in trouble (in_trouble)."))
            else:
                picking.message_post(body=self.env._("Shippingbo: receipt status → %s") % remote_state)

        if remote_state != "canceled" and open_picking:
            open_picking._shippingbo_apply_capsule_reception(
                capsule.get("supply_capsule_items") or [], pickings
            )

    @api.model
    def cron_shippingbo_poll_supply_capsules(self):
        """Fallback for webhooks: partial receptions do not always change the capsule state."""
        pickings = self.search([
            ("picking_type_code", "=", "incoming"),
            ("shippingbo_capsule_id", "!=", 0),
            ("state", "not in", ("done", "cancel")),
        ])
        api_client = self.env["shippingbo.api"]
        for capsule_id in set(pickings.mapped("shippingbo_capsule_id")):
            try:
                with self.env.cr.savepoint():
                    res = api_client._shippingbo_request("GET", "/supply_capsules/%s" % capsule_id)
                    capsule = (res or {}).get("supply_capsule")
                    if capsule:
                        self._shippingbo_dispatch_supply_capsule(capsule)
            except Exception:
                _logger.exception("ShippingBo: polling failed for supply capsule %s", capsule_id)

    def _shippingbo_apply_capsule_reception(self, items, capsule_pickings):
        """Receive the difference between Shippingbo's received_quantity and what
        the capsule pickings already received, so that a replayed webhook is a no-op."""
        self.ensure_one()
        label = "Shippingbo supply capsule (id=%s)" % self.shippingbo_capsule_id
        Move = self.env["stock.move"]
        open_moves = self.move_ids.filtered(lambda m: m.state not in ("done", "cancel"))
        done_moves = capsule_pickings.move_ids.filtered(lambda m: m.state == "done")

        to_receive = []
        unresolved = []
        for item in items:
            source_ref = str(item.get("source_ref") or "")
            ref_move = Move.browse(int(source_ref)).exists() if source_ref.isdigit() else Move
            if not ref_move:
                unresolved.append(source_ref)
                continue

            def same_line(m, ref=ref_move):
                return m.product_id == ref.product_id and m.purchase_line_id == ref.purchase_line_id

            product = ref_move.product_id
            already = sum(
                m.product_uom._compute_quantity(m.quantity, product.uom_id)
                for m in done_moves.filtered(same_line)
            )
            qty = float(item.get("received_quantity") or 0) - already
            if product.uom_id.compare(qty, 0) <= 0:
                continue
            targets = open_moves.filtered(same_line)
            if not targets:
                unresolved.append(source_ref)
                continue
            to_receive.append((targets, qty))

        if unresolved:
            self.message_post(body=self.env._("%s: unresolved line(s): %s") % (label, ", ".join(unresolved)))
        if not to_receive:
            return

        tracked = [t.product_id for t, _qty in to_receive if t.product_id.tracking != "none"]
        if tracked:
            self.message_post(
                body=self.env._("%s: products tracked by lot, validate the receipt manually: %s")
                     % (label, ", ".join(p.display_name for p in tracked))
            )
            return

        self._shippingbo_set_move_quantities(open_moves, to_receive)

        if not self._shippingbo_validate_with_backorder(label):
            return
        self.write({"shippingbo_state": "received"})

        # The backorder stays attached to the same capsule, which still expects the balance.
        backorders = self.search([("backorder_id", "=", self.id)])
        if backorders:
            backorders.write({
                "shippingbo_capsule_id": self.shippingbo_capsule_id,
                "shippingbo_state":      "in_progress",
            })
            for bo in backorders:
                bo.message_post(
                    body=self.env._("Backorder of %s — attached to Shippingbo capsule %s.")
                         % (self.name, self.shippingbo_capsule_id)
                )
