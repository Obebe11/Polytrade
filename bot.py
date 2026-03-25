"""
╔══════════════════════════════════════════════════════════╗
║         POLYMARKET TRADING BOT — bot.py                  ║
║  Поиск → Фильтрация → Лимитные ордера → Риск-контроль   ║
╚══════════════════════════════════════════════════════════╝
"""

import os
import time
import logging
import json
from datetime import datetime, timezone, timedelta
from typing import Optional
from dotenv import load_dotenv

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType

import requests

# ── Загрузка конфига ──────────────────────────────────────
load_dotenv()

PRIVATE_KEY    = os.getenv("PRIVATE_KEY")
API_KEY        = os.getenv("POLY_API_KEY")
API_SECRET     = os.getenv("POLY_API_SECRET")
API_PASSPHRASE = os.getenv("POLY_API_PASSPHRASE")

CLOB_HOST  = "https://clob.polymarket.com"
GAMMA_HOST = "https://gamma-api.polymarket.com"
CHAIN_ID   = 137  # Polygon mainnet

# ── Параметры стратегии ───────────────────────────────────
MIN_PRICE          = 0.001
MAX_PRICE          = 0.030
RISK_PCT           = 0.02    # 2% портфеля на сделку
MIN_DAYS_TO_EXPIRY = 3
MAX_DAYS_TO_EXPIRY = 365
MAX_POSITIONS_PER_CATEGORY = 2
PRICE_OFFSET       = 0.001

# Сколько рынков обработать за один запуск
MARKETS_TO_SCAN = int(os.getenv("MARKETS_TO_SCAN", "200"))

# ── Исключённые теги (elections) ──────────────────────────
EXCLUDED_TAGS_LOWER = {
    "elections", "election", "politics", "voting", "president",
    "senate", "congress", "political", "vote", "ballot", "democrat",
    "republican", "midterm", "primary", "caucus",
}

# ── Логгер ───────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("polybot")


# ════════════════════════════════════════════════════════════
#  ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ════════════════════════════════════════════════════════════

def get_portfolio_value(client: ClobClient) -> float:
    try:
        balance_info = client.get_balance()
        return float(balance_info.get("balance", 0))
    except Exception:
        return float(os.getenv("PORTFOLIO_VALUE", "100"))


def days_until(end_date_str: Optional[str]) -> Optional[float]:
    if not end_date_str:
        return None
    try:
        end_dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
        now    = datetime.now(timezone.utc)
        return (end_dt - now).total_seconds() / 86400
    except Exception:
        return None


def is_election_market(market: dict) -> bool:
    text = " ".join([
        market.get("question", ""),
        market.get("description", ""),
        " ".join(market.get("_event_tags", [])),
    ]).lower()
    return any(kw in text for kw in EXCLUDED_TAGS_LOWER)


def fetch_active_markets(limit: int = MARKETS_TO_SCAN) -> list:
    url = (
        f"{GAMMA_HOST}/events"
        f"?active=true&closed=false"
        f"&limit={limit}&offset=0"
        f"&order=volume_24hr&ascending=false"
    )
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        events  = resp.json()
        markets = []
        for event in events:
            for market in event.get("markets", []):
                market["_event_tags"]  = [t.get("label", "") for t in event.get("tags", [])]
                market["_event_title"] = event.get("title", "")
                market["_end_date"]    = event.get("endDate") or market.get("endDate")
                markets.append(market)
        return markets
    except Exception as e:
        log.error(f"Ошибка загрузки рынков: {e}")
        return []


def get_best_ask(client: ClobClient, token_id: str) -> Optional[float]:
    try:
        book = client.get_order_book(token_id)
        asks = book.asks if hasattr(book, "asks") else book.get("asks", [])
        if asks:
            prices = [float(a.price if hasattr(a, "price") else a["price"]) for a in asks]
            return min(prices)
        return None
    except Exception as e:
        log.warning(f"Не удалось получить стакан для {token_id}: {e}")
        return None


def get_tick_size(client: ClobClient, token_id: str) -> str:
    try:
        ts = client.get_tick_size(token_id)
        return ts if ts else "0.001"
    except Exception:
        return "0.001"


def get_neg_risk(client: ClobClient, token_id: str) -> bool:
    try:
        return client.get_neg_risk(token_id)
    except Exception:
        return False


def calc_order_size(portfolio_value: float, price: float) -> float:
    if price <= 0:
        return 0.0
    size = (portfolio_value * RISK_PCT) / price
    return max(round(size, 2), 1.0)


# ════════════════════════════════════════════════════════════
#  ОСНОВНОЙ КОД БОТА
# ════════════════════════════════════════════════════════════

class PolymarketBot:
    def __init__(self):
        from eth_account import Account
        from py_clob_client.clob_types import ApiCreds

        signer = Account.from_key(PRIVATE_KEY)

        self.client = ClobClient(
            host=CLOB_HOST,
            key=PRIVATE_KEY,
            chain_id=CHAIN_ID,
            signature_type=0,
            funder=signer.address,
        )

        if API_KEY and API_SECRET and API_PASSPHRASE:
            creds = ApiCreds(
                api_key=API_KEY,
                api_secret=API_SECRET,
                api_passphrase=API_PASSPHRASE,
            )
            self.client.set_api_creds(creds)
            log.info("API-ключи загружены из .env")
        else:
            log.info("API-ключи не заданы — деривируем из приватного ключа...")
            creds = self.client.create_or_derive_api_creds()
            self.client.set_api_creds(creds)
            log.info(f"API KEY: {creds.api_key}")

        self.open_positions: dict = {}
        self.category_counts: dict = {}

    # ── Фильтрация ────────────────────────────────────────

    def passes_filters(self, market: dict, best_ask: Optional[float]) -> tuple:
        if best_ask is None:
            return False, "нет данных об аске"
        if not (MIN_PRICE <= best_ask <= MAX_PRICE):
            return False, f"цена {best_ask:.4f} вне диапазона"

        days = days_until(market.get("_end_date"))
        if days is None:
            return False, "нет даты истечения"
        if days < MIN_DAYS_TO_EXPIRY:
            return False, f"до истечения {days:.1f} дн."
        if days > MAX_DAYS_TO_EXPIRY:
            return False, f"до истечения {days:.0f} дн. — слишком долго"

        if is_election_market(market):
            return False, "рынок выборов — пропуск"

        token_id = (market.get("clobTokenIds") or [None])[0]
        if token_id and token_id in self.open_positions:
            return False, "позиция уже открыта"

        return True, ""

    def get_category(self, market: dict) -> str:
        tags = market.get("_event_tags", [])
        return tags[0].lower() if tags else "other"

    # ── Лимитный ордер ────────────────────────────────────

    def place_limit_order(
        self,
        token_id: str,
        price: float,
        size: float,
        tick_size: str,
        neg_risk: bool,
        question: str,
    ) -> Optional[str]:
        tick = float(tick_size)
        order_price = min(round(price + PRICE_OFFSET, 4), MAX_PRICE)
        order_price = round(round(order_price / tick) * tick, 6)

        log.info(
            f"  → Ордер BUY | {question[:60]}"
            f" | price={order_price:.4f} | size={size}"
        )

        try:
            resp = self.client.create_and_post_order(
                OrderArgs(
                    token_id=token_id,
                    price=order_price,
                    size=size,
                    side="BUY",
                ),
                options={"tickSize": tick_size, "negRisk": neg_risk},
                order_type=OrderType.GTC,
            )
            order_id = resp.get("orderID") or resp.get("order_id", "unknown")
            log.info(f"  ✓ Ордер принят: {order_id}")
            return order_id
        except Exception as e:
            log.error(f"  ✗ Ошибка ордера: {e}")
            return None

    # ── Один запуск ───────────────────────────────────────

    def run_once(self):
        log.info("═" * 60)
        log.info(f"Запуск бота: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        log.info(f"Сканируем до {MARKETS_TO_SCAN} рынков...")

        portfolio_value = get_portfolio_value(self.client)
        log.info(f"Портфель: ${portfolio_value:.2f}")

        markets = fetch_active_markets()
        log.info(f"Загружено рынков: {len(markets)}")

        placed    = 0
        seen_cats: dict = {}

        for market in markets:
            token_ids = market.get("clobTokenIds") or []
            if not token_ids:
                continue

            token_id = token_ids[0]
            question = market.get("question") or market.get("_event_title", "")
            best_ask = get_best_ask(self.client, token_id)

            ok, reason = self.passes_filters(market, best_ask)
            if not ok:
                log.debug(f"  ✗ [{question[:50]}] — {reason}")
                continue

            category    = self.get_category(market)
            cycle_count = seen_cats.get(category, 0)
            if cycle_count >= MAX_POSITIONS_PER_CATEGORY:
                log.debug(f"  ✗ [{question[:50]}] — лимит категории '{category}'")
                continue

            size      = calc_order_size(portfolio_value, best_ask)
            tick_size = get_tick_size(self.client, token_id)
            neg_risk  = get_neg_risk(self.client, token_id)

            log.info(
                f"✔ [{category.upper()}] {question[:65]}"
                f" | ask={best_ask:.4f}"
                f" | days={days_until(market.get('_end_date')):.0f}"
            )

            order_id = self.place_limit_order(
                token_id=token_id,
                price=best_ask,
                size=size,
                tick_size=tick_size,
                neg_risk=neg_risk,
                question=question,
            )

            if order_id:
                self.open_positions[token_id] = {
                    "order_id":  order_id,
                    "question":  question,
                    "category":  category,
                    "price":     best_ask,
                    "size":      size,
                    "placed_at": datetime.utcnow().isoformat(),
                }
                seen_cats[category] = cycle_count + 1
                placed += 1

            time.sleep(0.3)

        log.info("═" * 60)
        log.info(f"Готово! Размещено ордеров: {placed}")

        with open("positions.json", "w", encoding="utf-8") as f:
            json.dump(self.open_positions, f, indent=2, ensure_ascii=False)
        log.info("Позиции сохранены в positions.json")


# ════════════════════════════════════════════════════════════
if __name__ == "__main__":
    if not PRIVATE_KEY:
        log.error("PRIVATE_KEY не задан в .env!")
        exit(1)
    bot = PolymarketBot()
    bot.run_once()
