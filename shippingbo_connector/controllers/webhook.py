import json
import logging
import traceback

from odoo import SUPERUSER_ID, http
from odoo.http import request

_logger = logging.getLogger(__name__)


class ShippingboWebhook(http.Controller):

    @http.route("/shippingbo/webhook", type="http", auth="public", methods=["POST"], csrf=False)
    def handle(self, **kwargs):
        """Always answer 200 so that Shippingbo does not retry."""
        raw_body = request.httprequest.get_data()
        try:
            payload = json.loads(raw_body or b"{}")
        except ValueError:
            _logger.warning("ShippingBo webhook: invalid JSON body: %s", raw_body[:500])
            self._log("WARNING", "Invalid JSON body", raw_body)
            return request.make_json_response({"status": "ignored"})

        if not isinstance(payload, dict):
            self._log("WARNING", "Ignored: body is not a JSON object", raw_body)
            return request.make_json_response({"status": "ignored"})

        object_class = payload.get("object_class")
        obj = payload.get("object") or payload
        summary = "%s id=%s state=%s" % (object_class, obj.get("id"), obj.get("state"))
        _logger.info("ShippingBo webhook received: %s", summary)

        # Run as OdooBot, in its language, so that chatter messages are not authored by the public user.
        env = request.env(user=SUPERUSER_ID)
        Picking = env["stock.picking"].with_context(lang=env.user.lang)
        try:
            with request.env.cr.savepoint():
                if object_class == "Shipment":
                    Picking._shippingbo_dispatch_shipment(obj)
                elif object_class == "Order":
                    Picking._shippingbo_dispatch_order(obj)
                elif object_class == "SupplyCapsule":
                    Picking._shippingbo_dispatch_supply_capsule(obj)
                elif object_class == "ReturnOrder":
                    Picking._shippingbo_dispatch_return_order(obj)
                else:
                    _logger.info("ShippingBo webhook: unhandled object_class %s", object_class)
                    self._log("WARNING", "Unhandled: %s" % summary, raw_body)
                    return request.make_json_response({"status": "ignored"})
        except Exception:
            _logger.exception("ShippingBo webhook: error while processing %s", object_class)
            self._log("ERROR", "Error: %s\n%s" % (summary, traceback.format_exc()), raw_body)
            return request.make_json_response({"status": "error"})

        self._log("INFO", "Processed: %s" % summary, raw_body)
        return request.make_json_response({"status": "ok"})

    @staticmethod
    def _log(level, message, raw_body):
        request.env["shippingbo.api"].with_user(SUPERUSER_ID)._shippingbo_log(
            "shippingbo.webhook", level,
            "%s\n\n%s" % (message, raw_body.decode("utf-8", "replace")),
            path="/shippingbo/webhook", func="handle",
        )
