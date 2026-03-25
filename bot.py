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
from py_clob_client.constants import BUY

import requests

# ── Загрузка конфига ──────────────────────────────────────
load_dotenv()

PRIVATE_KEY  = os.getenv("PRIVATE_KEY")       # приватный ключ Polygon-кошелька
API_KEY      = os.getenv("POLY_API_KEY")
API_SECRET   = os.getenv("POLY_API_SECRET")
API_PASSPHRASE = os.getenv("POLY_API_PASSPHRASE")

CLOB_HOST   = "https://clob.polymarket.com"
GAMMA_HOST  = "https://gamma-api.polymarket.com"
CHAIN_ID    = 137  # Polygon mainnet

# ── Параметры стратегии ───────────────────────────────────
MIN_PRICE          = 0.001   # минимальная цена контракта ($)
MAX_PRICE          = 0.030   # максимальная цена контракта ($)
RISK_PCT_MIN       = 0.01    # минимальный риск на сделку (1%)
RISK_PCT_MAX       = 0.03    # максимальный риск на сделку (3%)
RISK_PCT           = 0.02    # целевой риск на сделку (2%)
MIN_DAYS_TO_EXPIRY = 3       # минимум дней до истечения
MAX_DAYS_TO_EXPIRY = 365     # максимум дней до истечения
SCAN_INTERVAL_SEC  = 3600    # интервал сканирования (1 час)
MAX_POSITIONS_PER_CATEGORY = 2   # диверсификация: не более 2 позиций в одной категории
PRICE_OFFSET       = 0.001   # отступ от лучшей цены для лимитного ордера
MAX_MARKETS_PER_SCAN = 200   # сколько рынков грузить за раз

# ── Исключённые теги (elections и производные) ─────────────
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
    """Возвращает баланс USDC.e в кошельке (имитация, если SDK не поддерживает)."""
    try:
        balance_info = client.get_balance()
        return float(balance_info.get("balance", 0))
    except Exception:
        # Если метод недоступен — читаем из .env для тестового запуска
        return float(os.getenv("PORTFOLIO_VALUE", "100"))


def days_until(end_date_str: Optional[str]) -> Optional[float]:
    """Количество дней от сейчас до end_date_str (ISO-8601)."""
    if not end_date_str:
        return None
    try:
        end_dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
        now    = datetime.now(timezone.utc)
        return (end_dt - now).total_seconds() / 86400
    except Exception:
        return None


def is_election_market(market: dict) -> bool:
    """True если рынок связан с выборами."""
    text_to_check = " ".join([
        market.get("question", ""),
        market.get("description", ""),
        " ".join(market.get("tags", [])),
        " ".join(t.get("label", "") for t in market.get("tags_obj", [])),
    ]).lower()

    for kw in EXCLUDED_TAGS_LOWER:
        if kw in text_to_check:
            return True
    return False


def fetch_active_markets(limit: int = MAX_MARKETS_PER_SCAN, offset: int = 0) -> list[dict]:
    """
    Загружает активные рынки через Gamma API.
    Возвращает список рыночных объектов.
    """
    url = (
        f"{GAMMA_HOST}/events"
        f"?active=true&closed=false"
        f"&limit={limit}&offset={offset}"
        f"&order=volume_24hr&ascending=false"
    )
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        events = resp.json()
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
    """Лучший аск (минимальная цена продавцов) для токена."""
    try:
        book = client.get_order_book(token_id)
        asks = book.get("asks", [])
        if asks:
            return float(min(a["price"] for a in asks))
        return None
    except Exception as e:
        log.warning(f"Не удалось получить стакан для {token_id}: {e}")
        return None


def get_tick_size(client: ClobClient, token_id: str) -> str:
    """Возвращает тик-сайз рынка (по умолчанию '0.001')."""
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


def calc_order_size(portfolio_value: float, price: float, risk_pct: float = RISK_PCT) -> float:
    """
    Вычисляет количество контрактов при фиксированном риске.
    Риск = price * size  →  size = (portfolio * risk_pct) / price
    Минимум 1 контракт.
    """
    if price <= 0:
        return 0.0
    size = (portfolio_value * risk_pct) / price
    return max(round(size, 2), 1.0)


# ════════════════════════════════════════════════════════════
#  ОСНОВНАЯ ЛОГИКА СКАНИРОВАНИЯ И ТОРГОВЛИ
# ════════════════════════════════════════════════════════════

class PolymarketBot:
    def __init__(self):
        from eth_account import Account
        from py_clob_client.clob_types import ApiCreds

        signer = Account.from_key(PRIVATE_KEY)

        # Инициализация клиента
        self.client = ClobClient(
            host=CLOB_HOST,
            key=PRIVATE_KEY,
            chain_id=CHAIN_ID,
            signature_type=0,       # EOA кошелёк
            funder=signer.address,
        )

        # Устанавливаем API-ключи если заданы
        if API_KEY and API_SECRET and API_PASSPHRASE:
            from py_clob_client.clob_types import ApiCreds
            creds = ApiCreds(
                api_key=API_KEY,
                api_secret=API_SECRET,
                api_passphrase=API_PASSPHRASE,
            )
            self.client.set_api_creds(creds)
        else:
            log.info("API-ключи не заданы — деривируем из приватного ключа...")
            creds = self.client.create_or_derive_api_key()
            self.client.set_api_creds(creds)
            log.info(f"API KEY: {creds.api_key}")

        self.open_positions: dict[str, dict] = {}   # token_id → info
        self.category_counts: dict[str, int]  = {}  # category → count

    # ── Фильтрация рынка ─────────────────────────────────

    def passes_filters(
        self,
        market: dict,
        best_ask: Optional[float],
    ) -> tuple[bool, str]:
        """Возвращает (True, '') или (False, причина)."""

        # 1. Цена контракта
        if best_ask is None:
            return False, "нет данных об аске"
        if not (MIN_PRICE <= best_ask <= MAX_PRICE):
            return False, f"цена {best_ask:.4f} вне диапазона"

        # 2. Срок истечения
        days = days_until(market.get("_end_date"))
        if days is None:
            return False, "нет даты истечения"
        if days < MIN_DAYS_TO_EXPIRY:
            return False, f"до истечения {days:.1f} дн. (< {MIN_DAYS_TO_EXPIRY})"
        if days > MAX_DAYS_TO_EXPIRY:
            return False, f"до истечения {days:.0f} дн. (> {MAX_DAYS_TO_EXPIRY})"

        # 3. Исключаем выборы
        if is_election_market(market):
            return False, "рынок выборов — пропуск"

        # 4. Уже открыта позиция
        token_id = market.get("clobTokenIds", [None])[0]
        if token_id and token_id in self.open_positions:
            return False, "позиция уже открыта"

        # 5. Активный рынок
        if not market.get("active", True):
            return False, "рынок неактивен"

        return True, ""

    def get_category(self, market: dict) -> str:
        tags = market.get("_event_tags", [])
        if tags:
            return tags[0].lower()
        return "other"

    def is_category_allowed(self, category: str) -> bool:
        count = self.category_counts.get(category, 0)
        return count < MAX_POSITIONS_PER_CATEGORY

    # ── Размещение лимитного ордера ───────────────────────

    def place_limit_order(
        self,
        token_id: str,
        price: float,
        size: float,
        tick_size: str,
        neg_risk: bool,
        market_question: str,
    ) -> Optional[str]:
        """
        Выставляет лимитный ордер BUY чуть выше лучшего аска
        для максимально быстрого исполнения.
        """
        # Округляем до тик-сайза
        tick = float(tick_size)
        # Цена = best_ask + offset, но не выше MAX_PRICE
        order_price = min(round(price + PRICE_OFFSET, 4), MAX_PRICE)
        order_price = round(round(order_price / tick) * tick, 6)

        log.info(
            f"  → Ордер BUY | {market_question[:60]}"
            f" | price={order_price:.4f} | size={size} | negRisk={neg_risk}"
        )

        try:
            resp = self.client.create_and_post_order(
                OrderArgs(
                    token_id=token_id,
                    price=order_price,
                    size=size,
                    side=BUY,
                ),
                options={"tickSize": tick_size, "negRisk": neg_risk},
                order_type=OrderType.GTC,
            )
            order_id = resp.get("orderID") or resp.get("order_id", "unknown")
            log.info(f"  ✓ Ордер принят: {order_id}")
            return order_id
        except Exception as e:
            log.error(f"  ✗ Ошибка выставления ордера: {e}")
            return None

    # ── Один цикл сканирования ────────────────────────────

    def scan_and_trade(self):
        log.info("═" * 60)
        log.info(f"Начало сканирования: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

        portfolio_value = get_portfolio_value(self.client)
        log.info(f"Портфель: ${portfolio_value:.2f}")

        # Загружаем рынки
        markets = fetch_active_markets()
        log.info(f"Загружено рынков: {len(markets)}")

        placed_this_cycle = 0
        seen_categories: dict[str, int] = {}   # для диверсификации внутри цикла

        for market in markets:
            token_ids = market.get("clobTokenIds", [])
            if not token_ids:
                continue

            token_id = token_ids[0]   # первый исход (YES)
            question = market.get("question", market.get("_event_title", ""))

            # Получаем лучший аск
            best_ask = get_best_ask(self.client, token_id)

            # Применяем фильтры
            ok, reason = self.passes_filters(market, best_ask)
            if not ok:
                log.debug(f"  ✗ [{question[:50]}] — {reason}")
                continue

            # Категория и диверсификация
            category = self.get_category(market)
            cycle_count = seen_categories.get(category, 0)
            if cycle_count >= MAX_POSITIONS_PER_CATEGORY:
                log.debug(f"  ✗ [{question[:50]}] — лимит категории '{category}'")
                continue

            # Размер позиции
            size = calc_order_size(portfolio_value, best_ask)
            if size <= 0:
                continue

            tick_size = get_tick_size(self.client, token_id)
            neg_risk  = get_neg_risk(self.client, token_id)

            log.info(
                f"✔ СДЕЛКА: [{category.upper()}] {question[:70]}"
                f" | ask={best_ask:.4f} | days={days_until(market.get('_end_date')):.0f}"
            )

            order_id = self.place_limit_order(
                token_id=token_id,
                price=best_ask,
                size=size,
                tick_size=tick_size,
                neg_risk=neg_risk,
                market_question=question,
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
                seen_categories[category] = cycle_count + 1
                self.category_counts[category] = self.category_counts.get(category, 0) + 1
                placed_this_cycle += 1

            # Небольшая пауза, чтобы не перегружать API
            time.sleep(0.3)

        log.info(f"Цикл завершён. Размещено ордеров: {placed_this_cycle}")
        self._save_state()

    # ── Сохранение состояния ──────────────────────────────

    def _save_state(self):
        with open("positions.json", "w", encoding="utf-8") as f:
            json.dump(self.open_positions, f, indent=2, ensure_ascii=False)
        log.info(f"Позиции сохранены: {len(self.open_positions)} записей")

    # ── Главный цикл ─────────────────────────────────────

    def run(self):
        log.info("╔══════════════════════════════════════════╗")
        log.info("║   Polymarket Bot запущен                 ║")
        log.info(f"║   Интервал сканирования: {SCAN_INTERVAL_SEC//60} мин            ║")
        log.info("╚══════════════════════════════════════════╝")

        while True:
            try:
                self.scan_and_trade()
            except KeyboardInterrupt:
                log.info("Бот остановлен вручную.")
                break
            except Exception as e:
                log.error(f"Критическая ошибка цикла: {e}", exc_info=True)

            next_run = datetime.now() + timedelta(seconds=SCAN_INTERVAL_SEC)
            log.info(f"Следующее сканирование: {next_run.strftime('%H:%M:%S')}")
            time.sleep(SCAN_INTERVAL_SEC)


# ════════════════════════════════════════════════════════════
if __name__ == "__main__":
    if not PRIVATE_KEY:
        log.error("PRIVATE_KEY не задан в .env!")
        exit(1)
    bot = PolymarketBot()
    bot.run()
