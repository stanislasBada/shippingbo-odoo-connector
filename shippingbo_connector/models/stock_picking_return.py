import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

SHIPPINGBO_RETURN_STATE_MAP = {
    "pregenerated":      "transmitted",
    "new":               "transmitted",
    "sent_to_logistics": "transmitted",
    "dispatched":        "transmitted",
    "in_trouble":        "error",
    "returned":          "received",
    "closed":            "received",
    "canceled":          "cancelled",
}


class StockPicking(models.Model):
    _inherit = "stock.picking"

    shippingbo_return_order_id = fields.Integer(
        string="Shippingbo Return Order ID",
        copy=False,
        index=True,
    )

    def action_confirm(self):
        res = super().action_confirm()
        config = self.env["ir.config_parameter"].sudo()
        if config.get_param("shippingbo.auto_send_returns", "False") == "True":
            returns = self.filtered(
                lambda p: p.picking_type_code == "incoming"
                and p.return_id.shippingbo_order_id
                and not p.shippingbo_return_order_id
            )
            for picking in returns:
                picking._shippingbo_auto_send()
        return res

    def action_cancel(self):
        res = super().action_cancel()
        self.filtered(lambda p: p.picking_type_code == "incoming")._shippingbo_cancel_return_order()
        return res

    def _send_return_order_to_shippingbo(self):
        self.ensure_one()
        if self.shippingbo_return_order_id or self.state in ("done", "cancel"):
            return
        if not self.return_id.shippingbo_order_id:
            self.message_post(
                body=self.env._("Shippingbo: the original delivery %s was not sent to Shippingbo, return not sent.")
                     % self.return_id.name
            )
            return

        payload, missing = self._build_return_order_payload()
        if missing:
            self.message_post(
                body=self.env._("Shippingbo: products without internal reference not sent: %s")
                     % ", ".join(missing.mapped("display_name"))
            )
        if not payload["return_order"]["return_order_expected_items_attributes"]:
            self.message_post(body=self.env._("Shippingbo: no line to send."))
            return

        status, res = self.env["shippingbo.api"]._shippingbo_call("POST", "/return_orders", payload)
        return_order_id = self._shippingbo_extract_return_order(res).get("id")
        if return_order_id:
            self.write({"shippingbo_return_order_id": return_order_id, "shippingbo_state": "transmitted"})
            self.message_post(body=self.env._("Return sent to Shippingbo. return_order_id=%s") % return_order_id)
        else:
            self.write({"shippingbo_state": "error"})
            self.message_post(body=self.env._("Shippingbo: return sending failed (%s) — %s") % (status, res))

    def _build_return_order_payload(self):
        """Return (payload, products skipped for lack of default_code)."""
        moves = self.move_ids.filtered(lambda m: m.state != "cancel" and m.product_qty > 0)
        missing = moves.filtered(lambda m: not m.product_id.default_code).product_id
        items = [
            {"user_ref": move.product_id.default_code, "quantity": int(move.product_qty)}
            for move in moves if move.product_id.default_code
        ]
        payload = {
            "return_order": {
                "order_id":    self.return_id.shippingbo_order_id,
                "reason":      self.origin or self.name,
                "reason_ref":  self.name,
                "return_order_type": "return_order_customer",
                "skip_expected_items_creation": True,
                "return_order_expected_items_attributes": items,
            }
        }
        return payload, missing

    @staticmethod
    def _shippingbo_extract_return_order(res):
        if not isinstance(res, dict):
            return {}
        return res.get("return_order") or (res if res.get("id") else {})

    def _shippingbo_cancel_return_order(self):
        """Only cancel returns not received yet: a partially received return is handled in Shippingbo."""
        pickings = self.filtered(lambda p: p.shippingbo_return_order_id and p.shippingbo_state != "cancelled")
        for picking in pickings:
            if picking.shippingbo_state in ("in_progress", "received"):
                picking.message_post(
                    body=self.env._("Shippingbo: return already partially received, cancel it in Shippingbo.")
                )
                continue
            status, res = self.env["shippingbo.api"]._shippingbo_call(
                "PATCH", "/return_orders/%s" % picking.shippingbo_return_order_id, {"state": "canceled"}
            )
            if status < 300:
                picking.write({"shippingbo_state": "cancelled"})
                picking.message_post(body=self.env._("Shippingbo: return cancelled."))
            else:
                picking.message_post(
                    body=self.env._("Shippingbo: return cancellation refused (%s) — %s") % (status, res)
                )

    @api.model
    def _shippingbo_dispatch_return_order(self, return_order):
        return_order_id = return_order.get("id")
        remote_state = return_order.get("state")
        if not return_order_id:
            return
        pickings = self.search([("shippingbo_return_order_id", "=", return_order_id)], order="id")
        if not pickings:
            _logger.warning("ShippingBo webhook: no picking for return order %s", return_order_id)
            return
        open_picking = pickings.filtered(lambda p: p.state not in ("done", "cancel"))[:1]
        picking = open_picking or pickings[-1]

        state = SHIPPINGBO_RETURN_STATE_MAP.get(remote_state)
        if state and picking.shippingbo_state != state:
            picking.write({"shippingbo_state": state})
            if remote_state == "in_trouble":
                picking.message_post(body=self.env._("⚠️ Shippingbo: return in trouble (in_trouble)."))
            else:
                picking.message_post(body=self.env._("Shippingbo: return status → %s") % remote_state)

        if remote_state != "canceled" and open_picking:
            open_picking._shippingbo_apply_return_reception(return_order, pickings)
        if remote_state in ("returned", "closed"):
            pickings._shippingbo_scrap_not_restockables(return_order)

    def _shippingbo_return_qty_by_product(self, return_order, key):
        """Aggregate a return order item list by Odoo product.

        Received items only carry the Shippingbo product_id: it is resolved through the
        expected items' product_user_ref, then through product.shippingbo_product_id.
        """
        ref_by_sbo_id = {
            e.get("product_id"): e.get("product_user_ref") or e.get("user_ref")
            for e in return_order.get("return_order_expected_items") or []
        }
        Product = self.env["product.product"]
        qty_by_product = {}
        unknown = []
        for item in return_order.get(key) or []:
            sbo_product_id = item.get("product_id")
            ref = ref_by_sbo_id.get(sbo_product_id)
            domain = [("default_code", "=", ref)] if ref else [("shippingbo_product_id", "=", sbo_product_id)]
            product = Product.search(domain, limit=1) if (ref or sbo_product_id) else Product
            if not product:
                unknown.append(str(sbo_product_id))
                continue
            qty = item.get("quantity")
            qty = float(qty if qty is not None else (item.get("lu_quantity") or 1))
            qty_by_product[product] = qty_by_product.get(product, 0.0) + qty
        return qty_by_product, unknown

    def _shippingbo_apply_return_reception(self, return_order, return_pickings):
        """Receive the difference between Shippingbo's received items and what the
        return pickings already received, so that a replayed webhook is a no-op."""
        self.ensure_one()
        label = "Shippingbo return order (id=%s)" % self.shippingbo_return_order_id
        received, unknown = self._shippingbo_return_qty_by_product(return_order, "return_order_items")
        if unknown:
            self.message_post(body=self.env._("%s: unresolved Shippingbo product(s): %s") % (label, ", ".join(unknown)))

        open_moves = self.move_ids.filtered(lambda m: m.state not in ("done", "cancel"))
        done_moves = return_pickings.move_ids.filtered(lambda m: m.state == "done")
        to_receive = []
        for product, qty in received.items():
            already = sum(
                m.product_uom._compute_quantity(m.quantity, product.uom_id)
                for m in done_moves.filtered(lambda m: m.product_id == product)
            )
            qty -= already
            if product.uom_id.compare(qty, 0) <= 0:
                continue
            targets = open_moves.filtered(lambda m: m.product_id == product)
            if not targets:
                self.message_post(body=self.env._("%s: %s received but not in the return.") % (label, product.display_name))
                continue
            to_receive.append((targets, qty))

        closed = return_order.get("state") == "closed"
        if not to_receive:
            if closed:
                self.message_post(body=self.env._("%s: return closed, cancel the remaining backorder manually.") % label)
            return

        tracked = [t.product_id for t, _qty in to_receive if t.product_id.tracking != "none"]
        if tracked:
            self.message_post(
                body=self.env._("%s: products tracked by lot, validate the return manually: %s")
                     % (label, ", ".join(p.display_name for p in tracked))
            )
            return

        self._shippingbo_set_move_quantities(open_moves, to_receive)
        # A closed return expects nothing more: no backorder for the missing items.
        if not self._shippingbo_validate_with_backorder(label, backorder=not closed):
            return
        self.write({"shippingbo_state": "received"})

        backorders = self.search([("backorder_id", "=", self.id)])
        if backorders:
            backorders.write({
                "shippingbo_return_order_id": self.shippingbo_return_order_id,
                "shippingbo_state":           "in_progress",
            })
            for bo in backorders:
                bo.message_post(
                    body=self.env._("Backorder of %s — attached to Shippingbo return %s.")
                         % (self.name, self.shippingbo_return_order_id)
                )

    def _shippingbo_scrap_not_restockables(self, return_order):
        """Scrap the received items Shippingbo declared not restockable (only the part not scrapped yet)."""
        done = self.filtered(lambda p: p.state == "done")
        if not done:
            return
        picking = done[-1]
        label = "Shippingbo return order (id=%s)" % picking.shippingbo_return_order_id
        to_scrap, unknown = picking._shippingbo_return_qty_by_product(
            return_order, "returned_product_not_restockables"
        )
        if unknown:
            picking.message_post(body=self.env._("%s: unresolved non-restockable product(s): %s") % (label, ", ".join(unknown)))

        Scrap = self.env["stock.scrap"]
        scrapped = Scrap.search([("picking_id", "in", done.ids), ("state", "=", "done")])
        for product, qty in to_scrap.items():
            qty -= sum(scrapped.filtered(lambda s: s.product_id == product).mapped("scrap_qty"))
            if product.uom_id.compare(qty, 0) <= 0:
                continue
            if product.tracking != "none":
                picking.message_post(
                    body=self.env._("%s: %s is not restockable and tracked by lot, scrap it manually.")
                         % (label, product.display_name)
                )
                continue
            try:
                with self.env.cr.savepoint():
                    Scrap.create({
                        "product_id":     product.id,
                        "product_uom_id": product.uom_id.id,
                        "scrap_qty":      qty,
                        "picking_id":     picking.id,
                        "location_id":    picking.location_dest_id.id,
                        "origin":         picking.name,
                    }).do_scrap()
                picking.message_post(body=self.env._("%s: %s × %s scrapped (not restockable).") % (label, qty, product.display_name))
            except Exception as e:
                _logger.exception("ShippingBo: scrap failed for picking %s", picking.name)
                picking.message_post(body=self.env._("%s: scrap failed for %s — %s") % (label, product.display_name, e))

    @api.model
    def cron_shippingbo_poll_return_orders(self):
        pickings = self.search([
            ("picking_type_code", "=", "incoming"),
            ("shippingbo_return_order_id", "!=", 0),
            ("state", "not in", ("done", "cancel")),
        ])
        api_client = self.env["shippingbo.api"]
        for return_order_id in set(pickings.mapped("shippingbo_return_order_id")):
            try:
                with self.env.cr.savepoint():
                    res = api_client._shippingbo_request("GET", "/return_orders/%s" % return_order_id)
                    return_order = self._shippingbo_extract_return_order(res)
                    if return_order:
                        self._shippingbo_dispatch_return_order(return_order)
            except Exception:
                _logger.exception("ShippingBo: polling failed for return order %s", return_order_id)
