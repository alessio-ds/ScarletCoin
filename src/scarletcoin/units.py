"""Converting between ScarletCoin amounts and human-readable strings.

Amounts are always integers internally.  One SCT is ``COIN`` (100 000 000) scar,
the smallest unit; floating point is never used for money.

Byte counts (how big the chain is, how big a block is) are rendered here too, so
the command line, the desktop applications and the block explorer all spell a
size the same way.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from scarletcoin.core.params import COIN
from scarletcoin.core.transaction import MAX_MONEY

__all__ = ["format_amount", "format_bytes", "parse_amount"]

_PLACES = len(str(COIN)) - 1

#: Decimal byte units: 1 kB is 1000 B, as a disk manufacturer would have it.
_BYTE_UNITS = ("B", "kB", "MB", "GB", "TB", "PB")


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


def format_bytes(count: int) -> str:
    """Render a number of bytes the way a person reads it.

    Three significant figures are kept, which is enough to compare two sizes at
    a glance and short enough to fit on a card in the block explorer.

    >>> format_bytes(950)
    '950 B'
    >>> format_bytes(1_536_000)
    '1.54 MB'
    >>> format_bytes(21_500_000_000)
    '21.5 GB'
    """
    if isinstance(count, bool) or not isinstance(count, int):
        raise TypeError("byte counts must be integers")
    sign = "-" if count < 0 else ""
    size = float(abs(count))
    for unit in _BYTE_UNITS:
        if size < 1000 or unit == _BYTE_UNITS[-1]:
            if unit == "B":
                return f"{sign}{int(size)} B"
            places = 2 if size < 10 else (1 if size < 100 else 0)
            return f"{sign}{size:.{places}f} {unit}"
        size /= 1000
    raise AssertionError("unreachable")  # pragma: no cover


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
