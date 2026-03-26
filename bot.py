"""
╔══════════════════════════════════════════════════════════╗
║         POLYMARKET TRADING BOT — bot.py                  ║
║ YES + NO + resting limit orders                         ║
╚══════════════════════════════════════════════════════════╝
"""

import os
import json
import time
import logging
import requests
from datetime import datetime, timezone
from typing import Optional
from dotenv import load_dotenv

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType, ApiCreds
from py_clob_client.order_builder.constants import BUY

load_dotenv()

PRIVATE_KEY    = os.getenv("PRIVATE_KEY")
API_KEY        = os.getenv("POLY_API_KEY")
API_SECRET     = os.getenv("POLY_API_SECRET")
API_PASSPHRASE = os.getenv("POLY_API_PASSPHRASE")
FUNDER_ADDRESS = os.getenv("FUNDER_ADDRESS")
SIGNATURE_TYPE = int(os.getenv("SIGNATURE_TYPE", "2"))

CLOB_HOST  = "https://clob.polymarket.com"
GAMMA_HOST = "https://gamma-api.polymarket.com"
CHAIN_ID   = 137

MIN_PRICE       = float(os.getenv("MIN_PRICE", "0.001"))
MAX_PRICE       = float(os.getenv("MAX_PRICE", "0.030"))
RISK_PCT        = float(os.getenv("RISK_PCT", "0.02"))
MIN_DAYS        = int(os.getenv("MIN_DAYS", "3"))
MAX_DAYS        = int(os.getenv("MAX_DAYS", "365"))
MAX_PER_CAT     = int(os.getenv("MAX_PER_CAT", "2"))
MIN_SIZE        = float(os.getenv("MIN_SIZE", "5"))
MARKETS_TO_SCAN = int(os.getenv("MARKETS_TO_SCAN", "10000"))

TRADE_YES = True
TRADE_NO = True
ALLOW_BOTH_SIDES_SAME_MARKET = True
RESTING_ORDER_TICKS_BELOW_ASK = 1

EXCLUDED = {
    "elections", "election", "politics", "voting", "president",
    "senate", "congress", "political", "vote", "ballot",
    "democrat", "republican", "midterm", "primary", "caucus",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("polybot")


def parse_token_ids(raw) -> list:
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw if x]
    if isinstance(raw, str):
        raw = raw.strip()
        if raw.startswith("["):
            try:
                result = json.loads(raw)
                return [str(x) for x in result if x] if isinstance(result, list) else []
            except Exception:
                return []
        return [raw] if raw else []
    return []


def get_portfolio_value(client: ClobClient) -> float:
    try:
        b = client.get_balance()
        return float(b.get("balance", 0))
    except Exception:
        return float(os.getenv("PORTFOLIO_VALUE", "100"))


def days_until(s: Optional[str]) -> Optional[float]:
    if not s:
        return None
    try:
        end = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return (end - datetime.now(timezone.utc)).total_seconds() / 86400
    except Exception:
        return None


def is_election(market: dict) -> bool:
    text = " ".join([
        market.get("question", ""),
        market.get("description", ""),
        " ".join(market.get("_event_tags", [])),
    ]).lower()
    return any(k in text for k in EXCLUDED)


def fetch_markets() -> list:
    url = (
        f"{GAMMA_HOST}/events"
        f"?active=true&closed=false"
        f"&limit={MARKETS_TO_SCAN}&offset=0"
    )
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        markets = []
        for event in resp.json():
            for m in event.get("markets", []):
                m["_event_tags"]  = [t.get("label", "") for t in event.get("tags", [])]
                m["_event_title"] = event.get("title", "")
                m["_end_date"]    = event.get("endDate") or m.get("endDate")
                markets.append(m)
        log.info(f"Загружено рынков: {len(markets)}")
        return markets
    except Exception as e:
        log.error(f"Ошибка загрузки рынков: {e}")
        return []


def get_order_book_sides(client: ClobClient, token_id: str):
    try:
        book = client.get_order_book(token_id)
        asks = book.asks if hasattr(book, "asks") else book.get("asks", [])
        bids = book.bids if hasattr(book, "bids") else book.get("bids", [])
        return bids, asks
    except Exception:
        return [], []


def get_best_ask(client: ClobClient, token_id: str) -> Optional[float]:
    bids, asks = get_order_book_sides(client, token_id)
    if not asks:
        return None
    try:
        return min(float(a.price if hasattr(a, "price") else a["price"]) for a in asks)
    except Exception:
        return None


def get_best_bid(client: ClobClient, token_id: str) -> Optional[float]:
    bids, asks = get_order_book_sides(client, token_id)
    if not bids:
        return None
    try:
        return max(float(b.price if hasattr(b, "price") else b["price"]) for b in bids)
    except Exception:
        return None


def get_tick_size(client: ClobClient, token_id: str) -> str:
    try:
        ts = client.get_tick_size(token_id)
        return str(ts) if ts else "0.01"
    except Exception:
        return "0.01"


def calc_size(portfolio: float, price: float) -> float:
    if price <= 0:
        return float(MIN_SIZE)
    size = (portfolio * RISK_PCT) / price
    return max(round(size, 2), float(MIN_SIZE))


def calc_resting_buy_price(best_bid: Optional[float], best_ask: float, tick_size: str) -> Optional[float]:
    """
    Passive BUY:
    - строго ниже best ask
    - по возможности на 1 тик выше best bid
    - иначе на несколько тиков ниже ask
    """
    try:
        tick = float(tick_size)
        if tick <= 0 or best_ask <= 0:
            return None

        steps_below_ask = max(1, RESTING_ORDER_TICKS_BELOW_ASK)
        max_resting_price = round(best_ask - tick * steps_below_ask, 6)
        if max_resting_price <= 0:
            return None

        if best_bid is None or best_bid <= 0:
            return max_resting_price

        improved_bid = round(best_bid + tick, 6)
        order_price = min(improved_bid, max_resting_price)

        if order_price >= best_ask:
            order_price = round(best_ask - tick, 6)

        if order_price <= 0 or order_price >= best_ask:
            return None

        return round(order_price, 6)
    except Exception:
        return None


def build_outcomes(token_ids: list) -> list:
    """
    Polymarket binary market:
    token_ids[0] = YES
    token_ids[1] = NO
    """
    outcomes = []
    if len(token_ids) >= 1 and TRADE_YES:
        outcomes.append(("YES", token_ids[0]))
    if len(token_ids) >= 2 and TRADE_NO:
        outcomes.append(("NO", token_ids[1]))
    if len(token_ids) == 1 and TRADE_YES and not outcomes:
        outcomes.append(("YES", token_ids[0]))
    return outcomes


class PolymarketBot:

    def __init__(self):
        from eth_account import Account
        signer = Account.from_key(PRIVATE_KEY)
        funder = FUNDER_ADDRESS if FUNDER_ADDRESS else signer.address

        log.info(f"Signature type : {SIGNATURE_TYPE}")
        log.info(f"Funder address : {funder}")
        log.info("Trade sides are hardcoded: YES=True NO=True allow_both_same_market=True")

        self.client = ClobClient(
            host=CLOB_HOST,
            key=PRIVATE_KEY,
            chain_id=CHAIN_ID,
            signature_type=SIGNATURE_TYPE,
            funder=funder,
        )

        if API_KEY and API_SECRET and API_PASSPHRASE:
            self.client.set_api_creds(ApiCreds(
                api_key=API_KEY,
                api_secret=API_SECRET,
                api_passphrase=API_PASSPHRASE,
            ))
            log.info("API-ключи загружены из .env")
        else:
            log.info("Деривируем API-ключи...")
            creds = self.client.create_or_derive_api_creds()
            self.client.set_api_creds(creds)
            log.info(f"API KEY: {creds.api_key}")

        self.positions: dict = {}
        try:
            if os.path.exists("positions.json"):
                with open("positions.json", "r", encoding="utf-8") as f:
                    self.positions = json.load(f)
        except Exception:
            self.positions = {}

    def place_order(
        self,
        token_id: str,
        outcome: str,
        best_bid: Optional[float],
        best_ask: float,
        size: float,
        tick_size: str,
        question: str,
    ) -> Optional[str]:
        order_price = calc_resting_buy_price(best_bid, best_ask, tick_size)
        if order_price is None:
            log.info(
                f"  … Пропуск {outcome} | {question[:50]}"
                f" | bid={(best_bid or 0):.4f} | ask={best_ask:.4f}"
                f" | нельзя поставить resting BUY"
            )
            return None

        cost = round(order_price * size, 4)
        log.info(
            f"  → RESTING LIMIT BUY [{outcome}] | {question[:50]}"
            f" | bid={(best_bid or 0):.4f} | ask={best_ask:.4f}"
            f" | price={order_price:.4f} | size={size} | cost=${cost}"
        )

        try:
            signed = self.client.create_order(
                OrderArgs(
                    token_id=token_id,
                    price=order_price,
                    size=size,
                    side=BUY,
                )
            )
            resp = self.client.post_order(signed, OrderType.GTC)
            order_id = resp.get("orderID") or resp.get("order_id", "unknown")
            log.info(f"  ✓ Ордер принят [{outcome}]: {order_id}")
            return order_id
        except Exception as e:
            log.error(f"  ✗ Ошибка [{outcome}]: {e}")
            return None

    def has_market_position(self, market_key: str) -> bool:
        for _, pos in self.positions.items():
            if isinstance(pos, dict) and pos.get("market_key") == market_key:
                return True
        return False

    def run_once(self):
        log.info("=" * 60)
        log.info(f"Запуск: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

        portfolio = get_portfolio_value(self.client)
        log.info(f"Портфель: ${portfolio:.2f}")

        markets   = fetch_markets()
        placed    = 0
        seen_cats: dict = {}

        for m in markets:
            token_ids = parse_token_ids(m.get("clobTokenIds"))
            if not token_ids:
                continue

            question = m.get("question") or m.get("_event_title", "")
            market_key = m.get("questionID") or question

            days = days_until(m.get("_end_date"))
            if days is None or days < MIN_DAYS or days > MAX_DAYS:
                continue

            if is_election(m):
                continue

            cat = (m.get("_event_tags") or ["other"])[0].lower()
            if seen_cats.get(cat, 0) >= MAX_PER_CAT:
                continue


            outcomes = build_outcomes(token_ids)
            if not outcomes:
                continue

            log.info(
                f"MARKET outcomes | {question[:60]} | token_ids={token_ids} | outcomes={outcomes}"
            )


            for outcome, token_id in outcomes:
                position_key = f"{token_id}:{outcome}"
                if position_key in self.positions:
                    continue


                best_ask = get_best_ask(self.client, token_id)
                if best_ask is None:
                    continue
                if not (MIN_PRICE <= best_ask <= MAX_PRICE):
                    continue

                best_bid = get_best_bid(self.client, token_id)
                tick_size = get_tick_size(self.client, token_id)
                resting_price = calc_resting_buy_price(best_bid, best_ask, tick_size)
                if resting_price is None:
                    continue

                size = calc_size(portfolio, resting_price)

                log.info(
                    f"✔ [{cat.upper()}] [{outcome}] {question[:60]}"
                    f" | bid={(best_bid or 0):.4f} | ask={best_ask:.4f}"
                    f" | resting={resting_price:.4f} | days={days:.0f} | size={size}"
                )

                order_id = self.place_order(
                    token_id=token_id,
                    outcome=outcome,
                    best_bid=best_bid,
                    best_ask=best_ask,
                    size=size,
                    tick_size=tick_size,
                    question=question,
                )

                if order_id:
                    self.positions[position_key] = {
                        "order_id": order_id,
                        "market_key": market_key,
                        "outcome": outcome,
                        "token_id": token_id,
                        "question": question,
                        "category": cat,
                        "price": resting_price,
                        "size": size,
                        "placed_at": datetime.utcnow().isoformat(),
                    }
                    seen_cats[cat] = seen_cats.get(cat, 0) + 1
                    placed += 1

                time.sleep(0.3)

        log.info("=" * 60)
        log.info(f"Готово! Ордеров размещено: {placed}")

        with open("positions.json", "w", encoding="utf-8") as f:
            json.dump(self.positions, f, indent=2, ensure_ascii=False)
        log.info("Позиции сохранены в positions.json")


if __name__ == "__main__":
    if not PRIVATE_KEY:
        log.error("PRIVATE_KEY не задан в .env!")
        raise SystemExit(1)

    if not FUNDER_ADDRESS:
        log.warning(
            "FUNDER_ADDRESS не задан! Если деньги на proxy-кошельке "
            "Polymarket — добавь FUNDER_ADDRESS=0x... в .env"
        )

    PolymarketBot().run_once()
