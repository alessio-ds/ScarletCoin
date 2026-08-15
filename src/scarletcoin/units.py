"""Converting between ScarletCoin amounts and human-readable strings.

Amounts are always integers internally.  One SCT is ``COIN`` (100 000 000) scar,
the smallest unit; floating point is never used for money.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from scarletcoin.core.params import COIN
from scarletcoin.core.transaction import MAX_MONEY

__all__ = ["format_amount", "parse_amount"]

_PLACES = len(str(COIN)) - 1


def format_amount(scar: int, *, symbol: bool = False) -> str:
    """Render an integer amount of scar as a decimal SCT string.

    >>> format_amount(1_234_500_000)
    '12.345'
    """
    if not isinstance(scar, int):
        raise TypeError("amounts must be integers")
    sign = "-" if scar < 0 else ""
    whole, fraction = divmod(abs(scar), COIN)
    text = f"{sign}{whole}"
    if fraction:
        text += f".{fraction:0{_PLACES}d}".rstrip("0")
    return f"{text} SCT" if symbol else text


def parse_amount(text: str) -> int:
    """Parse a decimal SCT string into an integer number of scar.

    Raises:
        ValueError: if the text is not a number, has too many decimals, is
            negative, or exceeds the maximum money supply.
    """
    cleaned = str(text).strip().removesuffix("SCT").strip()
    if not cleaned:
        raise ValueError("no amount given")
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        raise ValueError(f"{text!r} is not a valid amount") from None
    if value < 0:
        raise ValueError("amounts must not be negative")
    scaled = value * COIN
    if scaled != scaled.to_integral_value():
        raise ValueError(f"{text!r} has more than {_PLACES} decimal places")
    scar = int(scaled)
    if scar > MAX_MONEY:
        raise ValueError("amount exceeds the maximum money supply")
    return scar
