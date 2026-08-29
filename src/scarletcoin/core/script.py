"""A minimal, non-Turing-complete script language for P2SH outputs.

A redeem script is a short bytecode program evaluated when the coins it protects
are spent.  There are no loops, no conditionals and no backwards jumps, so the
language cannot loop forever: evaluation is bounded by the length of the script.

Supported operations:

* data pushes (``OP_0``, ``OP_PUSHBYTES_N``, ``OP_PUSHDATA1``/``2``, ``OP_1``…
  ``OP_16``);
* ``OP_DUP``, ``OP_EQUAL``, ``OP_EQUALVERIFY``, ``OP_HASH160``;
* ``OP_CHECKSIG`` — verify one signature against one public key;
* ``OP_CHECKMULTISIG`` — m-of-n multisignature, with a clean stack layout (no
  dummy element, unlike Bitcoin's historical off-by-one).

The only context execution needs is the 32-byte signature hash of the input
being spent, supplied by the caller.  ``OP_HASH160`` uses ScarletCoin's
``hash256[:20]`` digest, the same convention as addresses.
"""

from __future__ import annotations

from scarletcoin.crypto.hashing import hash256

__all__ = [
    "MAX_PUBKEYS",
    "MAX_SCRIPT_OPS",
    "MAX_SCRIPT_SIZE",
    "OP_CHECKMULTISIG",
    "OP_CHECKSIG",
    "OP_DUP",
    "OP_EQUAL",
    "OP_EQUALVERIFY",
    "OP_HASH160",
    "OP_PUSHBYTES_MAX",
    "ScriptError",
    "decode_ops",
    "evaluate_script",
    "multisig_redeem",
    "p2pkh_redeem",
    "push_data",
]

MAX_SCRIPT_SIZE = 520
"""Largest redeem script, in bytes."""

MAX_PUBKEYS = 15
"""Most public keys a multisig redeem script may list (fits in 520 bytes)."""

MAX_STACK_ITEMS = 20
"""Largest stack a script may build."""

MAX_SCRIPT_OPS = 200
"""Most operations a single script execution may perform.

Together with :data:`MAX_SCRIPT_SIZE` this bounds the worst-case cost of
evaluating a redeem script.  Even a script built entirely of single-byte
push operations cannot exceed this limit.
"""

OP_0 = 0x00
OP_PUSHBYTES_MAX = 0x4B
OP_PUSHDATA1 = 0x4C
OP_PUSHDATA2 = 0x4D
OP_1 = 0x51
OP_16 = 0x60
OP_DUP = 0x76
OP_EQUAL = 0x87
OP_EQUALVERIFY = 0x88
OP_HASH160 = 0xA9
OP_CHECKSIG = 0xAC
OP_CHECKMULTISIG = 0xAE


class ScriptError(ValueError):
    """Raised when a script cannot be parsed or evaluated."""


def push_data(data: bytes) -> bytes:
    """Encode ``data`` as a push operation."""
    length = len(data)
    if length <= OP_PUSHBYTES_MAX:
        return bytes([length]) + data
    if length <= 0xFF:
        return bytes([OP_PUSHDATA1, length]) + data
    if length <= 0xFFFF:
        return bytes([OP_PUSHDATA2]) + length.to_bytes(2, "little") + data
    raise ScriptError(f"data push of {length} bytes is too large")


def _push_small_int(value: int) -> bytes:
    if not 1 <= value <= 16:
        raise ScriptError(f"multisig threshold {value} is out of range 1..16")
    return bytes([OP_1 - 1 + value])


def p2pkh_redeem(pubkey_hash: bytes) -> bytes:
    """Return the redeem script that pays to ``pubkey_hash``."""
    if len(pubkey_hash) != 20:
        raise ScriptError("a public-key hash must be 20 bytes")
    return (
        bytes([OP_DUP, OP_HASH160]) + push_data(pubkey_hash) + bytes([OP_EQUALVERIFY, OP_CHECKSIG])
    )


def multisig_redeem(pubkeys: list[bytes], threshold: int) -> bytes:
    """Return an m-of-n multisig redeem script.

    Args:
        pubkeys: The compressed public keys (33 bytes each).
        threshold: How many signatures are required.
    """
    if not pubkeys or len(pubkeys) > MAX_PUBKEYS:
        raise ScriptError(f"multisig needs 1..{MAX_PUBKEYS} public keys, got {len(pubkeys)}")
    if not 1 <= threshold <= len(pubkeys):
        raise ScriptError(f"threshold {threshold} does not fit {len(pubkeys)} public keys")
    for key in pubkeys:
        if len(key) != 33 or key[0] not in (0x02, 0x03):
            raise ScriptError("multisig public keys must be 33-byte compressed keys")
    return (
        _push_small_int(threshold)
        + b"".join(push_data(key) for key in pubkeys)
        + _push_small_int(len(pubkeys))
        + bytes([OP_CHECKMULTISIG])
    )


def decode_ops(script: bytes) -> list[tuple[int, bytes]]:
    """Split a script into its ``(opcode, data)`` operations.

    Raises:
        ScriptError: if the script is malformed or truncated.
    """
    if len(script) > MAX_SCRIPT_SIZE:
        raise ScriptError(f"script is {len(script)} bytes, the limit is {MAX_SCRIPT_SIZE}")
    ops: list[tuple[int, bytes]] = []
    offset = 0
    while offset < len(script):
        opcode = script[offset]
        offset += 1
        if opcode <= OP_PUSHBYTES_MAX:
            if offset + opcode > len(script):
                raise ScriptError("script ends in the middle of a data push")
            ops.append((opcode, script[offset : offset + opcode]))
            offset += opcode
        elif opcode == OP_PUSHDATA1:
            if offset + 1 > len(script):
                raise ScriptError("script ends after OP_PUSHDATA1")
            length = script[offset]
            offset += 1
            if offset + length > len(script):
                raise ScriptError("script ends in the middle of a data push")
            ops.append((opcode, script[offset : offset + length]))
            offset += length
        elif opcode == OP_PUSHDATA2:
            if offset + 2 > len(script):
                raise ScriptError("script ends after OP_PUSHDATA2")
            length = int.from_bytes(script[offset : offset + 2], "little")
            offset += 2
            if offset + length > len(script):
                raise ScriptError("script ends in the middle of a data push")
            ops.append((opcode, script[offset : offset + length]))
            offset += length
        elif (
            opcode
            in (
                OP_DUP,
                OP_EQUAL,
                OP_EQUALVERIFY,
                OP_HASH160,
                OP_CHECKSIG,
                OP_CHECKMULTISIG,
            )
            or OP_1 <= opcode <= OP_16
        ):
            ops.append((opcode, b""))
        else:
            raise ScriptError(f"unknown script opcode {opcode:#04x}")
    return ops


def _pop(stack: list[bytes]) -> bytes:
    if not stack:
        raise ScriptError("script tried to pop an empty stack")
    return stack.pop()


def _small_int(item: bytes) -> int | None:
    if len(item) != 1 or not 1 <= item[0] <= 16:
        return None
    return item[0]


def _verify_signature(pubkey: bytes, signature: bytes, digest: bytes) -> bool:
    from scarletcoin.crypto.keys import InvalidKeyError, PublicKey

    try:
        key = PublicKey.from_bytes(pubkey)
    except InvalidKeyError:
        return False
    try:
        return key.verify(digest, signature)
    except (InvalidKeyError, ValueError):
        return False


def evaluate_script(script: bytes, arguments: list[bytes], digest: bytes) -> bool:
    """Run ``script`` with ``arguments`` pre-loaded on the stack.

    Args:
        script: The redeem script.
        arguments: The witness items that follow the script in the input
            (signatures, public keys, …), in order.
        digest: The 32-byte signature hash of the input being spent.

    Returns:
        ``True`` if the script completes with a truthy top-of-stack value.
        ``False`` on any failure: a bad stack operation, an unknown opcode, or a
        falsy result.
    """
    try:
        stack = list(arguments)
        for index, (opcode, data) in enumerate(decode_ops(script), 1):
            if index > MAX_SCRIPT_OPS:
                return False
            if opcode <= OP_PUSHBYTES_MAX or opcode in (OP_PUSHDATA1, OP_PUSHDATA2):
                stack.append(data)
            elif OP_1 <= opcode <= OP_16:
                stack.append(bytes([opcode - OP_1 + 1]))
            elif opcode == OP_DUP:
                stack.append(stack[-1])
            elif opcode == OP_EQUAL:
                a, b = _pop(stack), _pop(stack)
                stack.append(b"\x01" if a == b else b"\x00")
            elif opcode == OP_EQUALVERIFY:
                a, b = _pop(stack), _pop(stack)
                if a != b:
                    return False
            elif opcode == OP_HASH160:
                stack.append(hash256(_pop(stack))[:20])
            elif opcode == OP_CHECKSIG:
                pubkey, signature = _pop(stack), _pop(stack)
                if not _verify_signature(pubkey, signature, digest):
                    return False
                stack.append(b"\x01")
            elif opcode == OP_CHECKMULTISIG:
                key_count = _small_int(_pop(stack))
                if key_count is None or key_count > MAX_PUBKEYS:
                    return False
                pubkeys = [_pop(stack) for _ in range(key_count)]
                threshold = _small_int(_pop(stack))
                if threshold is None or threshold > key_count:
                    return False
                signatures = [_pop(stack) for _ in range(threshold)]
                signatures.reverse()
                pubkeys.reverse()
                matched = 0
                key_index = 0
                for signature in signatures:
                    while key_index < len(pubkeys) and not _verify_signature(
                        pubkeys[key_index], signature, digest
                    ):
                        key_index += 1
                    if key_index >= len(pubkeys):
                        return False
                    matched += 1
                    key_index += 1
                if matched != threshold:
                    return False
                stack.append(b"\x01")
            else:  # pragma: no cover - decode_ops already rejects unknown opcodes
                return False

            if len(stack) > MAX_STACK_ITEMS:
                return False
    except ScriptError:
        return False

    if not stack:
        return False
    return any(byte != 0 for byte in stack[-1])
