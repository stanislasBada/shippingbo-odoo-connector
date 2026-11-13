from odoo import models


class PurchaseOrder(models.Model):
    _inherit = "purchase.order"

    def button_approve(self, force=False):
        res = super().button_approve(force=force)
        config = self.env["ir.config_parameter"].sudo()
        if config.get_param("shippingbo.auto_send_purchase_on_confirm", "False") == "True":
            receipts = self.picking_ids.filtered(
                lambda p: p.picking_type_code == "incoming"
                and p.state not in ("done", "cancel")
                and not p.shippingbo_capsule_id
            )
            for picking in receipts:
                picking._shippingbo_auto_send()
        return res


class PurchaseOrderLine(models.Model):
    _inherit = "purchase.order.line"

    def _create_or_update_picking(self):
        res = super()._create_or_update_picking()
        self.order_id.picking_ids.filtered(
            lambda p: p.picking_type_code == "incoming"
        )._shippingbo_resend_capsule()
        return res
