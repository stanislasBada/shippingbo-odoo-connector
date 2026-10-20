import logging
from urllib.parse import quote

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class ProductProduct(models.Model):
    _inherit = "product.product"

    shippingbo_product_id = fields.Integer(
        string="Shippingbo Product ID",
        copy=False,
        index=True,
        help="Shippingbo product ID, set by the first synchronization.",
    )
    shippingbo_last_sync = fields.Datetime(
        string="Last Shippingbo Sync",
        copy=False,
    )
    shippingbo_linked_supplier_ids = fields.Char(
        string="Shippingbo Warehouses",
        copy=False,
        help="Shippingbo warehouses (supplier IDs) in which this product has been declared.",
    )

    @api.model
    def cron_shippingbo_sync_products(self):
        products = self.search([
            ("product_tmpl_id.shippingbo_stock_sync", "=", True),
            ("default_code", "!=", False),
        ])
        products._shippingbo_sync_products()

    def _shippingbo_sync_products(self, force=False):
        """Create or update active variants in Shippingbo.

        Without force, only variants modified since their last sync are processed.
        """
        api_client = self.env["shippingbo.api"]
        supplier_ids = self._shippingbo_supplier_ids()
        todo = self if force else self.filtered(lambda p: p._shippingbo_needs_sync(supplier_ids))
        _logger.info("=== ShippingBo product sync: %d variant(s) to process ===", len(todo))

        success, skipped, errors = 0, 0, 0
        for product in todo:
            if not product.default_code:
                skipped += 1
                continue
            try:
                with self.env.cr.savepoint():
                    done = product._shippingbo_sync_one(api_client)
                if done:
                    success += 1
                else:
                    skipped += 1
            except Exception as e:
                _logger.error("ShippingBo product sync failed for [%s]: %s", product.default_code, e)
                errors += 1

        _logger.info(
            "=== ShippingBo product sync complete: %d synced / %d skipped / %d errors ===",
            success, skipped, errors,
        )

    def _shippingbo_needs_sync(self, supplier_ids=()):
        self.ensure_one()
        last = self.shippingbo_last_sync
        return (
            not last or last < self.write_date or last < self.product_tmpl_id.write_date
            or bool(set(supplier_ids) - set(self._shippingbo_linked_suppliers()))
        )

    @api.model
    def _shippingbo_supplier_ids(self):
        raw = self.env["ir.config_parameter"].sudo().get_param("shippingbo.supplier_ids") or ""
        return [int(x) for x in raw.replace(";", ",").split(",") if x.strip().isdigit()]

    def _shippingbo_linked_suppliers(self):
        return [int(x) for x in (self.shippingbo_linked_supplier_ids or "").split(",") if x.isdigit()]

    def _shippingbo_link_suppliers(self, api_client, sbo_id):
        """Declare the product in each configured Shippingbo warehouse (supplier).

        The warehouse creates the product when its catalog auto-creation option is enabled.
        """
        linked = self._shippingbo_linked_suppliers()
        for supplier_id in self._shippingbo_supplier_ids():
            if supplier_id in linked:
                continue
            status, res = api_client._shippingbo_call("POST", "/supplier_products", {
                "supplier_product": {
                    "product_id":   sbo_id,
                    "supplier_id":  supplier_id,
                    "supplier_ref": self.default_code,
                }
            })
            # A refusal usually means the link already exists: only then check it.
            if status >= 300:
                existing = api_client._shippingbo_request(
                    "GET", "/supplier_products?search[supplier_id__eq]=%s&search[product_id__eq]=%s"
                    % (supplier_id, sbo_id)
                )
                if not (existing or {}).get("supplier_products"):
                    raise ValueError("warehouse %s link failed: %s" % (supplier_id, res))
            linked.append(supplier_id)
        self.shippingbo_linked_supplier_ids = ",".join(str(x) for x in linked) or False

    def _shippingbo_sync_one(self, api_client):
        self.ensure_one()
        sbo_id = self.shippingbo_product_id or self._shippingbo_find_product_id(api_client)

        if not self.active:
            return False

        vals = self._shippingbo_product_vals()
        if sbo_id:
            res = api_client._shippingbo_request("PATCH", f"/products/{sbo_id}", vals)
        else:
            res = api_client._shippingbo_request("POST", "/products", vals)
            sbo_id = self._shippingbo_extract_id(res)

        if not self._shippingbo_extract_id(res):
            raise ValueError("unexpected Shippingbo response: %s" % res)

        self.write({"shippingbo_product_id": sbo_id})
        self._shippingbo_link_suppliers(api_client, sbo_id)
        self.write({"shippingbo_last_sync": fields.Datetime.now()})
        return True

    def _shippingbo_find_product_id(self, api_client):
        res = api_client._shippingbo_request(
            "GET", "/products?search[user_ref__eq]=%s" % quote(self.default_code, safe="")
        )
        found = (res or {}).get("products") or []
        return found[0].get("id") if found else False

    def _shippingbo_product_vals(self):
        return {
            "user_ref": self.default_code,
            "title": self.with_context(display_default_code=False).display_name,
            "ean13": self.barcode or None,
            # kg -> g
            "weight": int(round((self.weight or 0.0) * 1000)),
        }

    @staticmethod
    def _shippingbo_extract_id(res):
        if not isinstance(res, dict):
            return False
        return res.get("id") or (res.get("product") or {}).get("id")
