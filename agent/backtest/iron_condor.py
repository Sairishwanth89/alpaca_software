"""Iron condor pricing: purely synthetic, delta-targeted via Black-Scholes. Use when
you have a vol input (quoted IV or a realized-vol proxy) but not a real chain snapshot
for every strike — this is what the historical backtest uses, since full historical
options chains aren't reliably available for a broad watchlist.

Returns an IronCondorLegs so downstream backtest/metrics code has a uniform shape.
"""
from dataclasses import dataclass
from datetime import date
from typing import Optional

from agent.options_pricing import bs_price, bs_delta, strike_for_delta, RISK_FREE_RATE


@dataclass
class OptionLegPrice:
    option_type: str          # "call" | "put"
    strike: float
    side: str                 # "sell" | "buy"
    price: float
    is_real_quote: bool       # True if sourced from a real chain snapshot, False if BSM-theoretical
    delta: Optional[float] = None


@dataclass
class IronCondorLegs:
    short_put: OptionLegPrice
    long_put: OptionLegPrice
    short_call: OptionLegPrice
    long_call: OptionLegPrice
    net_credit: float          # $ per share (multiply by 100 for $/contract)
    max_loss: float            # $ per contract (already x100)
    real_quoted_legs: int      # 0-4, how many legs actually came from real quotes
    expiry: Optional[date] = None

    @property
    def legs(self) -> list:
        return [self.short_put, self.long_put, self.short_call, self.long_call]


def _bounded_max_loss(K_short_put, K_long_put, K_short_call, K_long_call, net_credit) -> float:
    put_width = K_short_put - K_long_put
    call_width = K_long_call - K_short_call
    return max(max(put_width, call_width) - net_credit, 0.0) * 100


def price_iron_condor(
    S: float,
    T: float,
    sigma: float,
    r: float = RISK_FREE_RATE,
    short_delta_target: float = 0.16,
    long_delta_target: float = 0.08,
    long_put_delta_target: Optional[float] = None,
    long_call_delta_target: Optional[float] = None,
    expiry: Optional[date] = None,
) -> IronCondorLegs:
    """Delta-targeted iron condor, priced purely from Black-Scholes."""
    lp_target = long_put_delta_target if long_put_delta_target is not None else long_delta_target
    lc_target = long_call_delta_target if long_call_delta_target is not None else long_delta_target

    K_short_put = strike_for_delta(S, T, sigma, "put", -short_delta_target, r)
    K_long_put = strike_for_delta(S, T, sigma, "put", -lp_target, r)
    K_short_call = strike_for_delta(S, T, sigma, "call", short_delta_target, r)
    K_long_call = strike_for_delta(S, T, sigma, "call", lc_target, r)

    # Guarantee well-ordered, non-degenerate strikes (long legs strictly further OTM).
    K_long_put = min(K_long_put, K_short_put - 0.5)
    K_long_call = max(K_long_call, K_short_call + 0.5)

    def leg(option_type, K, side):
        price = bs_price(S, K, T, sigma, option_type, r)
        delta = bs_delta(S, K, T, sigma, option_type, r)
        return OptionLegPrice(option_type, round(K, 2), side, round(price, 4), is_real_quote=False, delta=delta)

    short_put = leg("put", K_short_put, "sell")
    long_put = leg("put", K_long_put, "buy")
    short_call = leg("call", K_short_call, "sell")
    long_call = leg("call", K_long_call, "buy")

    net_credit = (short_put.price - long_put.price) + (short_call.price - long_call.price)
    max_loss = _bounded_max_loss(K_short_put, K_long_put, K_short_call, K_long_call, net_credit)

    return IronCondorLegs(short_put, long_put, short_call, long_call, net_credit, max_loss, 0, expiry)


@dataclass
class ChainQuote:
    strike: float
    option_type: str   # "call" | "put"
    price: float        # mid price
    expiry: Optional[date] = None
    bid: Optional[float] = None
    ask: Optional[float] = None
    symbol: Optional[str] = None       # real OCC symbol, needed to actually place an order
    delta: Optional[float] = None      # Alpaca-reported greek, when available — preferred over a BSM estimate
    implied_vol: Optional[float] = None  # Alpaca-reported IV, when available


def nearest_expiry(available_expiries: list, target_dte: int, today: Optional[date] = None) -> Optional[date]:
    """Pick the available expiry whose DTE is closest to `target_dte`."""
    if not available_expiries:
        return None
    today = today or date.today()
    return min(available_expiries, key=lambda e: abs((e - today).days - target_dte))


