import json
import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

# Unknown states are posted in the chatter without changing shippingbo_state.
SHIPPINGBO_ORDER_STATE_MAP = {
    "waiting_for_payment":  "transmitted",
    "waiting_for_stock":    "transmitted",
    "to_be_prepared":       "transmitted",
    "sent_to_logistician":  "transmitted",
    "in_preparation":       "in_progress",
    "partially_shipped":    "in_progress",
    "shipped":              "shipped",
    "handed_to_carrier":    "shipped",
    "delivered":            "delivered",
    "canceled":             "cancelled",
    "cancelled":            "cancelled",
    "rejected":             "error",
    "in_trouble":           "error",
}


class StockPicking(models.Model):
    _inherit = "stock.picking"

    shippingbo_order_id = fields.Integer(
        string="Shippingbo Order ID",
        copy=False,
        index=True,
        help="ID de la commande côté Shippingbo. Posé à l'envoi, utilisé pour les mises à jour.",
    )
    shippingbo_items_map = fields.Text(
        string="Shippingbo Items Map",
        copy=False,
        help=(
            "JSON {order_item_id: {product_id, units}} construit à l'envoi. "
            "Permet la conversion colis → unités au retour du webhook shipment."
        ),
    )
    shippingbo_state = fields.Selection(
        selection=[
            ("pending",      "En attente"),
            ("transmitted",  "Transmis"),
            ("in_progress",  "En préparation"),
            ("shipped",      "Expédié"),
            ("delivered",    "Livré"),
            ("received",     "Réceptionné"),
            ("cancelled",    "Annulé"),
            ("error",        "Erreur"),
        ],
        string="Statut Shippingbo",
        copy=False,
        default=False,
    )
    shippingbo_shipment_id = fields.Integer(
        string="Shippingbo Shipment ID",
        copy=False,
        index=True,
        help="ID du shipment Shippingbo ayant validé ce BL (anti-doublon webhook).",
    )
    shippingbo_earliest_ship_date = fields.Date(
        string="Date de préparation Shippingbo",
        help="Date à partir de laquelle Shippingbo peut expédier cette commande.",
    )

    def action_send_to_shippingbo(self):
        for picking in self:
            picking._shippingbo_send()

    def _shippingbo_send(self):
        self.ensure_one()
        if self.picking_type_code == "incoming" and self.return_id:
            self._send_return_order_to_shippingbo()
        elif self.picking_type_code == "incoming":
            self._send_capsule_to_shippingbo()
        else:
            self._send_to_shippingbo()

    def _shippingbo_auto_send(self):
        """Failures must not block the reservation that triggered the send."""
        self.ensure_one()
        try:
            with self.env.cr.savepoint():
                self._shippingbo_send()
        except Exception as e:
            _logger.exception("ShippingBo: auto send failed for picking %s", self.name)
            self.message_post(body="Shippingbo : envoi automatique échoué — %s" % e)
            self.write({"shippingbo_state": "error"})

    def _send_to_shippingbo(self):
        self.ensure_one()
        so = self.sale_id
        if not so:
            _logger.warning(
                "ShippingBo: picking %s has no linked sale order — skipped.", self.name
            )
            return

        if self.shippingbo_order_id:
            _logger.info(
                "ShippingBo: picking %s already sent (shippingbo_order_id=%s) — skipped.",
                self.name, self.shippingbo_order_id,
            )
            return

        payload, items_meta = self._build_shippingbo_payload(so)
        if not payload["order"].get("order_items_attributes"):
            self.message_post(body="Shippingbo : aucune ligne à envoyer (pas d'article 'consu' avec qté > 0).")
            return

        response = so.send_to_shippingbo(payload)
        self._process_send_response(response, items_meta)

    def _build_shippingbo_payload(self, so):
        """Return (payload, items_meta_by_source_ref).

        Moves with a carton reference are split into carton and unit lines.
        """
        order_items_attributes = []
        items_meta_by_source_ref = {}
        currency = so.currency_id.name
        total_weight = 0.0

        for move in self.move_ids.filtered(lambda m: m.state != "cancel"):
            product = move.product_id
            if not product or product.type != "consu":
                continue

            qty = int(move.product_qty)
            if qty <= 0:
                continue

            total_weight += product.weight * qty
            sale_line = move.sale_line_id
            title = sale_line.name if sale_line else product.display_name
            line_price_cents, line_tax_cents = self._move_price_cents(sale_line, qty)

            carton_ref, units_per_carton = self._shippingbo_carton_info(product)
            if not carton_ref or units_per_carton <= 0 or qty < units_per_carton:
                order_items_attributes.append(self._item_line(
                    source_ref=move.id,
                    product_ref=product.default_code,
                    product_id=product.id,
                    title=title,
                    quantity=qty,
                    price_cents=line_price_cents,
                    tax_cents=line_tax_cents,
                    currency=currency,
                ))
                items_meta_by_source_ref[str(move.id)] = {
                    "product_id": product.id, "units": 1
                }
                continue
            n_cartons = qty // units_per_carton
            remaining = qty % units_per_carton
            carton_units = n_cartons * units_per_carton

            carton_price_cents = line_price_cents * carton_units // qty
            carton_tax_cents   = line_tax_cents   * carton_units // qty
            unit_price_cents   = line_price_cents - carton_price_cents
            unit_tax_cents     = line_tax_cents   - carton_tax_cents

            order_items_attributes.append(self._item_line(
                source_ref="%s-COLIS" % move.id,
                product_ref=carton_ref,
                product_id=product.id,
                title="%s (colis de %s)" % (title, units_per_carton),
                quantity=n_cartons,
                price_cents=carton_price_cents,
                tax_cents=carton_tax_cents,
                currency=currency,
            ))
            items_meta_by_source_ref["%s-COLIS" % move.id] = {
                "product_id": product.id, "units": units_per_carton
            }

            if remaining > 0:
                order_items_attributes.append(self._item_line(
                    source_ref="%s-UNITE" % move.id,
                    product_ref=product.default_code,
                    product_id=product.id,
                    title=title,
                    quantity=remaining,
                    price_cents=unit_price_cents,
                    tax_cents=unit_tax_cents,
                    currency=currency,
                ))
                items_meta_by_source_ref["%s-UNITE" % move.id] = {
                    "product_id": product.id, "units": 1
                }
        shipping = self._build_address(self.partner_id or so.partner_shipping_id)
        billing  = self._build_address(so.partner_invoice_id)
        mapped_carrier = self._get_shippingbo_carrier_name()
        earliest = None
        if self.shippingbo_earliest_ship_date:
            earliest = self.shippingbo_earliest_ship_date.isoformat()

        payload = {
            "order": {
                "source":      "000",
                "source_ref":  self.name,
                "origin":      "ODOO",
                "origin_ref":  so.name,
                "origin_created_at": so.date_order.isoformat(),
                "earliest_shipped_at": earliest,
                "payment_medium": so.payment_term_id.name if so.payment_term_id else "unknown",
                "total_price_cents":    int(so.amount_total * 100),
                "total_price_currency": currency,
                "total_tax_cents":      int(so.amount_tax * 100),
                "total_tax_currency":   currency,
                "total_weight": total_weight,
                "relay_ref": self._shippingbo_relay_ref(),
                "mapped_carrier":           mapped_carrier,
                "chosen_delivery_service":  mapped_carrier,
                "order_items_attributes":   order_items_attributes,
                "shipping_address": shipping,
                "billing_address":  billing,
                "tags_to_add": ["ODOO"],
            }
        }

        return payload, items_meta_by_source_ref

    @staticmethod
    def _shippingbo_optional_field(record, fname, default=False):
        return record[fname] if fname in record._fields else default

    def _shippingbo_carton_info(self, product):
        """Return (carton product_ref, units per carton), or (False, 0). Override to customize."""
        carton_ref = self._shippingbo_optional_field(product, "x_studio_reference_colis_shippingbo")
        units = self._shippingbo_optional_field(product, "x_studio_unites_par_colis", 0)
        return carton_ref or False, int(units or 0)

    def _shippingbo_relay_ref(self):
        """Override to customize."""
        return self._shippingbo_optional_field(self, "x_studio_id_point_relais") or None

    @staticmethod
    def _move_price_cents(sale_line, qty):
        """Tax-included price and tax, in cents, for qty units of the sale line."""
        if not sale_line or not sale_line.product_uom_qty:
            return 0, 0
        ratio = qty / sale_line.product_uom_qty
        price_cents = int(round(sale_line.price_total * ratio * 100))
        tax_cents = int(round((sale_line.price_total - sale_line.price_subtotal) * ratio * 100))
        return price_cents, tax_cents

    def _process_send_response(self, response, items_meta_by_source_ref):
        self.ensure_one()

        if isinstance(response, str):
            try:
                response = json.loads(response)
            except Exception:
                response = {}

        if not isinstance(response, dict):
            self.message_post(body="Shippingbo : réponse inattendue — %s" % response)
            return

        shippingbo_id = (
            response.get("id")
            or (response.get("order") or {}).get("id")
            or (response.get("object") or {}).get("id")
        )

        returned_items = (
            response.get("order_items")
            or (response.get("order") or {}).get("order_items")
            or (response.get("object") or {}).get("order_items")
            or []
        )

        items_map = {}
        for oi in returned_items:
            if not isinstance(oi, dict):
                continue
            sref = oi.get("source_ref")
            oi_id = oi.get("id")
            if sref is None or not oi_id:
                continue
            meta = items_meta_by_source_ref.get(str(sref))
            if meta:
                items_map[str(oi_id)] = meta

        if shippingbo_id:
            vals = {
                "shippingbo_order_id": shippingbo_id,
                "shippingbo_state":    "transmitted",
            }
            if items_map:
                vals["shippingbo_items_map"] = json.dumps(items_map)
            self.write(vals)
            self.message_post(
                body="Commande envoyée à Shippingbo. shippingbo_order_id=%s" % shippingbo_id
            )
            if not items_map:
                self.message_post(
                    body="⚠️ Shippingbo : table order_item/produit vide "
                         "(order_items absents ou source_ref non rapproché) — suivi BL à surveiller."
                )
        else:
            self.message_post(
                body="Shippingbo : commande envoyée mais pas d'ID retourné. Réponse=%s" % response
            )
            self.write({"shippingbo_state": "error"})

    @api.model
    def _shippingbo_dispatch_shipment(self, shipment):
        """An already processed shipment only updates tracking; otherwise it
        validates the first open picking of the Shippingbo order."""
        order_id = shipment.get("order_id")
        shipment_id = shipment.get("id")
        if not order_id:
            _logger.warning("ShippingBo webhook: shipment %s without order_id", shipment_id)
            return
        pickings = self.search([("shippingbo_order_id", "=", order_id)], order="id")
        if not pickings:
            _logger.warning("ShippingBo webhook: no picking for order %s", order_id)
            return

        already = shipment_id and pickings.filtered(
            lambda p: p.shippingbo_shipment_id == shipment_id
        )[:1]
        if already:
            already._process_shipment_webhook(shipment, tracking_only=True)
            return

        picking = pickings.filtered(lambda p: p.state not in ("done", "cancel"))[:1]
        if not picking:
            _logger.warning(
                "ShippingBo webhook: no open picking for order %s (shipment %s)",
                order_id, shipment_id,
            )
            pickings[-1].message_post(
                body="Shippingbo shipment (id=%s) reçu mais aucun BL ouvert pour cette commande."
                     % shipment_id
            )
            return
        picking._process_shipment_webhook(shipment)

    @api.model
    def _shippingbo_dispatch_order(self, order):
        order_id = order.get("id")
        remote_state = order.get("state")
        if not order_id or not remote_state:
            return
        pickings = self.search([("shippingbo_order_id", "=", order_id)], order="id")
        if not pickings:
            _logger.warning("ShippingBo webhook: no picking for order %s", order_id)
            return
        targets = pickings.filtered(lambda p: p.state not in ("done", "cancel")) or pickings[-1]
        state = SHIPPINGBO_ORDER_STATE_MAP.get(remote_state)
        for picking in targets:
            if state and picking.shippingbo_state != state:
                picking.write({"shippingbo_state": state})
            picking.message_post(body="Shippingbo : statut commande → %s" % remote_state)

    def _process_shipment_webhook(self, shipment, tracking_only=False):
        self.ensure_one()

        tracking_ref  = (shipment.get("shipping_ref") or "").strip() or False
        tracking_url  = (shipment.get("tracking_url") or "").strip() or False
        carrier_name  = (shipment.get("carrier_name") or "").strip() or False
        shipment_id   = shipment.get("id")
        if carrier_name:
            carrier = self._find_odoo_carrier(carrier_name)
            if carrier == "multiple":
                self.message_post(
                    body="Shippingbo shipment (id=%s) : matching transporteur ambigu pour '%s'."
                         % (shipment_id, carrier_name)
                )
            elif carrier:
                if carrier != self.carrier_id:
                    self.write({"carrier_id": carrier.id})
            else:
                self.message_post(
                    body="Shippingbo shipment (id=%s) : aucun transporteur Odoo pour '%s'."
                         % (shipment_id, carrier_name)
                )
        if tracking_ref and tracking_ref != self.carrier_tracking_ref:
            self.write({"carrier_tracking_ref": tracking_ref})
        if tracking_url:
            self.message_post(body="Shippingbo : suivi colis %s" % tracking_url)

        if tracking_only or self.state in ("done", "cancel"):
            return
        items_ship = shipment.get("order_items_shipments") or []
        if not items_ship:
            self.message_post(
                body="Shippingbo shipment (id=%s) : pas d'order_items_shipments." % shipment_id
            )
        else:
            self._apply_shipped_quantities(items_ship, shipment_id)

    def _apply_shipped_quantities(self, items_ship, shipment_id):
        items_map = {}
        if self.shippingbo_items_map:
            try:
                items_map = json.loads(self.shippingbo_items_map) or {}
            except Exception:
                items_map = {}

        qty_by_product = {}
        unresolved = []

        for it in items_ship:
            oi_id = it.get("order_item_id")
            raw_qty = float(it.get("quantity") or 0)
            if raw_qty <= 0:
                continue

            meta = items_map.get(str(oi_id))
            if meta and meta.get("product_id"):
                pid  = meta["product_id"]
                mult = int(meta.get("units") or 1)
                qty_by_product[pid] = qty_by_product.get(pid, 0.0) + raw_qty * mult
            else:
                unresolved.append({"order_item_id": oi_id, "quantity": raw_qty})

        if unresolved:
            self.message_post(
                body="Shippingbo shipment (id=%s) : %s item(s) non résolus : %s"
                     % (shipment_id, len(unresolved), unresolved)
            )

        if not qty_by_product:
            # button_validate would otherwise validate the whole reserved quantity.
            self.message_post(
                body="Shippingbo shipment (id=%s) : aucune quantité exploitable — BL non validé."
                     % shipment_id
            )
            return

        moves = self.move_ids.filtered(lambda m: m.state not in ("done", "cancel"))
        touched = self.env["stock.move"]
        for pid, shipped_qty in qty_by_product.items():
            product_moves = moves.filtered(lambda m: m.product_id.id == pid)
            if not product_moves:
                self.message_post(
                    body="Shippingbo shipment (id=%s) : produit id=%s expédié absent du BL."
                         % (shipment_id, pid)
                )
                continue
            remaining = shipped_qty
            for mv in product_moves:
                take = remaining if mv == product_moves[-1] else min(remaining, mv.product_uom_qty)
                mv.write({"quantity": take, "picked": True})
                remaining -= take
            touched |= product_moves

        (moves - touched).write({"quantity": 0, "picked": False})

        if not self._shippingbo_validate_with_backorder("Shippingbo shipment (id=%s)" % shipment_id):
            return

        self.write({
            "shippingbo_state":       "shipped",
            "shippingbo_shipment_id": shipment_id or 0,
        })

        # The backorder stays attached to the same Shippingbo order: no new send.
        backorders = self.search([("backorder_id", "=", self.id)])
        if backorders:
            backorders.write({
                "shippingbo_order_id":  self.shippingbo_order_id,
                "shippingbo_items_map": self.shippingbo_items_map,
                "shippingbo_state":     "transmitted",
            })
            for bo in backorders:
                bo.message_post(
                    body="Reliquat de %s — rattaché à la commande Shippingbo %s."
                         % (self.name, self.shippingbo_order_id)
                )

    def _shippingbo_validate_with_backorder(self, label, backorder=True):
        """Validate with the quantities set on the moves; the rest goes to a backorder,
        or is cancelled when backorder is False.

        Return True when the picking ends up done.
        """
        ctx = {"shippingbo_skip_auto_send": True}
        try:
            with self.env.cr.savepoint():
                res = self.with_context(**ctx).button_validate()
                if isinstance(res, dict) and res.get("res_model") == "stock.backorder.confirmation":
                    Wizard = self.env["stock.backorder.confirmation"].with_context(
                        dict(res.get("context") or {}, **ctx)
                    )
                    wiz = Wizard.browse(res["res_id"]) if res.get("res_id") else Wizard.create({})
                    wiz.process() if backorder else wiz.process_cancel_backorder()
                elif isinstance(res, dict):
                    self.message_post(
                        body="%s : validation en attente (assistant %s)." % (label, res.get("res_model"))
                    )
        except Exception as e:
            _logger.exception("ShippingBo: auto validation failed for picking %s", self.name)
            self.message_post(body="%s : validation auto échouée — %s" % (label, e))
            return False
        return self.state == "done"

    @staticmethod
    def _shippingbo_set_move_quantities(open_moves, to_receive):
        """to_receive: [(moves, qty in product UoM)]. Untouched open moves go to the backorder."""
        touched = open_moves.browse()
        for targets, qty in to_receive:
            remaining = qty
            for move in targets:
                move_qty = move.product_id.uom_id._compute_quantity(remaining, move.product_uom)
                take = move_qty if move == targets[-1] else min(move_qty, move.product_uom_qty)
                move.write({"quantity": take, "picked": True})
                remaining -= move.product_uom._compute_quantity(take, move.product_id.uom_id)
            touched |= targets
        (open_moves - touched).write({"quantity": 0, "picked": False})

    @api.model
    def cron_shippingbo_poll(self):
        """Fallback for webhooks on supply capsules and return orders."""
        self.cron_shippingbo_poll_supply_capsules()
        self.cron_shippingbo_poll_return_orders()

    def action_cancel_shippingbo(self):
        self.ensure_one()
        if self.picking_type_code == "incoming":
            self._shippingbo_cancel_capsule()
            self._shippingbo_cancel_return_order()
            return
        if not self.shippingbo_order_id:
            return
        res = self.sale_id.update_shippingbo_state(
            self.shippingbo_order_id, "canceled"
        )
        if res.get("detail") == "error":
            self.message_post(body="Shippingbo : annulation échouée.")
        else:
            self.write({"shippingbo_state": "cancelled"})
            self.message_post(body="Shippingbo : commande annulée.")

    @staticmethod
    def _item_line(source_ref, product_ref, product_id, title,
                   quantity, price_cents, tax_cents, currency):
        return {
            "source":              "000",
            "source_ref":          source_ref,
            "product_ref":         product_ref,
            "product_source":      "ODOO",
            "product_source_ref":  product_id,
            "title":               title,
            "quantity":            quantity,
            "price_tax_included_cents":    price_cents,
            "price_tax_included_currency": currency,
            "tax_cents":           tax_cents,
            "tax_currency":        currency,
        }

    @staticmethod
    def _build_address(partner):
        return {
            "lastname":     partner.name or "",
            "place_name":   partner.name or "",
            "street1":      partner.street or "",
            "street2":      partner.street2 or "",
            "zip":          partner.zip or "",
            "city":         partner.city or "",
            "country":      partner.country_id.code or "",
            "phone1":       partner.phone or "",
            "email":        partner.email or "",
            "company_name": partner.name or "",
        }

    def _get_shippingbo_carrier_name(self):
        """Mapped Shippingbo carrier name, falling back to the Odoo carrier name."""
        if not self.carrier_id:
            return None
        mapping = self.env["shippingbo.carrier.mapping"].sudo().search([
            ("odoo_carrier_id", "=", self.carrier_id.id)
        ], limit=1)
        if mapping:
            return mapping.shippingbo_carrier_name
        return self.carrier_id.name

    def _find_odoo_carrier(self, carrier_name):
        """Return the carrier (mapping, then exact, then ilike match), 'multiple' or False."""
        mapping = self.env["shippingbo.carrier.mapping"].sudo().search([
            ("shippingbo_carrier_name", "=", carrier_name)
        ], limit=1)
        if mapping and mapping.odoo_carrier_id:
            return mapping.odoo_carrier_id

        DeliveryCarrier = self.env["delivery.carrier"].sudo()
        exact = DeliveryCarrier.search([("name", "=", carrier_name)], limit=1)
        if exact:
            return exact
        ilike = DeliveryCarrier.search([("name", "ilike", carrier_name)], limit=2)
        if len(ilike) == 1:
            return ilike[0]
        if len(ilike) > 1:
            return "multiple"
        return False
