class ShippingboRetryLater(Exception):
    """The webhook cannot be applied yet: the webhook answers 503 so that Shippingbo resends it."""
