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
from py_clob_client.order_builder.constants import BUY, SELL

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

# Величина профита, при которой позиция закрывается в режиме закрытия (в процентах)
# Например, 500 означает закрывать позиции при приросте 500% и более (5-кратный рост).
CLOSE_PROFIT_PCT = float(os.getenv("CLOSE_PROFIT_PCT", "500"))

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

# Получить лучшую бид-цену для указанного токена. Это максимальная цена, по которой
# другие участники готовы купить токен, полезно для закрытия (продажи) позиций.
def get_best_bid(client: ClobClient, token_id: str) -> Optional[float]:
    try:
        book = client.get_order_book(token_id)
        # py_clob_client возвращает bids либо как атрибут, либо как словарь
        bids = book.bids if hasattr(book, "bids") else book.get("bids", [])
        if not bids:
            return None
        prices = [float(b.price if hasattr(b, "price") else b["price"]) for b in bids]
        return max(prices) if prices else None
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
        # При инициализации пытаемся загрузить ранее сохранённые позиции, чтобы не торговать
        # повторно те же токены и учитывать открытые позиции при закрытии.
        self.load_positions()

    def load_positions(self) -> None:
        """
        Загрузить сохранённые позиции из файла positions.json, если он существует.
        Файл содержит словарь token_id -> информация о позиции.
        """
        try:
            with open("positions.json", "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    self.positions = data
                else:
                    self.positions = {}
        except Exception:
            # Файл отсутствует или повреждён — начинаем с пустого портфеля
            self.positions = {}

    def save_positions(self) -> None:
        """
        Сохранить текущие позиции в файл positions.json. Этот метод перезаписывает файл.
        """
        try:
            with open("positions.json", "w", encoding="utf-8") as f:
                json.dump(self.positions, f, indent=2, ensure_ascii=False)
        except Exception as e:
            log.error(f"Не удалось сохранить позиции: {e}")

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

    def place_sell_order(
        self,
        token_id: str,
        price: float,
        size: float,
        tick_size: str,
        question: str,
    ) -> Optional[str]:
        """
        Создать и отправить лимитный ордер на продажу по указанной цене.

        :param token_id: ID токена outcome, который продаём
        :param price: желаемая цена продажи (будет округлена по тик-сайзу)
        :param size: количество контрактов, которое нужно продать
        :param tick_size: минимальный шаг цены для этого токена
        :param question: текст вопроса рынка — используется только для логов
        :return: order_id при успешной отправке, иначе None
        """
        tick = float(tick_size)

        # Округляем цену по тик-сайзу. Для продажи ставим немного ниже лучшего бида
        order_price = price
        order_price = round(round(order_price / tick) * tick, 6)

        revenue = round(order_price * size, 4)
        log.info(
            f"  → LIMIT SELL | {question[:50]}"
            f" | price={order_price:.4f} | size={size} | revenue=${revenue}"
        )

        try:
            signed = self.client.create_order(
                OrderArgs(
                    token_id=token_id,
                    price=order_price,
                    size=size,
                    side=SELL,
                )
            )
            resp     = self.client.post_order(signed, OrderType.GTC)
            order_id = resp.get("orderID") or resp.get("order_id", "unknown")
            log.info(f"  ✓ SELL ордер принят: {order_id}")
            return order_id
        except Exception as e:
            log.error(f"  ✗ Ошибка при продаже: {e}")
            return None

    def run_close(self) -> None:
        """
        Режим закрытия позиций. Проходит по сохранённым позициям и закрывает
        те, которые достигли порога прибыли CLOSE_PROFIT_PCT. Закрытие
        осуществляется лимитным ордером по цене, близкой к лучшему бид-курсу.
        После успешной продажи позиция удаляется из списка и файл обновляется.
        """
        log.info("=" * 60)
        log.info(f"Запуск режима закрытия: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

        # Убедимся, что позиции загружены
        if not self.positions:
            self.load_positions()

        closed = 0

        for token_id, info in list(self.positions.items()):
            entry_price = float(info.get("price", 0))
            size        = float(info.get("size", 0))
            question    = info.get("question", "")

            if entry_price <= 0 or size <= 0:
                continue

            # Текущая лучшая бид цена
            best_bid = get_best_bid(self.client, token_id)
            if best_bid is None:
                continue

            # Рассчитываем прирост: (текущая цена - вход) / вход
            profit_ratio = (best_bid - entry_price) / entry_price
            if profit_ratio * 100 < CLOSE_PROFIT_PCT:
                # Недостаточная прибыль — пропускаем
                continue

            tick_size = get_tick_size(self.client, token_id)
            # Ставим цену чуть ниже лучшего бида для быстрой продажи
            sell_price = best_bid - PRICE_OFFSET
            if sell_price < MIN_PRICE:
                sell_price = MIN_PRICE

            log.info(
                f"✔ Закрываем позицию | {question[:60]}"
                f" | entry={entry_price:.4f} | bid={best_bid:.4f} | gain={profit_ratio*100:.1f}%"
            )

            order_id = self.place_sell_order(
                token_id=token_id,
                price=sell_price,
                size=size,
                tick_size=tick_size,
                question=question,
            )
            if order_id:
                closed += 1
                # Удаляем закрытую позицию
                self.positions.pop(token_id, None)

            # Делаем паузу, чтобы не отправлять слишком много ордеров
            time.sleep(0.3)

        # Сохраняем обновлённый список позиций
        self.save_positions()
        log.info("=" * 60)
        log.info(f"Режим закрытия завершён. Закрыто позиций: {closed}")

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

        # Сохраняем позиции в файл после выполнения открытого режима
        self.save_positions()
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

    # Спрашиваем у пользователя, в каком режиме запускать бота: открытие (open) или закрытие (close)
    try:
        user_input = input(
            "Выберите режим работы:\n"
            "  open  – открывать новые позиции по стратегии\n"
            "  close – закрывать существующие позиции при достижении прибыли\n"
            "Введите режим (open/close) и нажмите Enter [open]: "
        ).strip().lower()
    except Exception:
        # Если по какой-то причине input не работает (например, нет stdin), берём режим по умолчанию
        user_input = "open"

    mode = user_input or "open"
    bot = PolymarketBot()
    if mode.startswith("c"):
        bot.run_close()
    else:
        bot.run_once()
