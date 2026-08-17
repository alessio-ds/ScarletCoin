"""BIP-0039 mnemonic sentences and their seeds.

A mnemonic is a human-friendly encoding of 128-256 bits of entropy: a checksum
of ``entropy_length / 32`` bits is appended, the result is cut into 11-bit
chunks, and every chunk picks a word from a fixed list of 2048 words.  The seed
is PBKDF2-HMAC-SHA512 over the sentence, so the same words always produce the
same seed and any typo is caught by the checksum.

Only the English word list is bundled; it is the one every BIP-0039 tool
interoperates with.
"""

from __future__ import annotations

import functools
import hashlib
import os
import unicodedata
from importlib import resources

__all__ = [
    "ENTROPY_BITS",
    "MnemonicError",
    "entropy_to_mnemonic",
    "generate_mnemonic",
    "mnemonic_to_seed",
    "word_count",
]

#: Entropy sizes BIP-0039 allows, mapped to their word counts.
ENTROPY_BITS = {128: 12, 160: 15, 192: 18, 224: 21, 256: 24}

_WORDLIST_FILE = "wordlist/english.txt"


class MnemonicError(ValueError):
    """Raised when a mnemonic sentence is malformed or has a bad checksum."""


@functools.lru_cache(maxsize=1)
def _wordlist() -> tuple[str, ...]:
    text = resources.files("scarletcoin.crypto").joinpath(_WORDLIST_FILE).read_text("utf-8")
    words = tuple(line.strip() for line in text.splitlines() if line.strip())
    if len(words) != 2048:
        raise RuntimeError(f"BIP-0039 word list has {len(words)} words, expected 2048")
    return words


def word_count(strength: int) -> int:
    """Number of words a mnemonic of ``strength`` entropy bits produces."""
    try:
        return ENTROPY_BITS[strength]
    except KeyError:
        raise ValueError(f"entropy strength must be one of {sorted(ENTROPY_BITS)}") from None


def generate_mnemonic(strength: int = 128) -> str:
    """Return a new mnemonic sentence from the operating system's CSPRNG."""
    return entropy_to_mnemonic(os.urandom(strength // 8))


def entropy_to_mnemonic(entropy: bytes) -> str:
    """Encode ``entropy`` (16-32 bytes) as a BIP-0039 mnemonic sentence."""
    if len(entropy) not in {bits // 8 for bits in ENTROPY_BITS}:
        raise ValueError(f"entropy must be {sorted(ENTROPY_BITS)} bits, got {len(entropy) * 8}")
    checksum_bits = len(entropy) // 4
    bits = int.from_bytes(entropy, "big") << checksum_bits
    bits |= hashlib.sha256(entropy).digest()[0] >> (8 - checksum_bits)
    total = len(entropy) * 8 + checksum_bits
    wordlist = _wordlist()
    indices = [(bits >> shift) & 0x7FF for shift in range(total - 11, -1, -11)]
    return " ".join(wordlist[index] for index in indices)


def mnemonic_to_seed(mnemonic: str, passphrase: str = "") -> bytes:
    """Derive the 64-byte BIP-0039 seed from a mnemonic sentence."""
    validate_mnemonic(mnemonic)
    sentence = unicodedata.normalize("NFKD", " ".join(mnemonic.split()))
    salt = unicodedata.normalize("NFKD", "mnemonic" + passphrase)
    return hashlib.pbkdf2_hmac("sha512", sentence.encode("utf-8"), salt.encode("utf-8"), 2048)


def validate_mnemonic(mnemonic: str) -> None:
    """Check that ``mnemonic`` is a well-formed BIP-0039 sentence.

    Raises:
        MnemonicError: if a word is unknown, the word count is wrong, or the
            checksum does not match.
    """
    words = mnemonic.split()
    if len(words) not in ENTROPY_BITS.values():
        raise MnemonicError(
            f"a mnemonic has {sorted(ENTROPY_BITS.values())} words, got {len(words)}"
        )
    wordlist = _wordlist()
    indices: list[int] = []
    for word in words:
        try:
            indices.append(wordlist.index(word))
        except ValueError:
            raise MnemonicError(f"unknown mnemonic word: {word!r}") from None
    strength = next(bits for bits, count in ENTROPY_BITS.items() if count == len(words))
    checksum_bits = strength // 32
    value = 0
    for index in indices:
        value = (value << 11) | index
    entropy = (value >> checksum_bits).to_bytes(strength // 8, "big")
    checksum = value & ((1 << checksum_bits) - 1)
    expected = hashlib.sha256(entropy).digest()[0] >> (8 - checksum_bits)
    if checksum != expected:
        raise MnemonicError("the mnemonic's checksum does not match its words")
