#!/usr/bin/env python3
"""
multi_bot.py (v3) - Multi-coin ladder bot fleet.

One process, many bots: the supervisor scans the wallet every cycle. Every
SPL token worth more than ADOPT_MIN_USD gets its own independent ladder bot
(own anchor, own cycle state, own kill-switch). Send a new coin to the wallet
and a bot for it spins up within one poll. Send more SOL and every bot's
buying power grows.

Capital model:
  * Each token's bankroll = value of that token held + an equal share of the
    wallet's usable SOL (total SOL minus gas reserve, split across bots).
  * Position cap per token = its bankroll * MAX_POSITION_FRACTION.

Safety:
  * Tokens are only adopted above ADOPT_MIN_USD - scam dust airdrops (which
    anyone can send to any Solana address) are ignored. Tokens Jupiter won't
    price are ignored too, which filters honeypots and dead coins.
  * Manual sells/withdrawals per token are detected and reconciled: loud log,
    cycle reset, carry on. Same reconciliation as v2, but per coin.
  * DRY_RUN = True by default. Watch the fleet before arming it.

Usage:
    pip3 install solders requests
    python3 multi_bot.py
"""

import base64
import csv
import json
import os
import time
from datetime import datetime, timezone

import requests
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

# ============================ CONFIG =========================================
DRY_RUN = True                # flip to False to trade for real
ADOPT_MIN_USD = 5.0           # ignore tokens worth less (anti-dust/scam guard)
# Coins to ALWAYS adopt regardless of value (your deliberate holdings),
# e.g. ["YourTokenMintAddress..."]:
ALWAYS_ADOPT = []
GAS_RESERVE_SOL = 0.05        # SOL never spent - hard floor, re-checked live
                              # before every buy so big dips can't drain gas
RESERVE_PCT = 0.15            # dynamic reserve: keep at least this fraction of
                              # the SOL pile untouchable (profit ratchet - the
                              # bigger your banked gains, the more is protected)


def sol_reserve(sol_balance):
    """Untouchable SOL: the flat gas floor or RESERVE_PCT of the pile,
    whichever is larger."""
    return max(GAS_RESERVE_SOL, sol_balance * RESERVE_PCT)
MAX_POSITION_FRACTION = 1.15  # per-token cap = its bankroll * this
MIN_TRADE_USD = 0.50

# Ladder parameters (applied to every coin; per-coin overrides below)
STEP_PCT = 0.08
BUY_MULT = 1.4
SELL_MULT = 1.25
MAX_BUY_LEVELS = 8            # deep ladder: grab dips down to -64% from anchor
MAX_SELL_LEVELS = 5
BASE_SELL_FRAC = 0.12         # lighter skim: keep more of the bag alive into a pump
TRAIL_PCT = 0.15
TRAIL_PCT_MOON = 0.25         # wider trail once a coin has 2x'd: don't eject a runner
                              # on the first routine pullback
KILL_PCT = 1.0                # kill switch DISABLED: never panic-sell a dip, only sell into pumps

# Fraction of every DEPOSIT that is a permanent moon bag: the bot never sells
# it, no matter how high it goes. Captures the big-pump thesis; the remaining
# tradable bag is what the ladder churns. Rug risk: the moon bag rides to zero.
MOON_BAG_FRAC = 0.35

# Optional per-coin parameter overrides, keyed by mint address, e.g.:
# OVERRIDES = {"SomeMintAddress...": {"STEP_PCT": 0.05, "BUY_MULT": 2.0}}
OVERRIDES = {}

POLL_SECONDS = 300
SLIPPAGE_BPS = 250

SOL_MINT = "So11111111111111111111111111111111111111112"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

RPC = "https://api.mainnet-beta.solana.com"
JUP_PRICE = "https://lite-api.jup.ag/price/v3"
JUP_QUOTE = "https://lite-api.jup.ag/swap/v1/quote"
JUP_SWAP = "https://lite-api.jup.ag/swap/v1/swap"

WALLET_FILE = "bot_wallet.json"
STATE_FILE = "multi_state.json"
LOG_FILE = "trades_log.csv"

RECONCILE_TOLERANCE = 0.02
# =============================================================================


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def log(msg):
    print(f"[{now()}] {msg}", flush=True)


def short(mint):
    return mint[:4] + ".." + mint[-4:]


# --------------------------- chain helpers ------------------------------------

def rpc_call(method, params):
    r = requests.post(RPC, json={"jsonrpc": "2.0", "id": 1,
                                 "method": method, "params": params}, timeout=30)
    r.raise_for_status()
    j = r.json()
    if "error" in j:
        raise RuntimeError(j["error"])
    return j["result"]


def get_sol_balance(pubkey):
    return rpc_call("getBalance", [pubkey])["value"] / 1e9


def get_all_tokens(pubkey):
    """Return {mint: (ui_amount, raw_amount)} for every SPL token held,
    covering both the legacy token program and Token-2022 (pump.fun etc.)."""
    out = {}
    for program in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM):
        res = rpc_call("getTokenAccountsByOwner",
                       [pubkey, {"programId": program}, {"encoding": "jsonParsed"}])
        for acc in res.get("value", []):
            info = acc["account"]["data"]["parsed"]["info"]
            amt = info["tokenAmount"]
            ui = float(amt["uiAmount"] or 0)
            if ui > 0:
                out[info["mint"]] = (ui, int(amt["amount"]))
    return out


def get_prices(mints):
    """Return {mint: usd_price} for mints Jupiter can price (others omitted)."""
    ids = ",".join(list(mints) + [SOL_MINT])
    r = requests.get(JUP_PRICE, params={"ids": ids}, timeout=30)
    r.raise_for_status()
    d = r.json()
    return {m: float(v["usdPrice"]) for m, v in d.items() if v and "usdPrice" in v}


def jupiter_swap(kp, input_mint, output_mint, amount_raw):
    q = requests.get(JUP_QUOTE, params={
        "inputMint": input_mint, "outputMint": output_mint,
        "amount": str(amount_raw), "slippageBps": SLIPPAGE_BPS,
    }, timeout=30)
    q.raise_for_status()
    quote = q.json()
    s = requests.post(JUP_SWAP, json={
        "quoteResponse": quote,
        "userPublicKey": str(kp.pubkey()),
        "wrapAndUnwrapSol": True,
        "dynamicComputeUnitLimit": True,
        "prioritizationFeeLamports": "auto",
    }, timeout=30)
    s.raise_for_status()
    raw = base64.b64decode(s.json()["swapTransaction"])
    tx = VersionedTransaction.from_bytes(raw)
    signed = VersionedTransaction(tx.message, [kp])
    return rpc_call("sendTransaction",
                    [base64.b64encode(bytes(signed)).decode(),
                     {"encoding": "base64", "skipPreflight": False, "maxRetries": 3}])


def log_trade(mint, side, usd, price, sig):
    new = not os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time_utc", "mint", "side", "usd", "price_usd", "tx_or_mode"])
        w.writerow([now(), mint, side, f"{usd:.2f}", f"{price:.10f}", sig or "DRY_RUN"])


# ------------------------------ per-coin bot ----------------------------------

def fresh_state(price):
    return {"anchor": price, "buys_done": 0, "sells_done": 0, "peak": price,
            "moon_armed": False, "entered": False, "expected_raw": None,
            "sim_usd": 0.0, "retired": False, "net_sol_usd": 0.0}


def param(mint, name, default):
    return OVERRIDES.get(mint, {}).get(name, default)


class CoinBot:
    """One ladder instance for one mint."""

    def __init__(self, mint, st):
        self.mint = mint
        self.st = st
        self.units = sum(param(mint, "BUY_MULT", BUY_MULT) ** i
                         for i in range(param(mint, "MAX_BUY_LEVELS", MAX_BUY_LEVELS) + 1))

    def p(self, name, default):
        return param(self.mint, name, default)

    def buy(self, kp, usd, price, sol_price, usable_sol_usd, tag):
        if usd < MIN_TRADE_USD:
            return False
        if DRY_RUN:
            log(f"[{short(self.mint)}] DRY_RUN {tag}: would buy ${usd:.2f} @ {price:.8f}")
            log_trade(self.mint, tag, usd, price, None)
            self.st["sim_usd"] += usd
            self.st["net_sol_usd"] = self.st.get("net_sol_usd", 0.0) + usd
            return True
        usd = min(usd, usable_sol_usd)
        # HARD FLOOR: re-check the live SOL balance right before spending, so
        # stale cycle-start budgets or accumulated fees can never breach the
        # gas reserve. The wallet must always be able to pay for its own exits.
        try:
            live_sol = get_sol_balance(str(kp.pubkey()))
        except Exception:
            log(f"[{short(self.mint)}] skip {tag}: could not verify SOL balance")
            return False
        usd = min(usd, max(live_sol - sol_reserve(live_sol), 0.0) * sol_price)
        if usd < MIN_TRADE_USD:
            log(f"[{short(self.mint)}] skip {tag}: not enough SOL above reserve")
            return False
        try:
            sig = jupiter_swap(kp, SOL_MINT, self.mint, int(usd / sol_price * 1e9))
            log(f"[{short(self.mint)}] {tag}: ${usd:.2f} sent, tx {sig}")
            log_trade(self.mint, tag, usd, price, sig)
            self.st["net_sol_usd"] = self.st.get("net_sol_usd", 0.0) + usd
            # balance changed by our own trade: skip drift detection for one
            # cycle so a slow-confirming tx isn't mistaken for a manual withdrawal
            self.st["expected_raw"] = None
            time.sleep(8)
            return True
        except Exception as e:
            log(f"[{short(self.mint)}] {tag} FAILED: {e}")
            return False

    def sell(self, kp, frac, price, ui, raw, tag):
        frac = min(frac, 1.0)
        if DRY_RUN:
            usd = self.st["sim_usd"] * frac
            if usd < 0.01:
                return False
            log(f"[{short(self.mint)}] DRY_RUN {tag}: would sell {frac*100:.0f}% (~${usd:.2f})")
            log_trade(self.mint, tag, usd, price, None)
            self.st["sim_usd"] *= (1 - frac)
            self.st["net_sol_usd"] = self.st.get("net_sol_usd", 0.0) - usd
            return True
        # Moon bag is untouchable: only the tradable portion is ever sold.
        moon_raw = int(self.st.get("moon_raw") or 0)
        sellable = max(raw - moon_raw, 0)
        amount = int(sellable * frac)
        usd = (amount / raw) * ui * price if raw > 0 else 0.0
        if amount <= 0 or (usd < MIN_TRADE_USD and frac < 1.0):
            return False
        try:
            sig = jupiter_swap(kp, self.mint, SOL_MINT, amount)
            log(f"[{short(self.mint)}] {tag}: {frac*100:.0f}% (~${usd:.2f}) sent, tx {sig}")
            log_trade(self.mint, tag, usd, price, sig)
            self.st["net_sol_usd"] = self.st.get("net_sol_usd", 0.0) - usd
            # balance changed by our own trade: skip drift detection for one
            # cycle so a slow-confirming tx isn't mistaken for a manual withdrawal
            self.st["expected_raw"] = None
            time.sleep(8)
            return True
        except Exception as e:
            log(f"[{short(self.mint)}] {tag} FAILED: {e}")
            return False

    def reconcile(self, raw, price, ui):
        st = self.st
        # moon bag floor: at least MOON_BAG_FRAC of the current balance is
        # untouchable. Ratchets up (never down) so raising the fraction in
        # OVERRIDES takes effect on the next cycle, and sells never shrink it.
        moon_floor = int(raw * self.p("MOON_BAG_FRAC", MOON_BAG_FRAC))
        if st.get("moon_raw") is None or st["moon_raw"] < moon_floor:
            st["moon_raw"] = moon_floor
        if DRY_RUN or st["expected_raw"] is None:
            st["expected_raw"] = raw
            return
        expected = st["expected_raw"]
        drift = (raw - expected) / max(expected, 1)
        if drift < -RECONCILE_TOLERANCE:
            log(f"[{short(self.mint)}] MANUAL INTERVENTION: balance dropped "
                f"{abs(drift)*100:.0f}% outside bot trades - resetting cycle.")
            log_trade(self.mint, "MANUAL_OUT", ui * price, price, "external")
            anchor = price
            st.update(fresh_state(anchor))
            st["entered"] = raw > 0
            st["moon_raw"] = int(raw * self.p("MOON_BAG_FRAC", MOON_BAG_FRAC))
        elif drift > RECONCILE_TOLERANCE:
            log(f"[{short(self.mint)}] tokens deposited externally (+{drift*100:.0f}%) "
                f"- folded into managed position.")
            # a slice of every deposit joins the permanent moon bag
            st["moon_raw"] = int(st.get("moon_raw") or 0) + \
                int((raw - expected) * self.p("MOON_BAG_FRAC", MOON_BAG_FRAC))
        st["expected_raw"] = raw

    def step(self, kp, price, sol_price, ui, raw, sol_budget_usd):
        """One strategy tick for this coin. Returns SOL USD spent (approx)."""
        st = self.st
        step_pct = self.p("STEP_PCT", STEP_PCT)
        buy_mult = self.p("BUY_MULT", BUY_MULT)
        sell_mult = self.p("SELL_MULT", SELL_MULT)
        max_buys = self.p("MAX_BUY_LEVELS", MAX_BUY_LEVELS)
        max_sells = self.p("MAX_SELL_LEVELS", MAX_SELL_LEVELS)
        base_sell = self.p("BASE_SELL_FRAC", BASE_SELL_FRAC)
        trail = self.p("TRAIL_PCT", TRAIL_PCT)
        # scenario-aware trail: a coin that has 2x'd is a runner - give it
        # room to breathe instead of ejecting on the first routine pullback
        if st["peak"] >= st["anchor"] * 2:
            trail = self.p("TRAIL_PCT_MOON", TRAIL_PCT_MOON)
        kill = self.p("KILL_PCT", KILL_PCT)

        spent = 0.0
        st["peak"] = max(st["peak"], price)
        pos_usd = st["sim_usd"] if DRY_RUN else ui * price
        bankroll = pos_usd + sol_budget_usd
        base_buy = round(bankroll / self.units, 2)
        max_pos = bankroll * MAX_POSITION_FRACTION

        if not st["entered"]:
            if pos_usd > MIN_TRADE_USD:
                st["entered"] = True   # adopted coin arrives as a position
                log(f"[{short(self.mint)}] adopted existing ~${pos_usd:.2f} position, "
                    f"anchor {price:.8f}")
            elif self.buy(kp, base_buy, price, sol_price, sol_budget_usd, "ENTRY"):
                st["entered"] = True
                spent += base_buy

        while st["buys_done"] < max_buys:
            lvl = st["anchor"] * (1 - step_pct * (st["buys_done"] + 1))
            if price <= lvl:
                size = base_buy * (buy_mult ** (st["buys_done"] + 1))
                if pos_usd + size > max_pos:
                    break
                if self.buy(kp, size, price, sol_price, sol_budget_usd - spent,
                            f"LADDER_BUY_L{st['buys_done']+1}"):
                    st["buys_done"] += 1
                    spent += size
                    pos_usd += size
                else:
                    break
            else:
                break

        deepest = st["anchor"] * (1 - step_pct * max_buys)
        if st["buys_done"] >= max_buys and price < deepest * (1 - kill):
            log(f"[{short(self.mint)}] KILL SWITCH - liquidating")
            if self.sell(kp, 1.0, price, ui, raw, "KILL"):
                st.update(fresh_state(price))

        while st["sells_done"] < max_sells:
            lvl = st["anchor"] * (1 + step_pct * (st["sells_done"] + 1))
            if price >= lvl and pos_usd > MIN_TRADE_USD:
                frac = min(base_sell * (sell_mult ** st["sells_done"]), 1.0)
                if self.sell(kp, frac, price, ui, raw, f"LADDER_SELL_L{st['sells_done']+1}"):
                    st["sells_done"] += 1
                    pos_usd *= (1 - frac)
                    if st["sells_done"] >= 2:
                        st["moon_armed"] = True
                else:
                    break
            else:
                break

        if st["moon_armed"] and pos_usd > MIN_TRADE_USD and price < st["peak"] * (1 - trail):
            log(f"[{short(self.mint)}] TRAILING STOP - taking profit, resetting cycle")
            if self.sell(kp, 1.0, price, ui, raw, "TRAIL"):
                st.update(fresh_state(price))

        return spent


# ------------------------------ supervisor ------------------------------------

def load_states():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_states(states):
    with open(STATE_FILE, "w") as f:
        json.dump(states, f, indent=2)


def main():
    with open(WALLET_FILE) as f:
        kp = Keypair.from_bytes(bytes(json.load(f)))
    pub = str(kp.pubkey())

    mode = "DRY RUN (no real trades)" if DRY_RUN else "LIVE - REAL MONEY"
    log(f"Fleet bot v3 starting | {mode}")
    log(f"Wallet {pub} | adopt threshold ${ADOPT_MIN_USD} | "
        f"reserve max({GAS_RESERVE_SOL} SOL, {RESERVE_PCT*100:.0f}% of pile)")

    states = load_states()

    while True:
        try:
            holdings = get_all_tokens(pub)
            sol = get_sol_balance(pub)
            prices = get_prices(set(holdings) | set(states))
            sol_price = prices.get(SOL_MINT)
            if not sol_price:
                raise RuntimeError("no SOL price this cycle")

            # ---- adopt new coins ----
            for mint, (ui, raw) in holdings.items():
                if mint in states or mint == SOL_MINT:
                    continue
                price = prices.get(mint)
                if price is None:
                    continue                      # unpriceable = ignored (scam/dead)
                value = ui * price
                if value >= ADOPT_MIN_USD or mint in ALWAYS_ADOPT:
                    states[mint] = fresh_state(price)
                    states[mint]["entered"] = value > MIN_TRADE_USD
                    states[mint]["expected_raw"] = raw
                    log(f"[{short(mint)}] NEW COIN ADOPTED: ~${value:.2f} held. "
                        f"Bot instance started, anchor {price:.8f}")

            # ---- retire empty bots ----
            for mint in list(states):
                ui, raw = holdings.get(mint, (0.0, 0))
                st = states[mint]
                if (ui * prices.get(mint, 0) < 0.25 and not st["entered"]
                        and st["buys_done"] == 0):
                    continue  # freshly reset, waiting to re-enter; keep it

            # ---- run each bot ----
            active = [m for m in states if prices.get(m)]
            usable_sol_usd = max(sol - sol_reserve(sol), 0.0) * sol_price

            # Fair-share budgets: each coin's lifetime SOL draw is capped at an
            # equal slice of the total pool (SOL still in the wallet + SOL each
            # coin has already net-consumed). One coin dumping hard can only
            # burn ITS slice; the others keep theirs. Selling into a pump
            # repays the coin's ledger, freeing budget to buy dips again.
            # Adding SOL to the wallet grows every coin's slice automatically.
            nets = {m: max(states[m].get("net_sol_usd", 0.0), 0.0) for m in active}
            total_pool = usable_sol_usd + sum(nets.values())
            fair_share = total_pool / max(len(active), 1)
            budgets = {m: min(max(fair_share - nets[m], 0.0), usable_sol_usd)
                       for m in active}

            summary = []
            for mint in active:
                ui, raw = holdings.get(mint, (0.0, 0))
                price = prices[mint]
                bot = CoinBot(mint, states[mint])
                bot.reconcile(raw, price, ui)
                bot.step(kp, price, sol_price, ui, raw, budgets[mint])
                # (expected_raw is cleared by buy/sell themselves; reconcile
                # re-syncs it from a confirmed balance on the next cycle)
                pos = states[mint]["sim_usd"] if DRY_RUN else ui * price
                summary.append(f"{short(mint)} ${pos:.2f} "
                               f"B{states[mint]['buys_done']}/S{states[mint]['sells_done']}")

            save_states(states)
            log(f"SOL ${sol*sol_price:.2f} | {len(active)} bot(s): " +
                (" | ".join(summary) if summary else "none - send a coin or SOL"))

        except KeyboardInterrupt:
            log("Stopped by user. State saved.")
            save_states(states)
            break
        except Exception as e:
            log(f"loop error (will retry): {e}")

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
