from odoo import models


class StockMove(models.Model):
    _inherit = "stock.move"

    def _action_assign(self, *args, **kwargs):
        """Envoi automatique vers Shippingbo quand le BL passe à l'état prêt."""
        res = super()._action_assign(*args, **kwargs)
        if self.env.context.get("shippingbo_skip_auto_send"):
            return res
        config = self.env["ir.config_parameter"].sudo()
        if config.get_param("shippingbo.auto_send_on_ready", "False") != "True":
            return res
        pickings = self.picking_id.filtered(
            lambda p: p.state == "assigned"
            and p.picking_type_code == "outgoing"
            and p.sale_id
            and not p.shippingbo_order_id
            and p.shippingbo_state != "error"
        )
        for picking in pickings:
            picking._shippingbo_auto_send()
        return res
