"""The built-in block explorer.

Server-side rendered HTML with no external assets, so a node is browsable with
nothing but a browser pointed at its RPC port.  Every value that comes from the
chain is HTML-escaped: block and transaction data is attacker-controlled.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from html import escape
from typing import TYPE_CHECKING

from scarletcoin.core.transaction import Transaction
from scarletcoin.units import format_amount, format_bytes

if TYPE_CHECKING:  # pragma: no cover - import cycle only matters for type checking
    from scarletcoin.net.rpc import RpcServer

__all__ = ["NotFound", "render", "render_error"]

_STYLE = """
:root {
  --bg: #12100f; --panel: #1b1817; --line: #2e2825; --text: #e8e2df;
  --muted: #97877f; --accent: #e33a4e; --accent-soft: #f0a0a8; --good: #6fc98b;
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--text);
  font: 15px/1.55 ui-monospace, "SFMono-Regular", Menlo, Consolas, monospace;
}
a { color: var(--accent-soft); text-decoration: none; }
a:hover { text-decoration: underline; }
header {
  border-bottom: 1px solid var(--line); padding: 18px 24px; display: flex;
  gap: 24px; align-items: center; flex-wrap: wrap;
}
header h1 { font-size: 20px; margin: 0; letter-spacing: 0.08em; }
header h1 a { color: var(--accent); }
header nav { display: flex; gap: 16px; font-size: 14px; }
main { padding: 24px; max-width: 1100px; margin: 0 auto; }
h2 { font-size: 16px; text-transform: uppercase; letter-spacing: 0.12em;
     color: var(--muted); margin: 32px 0 12px; }
h2:first-child { margin-top: 0; }
form { display: flex; gap: 8px; margin-left: auto; }
input[type=text] {
  background: var(--panel); border: 1px solid var(--line); color: var(--text);
  padding: 7px 10px; border-radius: 6px; min-width: 320px; font: inherit;
}
button {
  background: var(--accent); border: 0; color: #fff; padding: 7px 14px;
  border-radius: 6px; cursor: pointer; font: inherit;
}
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px; }
.card { background: var(--panel); border: 1px solid var(--line);
        border-radius: 8px; padding: 14px; }
.card .label { color: var(--muted); font-size: 12px; text-transform: uppercase;
               letter-spacing: 0.1em; }
.card .value { font-size: 19px; margin-top: 6px; word-break: break-all; }
.card .sub { font-size: 12px; color: var(--muted); margin-top: 3px; }
table { width: 100%; border-collapse: collapse; background: var(--panel);
        border: 1px solid var(--line); border-radius: 8px; overflow: hidden; }
th, td { text-align: left; padding: 9px 12px; border-bottom: 1px solid var(--line);
         font-size: 14px; vertical-align: top; }
th { color: var(--muted); font-weight: 600; text-transform: uppercase; font-size: 12px;
     letter-spacing: 0.08em; }
tr:last-child td { border-bottom: 0; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
.hash { word-break: break-all; }
.tag { display: inline-block; padding: 1px 7px; border-radius: 999px; font-size: 12px;
       border: 1px solid var(--line); color: var(--muted); }
.tag.ok { color: var(--good); border-color: var(--good); }
.tag.warn { color: var(--accent); border-color: var(--accent); }
.amount { color: var(--good); }
footer { color: var(--muted); font-size: 12px; padding: 24px; text-align: center; }
.empty { color: var(--muted); padding: 12px 0; }
"""


class NotFound(Exception):
    """Raised when a page or object does not exist."""


def _page(server: RpcServer, title: str, body: str) -> str:
    node = server.node
    network = escape(node.params.name)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)} - ScarletCoin explorer</title>
<style>{_STYLE}</style>
</head>
<body>
<header>
  <h1><a href="/">ScarletCoin</a></h1>
  <nav>
    <a href="/">Overview</a>
    <a href="/blocks">Blocks</a>
    <a href="/mempool">Mempool</a>
    <a href="/peers">Peers</a>
  </nav>
  <form action="/search" method="get">
    <input type="text" name="q" placeholder="block height, hash or txid" required>
    <button type="submit">Search</button>
  </form>
</header>
<main>{body}</main>
<footer>
  ScarletCoin node on the {network} network &mdash; block explorer served by the node itself<br>
  <a href="https://github.com/alessio-ds/ScarletCoin">github.com/alessio-ds/ScarletCoin</a>
</footer>
</body>
</html>
"""


@dataclass(frozen=True, slots=True)
class Cell:
    """One table cell: markup that is ready to render, plus its alignment.

    Cells are built with :func:`_text` (which escapes) or :func:`_html` (which
    does not), so whether a value has been escaped is decided once, where the
    value is known, and never guessed by the renderer.
    """

    html: str
    numeric: bool = False


def _text(value: object, *, numeric: bool = False) -> Cell:
    """A cell holding plain text, HTML-escaped."""
    return Cell(escape(str(value)), numeric)


def _html(markup: str, *, numeric: bool = False) -> Cell:
    """A cell holding markup built by this module (a link, an amount, a tag)."""
    return Cell(markup, numeric)


def _cards(items: list[tuple[str, str]]) -> str:
    """Render a row of summary cards.  Labels are escaped, values are markup."""
    cards = "".join(
        f'<div class="card"><div class="label">{escape(label)}</div>'
        f'<div class="value">{value}</div></div>'
        for label, value in items
    )
    return f'<div class="cards">{cards}</div>'


def _rows(header: list[str], rows: list[list[Cell]], *, empty: str = "Nothing here yet.") -> str:
    """Render a table.  A column name starting with ``#`` is right-aligned."""
    if not rows:
        return f'<p class="empty">{escape(empty)}</p>'
    head = "".join(
        f'<th class="num">{escape(name[1:])}</th>'
        if name.startswith("#")
        else f"<th>{escape(name)}</th>"
        for name in header
    )
    body = "".join(
        "<tr>"
        + "".join(
            f'<td class="num">{cell.html}</td>' if cell.numeric else f"<td>{cell.html}</td>"
            for cell in row
        )
        + "</tr>"
        for row in rows
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _height_link(height: int) -> str:
    """A link to a block by height."""
    return f'<a href="/block/{height:d}">{height:d}</a>'


def _short(text: str, keep: int = 16) -> str:
    return text if len(text) <= keep * 2 else f"{text[:keep]}…{text[-keep:]}"


def _block_link(block_hash: str, *, short: bool = True) -> str:
    label = _short(block_hash) if short else block_hash
    return f'<a class="hash" href="/block/{escape(block_hash)}">{escape(label)}</a>'


def _tx_link(txid: str, *, short: bool = True) -> str:
    label = _short(txid) if short else txid
    return f'<a class="hash" href="/tx/{escape(txid)}">{escape(label)}</a>'


def _when(timestamp: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(timestamp)) + " UTC"


def _hash_rate(rate: float | None) -> str:
    """Render a hash rate with a sensible unit."""
    if rate is None:
        return "&mdash;"
    for unit in ("H/s", "kH/s", "MH/s", "GH/s", "TH/s"):
        if rate < 1000:
            return f"{rate:.2f} {unit}"
        rate /= 1000
    return f"{rate:.2f} PH/s"  # pragma: no cover - optimistic


def _duration(seconds: float | None) -> str:
    """Render a number of seconds the way a person reads it."""
    if seconds is None:
        return "&mdash;"
    if seconds < 10:
        return f"{seconds:.1f} s"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} s"
    if seconds < 3600:
        return f"{seconds // 60} min {seconds % 60} s"
    if seconds < 86400:
        return f"{seconds // 3600} h {seconds % 3600 // 60} min"
    return f"{seconds // 86400} d {seconds % 86400 // 3600} h"


def _amount(scar: int) -> str:
    """An amount in SCT, coloured."""
    return f'<span class="amount">{escape(format_amount(scar))}</span>'


def _weight(info: dict) -> str:
    """How big this blockchain is: serialised blocks, and what they cost on disk.

    The headline number is the serialised active chain, because that is what
    every node on the network has to carry.  The database underneath it is bigger
    — indexes, the UTXO set, SQLite's own bookkeeping — so that goes on the
    second line, where it explains the difference instead of hiding it.

    Every value here is a number this node produced, so there is nothing to
    escape; the separator is deliberate markup.
    """
    parts = [f"{format_bytes(info.get('disk_bytes') or 0)} on disk"]
    average = info.get("average_block_bytes") or 0
    if average:
        parts.append(f"{format_bytes(average)} per block")
    if info.get("pruned_blocks"):
        parts.append(f"pruned to height {int(info['prune_height'])}")
    detail = " &middot; ".join(parts)
    return f'{format_bytes(info.get("chain_bytes") or 0)}<div class="sub">{detail}</div>'


def _tag(label: str, kind: str = "") -> str:
    """A small pill-shaped label, such as "coinbase" or "active chain"."""
    classes = f"tag {kind}".strip()
    return f'<span class="{escape(classes)}">{escape(label)}</span>'


def _hash_span(value: str) -> str:
    """A hash or address rendered so it wraps instead of overflowing."""
    return f'<span class="hash">{escape(value)}</span>'


# --------------------------------------------------------------------------- pages


def _overview(server: RpcServer) -> str:
    node = server.node
    chain = node.chain
    info = node.info()
    blocks = []
    for height in range(chain.height, max(-1, chain.height - 10), -1):
        entry = chain.get_entry_by_height(height)
        if entry is None:  # pragma: no cover
            continue
        block = chain.get_block(entry.hash)
        if block is None:  # pragma: no cover
            continue
        reward = block.coinbase.total_output()
        miner = block.coinbase.outputs[0].one_time_key.hex()
        blocks.append(
            [
                _html(_height_link(height), numeric=True),
                _html(_block_link(entry.hash[::-1].hex())),
                _text(_when(entry.timestamp)),
                _text(len(block.transactions), numeric=True),
                _html(_amount(reward), numeric=True),
                _html(_hash_span(_short(miner))),
            ]
        )
    stats = chain.network_stats()
    pace = "&mdash;"
    if stats["average_spacing"]:
        pace = (
            f"{_duration(stats['average_spacing'])} / block"
            f'<div class="sub">target {_duration(stats["target_spacing"])}</div>'
        )
    change = stats["estimated_difficulty_change"]
    retarget = (
        f"in {stats['blocks_until_retarget']} blocks"
        f'<div class="sub">height {stats["next_retarget_height"]}'
        + ("" if change is None else f", estimated {'+' if change >= 0 else ''}{change:.1f}%")
        + "</div>"
    )

    body = _cards(
        [
            ("Network", escape(info["network"])),
            ("Height", str(info["height"])),
            (
                "Circulating supply",
                _amount(info["supply"])
                + f'<div class="sub">{info["utxo_count"]} unspent outputs</div>',
            ),
            ("Mempool", f"{info['mempool_size']} tx"),
        ]
    )
    body += "<h2>Network</h2>" + _cards(
        [
            ("Block rate", pace),
            ("Hash rate", _hash_rate(stats["hash_rate"])),
            ("Difficulty", f"{stats['difficulty']:.6g}"),
            ("Next retarget", retarget),
            ("Last block", f"{_duration(stats['seconds_since_last_block'])} ago"),
            ("Blocks last hour", str(stats["blocks_last_hour"])),
            ("Blocks last 24 h", str(stats["blocks_last_day"])),
            ("Chain weight", _weight(info)),
            ("Peers", str(info["peers"])),
            ("Node version", escape(info["version"])),
        ]
    )
    body += (
        f'<p class="empty">Measured over the last {stats["window"]} blocks'
        f" ({_duration(stats['window_seconds'])}).</p>"
    )
    body += "<h2>Latest blocks</h2>" + _rows(
        ["#Height", "Hash", "Time", "#Txs", "#Reward", "One-time key"], blocks
    )
    return _page(server, "Overview", body)


def _blocks_page(server: RpcServer, query: dict[str, list[str]]) -> str:
    chain = server.node.chain
    per_page = 50
    try:
        end = int(query.get("from", [chain.height])[0])
    except ValueError:
        raise NotFound("that is not a block height") from None
    end = max(0, min(end, chain.height))
    start = max(0, end - per_page + 1)
    rows = []
    for height in range(end, start - 1, -1):
        entry = chain.get_entry_by_height(height)
        if entry is None:  # pragma: no cover - the active chain has no gaps
            continue
        block = chain.get_block(entry.hash)
        if block is None:
            # Pruned: the row stays, so the list has no holes, but there is
            # nothing left to count.
            rows.append(
                [
                    _html(_height_link(height), numeric=True),
                    _html(_block_link(entry.hash[::-1].hex())),
                    _text(_when(entry.timestamp)),
                    _html(_tag("pruned", "warn"), numeric=True),
                    _text("", numeric=True),
                    _text("", numeric=True),
                ]
            )
            continue
        rows.append(
            [
                _html(_height_link(height), numeric=True),
                _html(_block_link(entry.hash[::-1].hex())),
                _text(_when(entry.timestamp)),
                _text(len(block.transactions), numeric=True),
                _text(block.size(), numeric=True),
                _html(_amount(block.coinbase.total_output()), numeric=True),
            ]
        )
    body = "<h2>Blocks</h2>" + _rows(["#Height", "Hash", "Time", "#Txs", "#Bytes", "#Reward"], rows)
    links = []
    if end < chain.height:
        links.append(f'<a href="/blocks?from={min(chain.height, end + per_page)}">Newer</a>')
    if start > 0:
        links.append(f'<a href="/blocks?from={start - 1}">Older</a>')
    if links:
        body += "<p>" + " &middot; ".join(links) + "</p>"
    return _page(server, "Blocks", body)


def _transaction_rows(server: RpcServer, transaction: Transaction) -> str:
    inputs: list[list[Cell]] = []
    for txin in transaction.inputs:
        if txin.is_coinbase_input:
            inputs.append([_html(_tag("coinbase", "warn")), _text(""), _text(""), _text("")])
            continue
        ring = " &middot; ".join(_short(member.hex(), 8) for member in txin.ring[:4])
        if len(txin.ring) > 4:
            ring += f" &middot; +{len(txin.ring) - 4} more"
        inputs.append(
            [
                _text(len(txin.ring), numeric=True),
                _html(_hash_span(_short(txin.key_image.hex(), 8))),
                _html(ring),
                _text("" if txin.signature else "not signed"),
            ]
        )
    outputs = [
        [
            _text(index, numeric=True),
            _html(_amount(output.value), numeric=True),
            _html(_hash_span(_short(output.one_time_key.hex(), 8))),
        ]
        for index, output in enumerate(transaction.outputs)
    ]
    return (
        "<h2>Inputs</h2>"
        + _rows(["#Ring", "Key image", "Ring members", "Signature"], inputs)
        + "<h2>Outputs</h2>"
        + _rows(["#Index", "#Amount", "One-time key"], outputs)
    )


def _block_page(server: RpcServer, identifier: str) -> str:
    chain = server.node.chain
    entry = None
    if identifier.isdigit():
        entry = chain.get_entry_by_height(int(identifier))
    else:
        try:
            entry = chain.get_entry(bytes.fromhex(identifier)[::-1])
        except ValueError:
            entry = None
    if entry is None:
        raise NotFound("no block with that height or hash")
    block = chain.get_block(entry.hash)
    if block is None and not entry.pruned:  # pragma: no cover
        raise NotFound("block data is missing")

    status = _tag("active chain", "ok") if entry.in_chain else _tag("side branch", "warn")
    if block is None:
        # A pruned block: the header is all this node kept. Say so plainly rather
        # than pretending the block does not exist.
        body = _cards(
            [
                ("Height", str(entry.height)),
                ("Confirmations", str(chain.confirmations(entry.height) if entry.in_chain else 0)),
                ("Time", escape(_when(entry.timestamp))),
                ("Difficulty target", escape(f"{entry.bits:#010x}")),
                ("Nonce", str(entry.header.nonce)),
                ("Body", _tag("pruned", "warn")),
            ]
        )
        body += (
            '<p class="empty">This node has pruned the body of this block: only the '
            "header is still stored, so its transactions cannot be shown here. Ask a "
            "node that keeps the whole chain.</p>"
        )
        body += "<h2>Header</h2>" + _rows(
            ["Field", "Value"],
            [
                [_text("Hash"), _html(f"{_hash_span(entry.hash[::-1].hex())} {status}")],
                [
                    _text("Previous block"),
                    _html(
                        _block_link(entry.prev_hash[::-1].hex(), short=False)
                        if entry.height
                        else "<em>none</em>"
                    ),
                ],
                [_text("Merkle root"), _html(_hash_span(entry.header.merkle_root[::-1].hex()))],
                [_text("Cumulative work"), _text(entry.chainwork)],
            ],
        )
        return _page(server, f"Block {entry.height}", body)

    total_out = sum(tx.total_output() for tx in block.transactions)
    body = _cards(
        [
            ("Height", str(entry.height)),
            ("Confirmations", str(chain.confirmations(entry.height) if entry.in_chain else 0)),
            ("Time", escape(_when(entry.timestamp))),
            ("Transactions", str(len(block.transactions))),
            ("Size", escape(format_bytes(block.size()))),
            ("Difficulty target", escape(f"{entry.bits:#010x}")),
            ("Nonce", str(block.header.nonce)),
            ("Value moved", _amount(total_out)),
        ]
    )
    previous = (
        _block_link(entry.prev_hash[::-1].hex(), short=False) if entry.height else "<em>none</em>"
    )
    children = chain.storage.children_of(entry.hash)
    body += "<h2>Header</h2>" + _rows(
        ["Field", "Value"],
        [
            [_text("Hash"), _html(f"{_hash_span(entry.hash[::-1].hex())} {status}")],
            [_text("Previous block"), _html(previous)],
            [_text("Merkle root"), _html(_hash_span(block.header.merkle_root[::-1].hex()))],
            [
                _text("Next block"),
                _html(
                    ", ".join(_block_link(child.hash[::-1].hex()) for child in children)
                    or "<em>none</em>"
                ),
            ],
            [_text("Cumulative work"), _text(entry.chainwork)],
        ],
    )
    rows = [
        [
            _html(_tx_link(tx.txid_hex())),
            _html(_tag("coinbase", "warn") if tx.is_coinbase else ""),
            _text(len(tx.inputs), numeric=True),
            _text(len(tx.outputs), numeric=True),
            _html(_amount(tx.total_output()), numeric=True),
        ]
        for tx in block.transactions
    ]
    body += "<h2>Transactions</h2>" + _rows(
        ["Transaction id", "Type", "#Inputs", "#Outputs", "#Value"], rows
    )
    return _page(server, f"Block {entry.height}", body)


def _tx_page(server: RpcServer, txid_hex: str) -> str:
    node = server.node
    try:
        txid = bytes.fromhex(txid_hex)[::-1]
    except ValueError:
        raise NotFound("that is not a transaction id") from None
    if len(txid) != 32:
        raise NotFound("a transaction id is 32 bytes long")

    found = node.chain.get_transaction(txid)
    if found is not None:
        transaction, location = found
        entry = node.chain.get_entry(location.block_hash)
        confirmations = node.chain.confirmations(location.height)
        status = (
            _tag(f"{confirmations} confirmations", "ok")
            if entry is not None and entry.in_chain
            else _tag("not on the active chain", "warn")
        )
        block_cell = _block_link(location.block_hash[::-1].hex())
        height = str(location.height)
    else:
        transaction = node.mempool.get(txid)
        if transaction is None:
            raise NotFound("no transaction with that id")
        status = _tag("unconfirmed", "warn")
        block_cell = "<em>in the mempool</em>"
        height = "&mdash;"

    body = _cards(
        [
            ("Transaction id", _hash_span(transaction.txid_hex())),
            ("Status", status),
            ("Block", block_cell),
            ("Height", height),
            ("Size", f"{transaction.size()} bytes"),
            ("Output total", _amount(transaction.total_output())),
        ]
    )
    body += _transaction_rows(server, transaction)
    return _page(server, "Transaction", body)


def _mempool_page(server: RpcServer) -> str:
    entries = server.node.mempool.entries()
    rows = [
        [
            _html(_tx_link(entry.txid[::-1].hex())),
            _text(entry.size, numeric=True),
            _html(_amount(entry.fee), numeric=True),
            _text(f"{entry.fee_rate:.0f}", numeric=True),
            _text(_when(int(entry.received))),
        ]
        for entry in entries
    ]
    body = _cards(
        [
            ("Transactions", str(len(entries))),
            ("Size", f"{server.node.mempool.total_bytes} bytes"),
            ("Total fees", _amount(sum(entry.fee for entry in entries))),
        ]
    )
    body += "<h2>Unconfirmed transactions</h2>" + _rows(
        ["Transaction id", "#Bytes", "#Fee", "#Fee/kB", "Seen"],
        rows,
        empty="The mempool is empty.",
    )
    return _page(server, "Mempool", body)


def _peers_page(server: RpcServer) -> str:
    node = server.node
    rows = [
        [
            _text(peer["address"]),
            _text(peer["direction"]),
            _text(peer["user_agent"] or "unknown"),
            _text(peer["start_height"], numeric=True),
            _text(peer["latency_ms"] if peer["latency_ms"] is not None else "-", numeric=True),
            _text(int(peer["connected_for"]), numeric=True),
        ]
        for peer in (p.to_dict() for p in node.peers)
    ]
    body = _cards(
        [
            ("Connected peers", str(len(rows))),
            ("Known addresses", str(len(node.addrbook))),
            ("Listening port", str(node.p2p_port or "not listening")),
        ]
    )
    body += "<h2>Peers</h2>" + _rows(
        ["Address", "Direction", "User agent", "#Height", "#Latency (ms)", "#Uptime (s)"],
        rows,
        empty="No peers connected.",
    )
    return _page(server, "Peers", body)


def _search(server: RpcServer, query: dict[str, list[str]]) -> str:
    term = (query.get("q") or [""])[0].strip()
    if not term:
        raise NotFound("nothing to search for")
    node = server.node
    if term.isdigit() and int(term) <= node.chain.height:
        return _block_page(server, term)
    if len(term) == 64:
        try:
            raw = bytes.fromhex(term)[::-1]
        except ValueError:
            raise NotFound(f"could not find anything matching {term!r}") from None
        if node.chain.get_entry(raw) is not None:
            return _block_page(server, term)
        if node.chain.get_transaction(raw) is not None or node.mempool.get(raw) is not None:
            return _tx_page(server, term)
    raise NotFound(f"could not find anything matching {term!r}")


def render(server: RpcServer, path: str, query: dict[str, list[str]]) -> str:
    """Render the page for ``path``.

    Raises:
        NotFound: if the path or the object it refers to does not exist.
    """
    if path == "/":
        return _overview(server)
    if path == "/blocks":
        return _blocks_page(server, query)
    if path == "/mempool":
        return _mempool_page(server)
    if path == "/peers":
        return _peers_page(server)
    if path == "/search":
        return _search(server, query)
    for prefix, handler in (
        ("/block/", _block_page),
        ("/tx/", _tx_page),
    ):
        if path.startswith(prefix):
            return handler(server, path[len(prefix) :])
    raise NotFound(f"no page at {path}")


def render_error(server: RpcServer, message: str) -> str:
    """Render a "not found" page."""
    body = f'<h2>Not found</h2><p>{escape(message)}</p><p><a href="/">Back to the overview</a></p>'
    return _page(server, "Not found", body)
