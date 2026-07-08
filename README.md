# bot-fleet

Two ladder-strategy crypto trading bots — raw code, bring your own coins.

| File | What it does |
|------|--------------|
| `kraken_bot.py` | Kraken exchange multi-timeframe (1m/1h/1d) ladder bot. Adopts any coin you deposit, buys dips / sells pumps around an anchor price, keeps a USD reserve. Paper-trading mode by default. |
| `multi_bot.py` | Solana on-chain multi-coin ladder bot (Jupiter swaps). Scans the wallet, spins up an independent ladder bot per token, splits usable SOL fairly across them. Dry-run mode by default. |

## Quick start

**Kraken bot** (paper mode, no keys needed):
```
pip3 install requests
python3 kraken_bot.py
```
Set your coins in `WHITELIST` and dip-buying budgets in `SEEDS`. To go live, create a Kraken API key (no withdrawal permission!), save it as `kraken_keys.json`, and set `LIVE = True`. Details are in the comments at the top of the file.

**Solana bot** (dry-run by default):
```
pip3 install solders requests
python3 multi_bot.py
```
Needs a `bot_wallet.json` keypair file for a dedicated bot wallet. Send SOL plus the tokens you want traded to that wallet; every token above `ADOPT_MIN_USD` gets its own ladder. Pin specific coins with `ALWAYS_ADOPT`.

**Never commit `kraken_keys.json` or `bot_wallet.json`** — the included `.gitignore` blocks them.

## Disclaimer

For educational purposes. Trading cryptocurrencies is risky; use at your own risk and never trade money you cannot afford to lose.
