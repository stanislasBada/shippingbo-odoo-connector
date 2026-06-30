# ShippingBo Connector for Odoo

**Seamlessly connect your Odoo sales orders to ShippingBo's fulfillment platform.**

---

## Overview

The ShippingBo Connector bridges Odoo and ShippingBo, enabling you to push orders to ShippingBo, update their status, manage order items, and automatically synchronize stock levels back into Odoo — all without leaving your Odoo environment.

The module is intentionally **lightweight and generic**: it exposes a clean Python interface to the ShippingBo API directly on the `sale.order` model, and lets you wire up your own business logic through **Odoo Server Actions**, automations, or custom modules.

---

## Key Features

- **Order creation** — push a sales order to ShippingBo with shipping and billing addresses in a single call
- **Order state management** — update a ShippingBo order's lifecycle state (e.g. cancel, hold) from Odoo
- **Order item updates** — modify quantities, remove products, or strip digital items from a ShippingBo order
- **Nightly stock synchronization** — a scheduled cron pulls ShippingBo stock levels and updates `stock.quant` records in Odoo automatically
- **Transparent OAuth handling** — tokens are refreshed automatically; failed requests due to token expiry are retried transparently

---

## How It Works

The module extends `sale.order` and exposes the following methods:

```
send_to_shippingbo(payload)          # Push an order to ShippingBo
update_shippingbo_state(order_id, state)   # Update order state
update_shippingbo_items(order_id, payload) # Modify order items
sync_stock_from_shippingbo()         # Sync stock (run by cron)
```

All HTTP calls are routed through an internal `_shippingbo_request()` wrapper that handles authentication, headers, logging, and retries.

---

## Configuration

Credentials are stored in Odoo's **System Parameters** (`ir.config_parameter`):

| Key                           | Description                     |
| ----------------------------- | ------------------------------- |
| `shippingbo.client_id`        | OAuth client ID                 |
| `shippingbo.client_secret`    | OAuth client secret             |
| `shippingbo.refresh_token`    | OAuth refresh token             |
| `shippingbo.access_token`     | Active access token (managed)   |
| `shippingbo.token_expiration` | Token expiration timestamp      |
| `shippingbo.redirect_uri`     | OAuth redirect URI              |
| `shippingbo.app_id`           | ShippingBo application ID       |

The module checks token expiry before each request, refreshes automatically via:

```
POST https://oauth.shippingbo.com/oauth/token
```

And retries the original request on a `401` response.

---

## API Reference

### Base URL

```
https://app.shippingbo.com
```

### Request Headers

```
Authorization: Bearer <token>
X-API-APP-ID: <app_id>
X-API-VERSION: 1
Content-Type: application/json
```

---

### `send_to_shippingbo(payload)`

Pushes a sales order to ShippingBo. Internally:

1. Creates the shipping address
2. Creates the billing address
3. Retrieves the address IDs
4. Creates the order

**Endpoints:** `POST /addresses`, `POST /orders`

**Expected payload structure:**

```json
{
    "order": {
        "shipping_address": { ... },
        "billing_address": { ... }
    }
}
```

---

### `update_shippingbo_state(order_id, state)`

Updates the state of an existing ShippingBo order.

**Endpoint:** `PATCH /orders/{order_id}`

**Payload:**

```json
{
    "state": "canceled"
}
```

Any state accepted by the ShippingBo API is valid.

---

### `update_shippingbo_items(order_id, payload)`

Replaces or updates the item lines on a ShippingBo order. Useful for removing products, adjusting quantities, or stripping digital items before fulfillment.

**Endpoint:** `POST /orders/{order_id}/update_order_items`

**Payload:**

```json
{
    "order_items": [ ... ]
}
```

---

### `sync_stock_from_shippingbo()`

Scheduled nightly by cron. For each active stock quant in location ID 8 (without a lot), it queries ShippingBo by `default_code` and updates the quantity on hand in Odoo.

**Endpoint:** `GET /products?search[user_ref_eq]={default_code}`

**Stock field returned by ShippingBo:** `stock`

**Rules:**
- Only products with a `default_code` (internal reference) are synced
- Quants with a lot (`lot_id`) are skipped
- Archived products are skipped
- If the quantity is already up to date, the quant is not written

**Sample log output:**

```
Shippingbo sync: [REF001] My Product: 10 → 8 units
```

---

## Logging

All API interactions are logged at the `info` level:

```
Shippingbo POST /orders payload=...
Shippingbo order created 123
Shippingbo state updated 123 -> canceled
Shippingbo items updated 123
Starting ShippingBo → Odoo stock sync
Sync complete: X success / Y errors
```

API errors are also logged with full response details.

---

## Design Philosophy

This module is intentionally **minimal**. It provides only a thin Python wrapper around the ShippingBo API — no complex product mapping, no opinionated business logic, no heavy UI.

Business logic belongs in:

- **Odoo Server Actions** (call these methods from automation rules)
- **Odoo Automations** (trigger on record events)
- **Project-specific modules** (extend this module for your own use cases)

---

## Dependencies

- `sale` — extends `sale.order`
- `stock` — updates `stock.quant`
- `requests` — HTTP client for ShippingBo API calls
