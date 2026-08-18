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
from scarletcoin.crypto.keys import Address, InvalidKeyError
from scarletcoin.units import format_amount, format_bytes

if TYPE_CHECKING:  # pragma: no cover - import cycle only matters for type checking
    from scarletcoin.net.rpc import RpcServer

__all__ = ["NotFound", "render", "render_error"]

#: Maximum number of unspent outputs the address page renders; the rest are
#: summarised, so an address with thousands of coins cannot produce a huge page.
MAX_UNSPENT_ROWS = 200

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
    live_script = _live_reload_script(node)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)} - ScarletCoin explorer</title>
<link rel="icon" type="image/svg+xml" href="/icon.svg">
<style>{_STYLE}</style>
{live_script}
</head>
<body>
<header>
  <h1><a href="/">ScarletCoin</a></h1>
  <nav>
    <a href="/">Overview</a>
    <a href="/blocks">Blocks</a>
    <a href="/hashrate">Hash rate</a>
    <a href="/mempool">Mempool</a>
    <a href="/peers">Peers</a>
    <a href="/rich">Rich list</a>
    <a href="https://alessio-ds.github.io/scarletcoin-web-wallet/">Web wallet</a>
  </nav>
  <form action="/search" method="get">
    <input type="text" name="q" placeholder="block height, hash, txid or address" required>
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


def _live_reload_script(node) -> str:
    """A small script that reloads the page when a new block arrives.

    Returns an empty string when the WebSocket endpoint is not running.
    """
    if not node.config.ws or not node.ws_hub.running or not node.ws_hub.port:
        return ""
    port = node.ws_hub.port
    return f"""<script>
(function () {{
  if (!window.WebSocket) return;
  var connect = function () {{
    var socket = new WebSocket("ws://" + location.hostname + ":{port}/");
    socket.onmessage = function (event) {{
      try {{ if (JSON.parse(event.data).type === "block") location.reload(); }}
      catch (e) {{ }}
    }};
    // Reconnect silently when the link drops; never reload here, or a normal
    // click that navigates to another page would be aborted.
    socket.onclose = function () {{ setTimeout(connect, 2000); }};
  }};
  connect();
}})();
</script>"""


#: The explorer's favicon, shared with the browser wallet.
FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
    '<rect width="64" height="64" rx="14" fill="#12100f"/>'
    '<circle cx="32" cy="32" r="22" fill="none" stroke="#e33a4e" stroke-width="6"/>'
    '<path d="M32 14 L46 56 L32 44 L18 56 Z" fill="#e33a4e"/>'
    "</svg>"
)


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


def _address_link(address: str, *, short: bool = False) -> str:
    label = _short(address, 12) if short else address
    return f'<a class="hash" href="/address/{escape(address)}">{escape(label)}</a>'


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
        miner = str(block.coinbase.outputs[0].address(node.params.address_version))
        blocks.append(
            [
                _html(_height_link(height), numeric=True),
                _html(_block_link(entry.hash[::-1].hex())),
                _text(_when(entry.timestamp)),
                _text(len(block.transactions), numeric=True),
                _html(_amount(reward), numeric=True),
                _html(_address_link(miner, short=True)),
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
        ["#Height", "Hash", "Time", "#Txs", "#Reward", "Miner"], blocks
    )
    return _page(server, "Overview", body)


def _hashrate_chart(history: list[dict]) -> str:
    """Render the hashrate history as an inline SVG line chart.

    No external assets, matching the rest of the explorer: the chart is drawn
    server-side, with the y-axis scaled linearly to the observed peak.
    """
    if not history:
        return '<p class="empty">Not enough history to plot yet.</p>'
    peak = max(point["hash_rate"] for point in history)
    if peak <= 0:
        return '<p class="empty">No measurable hashrate yet.</p>'

    width, height = 1000, 260
    pad_l, pad_r, pad_t, pad_b = 100, 12, 14, 24
    inner_w = width - pad_l - pad_r
    inner_h = height - pad_t - pad_b
    t0 = history[0]["time"]
    span = max(1, history[-1]["time"] - t0)

    def x(point: dict) -> float:
        return pad_l + (point["time"] - t0) / span * inner_w

    def y(point: dict) -> float:
        return pad_t + inner_h - (point["hash_rate"] / peak) * inner_h

    grid = ""
    for fraction in (0.0, 0.25, 0.5, 0.75, 1.0):
        yy = pad_t + inner_h - fraction * inner_h
        grid += (
            f'<line x1="{pad_l}" y1="{yy:.1f}" x2="{width - pad_r}" y2="{yy:.1f}" '
            f'stroke="#2e2825" stroke-width="1"/>'
            f'<text x="{pad_l - 6}" y="{yy + 4:.1f}" fill="#97877f" font-size="11" '
            f'text-anchor="end">{_hash_rate(peak * fraction)}</text>'
        )

    line_points = " ".join(f"{x(p):.1f},{y(p):.1f}" for p in history)
    area = (
        f"{x(history[0]):.1f},{pad_t + inner_h:.1f} "
        f"{line_points} "
        f"{x(history[-1]):.1f},{pad_t + inner_h:.1f}"
    )
    start = time.strftime("%Y-%m-%d", time.gmtime(history[0]["time"]))
    end = time.strftime("%Y-%m-%d", time.gmtime(history[-1]["time"]))

    return (
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="Hash rate history" '
        f'style="width:100%;height:auto;background:#1b1817;border:1px solid #2e2825;'
        f'border-radius:8px;">'
        f"{grid}"
        f'<polygon points="{area}" fill="#e33a4e" opacity="0.12"/>'
        f'<polyline points="{line_points}" fill="none" stroke="#e33a4e" stroke-width="2"/>'
        f'<text x="{pad_l}" y="{height - 6:.1f}" fill="#97877f" font-size="11">{start}</text>'
        f'<text x="{width - pad_r:.1f}" y="{height - 6:.1f}" fill="#97877f" font-size="11" '
        f'text-anchor="end">{end}</text>'
        f"</svg>"
    )


def _hashrate_page(server: RpcServer, query: dict[str, list[str]]) -> str:
    chain = server.node.chain
    window: int | None = None
    points = 240
    if "window" in query:
        try:
            window = int(query["window"][0])
        except ValueError:
            raise NotFound("window must be a number of blocks") from None
    if "points" in query:
        try:
            points = max(1, min(int(query["points"][0]), 1000))
        except ValueError:
            raise NotFound("points must be a number") from None

    history = chain.hashrate_history(window, points)
    window = max(2, window or server.node.params.retarget_interval)

    if history:
        current = history[-1]["hash_rate"]
        peak = max(point["hash_rate"] for point in history)
        average = sum(point["hash_rate"] for point in history) / len(history)
        difficulty_now = history[-1]["difficulty"]
    else:
        current = peak = average = 0.0
        difficulty_now = chain.difficulty()

    body = _cards(
        [
            ("Current hash rate", _hash_rate(current)),
            ("Peak", _hash_rate(peak)),
            ("Average", _hash_rate(average)),
            ("Difficulty", f"{difficulty_now:.6g}"),
            ("Measured over", f"{window} blocks"),
        ]
    )
    body += "<h2>Hash rate history</h2>" + _hashrate_chart(history)

    rows = [
        [
            _html(_height_link(point["height"]), numeric=True),
            _text(_when(point["time"])),
            _html(_hash_rate(point["hash_rate"]), numeric=True),
            _text(f"{point['difficulty']:.6g}", numeric=True),
        ]
        for point in history[-30:]
    ]
    body += "<h2>Recent samples</h2>" + _rows(
        ["#Height", "Time", "#Hash rate", "#Difficulty"],
        rows,
        empty="Not enough history to sample yet.",
    )
    return _page(server, "Hash rate", body)


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
    node = server.node
    version = node.params.address_version
    inputs: list[list[Cell]] = []
    for txin in transaction.inputs:
        if txin.prevout.is_null:
            inputs.append([_html(_tag("coinbase", "warn")), _text(""), _text("")])
            continue
        parent = node.chain.get_transaction(txin.prevout.txid)
        if parent is None:
            inputs.append(
                [
                    _html(_tx_link(txin.prevout.txid[::-1].hex())),
                    _text(txin.prevout.index, numeric=True),
                    _text("unknown"),
                ]
            )
            continue
        output = parent[0].outputs[txin.prevout.index]
        source = _address_link(str(output.address(version))) + " &nbsp; " + _amount(output.value)
        inputs.append(
            [
                _html(_tx_link(txin.prevout.txid[::-1].hex())),
                _text(txin.prevout.index, numeric=True),
                _html(source),
            ]
        )
    outputs = [
        [
            _text(index, numeric=True),
            _html(_address_link(str(output.address(version)))),
            _html(_amount(output.value), numeric=True),
        ]
        for index, output in enumerate(transaction.outputs)
    ]
    return (
        "<h2>Inputs</h2>"
        + _rows(["Previous transaction", "#Index", "Source"], inputs)
        + "<h2>Outputs</h2>"
        + _rows(["#Index", "Address", "#Amount"], outputs)
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


def _address_page(server: RpcServer, text: str) -> str:
    node = server.node
    try:
        address = Address.decode(text, expected_version=node.params.address_version)
    except InvalidKeyError as exc:
        raise NotFound(str(exc)) from exc

    coins = node.storage.coins_of(address.hash)
    balance = sum(coin.value for _, coin in coins)
    history = node.storage.address_history(address.hash, 100)
    body = _cards(
        [
            ("Address", _hash_span(str(address))),
            ("Balance", _amount(balance)),
            ("Unspent outputs", str(len(coins))),
            ("Transactions", str(len(history))),
        ]
    )
    rows = []
    for txid, height, received, sent, _coinbase in history:
        rows.append(
            [
                _html(_height_link(height), numeric=True),
                _html(_tx_link(txid[::-1].hex())),
                _html(_amount(received - sent), numeric=True),
                _text(node.chain.confirmations(height), numeric=True),
            ]
        )
    body += "<h2>History</h2>" + _rows(
        ["#Height", "Transaction", "#Net amount", "#Confirmations"],
        rows,
        empty="This address has never been used.",
    )
    unspent = [
        [
            _html(_tx_link(outpoint.txid[::-1].hex())),
            _text(outpoint.index, numeric=True),
            _html(_amount(coin.value), numeric=True),
            _html(_tag("coinbase", "warn") if coin.is_coinbase else ""),
        ]
        for outpoint, coin in coins[:MAX_UNSPENT_ROWS]
    ]
    if len(coins) > MAX_UNSPENT_ROWS:
        unspent.append(
            [
                _text(f"… and {len(coins) - MAX_UNSPENT_ROWS} more"),
                _text(""),
                _text(""),
                _text(""),
            ]
        )
    body += "<h2>Unspent outputs</h2>" + _rows(
        ["Transaction", "#Index", "#Amount", "Type"], unspent, empty="No unspent outputs."
    )
    return _page(server, "Address", body)


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


def _rich_page(server: RpcServer) -> str:
    node = server.node
    supply = max(1, node.chain.total_supply())
    rows = [
        [
            _text(rank, numeric=True),
            _html(_address_link(str(Address(node.params.address_version, pubkey_hash)))),
            _html(_amount(total), numeric=True),
            _text(f"{total * 100 / supply:.2f}%", numeric=True),
        ]
        for rank, (pubkey_hash, total) in enumerate(node.storage.richest_addresses(25), start=1)
    ]
    body = "<h2>Largest balances</h2>" + _rows(
        ["#Rank", "Address", "#Balance", "#Share"], rows, empty="No coins have been mined yet."
    )
    return _page(server, "Rich list", body)


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
    if Address.is_valid(term, expected_version=node.params.address_version):
        return _address_page(server, term)
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
    if path == "/hashrate":
        return _hashrate_page(server, query)
    if path == "/mempool":
        return _mempool_page(server)
    if path == "/peers":
        return _peers_page(server)
    if path == "/rich":
        return _rich_page(server)
    if path == "/search":
        return _search(server, query)
    for prefix, handler in (
        ("/block/", _block_page),
        ("/tx/", _tx_page),
        ("/address/", _address_page),
    ):
        if path.startswith(prefix):
            return handler(server, path[len(prefix) :])
    raise NotFound(f"no page at {path}")


def render_error(server: RpcServer, message: str) -> str:
    """Render a "not found" page."""
    body = f'<h2>Not found</h2><p>{escape(message)}</p><p><a href="/">Back to the overview</a></p>'
    return _page(server, "Not found", body)
