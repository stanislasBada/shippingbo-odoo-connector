import json
import logging

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)


class ShippingboWebhook(http.Controller):

    # TODO: authentification (signature / token) à ajouter dans une version ultérieure.
    @http.route("/shippingbo/webhook", type="http", auth="public", methods=["POST"], csrf=False)
    def handle(self, **kwargs):
        """Réception des webhooks Shippingbo. Retourne toujours 200."""
        raw_body = request.httprequest.get_data()
        try:
            payload = json.loads(raw_body or b"{}")
        except ValueError:
            _logger.warning("ShippingBo webhook: invalid JSON body: %s", raw_body[:500])
            return request.make_json_response({"status": "ignored"})

        if not isinstance(payload, dict):
            return request.make_json_response({"status": "ignored"})

        object_class = payload.get("object_class")
        obj = payload.get("object") or payload
        _logger.info("ShippingBo webhook received: %s id=%s", object_class, obj.get("id"))

        Picking = request.env["stock.picking"].sudo()
        try:
            with request.env.cr.savepoint():
                if object_class == "Shipment":
                    Picking._shippingbo_dispatch_shipment(obj)
                elif object_class == "Order":
                    Picking._shippingbo_dispatch_order(obj)
                else:
                    _logger.info("ShippingBo webhook: unhandled object_class %s", object_class)
        except Exception:
            _logger.exception("ShippingBo webhook: error while processing %s", object_class)
            return request.make_json_response({"status": "error"})

        return request.make_json_response({"status": "ok"})
