from odoo import models
import logging

_logger = logging.getLogger(__name__)


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    def _shippingbo(self):
        return self.env['shippingbo.api'].sudo()

    def send_to_shippingbo(self, payload):
        api = self._shippingbo()
        shipping_address = api._shippingbo_request(
            'POST', '/addresses', payload['order']['shipping_address']
        )
        billing_address = api._shippingbo_request(
            'POST', '/addresses', payload['order']['billing_address']
        )
        try:
            payload['order']['shipping_address_id'] = shipping_address['address']['id']
            payload['order']['billing_address_id'] = billing_address['address']['id']
        except Exception:
            return shipping_address

        res = api._shippingbo_request('POST', '/orders', payload)
        if res.get('id'):
            _logger.info('ShippingBo order created: %s', res.get('id'))
        return res

    def update_shippingbo_state(self, order_id, state):
        res = self._shippingbo()._shippingbo_request(
            'PATCH', f'/orders/{order_id}', {'state': state}
        )
        if not res:
            return {'detail': 'error'}
        _logger.info('ShippingBo state updated %s -> %s', order_id, state)
        return res

    def update_shippingbo_items(self, order_id, payload):
        res = self._shippingbo()._shippingbo_request(
            'POST', f'/orders/{order_id}/update_order_items', payload
        )
        if not res:
            return {'detail': 'error'}
        _logger.info('ShippingBo items updated for order %s', order_id)
        return res
