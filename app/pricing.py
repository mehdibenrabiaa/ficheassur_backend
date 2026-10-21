"""What FicheAssur sells leads for. PLACEHOLDER prices -- set the real ones
before going live. Amounts in cents, charged to the card as they are (no VAT
added on top yet)."""

# Market -> (shared price, exclusive price), in cents.
PRICES_CENTS: dict[str, tuple[int, int]] = {
    "AUTO": (1500, 4000),
    "MOTO": (1200, 3500),
    "AUTO_PRO": (2500, 6000),
    "COMMERCIAL_AUTO": (2000, 5000),
}

# A shared lead is sold to at most this many brokers; an exclusive one to one.
MAX_SHARED_BUYERS = 3

# Leads older than this leave the marketplace.
SHELF_DAYS = 30

# How long the leads of an unpaid order stay reserved for that broker while
# they're on the payment page (Stripe's minimum session length is 30 min).
HOLD_MINUTES = 30


def price_cents(insurance_type: str, exclusive: bool) -> int:
    shared, excl = PRICES_CENTS[insurance_type]
    return excl if exclusive else shared
