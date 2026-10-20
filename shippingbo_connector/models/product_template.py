from odoo import fields, models


class ProductTemplate(models.Model):
    _inherit = "product.template"

    shippingbo_stock_sync = fields.Boolean(
        string="Shippingbo Stock Sync",
        default=False,
        help="The stock of this product is driven by Shippingbo: the synchronization cron "
             "overwrites the Odoo quantity with the Shippingbo one.",
    )

    def action_shippingbo_sync_products(self):
        self.product_variant_ids._shippingbo_sync_products(force=True)
