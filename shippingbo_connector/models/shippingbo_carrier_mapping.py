from odoo import fields, models


class ShippingboCarrierMapping(models.Model):
    _name = "shippingbo.carrier.mapping"
    _description = "Shippingbo Carrier Mapping"
    _order = "odoo_carrier_id"

    odoo_carrier_id = fields.Many2one(
        comodel_name="delivery.carrier",
        string="Odoo Carrier",
        required=True,
        ondelete="cascade",
    )
    shippingbo_carrier_name = fields.Char(
        string="Shippingbo Carrier Name",
        required=True,
        help="Exact value of the carrier_name field in Shippingbo.",
    )
    active = fields.Boolean(default=True)
