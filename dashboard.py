#!/usr/bin/env python3
"""
dashboard.py — READ-ONLY fleet dashboard for the two live trading bots.

    http://127.0.0.1:8899

Watches (never touches) the bots run from other terminal sessions:
  * kraken_bot.py  — Kraken multi-timeframe ladder (whitelisted coins)
  * multi_bot.py   — Solana multi-coin ladder (your wallet's tokens)

Safety rules baked in:
  * NEVER writes to any bot file (state jsons, trade csvs, key/wallet files).
  * NEVER reads kraken_keys.json or bot_wallet.json.
  * NEVER calls Kraken's private API — a second client would race the live
    bot's nonce. Balances are reconstructed from kraken_state.json and
    priced with the public Ticker endpoint only.
  * Solana wallet is read through the public RPC (address only, no keys).

The only file it owns and writes is dashboard_history.json (its own
portfolio-value history, for the trend line).

Run:  python3 dashboard.py          (Ctrl-C to stop)
"""

import json
import os
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ----------------------------- config ----------------------------------------

DIR = os.path.dirname(os.path.abspath(__file__))
PORT = 8899
CACHE_SECONDS = 20          # min gap between refetches of external APIs
HISTORY_FILE = os.path.join(DIR, "dashboard_history.json")   # ours, safe
HISTORY_MAX = 5000

KRAKEN_STATE = os.path.join(DIR, "kraken_state.json")
KRAKEN_TRADES = os.path.join(DIR, "kraken_trades.csv")
SOL_STATE = os.path.join(DIR, "multi_state.json")
SOL_TRADES = os.path.join(DIR, "trades_log.csv")

# public wallet address of the Solana bot (address only — no key material)
SOL_WALLET = "YOUR_BOT_WALLET_ADDRESS"
SOL_RPC = "https://api.mainnet-beta.solana.com"
JUP_PRICE = "https://lite-api.jup.ag/price/v3"
KRAKEN_API = "https://api.kraken.com/0/public"
SOL_MINT = "So11111111111111111111111111111111111111112"
TOKEN_PROGRAMS = [
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",   # classic SPL
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",   # Token-2022 (pump.fun)
]

# friendly display names for your token mints, e.g.
# {"YourTokenMintAddress...": "MYCOIN"}
MINT_NAMES = {}

# ladder parameters mirrored from the bots (display only — bots stay the
# source of truth; update here if you retune them there)
KRAKEN_TF = {
    "1m": {"step": 0.025, "max_buys": 3, "max_sells": 3, "share": 0.20},
    "1h": {"step": 0.05,  "max_buys": 4, "max_sells": 4, "share": 0.30},
    "1d": {"step": 0.12,  "max_buys": 4, "max_sells": 4, "share": 0.50},
}
SOL_STEP = 0.08
SOL_MAX_BUYS = 8
SOL_MAX_SELLS = 5

KRAKEN_POLL = 60            # bot cycle seconds -> heartbeat staleness bound
SOL_POLL = 300


# ----------------------------- helpers ---------------------------------------

def fetch_json(url, payload=None, timeout=12):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Content-Type": "application/json",
                 "User-Agent": "bot-dashboard/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def read_json(path):
    with open(path) as f:
        return json.load(f)


def read_csv_rows(path, limit=15):
    """Last `limit` data rows of a csv, newest first."""
    try:
        with open(path) as f:
            lines = [l.strip() for l in f if l.strip()]
    except FileNotFoundError:
        return []
    return [l.split(",") for l in lines[1:]][-limit:][::-1]


def proc_running(script):
    try:
        out = subprocess.run(["pgrep", "-f", script], capture_output=True,
                             text=True, timeout=5)
        return bool(out.stdout.strip())
    except Exception:
        return None


def file_age(path):
    try:
        return time.time() - os.path.getmtime(path)
    except OSError:
        return None


# ----------------------------- kraken section --------------------------------

def kraken_prices(coins):
    pairs = ",".join(f"{c}USD" for c in coins)
    res = fetch_json(f"{KRAKEN_API}/Ticker?pair={urllib.parse.quote(pairs)}")
    out = {}
    for pair, t in res.get("result", {}).items():
        for c in coins:
            if pair.replace("XBT", "XXBT").startswith((c, "X" + c, "XX" + c)) \
                    or pair.startswith(c):
                out[c] = {"price": float(t["c"][0]),
                          "open24h": float(t["o"]),
                          "low24h": float(t["l"][1]),
                          "high24h": float(t["h"][1])}
    return out


def kraken_section(errors):
    sec = {"running": proc_running("kraken_bot.py"),
           "heartbeat_age": file_age(KRAKEN_STATE),
           "poll": KRAKEN_POLL, "coins": [], "trades": []}
    try:
        state = read_json(KRAKEN_STATE)
    except Exception as e:
        errors.append(f"kraken_state.json: {e}")
        return sec

    coins = list(state.get("coins", {}))
    try:
        prices = kraken_prices(coins) if coins else {}
    except Exception as e:
        errors.append(f"kraken ticker: {e}")
        prices = {}

    baseline = state.get("baseline", {})
    net_total = 0.0
    banked = 0.0
    portfolio_coins = 0.0

    for c, entry in state.get("coins", {}).items():
        p = prices.get(c, {})
        price = p.get("price")
        owned = sum(tf["coin_owned"] for tf in entry["tf"].values())
        tfs = []
        for name, tf in entry["tf"].items():
            cfg = KRAKEN_TF.get(name, {"step": 0, "max_buys": 0, "max_sells": 0})
            net_total += tf["net_usd"]
            banked += max(-tf["net_usd"], 0.0)
            nxt_buy = (tf["anchor"] * (1 - cfg["step"] * (tf["buys_done"] + 1))
                       if tf["buys_done"] < cfg["max_buys"] else None)
            nxt_sell = (tf["anchor"] * (1 + cfg["step"] * (tf["sells_done"] + 1))
                        if tf["sells_done"] < cfg["max_sells"] else None)
            tfs.append({"tf": name, "anchor": tf["anchor"], "peak": tf["peak"],
                        "buys": tf["buys_done"], "sells": tf["sells_done"],
                        "max_buys": cfg["max_buys"], "max_sells": cfg["max_sells"],
                        "owned": tf["coin_owned"], "net_usd": tf["net_usd"],
                        "step": cfg["step"], "share": cfg["share"],
                        "next_buy": nxt_buy, "next_sell": nxt_sell})
        value = owned * price if price else None
        if value:
            portfolio_coins += value
        anchor = entry["tf"]["1m"]["anchor"] if entry["tf"] else None
        sec["coins"].append({
            "coin": c, "adopted": entry.get("adopted"), "price": price,
            "open24h": p.get("open24h"), "low24h": p.get("low24h"),
            "high24h": p.get("high24h"), "owned": owned, "value": value,
            "anchor": anchor,
            "drift_pct": (price / anchor - 1) * 100 if price and anchor else None,
            "tfs": tfs})

    # USD reconstructed: baseline snapshot, plus stable deposits CONVERTed to
    # USD after that snapshot, minus net USD the ladders consumed. Deposits
    # are added to BOTH portfolio and hodl so they never inflate the edge.
    usd_est = None
    if baseline:
        cutoff = min((e.get("adopted", "") for e in state["coins"].values()
                      if e.get("adopted")), default="")
        deposits = 0.0
        try:
            with open(KRAKEN_TRADES) as f:
                for line in list(f)[1:]:
                    r = line.strip().split(",")
                    if len(r) >= 5 and r[3] == "CONVERT" and r[0] > cutoff:
                        deposits += float(r[4])
        except OSError:
            pass
        usd_est = baseline.get("usd", 0.0) + deposits - net_total
        sec["deposits_after_baseline"] = deposits
        sec["portfolio"] = usd_est + portfolio_coins
        hodl = baseline.get("usd", 0.0) + deposits + sum(
            amt * prices.get(c, {}).get("price", 0.0)
            for c, amt in baseline.get("coins", {}).items())
        sec["hodl"] = hodl
        sec["edge_usd"] = sec["portfolio"] - hodl
        sec["edge_pct"] = (sec["portfolio"] / hodl - 1) * 100 if hodl else None
    sec["usd_est"] = usd_est
    sec["banked"] = banked
    sec["baseline"] = baseline
    sec["trades"] = read_csv_rows(KRAKEN_TRADES)
    return sec


# ----------------------------- phantom / solana section ----------------------

def sol_wallet_balances():
    """SOL + token ui-amounts for the bot wallet, via public RPC only."""
    bal = fetch_json(SOL_RPC, {"jsonrpc": "2.0", "id": 1, "method": "getBalance",
                               "params": [SOL_WALLET]})
    sol = bal["result"]["value"] / 1e9
    tokens = {}
    for prog in TOKEN_PROGRAMS:
        res = fetch_json(SOL_RPC, {
            "jsonrpc": "2.0", "id": 1, "method": "getTokenAccountsByOwner",
            "params": [SOL_WALLET, {"programId": prog},
                       {"encoding": "jsonParsed"}]})
        for acc in res.get("result", {}).get("value", []):
            info = acc["account"]["data"]["parsed"]["info"]
            amt = info["tokenAmount"]
            tokens[info["mint"]] = tokens.get(info["mint"], 0.0) + \
                float(amt["uiAmount"] or 0.0)
    return sol, tokens


def phantom_section(errors):
    sec = {"running": proc_running("multi_bot.py"),
           "heartbeat_age": file_age(SOL_STATE),
           "poll": SOL_POLL, "coins": [], "trades": [],
           "wallet": SOL_WALLET}
    try:
        state = read_json(SOL_STATE)
    except Exception as e:
        errors.append(f"multi_state.json: {e}")
        state = {}

    sol, held = None, {}
    try:
        sol, held = sol_wallet_balances()
    except Exception as e:
        errors.append(f"solana rpc: {e}")

    mints = list(dict.fromkeys(list(state) + list(held) + [SOL_MINT]))
    prices = {}
    try:
        ids = ",".join(mints)
        prices = fetch_json(f"{JUP_PRICE}?ids={ids}")
    except Exception as e:
        errors.append(f"jupiter price: {e}")

    sol_price = prices.get(SOL_MINT, {}).get("usdPrice")
    sec["sol_balance"] = sol
    sec["sol_price"] = sol_price
    sec["sol_usd"] = sol * sol_price if sol is not None and sol_price else None

    total = sec["sol_usd"] or 0.0
    for mint, st in state.items():
        info = prices.get(mint, {})
        price = info.get("usdPrice")
        dec = info.get("decimals", 6)
        ui = held.get(mint)
        if ui is None and st.get("expected_raw") is not None:
            ui = st["expected_raw"] / (10 ** dec)     # RPC down: use bot's view
        value = ui * price if ui is not None and price else None
        if value:
            total += value
        moon_ui = (st.get("moon_raw") or 0) / (10 ** dec)
        anchor = st.get("anchor")
        nxt_buy = (anchor * (1 - SOL_STEP * (st["buys_done"] + 1))
                   if anchor and st["buys_done"] < SOL_MAX_BUYS else None)
        nxt_sell = (anchor * (1 + SOL_STEP * (st["sells_done"] + 1))
                    if anchor and st["sells_done"] < SOL_MAX_SELLS else None)
        sec["coins"].append({
            "mint": mint, "name": MINT_NAMES.get(mint, mint[:4] + "…" + mint[-4:]),
            "price": price, "change24h": info.get("priceChange24h"),
            "liquidity": info.get("liquidity"),
            "anchor": anchor,
            "drift_pct": (price / anchor - 1) * 100 if price and anchor else None,
            "peak": st.get("peak"),
            "buys": st.get("buys_done"), "sells": st.get("sells_done"),
            "max_buys": SOL_MAX_BUYS, "max_sells": SOL_MAX_SELLS,
            "next_buy": nxt_buy, "next_sell": nxt_sell,
            "holding_ui": ui, "value": value,
            "moon_ui": moon_ui, "moon_usd": moon_ui * price if price else None,
            "net_sol_usd": st.get("net_sol_usd"),
            "retired": st.get("retired", False),
            "moon_armed": st.get("moon_armed", False)})
    sec["wallet_total"] = total if (sol is not None or held) else None

    rows = []
    for r in read_csv_rows(SOL_TRADES):
        if len(r) >= 5 and len(r[1]) > 30:            # newer schema: mint in col2
            rows.append([r[0], MINT_NAMES.get(r[1], r[1][:6]), r[2], r[3], r[4]])
        elif len(r) >= 4:                              # early schema: single coin
            rows.append([r[0], "TOKEN", r[1], r[2], r[3]])
    sec["trades"] = rows
    return sec


# ----------------------------- history (our own file) ------------------------

_hist_lock = threading.Lock()


def record_history(snapshot):
    point = {"t": int(time.time()),
             "kraken": snapshot["kraken"].get("portfolio"),
             "edge": snapshot["kraken"].get("edge_usd"),
             "phantom": snapshot["phantom"].get("wallet_total")}
    if point["kraken"] is None and point["phantom"] is None:
        return []
    with _hist_lock:
        try:
            hist = read_json(HISTORY_FILE)
        except Exception:
            hist = []
        if not hist or point["t"] - hist[-1]["t"] >= 55:
            hist.append(point)
            hist = hist[-HISTORY_MAX:]
            tmp = HISTORY_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(hist, f)
            os.replace(tmp, HISTORY_FILE)
        return hist


# ----------------------------- snapshot cache --------------------------------

_cache = {"ts": 0, "data": None}
_cache_lock = threading.Lock()


def build_snapshot():
    errors = []
    snap = {"ts": time.time(),
            "kraken": kraken_section(errors),
            "phantom": phantom_section(errors),
            "errors": errors}
    snap["history"] = record_history(snap)[-500:]
    k = snap["kraken"].get("portfolio")
    p = snap["phantom"].get("wallet_total")
    snap["fleet_total"] = (k or 0.0) + (p or 0.0) if (k or p) else None
    return snap


def get_snapshot():
    with _cache_lock:
        if time.time() - _cache["ts"] > CACHE_SECONDS or _cache["data"] is None:
            _cache["data"] = build_snapshot()
            _cache["ts"] = time.time()
        return _cache["data"]


# ----------------------------- web layer -------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/status":
            try:
                body = json.dumps(get_snapshot()).encode()
                self._send(200, body, "application/json")
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}).encode(),
                           "application/json")
        elif path == "/":
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        else:
            self._send(404, b"not found", "text/plain")


PAGE = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bot Fleet — Kraken &amp; Phantom</title>
<style>
:root{
  --page:#f9f9f7; --surface:#fcfcfb; --ink:#0b0b0b; --ink2:#52514e;
  --muted:#898781; --grid:#e1e0d9; --border:rgba(11,11,11,.10);
  --blue:#2a78d6; --good:#0ca30c; --goodtext:#006300; --bad:#d03b3b;
  --warn:#b97f00;
}
@media (prefers-color-scheme: dark){
  :root{
    --page:#0d0d0d; --surface:#1a1a19; --ink:#fff; --ink2:#c3c2b7;
    --muted:#898781; --grid:#2c2c2a; --border:rgba(255,255,255,.10);
    --blue:#3987e5; --good:#0ca30c; --goodtext:#0ca30c; --bad:#e66767;
    --warn:#fab219;
  }
}
*{box-sizing:border-box;margin:0}
body{background:var(--page);color:var(--ink);
  font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif;padding:20px}
a{color:var(--blue)}
header{display:flex;flex-wrap:wrap;align-items:baseline;gap:12px;margin-bottom:16px}
h1{font-size:19px;font-weight:650}
h2{font-size:15px;font-weight:650;margin-bottom:2px}
.sub{color:var(--muted);font-size:12px}
.pill{display:inline-flex;align-items:center;gap:6px;font-size:12px;
  font-weight:600;padding:3px 10px;border:1px solid var(--border);
  border-radius:999px;background:var(--surface)}
.dot{width:8px;height:8px;border-radius:50%}
.on .dot{background:var(--good)} .off .dot{background:var(--bad)}
.stale .dot{background:var(--warn)}
.grid{display:grid;gap:12px}
.kpis{grid-template-columns:repeat(auto-fit,minmax(160px,1fr));margin-bottom:16px}
.tile{background:var(--surface);border:1px solid var(--border);
  border-radius:10px;padding:12px 14px}
.tile .label{font-size:11px;letter-spacing:.04em;text-transform:uppercase;
  color:var(--muted);margin-bottom:4px}
.tile .value{font-size:22px;font-weight:650}
.tile .delta{font-size:12px;margin-top:2px;color:var(--ink2)}
.up{color:var(--goodtext)} .down{color:var(--bad)}
.board{display:grid;gap:12px;
  grid-template-columns:repeat(auto-fit,minmax(430px,1fr));align-items:start}
.card{background:var(--surface);border:1px solid var(--border);
  border-radius:10px;padding:14px 16px}
.wide{grid-column:1/-1}
.card.dragging{opacity:.45}
.grip{cursor:grab;color:var(--muted);font-size:14px;letter-spacing:-1px;
  user-select:none;-webkit-user-select:none;padding:0 6px 0 0}
.grip:active{cursor:grabbing}
.k-card{--surface:#f0f6fe;--grid:#d9e3f0;--accent:#2a78d6;
  border-color:rgba(42,120,214,.28)}
.p-card{--surface:#f5f2fc;--grid:#e2dcf0;--accent:#4a3aa7;
  border-color:rgba(74,58,167,.25)}
@media (prefers-color-scheme: dark){
  .k-card{--surface:#141f2d;--grid:#243447;--accent:#3987e5;
    border-color:rgba(57,135,229,.35)}
  .p-card{--surface:#1e1930;--grid:#322a4a;--accent:#9085e9;
    border-color:rgba(144,133,233,.30)}
}
.k-card h2::before,.p-card h2::before{content:"";display:inline-block;
  width:8px;height:8px;border-radius:50%;background:var(--accent);
  margin-right:7px;vertical-align:1px}
.card>.head{display:flex;justify-content:space-between;align-items:baseline;
  gap:8px;margin-bottom:10px;flex-wrap:wrap}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
th{font-size:11px;text-transform:uppercase;letter-spacing:.04em;
  color:var(--muted);font-weight:600;text-align:right;padding:5px 8px;
  border-bottom:1px solid var(--grid)}
td{padding:5px 8px;text-align:right;border-bottom:1px solid var(--grid);
  font-size:13px;white-space:nowrap}
th:first-child,td:first-child{text-align:left}
tr:last-child td{border-bottom:none}
.twrap{overflow-x:auto}
.coinrow{display:flex;justify-content:space-between;align-items:baseline;
  flex-wrap:wrap;gap:6px;margin:12px 0 6px}
.coinrow:first-of-type{margin-top:0}
.coin{font-size:15px;font-weight:650}
.tag{font-size:11px;color:var(--muted)}
.bar{position:relative;height:6px;border-radius:3px;background:var(--grid);
  margin:6px 0 10px;overflow:hidden}
.bar>i{position:absolute;top:0;bottom:0;border-radius:3px}
.bar>i.neg{background:var(--bad)} .bar>i.pos{background:var(--good)}
.bar>b{position:absolute;top:-2px;bottom:-2px;left:50%;width:2px;
  background:var(--muted)}
.small{font-size:12px;color:var(--ink2)}
.mono{font-family:ui-monospace,Menlo,monospace;font-size:11px}
#spark{width:100%;height:170px;display:block}
.footer{margin-top:18px;color:var(--muted);font-size:11px;line-height:1.6}
.err{background:var(--surface);border:1px solid var(--bad);border-radius:8px;
  color:var(--bad);padding:8px 12px;margin-bottom:12px;font-size:12px;display:none}
section{margin-bottom:16px}
</style></head><body>
<header>
  <h1>Bot Fleet</h1>
  <span id="pill-k" class="pill"><span class="dot"></span>KRAKEN</span>
  <span id="pill-p" class="pill"><span class="dot"></span>PHANTOM</span>
  <span class="sub" id="updated"></span>
</header>
<div class="err" id="errors"></div>

<div class="grid kpis" id="kpis"></div>

<div class="board" id="board">
  <section class="card wide" id="trend" data-card="trend" style="display:none">
    <div class="head"><span><span class="grip" title="drag to move">⠿</span>
      <h2 style="display:inline">Fleet value</h2></span>
      <span class="sub">total portfolio, sampled each refresh</span></div>
    <svg id="spark" preserveAspectRatio="none"></svg>
  </section>

  <section class="card k-card" data-card="kraken">
    <div class="head"><span><span class="grip" title="drag to move">⠿</span>
      <h2 style="display:inline">Kraken — multi-timeframe ladders</h2></span>
      <span class="sub" id="kraken-sub"></span></div>
    <div id="kraken"></div>
  </section>

  <section class="card p-card" data-card="phantom">
    <div class="head"><span><span class="grip" title="drag to move">⠿</span>
      <h2 style="display:inline">Phantom — Solana ladders</h2></span>
      <span class="sub" id="phantom-sub"></span></div>
    <div id="phantom"></div>
  </section>

  <section class="card k-card" data-card="ktrades">
    <div class="head"><span><span class="grip" title="drag to move">⠿</span>
      <h2 style="display:inline">Kraken trades</h2></span>
      <span class="sub">latest first</span></div>
    <div class="twrap"><table id="ktrades"></table></div>
  </section>

  <section class="card p-card" data-card="ptrades">
    <div class="head"><span><span class="grip" title="drag to move">⠿</span>
      <h2 style="display:inline">Phantom trades</h2></span>
      <span class="sub">latest first</span></div>
    <div class="twrap"><table id="ptrades"></table></div>
  </section>
</div>

<p class="footer">Read-only observer — reads the bots’ state files and public
APIs only (Kraken public ticker, Jupiter prices, Solana RPC). It never writes
to bot files and never uses the private Kraken API, so it cannot interfere
with the live bots. Kraken USD balance is an estimate reconstructed from the
baseline snapshot and ladder flows. Refreshes every 30&nbsp;s.</p>

<script>
const $=id=>document.getElementById(id);
const usd=(v,d)=>v==null?"—":"$"+v.toLocaleString(undefined,
  {minimumFractionDigits:d??2,maximumFractionDigits:d??2});
const px=v=>{ if(v==null)return"—";
  const d=v>=1000?2:v>=1?4:v>=0.01?6:10;
  return "$"+v.toLocaleString(undefined,{maximumFractionDigits:d}); };
const pct=v=>v==null?"—":(v>=0?"+":"")+v.toFixed(2)+"%";
const num=(v,d)=>{ if(v==null)return"—";
  if(d==null) d=v!==0&&Math.abs(v)<1?6:Math.abs(v)<100?4:2;
  return v.toLocaleString(undefined,{maximumFractionDigits:d}); };
const cls=v=>v==null?"":v>=0?"up":"down";
const esc=s=>String(s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",
  ">":"&gt;",'"':"&quot;"}[c]));

function pillState(el,sec){
  el.classList.remove("on","off","stale");
  let label, stale = sec.heartbeat_age!=null && sec.heartbeat_age > sec.poll*5;
  if(sec.running===false){ el.classList.add("off"); label="STOPPED"; }
  else if(stale){ el.classList.add("stale"); label="STALE"; }
  else if(sec.running){ el.classList.add("on"); label="LIVE"; }
  else { el.classList.add("stale"); label="?"; }
  const age=sec.heartbeat_age==null?"":" · state "+
    (sec.heartbeat_age<90?Math.round(sec.heartbeat_age)+"s":
     Math.round(sec.heartbeat_age/60)+"m")+" ago";
  el.innerHTML='<span class="dot"></span>'+el.dataset.name+" "+label+
    '<span class="sub">'+age+'</span>';
}

function driftBar(drift){
  if(drift==null) return "";
  const mag=Math.min(Math.abs(drift),80)/80*50;
  const side=drift<0?"right:50%":"left:50%";
  return '<div class="bar"><b></b><i class="'+(drift<0?"neg":"pos")+
    '" style="'+side+';width:'+mag+'%"></i></div>';
}

function kraken(k){
  $("kraken-sub").textContent="USD est. "+usd(k.usd_est)+" · banked "+usd(k.banked);
  let h="";
  if(k.portfolio!=null){
    h+='<div class="small" style="margin-bottom:8px">Scoreboard: portfolio <b>'+
      usd(k.portfolio)+'</b> vs just-holding '+usd(k.hodl)+' → bot edge <b class="'+
      cls(k.edge_usd)+'">'+(k.edge_usd>=0?"+":"")+usd(k.edge_usd).replace("$","$")+
      " ("+pct(k.edge_pct)+")</b></div>";
  }
  for(const c of k.coins){
    h+='<div class="coinrow"><span><span class="coin">'+esc(c.coin)+
      '</span> <span class="tag">'+num(c.owned)+' held · '+usd(c.value)+
      '</span></span><span><b>'+px(c.price)+'</b> <span class="'+cls(c.drift_pct)+
      '">'+pct(c.drift_pct)+'</span> <span class="tag">vs anchor</span></span></div>';
    h+=driftBar(c.drift_pct);
    h+='<div class="twrap"><table><tr><th>Bot</th><th>Anchor</th><th>Buys</th>'+
      '<th>Sells</th><th>Next buy</th><th>Next sell</th><th>Holdings</th>'+
      '<th>Net USD</th></tr>';
    for(const t of c.tfs){
      h+="<tr><td>"+t.tf+' <span class="tag">±'+(t.step*100).toFixed(1)+
        "%</span></td><td>"+px(t.anchor)+"</td><td>"+t.buys+"/"+t.max_buys+
        "</td><td>"+t.sells+"/"+t.max_sells+"</td><td>"+px(t.next_buy)+
        "</td><td>"+px(t.next_sell)+"</td><td>"+num(t.owned)+
        '</td><td class="'+cls(-t.net_usd)+'">'+usd(-t.net_usd)+"</td></tr>";
    }
    h+="</table></div>";
  }
  if(!k.coins.length) h+='<p class="small">No coins adopted yet.</p>';
  $("kraken").innerHTML=h;
}

function phantom(p){
  $("phantom-sub").textContent="SOL "+num(p.sol_balance,4)+" ("+usd(p.sol_usd)+
    ") · SOL "+px(p.sol_price);
  let h="";
  for(const c of p.coins){
    h+='<div class="coinrow"><span><span class="coin">'+esc(c.name)+'</span> '+
      '<span class="tag">'+num(c.holding_ui,0)+' held · '+usd(c.value)+
      (c.retired?' · RETIRED':'')+'</span></span><span><b>'+px(c.price)+
      '</b> <span class="'+cls(c.drift_pct)+'">'+pct(c.drift_pct)+
      '</span> <span class="tag">vs anchor</span></span></div>';
    h+=driftBar(c.drift_pct);
    h+='<div class="twrap"><table>'+
      "<tr><td>Ladder</td><td>buys "+c.buys+"/"+c.max_buys+" · sells "+
        c.sells+"/"+c.max_sells+"</td><td>next buy "+px(c.next_buy)+
        "</td><td>next sell "+px(c.next_sell)+"</td></tr>"+
      "<tr><td>Moon bag</td><td>"+num(c.moon_ui,0)+" ("+usd(c.moon_usd)+
        ")</td><td>24h <span class='"+cls(c.change24h)+"'>"+pct(c.change24h)+
        "</span></td><td>pool liq "+usd(c.liquidity,0)+"</td></tr>"+
      "<tr><td>SOL drawn (net)</td><td class='"+cls(-(c.net_sol_usd??0))+"'>"+
        usd(c.net_sol_usd)+"</td><td>peak "+px(c.peak)+"</td><td>"+
        (c.moon_armed?"moon trail ARMED":"moon trail idle")+"</td></tr>"+
      "</table></div>";
  }
  if(!p.coins.length) h+='<p class="small">No coins in state.</p>';
  $("phantom").innerHTML=h;
}

function trades(el,rows,head){
  let h="<tr>"+head.map(x=>"<th>"+x+"</th>").join("")+"</tr>";
  for(const r of rows){
    h+="<tr>"+r.map((x,i)=>"<td"+(i>=head.length-1?' class="mono"':"")+">"+
      esc(String(x).length>14&&i===r.length-1?String(x).slice(0,12)+"…":x)+
      "</td>").join("")+"</tr>";
  }
  if(!rows.length) h+='<tr><td colspan="'+head.length+
    '" class="small">no trades yet</td></tr>';
  el.innerHTML=h;
}

function spark(hist){
  const pts=hist.filter(p=>p.kraken!=null||p.phantom!=null)
    .map(p=>({t:p.t,v:(p.kraken||0)+(p.phantom||0)}));
  if(pts.length<3){ $("trend").style.display="none"; return; }
  $("trend").style.display="";
  const svg=$("spark"),W=svg.clientWidth||800,H=svg.clientHeight||170,P=8;
  const vs=pts.map(p=>p.v),mn=Math.min(...vs),mx=Math.max(...vs),sp=mx-mn||1;
  const X=i=>P+i/(pts.length-1)*(W-2*P);
  const Y=v=>H-P-(v-mn)/sp*(H-2*P);
  svg.setAttribute("viewBox","0 0 "+W+" "+H);
  const d=pts.map((p,i)=>(i?"L":"M")+X(i).toFixed(1)+" "+Y(p.v).toFixed(1)).join(" ");
  const area=d+" L"+X(pts.length-1).toFixed(1)+" "+(H-P)+" L"+X(0).toFixed(1)+" "+(H-P)+" Z";
  const mid=(mn+mx)/2;
  const gl=[mx,mid,mn].map(v=>'<line x1="'+P+'" x2="'+(W-P)+'" y1="'+Y(v).toFixed(1)+
    '" y2="'+Y(v).toFixed(1)+'" stroke="var(--grid)" stroke-width="1"/>').join("");
  const lb=[mx,mid,mn].map((v,i)=>'<text x="'+(P+2)+'" y="'+
    (i===0?Y(v)+12:Y(v)-4).toFixed(1)+
    '" fill="var(--muted)" font-size="10">'+usd(v)+'</text>').join("");
  svg.innerHTML=gl+
    '<path d="'+area+'" fill="var(--blue)" opacity="0.08"/>'+
    '<path d="'+d+'" fill="none" stroke="var(--blue)" stroke-width="2"/>'+lb;
}

function kpis(d){
  const k=d.kraken,p=d.phantom;
  const t=[
    ["Fleet total",usd(d.fleet_total),"Kraken + Phantom"],
    ["Kraken portfolio",usd(k.portfolio),
      k.edge_usd==null?"":"edge vs hodl: <span class='"+cls(k.edge_usd)+"'>"+
      pct(k.edge_pct)+"</span>"],
    ["Kraken bot edge",
      "<span class='"+cls(k.edge_usd)+"'>"+usd(k.edge_usd)+"</span>",
      "vs buy-and-hold baseline"],
    ["Kraken banked",usd(k.banked),"realized sell gains"],
    ["Phantom wallet",usd(p.wallet_total),
      "SOL + tokens at live prices"],
  ];
  $("kpis").innerHTML=t.map(x=>'<div class="tile"><div class="label">'+x[0]+
    '</div><div class="value">'+x[1]+'</div><div class="delta">'+x[2]+
    "</div></div>").join("");
}

async function refresh(){
  try{
    const d=await (await fetch("/api/status")).json();
    pillState($("pill-k"),d.kraken); pillState($("pill-p"),d.phantom);
    kpis(d); kraken(d.kraken); phantom(d.phantom);
    trades($("ktrades"),d.kraken.trades,
      ["Time (UTC)","Coin","TF","Side","USD","Price","Volume","Mode"]);
    trades($("ptrades"),d.phantom.trades,
      ["Time (UTC)","Coin","Action","USD","Price / Tx"]);
    lastHist=d.history||[]; spark(lastHist);
    $("updated").textContent="updated "+new Date().toLocaleTimeString();
    const errs=d.errors||[];
    $("errors").style.display=errs.length?"block":"none";
    $("errors").textContent=errs.join(" · ");
  }catch(e){
    $("errors").style.display="block";
    $("errors").textContent="dashboard fetch failed: "+e;
  }
}
document.querySelectorAll(".pill").forEach((el,i)=>
  el.dataset.name=["KRAKEN","PHANTOM"][i]);

/* ---- draggable cards: grab the ⠿ grip, drop anywhere on the board ---- */
let dragEl=null;
const board=$("board");
function saveOrder(){
  localStorage.setItem("boardOrder",JSON.stringify(
    [...board.querySelectorAll(".card")].map(c=>c.dataset.card)));
}
(function restoreOrder(){
  try{
    const o=JSON.parse(localStorage.getItem("boardOrder")||"null");
    if(o) o.forEach(id=>{
      const el=board.querySelector('[data-card="'+id+'"]');
      if(el) board.appendChild(el);
    });
  }catch(e){}
})();
board.querySelectorAll(".card").forEach(c=>{
  const g=c.querySelector(".grip");
  g.addEventListener("mousedown",()=>c.draggable=true);
  document.addEventListener("mouseup",()=>c.draggable=false);
  c.addEventListener("dragstart",e=>{
    dragEl=c; c.classList.add("dragging");
    e.dataTransfer.effectAllowed="move";
  });
  c.addEventListener("dragend",()=>{
    c.draggable=false; c.classList.remove("dragging");
    dragEl=null; saveOrder(); spark(lastHist);
  });
  c.addEventListener("dragover",e=>{
    e.preventDefault();
    if(!dragEl||dragEl===c) return;
    const r=c.getBoundingClientRect();
    const before=(e.clientY-r.top)/r.height<0.5;
    board.insertBefore(dragEl,before?c:c.nextSibling);
  });
});
board.addEventListener("drop",e=>e.preventDefault());

let lastHist=[];
refresh(); setInterval(refresh,30000);
</script></body></html>
"""


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Bot fleet dashboard (read-only) -> http://127.0.0.1:{PORT}")
    print("Watching: kraken_bot.py + multi_bot.py state files. Ctrl-C stops.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")


if __name__ == "__main__":
    main()
