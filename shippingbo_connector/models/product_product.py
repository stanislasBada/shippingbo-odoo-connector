import logging
from urllib.parse import quote

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

# TODO: confirmer le champ d'archivage côté Shippingbo sur l'environnement de test.
SHIPPINGBO_PRODUCT_ARCHIVE_VALS = {"is_active": False}


class ProductProduct(models.Model):
    _inherit = "product.product"

    shippingbo_product_id = fields.Integer(
        string="Shippingbo Product ID",
        copy=False,
        index=True,
        help="ID du produit côté Shippingbo, posé à la première synchronisation.",
    )
    shippingbo_last_sync = fields.Datetime(
        string="Dernière synchro Shippingbo",
        copy=False,
    )

    # ------------------------------------------------------------------
    # Synchronisation référentiel articles Odoo → Shippingbo
    # ------------------------------------------------------------------

    @api.model
    def cron_shippingbo_sync_products(self):
        products = self.with_context(active_test=False).search([
            ("product_tmpl_id.shippingbo_stock_sync", "=", True),
            ("default_code", "!=", False),
        ])
        products._shippingbo_sync_products()

    def _shippingbo_sync_products(self, force=False):
        """Crée / met à jour / archive les variantes dans Shippingbo.
        Seules les variantes modifiées depuis la dernière synchro sont traitées.
        """
        api_client = self.env["shippingbo.api"]
        todo = self if force else self.filtered(lambda p: p._shippingbo_needs_sync())
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

    def _shippingbo_needs_sync(self):
        self.ensure_one()
        last = self.shippingbo_last_sync
        return not last or last < self.write_date or last < self.product_tmpl_id.write_date

    def _shippingbo_sync_one(self, api_client):
        """Retourne True si un appel de création / mise à jour / archivage a abouti."""
        self.ensure_one()
        sbo_id = self.shippingbo_product_id or self._shippingbo_find_product_id(api_client)

        if self.active:
            vals = self._shippingbo_product_vals()
            if sbo_id:
                res = api_client._shippingbo_request("PATCH", f"/products/{sbo_id}", vals)
            else:
                res = api_client._shippingbo_request("POST", "/products", vals)
                sbo_id = self._shippingbo_extract_id(res)
        elif sbo_id:
            res = api_client._shippingbo_request(
                "PATCH", f"/products/{sbo_id}", SHIPPINGBO_PRODUCT_ARCHIVE_VALS
            )
        else:
            # Archivé dans Odoo et inconnu de Shippingbo : rien à faire
            self.shippingbo_last_sync = fields.Datetime.now()
            return False

        if not self._shippingbo_extract_id(res):
            raise ValueError("réponse Shippingbo inattendue : %s" % res)

        self.write({
            "shippingbo_product_id": sbo_id,
            "shippingbo_last_sync": fields.Datetime.now(),
        })
        return True

    def _shippingbo_find_product_id(self, api_client):
        """Recherche par user_ref pour éviter les doublons."""
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
            # Odoo en kg, Shippingbo en grammes
            "weight": int(round((self.weight or 0.0) * 1000)),
        }

    @staticmethod
    def _shippingbo_extract_id(res):
        if not isinstance(res, dict):
            return False
        return res.get("id") or (res.get("product") or {}).get("id")
