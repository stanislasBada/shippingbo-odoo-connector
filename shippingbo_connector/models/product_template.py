from odoo import fields, models


class ProductTemplate(models.Model):
    _inherit = "product.template"

    shippingbo_stock_sync = fields.Boolean(
        string="Stock synchronisé Shippingbo",
        default=False,
        help="Le stock de cet article est piloté par Shippingbo : le cron de synchronisation "
             "écrase la quantité Odoo par celle de Shippingbo.",
    )
