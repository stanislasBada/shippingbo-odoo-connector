from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    shippingbo_client_id = fields.Char(
        string="Client ID",
        config_parameter="shippingbo.client_id",
    )
    shippingbo_client_secret = fields.Char(
        string="Client Secret",
        config_parameter="shippingbo.client_secret",
    )
    shippingbo_app_id = fields.Char(
        string="App ID",
        config_parameter="shippingbo.app_id",
    )
    shippingbo_redirect_uri = fields.Char(
        string="Redirect URI",
        config_parameter="shippingbo.redirect_uri",
    )
    shippingbo_refresh_token = fields.Char(
        string="Refresh Token",
        config_parameter="shippingbo.refresh_token",
        help="Initial refresh token obtained from the ShippingBo OAuth2 authorization flow.",
    )
    shippingbo_stock_location_id = fields.Many2one(
        comodel_name="stock.location",
        string="Stock Synchronization Location",
        config_parameter="shippingbo.stock_location_id",
        help="Stock location used as source for the nightly ShippingBo stock synchronization.",
    )
    shippingbo_auto_send_on_ready = fields.Boolean(
        string="Auto-send ready deliveries",
        config_parameter="shippingbo.auto_send_on_ready",
        help="Send outgoing deliveries to ShippingBo as soon as they are ready (reserved).",
    )
