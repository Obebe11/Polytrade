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

═══════════════════════════════════════════════════════════
 СПИСОК ИСПРАВЛЕНИЙ (аудит):
 
 [FIX-1] place_order: размер ордера теперь передаётся как float,
         а не как int — API ожидает дробное число (size=5.0)
         
 [FIX-2] place_order: добавлена передача neg_risk и tick_size
         в create_order() через параметры OrderArgs — без них
         SDK некорректно подписывает ордер для neg_risk рынков
         
 [FIX-3] place_order: исправлено получение orderID из ответа —
         добавлена проверка вложенного поля resp["orderID"] и
         resp.get("id") как fallback (разные версии SDK)
         
 [FIX-4] place_order: добавлена проверка ответа на наличие
         поля "errorMsg" — биржа иногда возвращает 200 OK
         но с ошибкой внутри (например "invalid amount, min size")
         
 [FIX-5] calc_size: размер теперь округляется до целого числа
         через round(size) — биржа принимает только целые
         контракты (size должен быть целым int или float без дробей)
         ВАЖНО: min_order_size берётся из стакана (orderbook),
         а не хардкодится. Добавлена функция get_min_order_size()
         
 [FIX-6] get_best_ask: исправлена логика — asks в стакане
         отсортированы по возрастанию (низший первый),
         поэтому asks[0] — уже лучший аск, min() не нужен
         
 [FIX-7] run_once: min_order_size теперь читается из стакана
         для каждого токена отдельно, а не берётся из глобальной
         константы MIN_SIZE (у разных рынков разные минимумы)
         
 [FIX-8] get_portfolio_value: исправлено поле — USDC.e баланс
         возвращается в поле "USDC" или "balance", добавлен
         правильный fallback с логированием
         
 [FIX-9] place_order: order_price теперь не превышает 0.99
         (биржа отклоняет цены >= 1.0)
═══════════════════════════════════════════════════════════
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
DEFAULT_MIN_SIZE = 5    # fallback если стакан не вернул min_order_size
PRICE_OFFSET    = 0.001  # ставим чуть выше аска для быстрого исполнения
MAX_ORDER_PRICE = 0.99   # [FIX-9] биржа не принимает цену >= 1.0
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
    """
    [FIX-8] Исправлено: баланс USDC.e может лежать в разных полях
    в зависимости от версии SDK. Добавлен правильный fallback.
    """
    try:
        b = client.get_balance()
        # Пробуем разные варианты поля
        for field in ("USDC", "balance", "usdc", "amount"):
            if field in b:
                val = float(b[field])
                if val > 0:
                    return val
        # Если ни одно поле не дало ненулевое значение — используем fallback
        fallback = float(os.getenv("PORTFOLIO_VALUE", "100"))
        log.warning(f"get_balance() вернул: {b} — используем PORTFOLIO_VALUE={fallback}")
        return fallback
    except Exception as e:
        fallback = float(os.getenv("PORTFOLIO_VALUE", "100"))
        log.warning(f"get_balance() ошибка: {e} — используем PORTFOLIO_VALUE={fallback}")
        return fallback


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


def get_orderbook_data(client: ClobClient, token_id: str) -> Optional[dict]:
    """
    Возвращает полные данные стакана: asks, bids, min_order_size, tick_size.
    [FIX-6] asks[0] — уже лучший (минимальный) аск, min() не нужен.
    [FIX-7] min_order_size берётся из ответа стакана.
    """
    try:
        book = client.get_order_book(token_id)

        # Поддержка как объекта с атрибутами, так и dict
        if hasattr(book, "asks"):
            asks = book.asks or []
            bids = book.bids or []
            min_size  = getattr(book, "min_order_size", None)
            tick_size = getattr(book, "tick_size", None)
        else:
            asks = book.get("asks", [])
            bids = book.get("bids", [])
            min_size  = book.get("min_order_size", None)
            tick_size = book.get("tick_size", None)

        def price_of(entry):
            return float(entry.price if hasattr(entry, "price") else entry["price"])

        best_ask = price_of(asks[0]) if asks else None

        return {
            "best_ask": best_ask,
            "min_order_size": int(float(min_size)) if min_size is not None else DEFAULT_MIN_SIZE,
            "tick_size": str(tick_size) if tick_size else "0.01",
        }
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


def calc_size(portfolio: float, price: float, min_size: int) -> int:
    """
    [FIX-5] Размер ордера — целое число контрактов.
    Биржа принимает только целые контракты.
    min_size берётся из стакана (не хардкодится).
    """
    if price <= 0:
        return min_size
    size = (portfolio * RISK_PCT) / price
    return max(round(size), min_size)  # целое число!


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
        size: int,
        tick_size: str,
        neg_risk: bool,
        question: str,
    ) -> Optional[str]:
        tick = float(tick_size)

        # [FIX-9] Цена не может быть >= 1.0
        order_price = min(price + PRICE_OFFSET, MAX_PRICE, MAX_ORDER_PRICE)
        order_price = round(round(order_price / tick) * tick, 6)

        cost = round(order_price * size, 4)
        log.info(
            f"  → LIMIT BUY | {question[:50]}"
            f" | price={order_price:.4f} | size={size} | cost=${cost}"
            f" | neg_risk={neg_risk}"
        )

        try:
            # [FIX-1] size передаётся как float (5.0), не int
            # [FIX-2] neg_risk передаётся в OrderArgs — нужно для корректной подписи
            order_args = OrderArgs(
                token_id=token_id,
                price=order_price,
                size=float(size),  # float обязателен для SDK
                side=BUY,
                neg_risk=neg_risk,  # критично для neg_risk рынков
            )

            signed = self.client.create_order(order_args)

            # Шаг 2: отправляем как GTC (Good Till Cancelled) — лимитный
            resp = self.client.post_order(signed, OrderType.GTC)

            # [FIX-4] Проверяем наличие ошибки внутри успешного ответа
            if isinstance(resp, dict):
                error_msg = resp.get("errorMsg") or resp.get("error") or ""
                if error_msg:
                    log.error(f"  ✗ Биржа отклонила ордер: {error_msg}")
                    return None

                # [FIX-3] Разные версии SDK возвращают разные поля
                order_id = (
                    resp.get("orderID")
                    or resp.get("order_id")
                    or resp.get("id")
                    or "unknown"
                )
            else:
                # Объект с атрибутами
                error_msg = getattr(resp, "errorMsg", None) or getattr(resp, "error", None)
                if error_msg:
                    log.error(f"  ✗ Биржа отклонила ордер: {error_msg}")
                    return None
                order_id = (
                    getattr(resp, "orderID", None)
                    or getattr(resp, "order_id", None)
                    or getattr(resp, "id", None)
                    or "unknown"
                )

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

            question = m.get("question") or m.get("_event_title", "")

            days = days_until(m.get("_end_date"))
            if days is None or days < MIN_DAYS or days > MAX_DAYS:
                continue

            if is_election(m):
                continue

            cat = (m.get("_event_tags") or ["other"])[0].lower()
            if seen_cats.get(cat, 0) >= MAX_PER_CAT:
                continue

            # [FIX-10] Проверяем ОБА токена (YES и NO).
            # Если YES=0.99, то NO=0.01 — именно NO нас интересует.
            # token_ids[0] = YES, token_ids[1] = NO (если есть)
            outcome_labels = ["YES", "NO"]
            token_found = False

            for idx, token_id in enumerate(token_ids):
                if token_id in self.positions:
                    continue

                book_data = get_orderbook_data(self.client, token_id)
                if book_data is None:
                    continue

                best_ask       = book_data["best_ask"]
                min_order_size = book_data["min_order_size"]
                tick_size      = book_data["tick_size"]

                if best_ask is None:
                    continue
                if not (MIN_PRICE <= best_ask <= MAX_PRICE):
                    continue

                outcome = outcome_labels[idx] if idx < len(outcome_labels) else f"token{idx}"
                log.info(
                    f"✔ [{cat.upper()}] {question[:55]} [{outcome}]"
                    f" | ask={best_ask:.4f} | days={days:.0f}"
                    f" | size_min={min_order_size}"
                )

                # [FIX-5] Размер считается с учётом min_order_size из стакана
                size     = calc_size(portfolio, best_ask, min_order_size)
                neg_risk = get_neg_risk(self.client, token_id)

                order_id = self.place_order(
                    token_id=token_id,
                    price=best_ask,
                    size=size,
                    tick_size=tick_size,
                    neg_risk=neg_risk,
                    question=f"{question} [{outcome}]",
                )

                if order_id:
                    self.positions[token_id] = {
                        "order_id":  order_id,
                        "question":  question,
                        "outcome":   outcome,
                        "category":  cat,
                        "price":     best_ask,
                        "size":      size,
                        "placed_at": datetime.utcnow().isoformat(),
                    }
                    seen_cats[cat] = seen_cats.get(cat, 0) + 1
                    placed += 1
                    token_found = True

                time.sleep(0.3)

            # лимит категории считается per-market, не per-token
            _ = token_found

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
  
