from odoo import fields, models


class StockWarehouse(models.Model):
    _inherit = "stock.warehouse"

    shippingbo_enabled = fields.Boolean(
        string="Synced with Shippingbo",
        default=True,
        help="Deliveries, receipts and returns of this warehouse are sent to Shippingbo.",
    )
