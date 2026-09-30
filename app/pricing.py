"""Plan prices and VAT — the one source for every amount shown or charged.

Prices are set excluding VAT (the B2B convention; businesses deduct it). Since
Datamynt AS is VAT-registered (from 2026-08-04), every charge adds 25 % MVA, and
every price shown to the public also shows the total including VAT, because
private persons may buy too (prisopplysningsforskriften: consumers must see the
total price).

Stripe adds the VAT itself through a TaxRate (STRIPE_TAX_RATE), Vipps charges
the total we send (incl()), and an annual invoice is 10 months (2 months free).
"""

from __future__ import annotations

VAT_RATE = 0.25
ANNUAL_MONTHS = 10  # «årlig mot faktura = 2 måneder gratis»

# øre per month, excluding VAT
PLAN_ORE = {"liten": 9900, "vekst": 24900, "pro": 59900}
PLAN_NAMES = {"liten": "Liten", "vekst": "Vekst", "pro": "Pro"}


def incl(ore: int) -> int:
    """Amount including VAT, in øre."""
    return round(ore * (1 + VAT_RATE))


def vat(ore: int) -> int:
    return incl(ore) - ore


def kr(ore: int) -> str:
    """Norwegian formatting: 123,75 · 990 · 1 237,50 (narrow thin spaces as thousands)."""
    whole, frac = divmod(int(ore), 100)
    s = f"{whole:,}".replace(",", " ")
    return s if frac == 0 else f"{s},{frac:02d}"


def annual_ore(plan: str) -> int:
    """Annual invoice amount excluding VAT."""
    return PLAN_ORE[plan] * ANNUAL_MONTHS
