"""
╔══════════════════════════════════════════════════════════╗
║         POLYMARKET TRADING BOT — bot.py                  ║
║  Поиск → Фильтрация → Лимитные ордера → Риск-контроль   ║
╚══════════════════════════════════════════════════════════╝

ВАЖНО — типы подписи (SIGNATURE_TYPE в .env):
  0 = EOA         — обычный кошелёк, сам платит газ
  1 = POLY_PROXY  — аккаунт Polymarket через email/Google
  2 = GNOSIS_SAFE — аккаунт Polymarket через MetaMask/Rabby

FUNDER_ADDRESS = proxy-адрес из polymarket.com/settings
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

# ── Загрузка конфига ─────────────────────────────────────
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

# ── Параметры стратегии ──────────────────────────────────
MIN_PRICE       = 0.001
MAX_PRICE       = 0.030
RISK_PCT        = 0.02
MIN_DAYS        = 3
MAX_DAYS        = 365
MAX_PER_CAT     = 2
MIN_SIZE        = 5      # минимум 5 контрактов (требование биржи для лимитных)
PRICE_OFFSET    = 0.001  # ставим чуть выше аска для быстрого исполнения
MARKETS_TO_SCAN = int(os.getenv("MARKETS_TO_SCAN", "200"))

# ── Исключённые ключевые слова ───────────────────────────
EXCLUDED = {
    "elections", "election", "politics", "voting", "president",
    "senate", "congress", "political", "vote", "ballot",
    "democrat", "republican", "midterm", "primary", "caucus",
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


# ════════════════════════════════════════════════════════
#  ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ════════════════════════════════════════════════════════

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


def get_best_ask(client: ClobClient, token_id: str) -> Optional[float]:
    try:
        book = client.get_order_book(token_id)
        asks = book.asks if hasattr(book, "asks") else book.get("asks", [])
        if not asks:
            return None
        prices = [float(a.price if hasattr(a, "price") else a["price"]) for a in asks]
        return min(prices) if prices else None
    except Exception:
        return None


def get_tick_size(client: ClobClient, token_id: str) -> str:
    try:
        ts = client.get_tick_size(token_id)
        return str(ts) if ts else "0.01"
    except Exception:
        return "0.01"


def get_neg_risk(client: ClobClient, token_id: str) -> bool:
    try:
        return bool(client.get_neg_risk(token_id))
    except Exception:
        return False


def calc_size(portfolio: float, price: float) -> float:
    """
    Размер позиции = (портфель * риск%) / цена
    Минимум MIN_SIZE контрактов (требование биржи для лимитных ордеров)
    """
    if price <= 0:
        return float(MIN_SIZE)
    size = (portfolio * RISK_PCT) / price
    return max(round(size, 2), float(MIN_SIZE))


# ════════════════════════════════════════════════════════
#  БОТ
# ════════════════════════════════════════════════════════

class PolymarketBot:

    def __init__(self):
        from eth_account import Account
        signer = Account.from_key(PRIVATE_KEY)

        funder = FUNDER_ADDRESS if FUNDER_ADDRESS else signer.address

        log.info(f"Signature type : {SIGNATURE_TYPE}")
        log.info(f"Funder address : {funder}")

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

    def place_order(
        self,
        token_id: str,
        price: float,
        size: float,
        tick_size: str,
        question: str,
    ) -> Optional[str]:
        tick = float(tick_size)

        # Цена лимитного ордера — чуть выше аска для быстрого исполнения
        order_price = min(price + PRICE_OFFSET, MAX_PRICE)
        order_price = round(round(order_price / tick) * tick, 6)

        cost = round(order_price * size, 4)
        log.info(
            f"  → LIMIT BUY | {question[:50]}"
            f" | price={order_price:.4f} | size={size} | cost=${cost}"
        )

        try:
            # Шаг 1: создаём подписанный лимитный ордер
            signed = self.client.create_order(
                OrderArgs(
                    token_id=token_id,
                    price=order_price,
                    size=size,
                    side=BUY,
                )
            )
            # Шаг 2: отправляем как GTC (Good Till Cancelled) — лимитный
            resp     = self.client.post_order(signed, OrderType.GTC)
            order_id = resp.get("orderID") or resp.get("order_id", "unknown")
            log.info(f"  ✓ Ордер принят: {order_id}")
            return order_id
        except Exception as e:
            log.error(f"  ✗ Ошибка: {e}")
            return None

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

            token_id = token_ids[0]
            if token_id in self.positions:
                continue

            question = m.get("question") or m.get("_event_title", "")
            best_ask = get_best_ask(self.client, token_id)
            if best_ask is None:
                continue
            if not (MIN_PRICE <= best_ask <= MAX_PRICE):
                continue

            days = days_until(m.get("_end_date"))
            if days is None or days < MIN_DAYS or days > MAX_DAYS:
                continue

            if is_election(m):
                continue

            cat = (m.get("_event_tags") or ["other"])[0].lower()
            if seen_cats.get(cat, 0) >= MAX_PER_CAT:
                continue

            size      = calc_size(portfolio, best_ask)
            tick_size = get_tick_size(self.client, token_id)

            log.info(
                f"✔ [{cat.upper()}] {question[:60]}"
                f" | ask={best_ask:.4f} | days={days:.0f} | size={size}"
            )

            order_id = self.place_order(
                token_id=token_id,
                price=best_ask,
                size=size,
                tick_size=tick_size,
                question=question,
            )

            if order_id:
                self.positions[token_id] = {
                    "order_id":  order_id,
                    "question":  question,
                    "category":  cat,
                    "price":     best_ask,
                    "size":      size,
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


# ════════════════════════════════════════════════════════
if __name__ == "__main__":
    if not PRIVATE_KEY:
        log.error("PRIVATE_KEY не задан в .env!")
        exit(1)
    if not FUNDER_ADDRESS:
        log.warning(
            "FUNDER_ADDRESS не задан! Если деньги на proxy-кошельке "
            "Polymarket — добавь FUNDER_ADDRESS=0x... в .env"
        )
    PolymarketBot().run_once()
