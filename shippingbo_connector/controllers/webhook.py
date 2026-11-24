import json
import logging
import traceback

from psycopg2 import OperationalError

from odoo import SUPERUSER_ID, http
from odoo.http import Response, request

from ..exceptions import ShippingboRetryLater

_logger = logging.getLogger(__name__)


class ShippingboWebhook(http.Controller):

    @http.route("/shippingbo/webhook", type="http", auth="public", methods=["POST"], csrf=False)
    def handle(self, **kwargs):
        """Answer 200 once processed, so that Shippingbo does not retry.

        Transient database conflicts (serialization failure, deadlock) are answered with 503:
        the change was rolled back, so Shippingbo must send the webhook again.
        """
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
        except (OperationalError, ShippingboRetryLater) as e:
            # SerializationFailure / DeadlockDetected are subclasses of OperationalError.
            # ShippingboRetryLater: the change cannot be applied yet (e.g. the order link is not saved).
            _logger.warning("ShippingBo webhook: retry requested on %s: %s", object_class, e)
            self._log("WARNING", "Retry requested: %s (%s)" % (summary, e), raw_body)
            return Response(
                json.dumps({"status": "retry"}), status=503, mimetype="application/json",
            )
        except Exception as exc:
            _logger.exception("ShippingBo webhook: error while processing %s", object_class)
            self._log("ERROR", "Error: %s\n%s" % (summary, traceback.format_exc()), raw_body)
            self._alert_pickings(Picking, object_class, obj, exc)
            return request.make_json_response({"status": "error"})

        self._log("INFO", "Processed: %s" % summary, raw_body)
        return request.make_json_response({"status": "ok"})

    @staticmethod
    def _alert_pickings(Picking, object_class, obj, exc):
        """Post the error in the chatter of the related picking, so that it is visible in Odoo.
        Never fails the response: the alert is best effort."""
        try:
            if object_class == "Order":
                pickings = Picking.search([("shippingbo_order_id", "=", obj.get("id"))])
            elif object_class == "Shipment":
                pickings = Picking.search([("shippingbo_order_id", "=", obj.get("order_id"))])
            elif object_class == "SupplyCapsule":
                pickings = Picking.search([("shippingbo_capsule_id", "=", obj.get("id"))])
            else:
                return
            for picking in pickings:
                picking.message_post(
                    body=Picking.env._("⚠️ Shippingbo: webhook %s not applied (%s). Check the connector logs.")
                         % (object_class, exc)
                )
        except Exception:
            _logger.exception("ShippingBo webhook: could not post the error alert")

    @staticmethod
    def _log(level, message, raw_body):
        request.env["shippingbo.api"].with_user(SUPERUSER_ID)._shippingbo_log(
            "shippingbo.webhook", level,
            "%s\n\n%s" % (message, raw_body.decode("utf-8", "replace")),
            path="/shippingbo/webhook", func="handle",
        )
