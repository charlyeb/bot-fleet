#!/usr/bin/env python3
"""
kraken_bot.py - Multi-coin, multi-timeframe ladder bot for Kraken spot.

How it works
------------
Deposit any coin on Kraken (e.g. KAS). The bot adopts it and records the
price at that moment as the ANCHOR. Three sub-bots then trade it, each
watching a different scale of volatility:

    1m bot : tight steps  (~2.5%)  - minute-scale wiggles
    1h bot : medium steps (~5%)    - hour-scale swings
    1d bot : wide steps   (~12%)   - day-scale moves

Each sub-bot, independently:
  * BUYS the coin with USD when price dips  step%, 2*step%, ... below its anchor
  * SELLS a slice of its coin for USD when price pumps step%, 2*step%, ... above
  * After it has taken profits and the pump rolls over (trailing %), it
    RE-ANCHORS at the current price and starts a fresh cycle - this is how
    the bot "moves with the market".
  * It NEVER sells below its anchor. Dips are only ever bought.

You fund both sides yourself: deposit the coin (kept 100%, never auto-sold)
plus a stable reserve for dip-buying. Deposit USDC and the bot converts it
to USD once (Kraken has no KAS/USDC pair; every coin has a USD pair).

Modes
-----
  PAPER (default): no API keys needed. Live Kraken prices, simulated
    balances from PAPER_BALANCES below, simulated fills with real fees.
  LIVE: set LIVE = True and create kraken_keys.json (see below).

API key setup (when ready to go live)
-------------------------------------
  Kraken.com -> Settings -> API -> Create key with ONLY these permissions:
      [x] Query Funds
      [x] Create & Modify Orders
      [ ] Withdraw Funds        <- LEAVE OFF. Bot can never withdraw.
  Save as kraken_keys.json next to this script:
      {"key": "YOUR_API_KEY", "secret": "YOUR_PRIVATE_KEY"}

Usage
-----
    python3 kraken_bot.py            # run forever (poll every 60s)
    python3 kraken_bot.py --once     # single cycle, then exit (for testing)
"""

import base64
import csv
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# ============================ CONFIG =========================================
LIVE = False                  # False = paper trading. True = real orders.

# Simulated wallet for PAPER mode (ignored in LIVE mode):
PAPER_BALANCES = {"XBT": 0.01, "USD": 500.0}

STABLE = "USD"                # reserve currency all coins trade against
AUTO_CONVERT = ["USDC", "USDT", "USDG"]  # stables auto-sold into USD on sight
WHITELIST = []                # e.g. ["XBT", "ETH", "SOL"] — ONLY these coins
                              # are traded; other holdings are never touched.
                              # Empty list [] = adopt everything above
                              # ADOPT_MIN_USD. Note: Bitcoin is "XBT" on
                              # Kraken. Coins listed here but not yet bought are
                              # adopted automatically the moment they appear.
SEEDS = {}                    # USD dip-buying seed per coin, e.g. {"XBT": 100.0}.
                              # Coins NOT listed here run on HOUSE MONEY: they
                              # can only spend USD that their own pump-sells
                              # have earned. Keep the total of all seeds <= the
                              # USD you deposited.
ADOPT_MIN_USD = 5.0           # coins worth less than this are ignored (dust)
STABLE_KEEP_USD = 0.0         # USD floor the bot never spends

# The three timeframe sub-bots. share = slice of the coin bag and of the USD
# reserve this sub-bot gets. step = dip/pump trigger distance from anchor.
TIMEFRAMES = {
    "1m": {"share": 0.20, "step": 0.025, "max_buys": 3, "max_sells": 3,
           "buy_mult": 1.30, "sell_frac": 0.30, "sell_mult": 1.20, "trail": 0.02},
    "1h": {"share": 0.30, "step": 0.05,  "max_buys": 4, "max_sells": 4,
           "buy_mult": 1.35, "sell_frac": 0.28, "sell_mult": 1.20, "trail": 0.04},
    "1d": {"share": 0.50, "step": 0.12,  "max_buys": 4, "max_sells": 4,
           "buy_mult": 1.40, "sell_frac": 0.25, "sell_mult": 1.25, "trail": 0.08},
}
MIN_SELLS_BEFORE_REANCHOR = 2  # take at least this many profit slices before
                               # a trailing re-anchor can fire
POLL_SECONDS = 60
RECONCILE_TOLERANCE = 0.02     # >2% unexplained balance change = manual move

API = "https://api.kraken.com"
KEYS_FILE = "kraken_keys.json"
STATE_FILE = "kraken_state.json"
LOG_FILE = "kraken_trades.csv"
PAPER_FEE = 0.004              # taker fee simulated on paper fills
# =============================================================================


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def log(msg):
    print(f"[{now()}] {msg}", flush=True)


# ----------------------------- Kraken REST -----------------------------------

def public(method, params=None):
    url = f"{API}/0/public/{method}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=30) as r:
        j = json.load(r)
    if j.get("error"):
        raise RuntimeError(f"Kraken {method}: {j['error']}")
    return j["result"]


_keys = None


def keys():
    global _keys
    if _keys is None:
        with open(KEYS_FILE) as f:
            _keys = json.load(f)
    return _keys


def private(method, data=None):
    data = dict(data or {})
    data["nonce"] = str(int(time.time() * 1000))
    path = f"/0/private/{method}"
    post = urllib.parse.urlencode(data)
    digest = hashlib.sha256((data["nonce"] + post).encode()).digest()
    mac = hmac.new(base64.b64decode(keys()["secret"]),
                   path.encode() + digest, hashlib.sha512)
    req = urllib.request.Request(API + path, data=post.encode(), headers={
        "API-Key": keys()["key"],
        "API-Sign": base64.b64encode(mac.digest()).decode(),
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        j = json.load(r)
    if j.get("error"):
        raise RuntimeError(f"Kraken {method}: {j['error']}")
    return j["result"]


# --------------------------- market metadata ----------------------------------

class Market:
    """Everything the bot needs to know about tradable */USD pairs."""

    def __init__(self):
        self.pairs = {}        # base altname -> {pair, ordermin, lot_decimals}
        self.asset_alt = {}    # kraken asset code (ZUSD, XXBT..) -> altname (USD, XBT..)
        self.refresh()

    def refresh(self):
        for code, a in public("Assets").items():
            self.asset_alt[code] = a["altname"]
        for key, p in public("AssetPairs").items():
            ws = p.get("wsname", "")
            if ws.endswith(f"/{STABLE}"):
                base = ws.split("/")[0]
                self.pairs[base] = {
                    "pair": key,
                    "ordermin": float(p.get("ordermin", 0)),
                    "costmin": float(p.get("costmin", 0)),
                    "lot_decimals": int(p.get("lot_decimals", 8)),
                }

    def tickers(self, bases):
        """{base: last_price} for the given base assets."""
        wanted = {self.pairs[b]["pair"]: b for b in bases if b in self.pairs}
        if not wanted:
            return {}
        res = public("Ticker", {"pair": ",".join(wanted)})
        out = {}
        for k, v in res.items():
            if k in wanted:
                out[wanted[k]] = float(v["c"][0])
        return out


# ------------------------------- wallet ---------------------------------------

class Wallet:
    """Balance access + order placement. Paper or live."""

    def __init__(self, market):
        self.m = market
        self.paper = dict(PAPER_BALANCES) if not LIVE else None

    def balances(self):
        if not LIVE:
            return {k: v for k, v in self.paper.items() if v > 0}
        out = {}
        for code, amt in private("Balance").items():
            if "." in code:          # staked/earn balances - not spot tradable
                continue
            alt = self.m.asset_alt.get(code, code)
            amt = float(amt)
            if amt > 0:
                out[alt] = out.get(alt, 0.0) + amt
        return out

    def market_order(self, base, side, volume, price):
        """Place a market order for `volume` of `base`. Returns True on success."""
        info = self.m.pairs[base]
        volume = round(volume, info["lot_decimals"])
        if volume < info["ordermin"] or volume * price < info["costmin"]:
            return False
        if not LIVE:
            usd = volume * price
            fee = usd * PAPER_FEE
            if side == "buy":
                if self.paper.get(STABLE, 0) < usd + fee:
                    return False
                self.paper[STABLE] = self.paper.get(STABLE, 0) - usd - fee
                self.paper[base] = self.paper.get(base, 0) + volume
            else:
                if self.paper.get(base, 0) < volume:
                    return False
                self.paper[base] -= volume
                self.paper[STABLE] = self.paper.get(STABLE, 0) + usd - fee
            return True
        private("AddOrder", {"pair": info["pair"], "type": side,
                             "ordertype": "market", "volume": f"{volume:.{info['lot_decimals']}f}"})
        return True


def log_trade(coin, tf, side, usd, price, volume):
    new = not os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time_utc", "coin", "timeframe", "side",
                        "usd", "price", "volume", "mode"])
        w.writerow([now(), coin, tf, side, f"{usd:.2f}", f"{price:.8f}",
                    f"{volume:.8f}", "LIVE" if LIVE else "PAPER"])


# ----------------------------- strategy ---------------------------------------

def fresh_tf_state(price, coin_owned):
    return {"anchor": price, "peak": price, "buys_done": 0, "sells_done": 0,
            "coin_owned": coin_owned, "net_usd": 0.0}


class SubBot:
    """One ladder on one timeframe for one coin."""

    def __init__(self, coin, tf, st, wallet):
        self.coin, self.tf, self.st, self.w = coin, tf, st, wallet
        self.p = TIMEFRAMES[tf]
        self.units = sum(self.p["buy_mult"] ** i for i in range(self.p["max_buys"]))

    def min_usd(self, price):
        info = self.w.m.pairs[self.coin]
        return max(info["ordermin"] * price, info["costmin"])

    def buy(self, usd, price, budget, tag):
        floor = self.min_usd(price)
        usd = max(usd, floor)            # bump tiny rungs up to the exchange min
        if budget < 0.01:                # unfunded house-money coin: quietly
            return 0.0                   # wait for its sells to earn a budget
        if usd > budget + 0.01 or usd < floor:
            log(f"[{self.coin}/{self.tf}] skip {tag}: needs ${usd:.2f}, budget ${budget:.2f}")
            return 0.0
        vol = usd / price
        try:
            if not self.w.market_order(self.coin, "buy", vol, price):
                return 0.0
        except Exception as e:
            log(f"[{self.coin}/{self.tf}] {tag} FAILED: {e}")
            return 0.0
        log(f"[{self.coin}/{self.tf}] {tag}: bought ${usd:.2f} ({vol:.4f} {self.coin}) @ {price:.6f}")
        log_trade(self.coin, self.tf, tag, usd, price, vol)
        self.st["coin_owned"] += vol
        self.st["net_usd"] += usd
        return usd

    def sell(self, vol, price, tag):
        vol = min(vol, self.st["coin_owned"])
        info = self.w.m.pairs[self.coin]
        if vol < info["ordermin"]:       # bump up to min if the bag allows
            if self.st["coin_owned"] >= info["ordermin"]:
                vol = info["ordermin"]
            else:
                log(f"[{self.coin}/{self.tf}] skip {tag}: {vol:.4f} below exchange min")
                return False
        usd = vol * price
        try:
            if not self.w.market_order(self.coin, "sell", vol, price):
                return False
        except Exception as e:
            log(f"[{self.coin}/{self.tf}] {tag} FAILED: {e}")
            return False
        log(f"[{self.coin}/{self.tf}] {tag}: sold {vol:.4f} {self.coin} (~${usd:.2f}) @ {price:.6f}")
        log_trade(self.coin, self.tf, tag, usd, price, vol)
        self.st["coin_owned"] -= vol
        self.st["net_usd"] -= usd
        return True

    def step(self, price, budget):
        """One tick. Returns USD spent this tick."""
        st, p = self.st, self.p
        st["peak"] = max(st["peak"], price)
        spent = 0.0

        # ---- BUY DIPS: each level below anchor buys once per cycle ----
        base_buy = budget_base = None
        while st["buys_done"] < p["max_buys"]:
            lvl = st["anchor"] * (1 - p["step"] * (st["buys_done"] + 1))
            if price > lvl:
                break
            if base_buy is None:
                pool = budget + max(st["net_usd"], 0.0)
                base_buy = pool / self.units
            size = base_buy * (p["buy_mult"] ** st["buys_done"])
            got = self.buy(size, price, budget - spent,
                           f"DIP_BUY_L{st['buys_done'] + 1}")
            if got <= 0:
                break
            st["buys_done"] += 1
            spent += got

        # ---- SELL PUMPS: each level above anchor sells a slice ----
        while st["sells_done"] < p["max_sells"]:
            lvl = st["anchor"] * (1 + p["step"] * (st["sells_done"] + 1))
            if price < lvl:
                break
            frac = min(p["sell_frac"] * (p["sell_mult"] ** st["sells_done"]), 1.0)
            if not self.sell(st["coin_owned"] * frac, price,
                             f"PUMP_SELL_L{st['sells_done'] + 1}"):
                break
            st["sells_done"] += 1

        # ---- RE-ANCHOR: profits taken and the pump rolled over ----
        if (st["sells_done"] >= MIN_SELLS_BEFORE_REANCHOR
                and price < st["peak"] * (1 - p["trail"])
                and price > st["anchor"]):
            log(f"[{self.coin}/{self.tf}] RE-ANCHOR {st['anchor']:.6f} -> {price:.6f} "
                f"(cycle done: {st['buys_done']} buys, {st['sells_done']} sells)")
            st.update({"anchor": price, "peak": price,
                       "buys_done": 0, "sells_done": 0})
        return spent


# ----------------------------- supervisor --------------------------------------

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"coins": {}}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def adopt(state, coin, price, amount):
    tfs = {tf: fresh_tf_state(price, amount * TIMEFRAMES[tf]["share"])
           for tf in TIMEFRAMES}
    state["coins"][coin] = {"adopted": now(), "tf": tfs, "expected": amount}
    log(f"[{coin}] ADOPTED: {amount:.4f} {coin} @ {price:.6f} "
        f"(~${amount * price:.2f}). Anchors set on all timeframes.")
    for tf, c in TIMEFRAMES.items():
        st = tfs[tf]
        buys = ", ".join(f"-{c['step']*(i+1)*100:.1f}%" for i in range(c["max_buys"]))
        sells = ", ".join(f"+{c['step']*(i+1)*100:.1f}%" for i in range(c["max_sells"]))
        log(f"  {tf}: owns {st['coin_owned']:.2f} {coin} | buys at {buys} | sells at {sells}")


def reconcile(state, coin, actual, price):
    """Detect deposits/withdrawals made outside the bot and fold them in."""
    entry = state["coins"][coin]
    expected = entry.get("expected", actual)
    if expected <= 0:
        entry["expected"] = actual
        return
    drift = (actual - expected) / expected
    if abs(drift) <= RECONCILE_TOLERANCE:
        return
    if drift > 0:
        log(f"[{coin}] external deposit detected (+{drift*100:.1f}%) - folded in.")
    else:
        log(f"[{coin}] external withdrawal detected ({drift*100:.1f}%) - rescaled.")
    # distribute the difference across timeframes by configured share
    for tf_name, tf in entry["tf"].items():
        tf["coin_owned"] = max(tf["coin_owned"] +
                               (actual - expected) * TIMEFRAMES[tf_name]["share"], 0.0)
    # external deposits/withdrawals also shift the buy-and-hold benchmark
    base = state.get("baseline", {}).get("coins", {})
    if coin in base:
        base[coin] = max(base[coin] + (actual - expected), 0.0)
    entry["expected"] = actual


def main():
    once = "--once" in sys.argv
    market = Market()
    wallet = Wallet(market)
    state = load_state()

    mode = "LIVE - REAL MONEY" if LIVE else "PAPER (simulated fills, live prices)"
    log(f"Kraken multi-timeframe ladder bot | {mode}")
    log(f"Reserve currency: {STABLE} | timeframes: {', '.join(TIMEFRAMES)} | "
        f"poll {POLL_SECONDS}s")

    while True:
        try:
            bal = wallet.balances()

            # -- auto-convert deposited stables (USDC/USDT) into USD --
            for stb in AUTO_CONVERT:
                amt = bal.get(stb, 0.0)
                if amt >= 5.0 and stb in market.pairs:
                    log(f"converting {amt:.2f} {stb} -> {STABLE}")
                    try:
                        wallet.market_order(stb, "sell", amt, 1.0)
                        log_trade(stb, "-", "CONVERT", amt, 1.0, amt)
                    except Exception as e:
                        log(f"{stb} conversion failed: {e}")
                    bal = wallet.balances()

            coins = [a for a in bal if a != STABLE and a not in AUTO_CONVERT
                     and a in market.pairs
                     and (not WHITELIST or a in WHITELIST)]
            prices = market.tickers(set(coins) | set(state["coins"]))

            # -- adopt new deposits --
            for c in coins:
                if c in state["coins"] or c not in prices:
                    continue
                if bal[c] * prices[c] >= ADOPT_MIN_USD:
                    adopt(state, c, prices[c], bal[c])
                    # snapshot the buy-and-hold benchmark: what the account
                    # would be worth if it just sat on these exact holdings
                    base = state.setdefault("baseline", {
                        "usd": max(bal.get(STABLE, 0.0) - STABLE_KEEP_USD, 0.0),
                        "coins": {}})
                    base["coins"][c] = bal[c]

            active = [c for c in state["coins"] if c in prices]
            usd_free = max(bal.get(STABLE, 0.0) - STABLE_KEEP_USD, 0.0)

            # -- seeded budgets: each coin's dip-buying power = its SEEDS entry
            #    plus whatever its own sells have earned (net_usd goes negative
            #    on sells). Unseeded coins trade on house money only. No coin
            #    can ever raid another coin's seed or gains. --
            cycle_spent = 0.0
            summary = []
            for c in active:
                entry = state["coins"][c]
                reconcile(state, c, bal.get(c, 0.0), prices[c])
                for tf_name in TIMEFRAMES:
                    st = entry["tf"][tf_name]
                    seed_tf = SEEDS.get(c, 0.0) * TIMEFRAMES[tf_name]["share"]
                    budget = min(max(seed_tf - st["net_usd"], 0.0),
                                 max(usd_free - cycle_spent, 0.0))
                    bot = SubBot(c, tf_name, st, wallet)
                    cycle_spent += bot.step(prices[c], budget)
                entry["expected"] = wallet.balances().get(c, entry.get("expected", 0.0))
                held = sum(t["coin_owned"] for t in entry["tf"].values())
                summary.append(
                    f"{c} ~${held * prices[c]:.2f} [" +
                    " ".join(f"{tf}:B{entry['tf'][tf]['buys_done']}"
                             f"S{entry['tf'][tf]['sells_done']}" for tf in TIMEFRAMES) + "]")

            save_state(state)
            bal = wallet.balances()
            log(f"{STABLE} ${bal.get(STABLE, 0.0):.2f} | " +
                (" | ".join(summary) if summary else
                 "no coins adopted - deposit a coin worth $5+"))

            # -- scoreboard: are we beating just holding? --
            base = state.get("baseline")
            if base and active:
                total = bal.get(STABLE, 0.0) + sum(
                    bal.get(c, 0.0) * prices[c] for c in active)
                hodl = base["usd"] + sum(
                    amt * prices[c] for c, amt in base["coins"].items()
                    if c in prices)
                log(f"SCOREBOARD: portfolio ${total:.2f} | just-holding would be "
                    f"${hodl:.2f} | bot edge {total - hodl:+.2f} USD "
                    f"({(total / hodl - 1) * 100:+.2f}%)")

        except KeyboardInterrupt:
            save_state(state)
            log("Stopped by user. State saved.")
            break
        except Exception as e:
            log(f"cycle error (will retry): {e}")

        if once:
            break
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
