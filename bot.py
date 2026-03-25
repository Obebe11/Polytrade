"""
╔══════════════════════════════════════════════════════════╗
║         POLYMARKET TRADING BOT — bot.py                  ║
║  Поиск → Фильтрация → Лимитные ордера → Риск-контроль   ║
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

# ── Загрузка конфига ──────────────────────────────────────
load_dotenv()

PRIVATE_KEY    = os.getenv("PRIVATE_KEY")
API_KEY        = os.getenv("POLY_API_KEY")
API_SECRET     = os.getenv("POLY_API_SECRET")
API_PASSPHRASE = os.getenv("POLY_API_PASSPHRASE")

CLOB_HOST  = "https://clob.polymarket.com"
GAMMA_HOST = "https://gamma-api.polymarket.com"
CHAIN_ID   = 137

# ── Параметры стратегии ───────────────────────────────────
MIN_PRICE                  = 0.001
MAX_PRICE                  = 0.030
RISK_PCT                   = 0.02
MIN_DAYS_TO_EXPIRY         = 3
MAX_DAYS_TO_EXPIRY         = 365
MAX_POSITIONS_PER_CATEGORY = 2
PRICE_OFFSET               = 0.001
MARKETS_TO_SCAN            = int(os.getenv("MARKETS_TO_SCAN", "200"))

# ── Исключённые категории ─────────────────────────────────
EXCLUDED_KEYWORDS = {
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

def parse_token_ids(raw) -> list:
    """Безопасно парсит clobTokenIds — может прийти строкой или списком."""
    if not raw:
        return []
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        raw = raw.strip()
        # Если строка выглядит как JSON-массив
        if raw.startswith("["):
            try:
                result = json.loads(raw)
                return result if isinstance(result, list) else []
            except Exception:
                return []
        # Если просто один ID строкой
        if raw:
            return [raw]
    return []


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
        return (end_dt - datetime.now(timezone.utc)).total_seconds() / 86400
    except Exception:
        return None


def is_election_market(market: dict) -> bool:
    text = " ".join([
        market.get("question", ""),
        market.get("description", ""),
        " ".join(market.get("_event_tags", [])),
    ]).lower()
    return any(kw in text for kw in EXCLUDED_KEYWORDS)


def fetch_active_markets() -> list:
    url = (
        f"{GAMMA_HOST}/events"
        f"?active=true&closed=false"
        f"&limit={MARKETS_TO_SCAN}&offset=0"
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
        log.info(f"Загружено рынков: {len(markets)}")
        return markets
    except Exception as e:
        log.error(f"Ошибка загрузки рынков: {e}")
        return []


def get_best_ask(client: ClobClient, token_id: str) -> Optional[float]:
    try:
        book = client.get_order_book(token_id)
        asks = book.asks if hasattr(book, "asks") else book.get("asks", [])
        if not asks:
            return None
        prices = []
        for a in asks:
            p = a.price if hasattr(a, "price") else a.get("price")
            if p is not None:
                prices.append(float(p))
        return min(prices) if prices else None
    except Exception:
        return None


def get_tick_size(client: ClobClient, token_id: str) -> str:
    try:
        ts = client.get_tick_size(token_id)
        return str(ts) if ts else "0.001"
    except Exception:
        return "0.001"


def get_neg_risk(client: ClobClient, token_id: str) -> bool:
    try:
        return bool(client.get_neg_risk(token_id))
    except Exception:
        return False


def calc_order_size(portfolio_value: float, price: float) -> float:
    if price <= 0:
        return 0.0
    return max(round((portfolio_value * RISK_PCT) / price, 2), 1.0)


# ════════════════════════════════════════════════════════════
#  БОТ
# ════════════════════════════════════════════════════════════

class PolymarketBot:

    def __init__(self):
        from eth_account import Account
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
            log.info("Деривируем API-ключи из приватного ключа...")
            creds = self.client.create_or_derive_api_creds()
            self.client.set_api_creds(creds)
            log.info(f"API KEY: {creds.api_key}")

        self.open_positions: dict = {}

    def passes_filters(self, market: dict, best_ask: Optional[float]) -> tuple:
        if best_ask is None:
            return False, "нет стакана"
        if not (MIN_PRICE <= best_ask <= MAX_PRICE):
            return False, f"цена {best_ask:.4f} вне диапазона"

        days = days_until(market.get("_end_date"))
        if days is None:
            return False, "нет даты истечения"
        if days < MIN_DAYS_TO_EXPIRY:
            return False, f"истекает через {days:.1f} дн."
        if days > MAX_DAYS_TO_EXPIRY:
            return False, f"слишком далеко {days:.0f} дн."

        if is_election_market(market):
            return False, "выборы — пропуск"

        return True, ""

    def get_category(self, market: dict) -> str:
        tags = market.get("_event_tags", [])
        return tags[0].lower() if tags else "other"

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
        order_price = min(price + PRICE_OFFSET, MAX_PRICE)
        order_price = round(round(order_price / tick) * tick, 6)

        log.info(f"  → BUY | {question[:55]} | price={order_price:.4f} | size={size}")

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
            log.info(f"  ✓ Принят: {order_id}")
            return order_id
        except Exception as e:
            log.error(f"  ✗ Ошибка: {e}")
            return None

    def run_once(self):
        log.info("═" * 60)
        log.info(f"Запуск: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

        portfolio_value = get_portfolio_value(self.client)
        log.info(f"Портфель: ${portfolio_value:.2f}")

        markets   = fetch_active_markets()
        placed    = 0
        seen_cats: dict = {}

        for market in markets:
            # Парсим token_id безопасно
            raw_ids  = market.get("clobTokenIds")
            token_ids = parse_token_ids(raw_ids)

            if not token_ids:
                continue

            token_id = token_ids[0]

            # Пропускаем уже открытые позиции
            if token_id in self.open_positions:
                continue

            question = market.get("question") or market.get("_event_title", "")
            best_ask = get_best_ask(self.client, token_id)

            ok, reason = self.passes_filters(market, best_ask)
            if not ok:
                log.debug(f"✗ {question[:50]} — {reason}")
                continue

            category    = self.get_category(market)
            cycle_count = seen_cats.get(category, 0)
            if cycle_count >= MAX_POSITIONS_PER_CATEGORY:
                continue

            size      = calc_order_size(portfolio_value, best_ask)
            tick_size = get_tick_size(self.client, token_id)
            neg_risk  = get_neg_risk(self.client, token_id)

            d = days_until(market.get("_end_date"))
            log.info(f"✔ [{category.upper()}] {question[:60]} | ask={best_ask:.4f} | days={d:.0f}")

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
        log.info(f"Готово! Ордеров размещено: {placed}")

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
