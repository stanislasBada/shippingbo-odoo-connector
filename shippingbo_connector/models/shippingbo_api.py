import requests
import logging
import time
from odoo import models, api

_logger = logging.getLogger(__name__)


class ShippingboApi(models.AbstractModel):
    _name = 'shippingbo.api'
    _description = 'ShippingBo API Client'

    def _shippingbo_get_token(self):
        config = self.env['ir.config_parameter'].sudo()

        access_token = config.get_param('shippingbo.access_token')
        refresh_token = config.get_param('shippingbo.refresh_token')
        expiration = float(config.get_param('shippingbo.token_expiration', 0))

        if access_token and expiration > time.time():
            return access_token

        payload = {
            'grant_type': 'refresh_token',
            'client_id': config.get_param('shippingbo.client_id'),
            'client_secret': config.get_param('shippingbo.client_secret'),
            'refresh_token': refresh_token,
            'redirect_uri': config.get_param('shippingbo.redirect_uri'),
        }

        r = requests.post('https://oauth.shippingbo.com/oauth/token', json=payload)
        data = r.json()
        if 'access_token' not in data:
            _logger.error('ShippingBo OAuth error: %s', data)
            return False

        config.set_param('shippingbo.access_token', data['access_token'])
        config.set_param('shippingbo.refresh_token', data['refresh_token'])
        config.set_param('shippingbo.token_expiration', time.time() + data['expires_in'])

        return data['access_token']

    def _shippingbo_request(self, method, endpoint, payload=None):
        config = self.env['ir.config_parameter'].sudo()
        url = f'https://app.shippingbo.com{endpoint}'
        token = self._shippingbo_get_token()

        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {token}',
            'X-API-APP-ID': config.get_param('shippingbo.app_id'),
            'X-API-VERSION': '1',
        }

        _logger.info('ShippingBo %s %s payload=%s', method, endpoint, payload)
        r = requests.request(method, url, json=payload, headers=headers)

        if r.status_code == 401:
            _logger.warning('ShippingBo token expired, refreshing')
            config.set_param('shippingbo.token_expiration', 0)
            headers['Authorization'] = f'Bearer {self._shippingbo_get_token()}'
            r = requests.request(method, url, json=payload, headers=headers)

        if r.status_code >= 300:
            _logger.error('ShippingBo error %s %s', r.status_code, r.text)

        try:
            return r.json()
        except Exception:
            return {'detail': 'error'}

    @api.model
    def sync_stock_from_shippingbo(self):
        """Shippingbo fait foi : aligne le stock Odoo des articles cochés
        « Stock synchronisé Shippingbo » sur le stock Shippingbo."""
        _logger.info('=== Starting ShippingBo -> Odoo stock sync ===')

        location_id_str = self.env['ir.config_parameter'].sudo().get_param(
            'shippingbo.stock_location_id'
        )
        if not location_id_str:
            _logger.error(
                'ShippingBo stock location is not configured. '
                'Set it in Settings > ShippingBo.'
            )
            return

        location = self.env['stock.location'].sudo().browse(int(location_id_str))
        Quant = self.env['stock.quant'].sudo()
        products = self.env['product.product'].sudo().search([
            ('product_tmpl_id.shippingbo_stock_sync', '=', True),
            ('is_storable', '=', True),
            ('default_code', '!=', False),
        ])
        _logger.info('%d product(s) flagged for ShippingBo stock sync', len(products))

        success, skipped, errors = 0, 0, 0

        for product in products:
            ref = product.default_code
            if product.tracking != 'none':
                _logger.warning('[%s] tracked by lot/serial — skipped', ref)
                skipped += 1
                continue

            result = self._shippingbo_request(
                'GET', f'/products?search[user_ref__eq]={ref}'
            )
            sbo_products = result.get('products', [])
            if not sbo_products:
                _logger.warning("Product '%s' not found in ShippingBo", ref)
                errors += 1
                continue

            shippingbo_qty = float(sbo_products[0].get('stock', 0))
            current_qty = sum(Quant.search([
                ('product_id', '=', product.id),
                ('location_id', 'child_of', location.id),
            ]).mapped('quantity'))
            delta = shippingbo_qty - current_qty
            if product.uom_id.is_zero(delta):
                continue

            try:
                with self.env.cr.savepoint():
                    # Ajustement d'inventaire (tracé) sur l'emplacement principal,
                    # le quant est créé s'il n'existe pas encore.
                    InvQuant = Quant.with_context(inventory_mode=True)
                    quant = InvQuant.search([
                        ('product_id', '=', product.id),
                        ('location_id', '=', location.id),
                        ('lot_id', '=', False),
                        ('package_id', '=', False),
                        ('owner_id', '=', False),
                    ], limit=1)
                    if quant:
                        quant.inventory_quantity = quant.quantity + delta
                    else:
                        quant = InvQuant.create({
                            'product_id': product.id,
                            'location_id': location.id,
                            'inventory_quantity': delta,
                        })
                    quant.action_apply_inventory()
                _logger.info(
                    '[%s] %s: %.0f -> %.0f units',
                    ref, product.name, current_qty, shippingbo_qty,
                )
                success += 1
            except Exception as e:
                _logger.error('Error updating stock for [%s]: %s', ref, str(e))
                errors += 1

        _logger.info(
            '=== Sync complete: %d updated / %d skipped / %d errors ===',
            success, skipped, errors,
        )
