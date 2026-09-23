from odoo import fields, models


class ShippingboCarrierMapping(models.Model):
    """Table de correspondance transporteurs Odoo ↔ Shippingbo.

    Configurable par le client depuis Settings > Shippingbo.
    """
    _name = "shippingbo.carrier.mapping"
    _description = "Shippingbo Carrier Mapping"
    _order = "odoo_carrier_id"

    odoo_carrier_id = fields.Many2one(
        comodel_name="delivery.carrier",
        string="Transporteur Odoo",
        required=True,
        ondelete="cascade",
    )
    shippingbo_carrier_name = fields.Char(
        string="Nom transporteur Shippingbo",
        required=True,
        help="Valeur exacte du champ carrier_name côté Shippingbo.",
    )
    active = fields.Boolean(default=True)
