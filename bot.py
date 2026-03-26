"""
╔════════════════════════════════════════════════════════════════╗
║   POLYMARKET TRADING BOT — audited YES/NO + heartbeat build   ║
║   Поиск → Фильтрация → Лимитные ордера → Риск-контроль       ║
╚════════════════════════════════════════════════════════════════╝

Что изменено по сравнению с исходной версией:
- бот умеет покупать как YES, так и NO outcome-токены;
- бот загружает сохранённое состояние и текущие open orders;
- бот поддерживает heartbeat для живых GTC-ордеров;
- бот совместим с актуальным py-clob-client (get_balance_allowance);
- бот явно передаёт tick_size / neg_risk в create_order, если SDK это поддерживает.

ВАЖНО:
Покупка NO на Polymarket — это НЕ side=SELL.
Нужно покупать отдельный NO token_id c side=BUY.
SELL нужен уже для закрытия ранее купленной позиции.
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import requests
from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY

try:
    from py_clob_client.clob_types import (
        AssetType,
        BalanceAllowanceParams,
        OpenOrderParams,
        PartialCreateOrderOptions,
    )
except Exception:
    AssetType = None
    BalanceAllowanceParams = None
    OpenOrderParams = None
    PartialCreateOrderOptions = None


# ── Загрузка конфига ────────────────────────────────────────────
load_dotenv()


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


PRIVATE_KEY = os.getenv("PRIVATE_KEY")
API_KEY = os.getenv("POLY_API_KEY")
API_SECRET = os.getenv("POLY_API_SECRET")
API_PASSPHRASE = os.getenv("POLY_API_PASSPHRASE")
FUNDER_ADDRESS = os.getenv("FUNDER_ADDRESS")
SIGNATURE_TYPE = int(os.getenv("SIGNATURE_TYPE", "2"))

CLOB_HOST = "https://clob.polymarket.com"
GAMMA_HOST = "https://gamma-api.polymarket.com"
CHAIN_ID = 137

# ── Параметры стратегии ────────────────────────────────────────
MIN_PRICE = float(os.getenv("MIN_PRICE", "0.001"))
MAX_PRICE = float(os.getenv("MAX_PRICE", "0.030"))
RISK_PCT = float(os.getenv("RISK_PCT", "0.02"))
MIN_DAYS = int(os.getenv("MIN_DAYS", "3"))
MAX_DAYS = int(os.getenv("MAX_DAYS", "365"))
MAX_PER_CAT = int(os.getenv("MAX_PER_CAT", "2"))
MIN_SIZE = float(os.getenv("MIN_SIZE", "5"))
PRICE_OFFSET = float(os.getenv("PRICE_OFFSET", "0.001"))
MARKETS_TO_SCAN = int(os.getenv("MARKETS_TO_SCAN", "200"))
STATE_FILE = Path(os.getenv("STATE_FILE", "positions.json"))

# YES / NO настройки
TRADE_YES = env_bool("TRADE_YES", True)
TRADE_NO = env_bool("TRADE_NO", True)
ALLOW_BOTH_SIDES_SAME_MARKET = env_bool("ALLOW_BOTH_SIDES_SAME_MARKET", False)

# Режим работы
RUN_MODE = os.getenv("RUN_MODE", "loop").strip().lower()  # loop | once
SCAN_INTERVAL_SEC = int(os.getenv("SCAN_INTERVAL_SEC", "300"))
HEARTBEAT_ENABLED = env_bool("HEARTBEAT_ENABLED", True)
HEARTBEAT_INTERVAL_SEC = float(os.getenv("HEARTBEAT_INTERVAL_SEC", "5"))

# ── Исключённые ключевые слова ─────────────────────────────────
EXCLUDED = {
    "elections",
    "election",
    "politics",
    "voting",
    "president",
    "senate",
    "congress",
    "political",
    "vote",
    "ballot",
    "democrat",
    "republican",
    "midterm",
    "primary",
    "caucus",
}

# ── Логгер ─────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("polybot")


# ════════════════════════════════════════════════════════════════
#  ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ════════════════════════════════════════════════════════════════

def pick(obj: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(obj, dict) and name in obj:
            return obj[name]
        if hasattr(obj, name):
            return getattr(obj, name)
    return default


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def parse_listish(raw: Any) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw if x not in (None, "")]
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return []
        if raw.startswith("["):
            try:
                data = json.loads(raw)
                if isinstance(data, list):
                    return [str(x) for x in data if x not in (None, "")]
            except Exception:
                return []
        return [raw]
    return []


parse_token_ids = parse_listish
parse_outcomes = parse_listish


def normalize_outcome_name(name: str, index: int) -> str:
    normalized = str(name or "").strip().upper()
    if normalized in {"YES", "NO"}:
        return normalized
    if not normalized:
        return "YES" if index == 0 else "NO" if index == 1 else f"OUTCOME_{index + 1}"
    return normalized


def calc_limit_price(best_ask: float, tick_size: str) -> float:
    tick = max(safe_float(tick_size, 0.01), 0.0001)
    target = min(best_ask + PRICE_OFFSET, MAX_PRICE)
    steps = math.ceil((target / tick) - 1e-12)
    rounded = round(steps * tick, 6)
    capped = min(rounded, MAX_PRICE)
    # На всякий случай ещё раз приводим к тиковому шагу вниз от cap, если cap не кратен tick.
    capped_steps = math.floor((capped / tick) + 1e-12)
    return round(max(capped_steps * tick, tick), 6)


def calc_target_size(total_portfolio: float, order_price: float, min_order_size: float) -> float:
    """
    Риск на сделку = total_portfolio * RISK_PCT.
    Минимальный размер берём как максимум из MIN_SIZE и min_order_size с биржи.
    """
    min_size = max(MIN_SIZE, safe_float(min_order_size, MIN_SIZE))
    if order_price <= 0:
        return round(min_size, 2)
    risk_size = (total_portfolio * RISK_PCT) / order_price
    return round(max(risk_size, min_size), 2)


def get_portfolio_value(client: ClobClient) -> float:
    """
    Совместимость с новыми и старыми версиями SDK.
    В актуальном SDK есть get_balance_allowance(), а не get_balance().
    """
    fallback = float(os.getenv("PORTFOLIO_VALUE", "100"))

    # Новый SDK: get_balance_allowance(asset_type=COLLATERAL)
    try:
        if BalanceAllowanceParams is not None and AssetType is not None and hasattr(client, "get_balance_allowance"):
            params = BalanceAllowanceParams(
                asset_type=AssetType.COLLATERAL,
                signature_type=SIGNATURE_TYPE,
            )
            data = client.get_balance_allowance(params)
            balance = safe_float(pick(data, "balance", "available", "balance_available"), 0.0)
            if balance > 0:
                return balance
    except Exception as e:
        log.warning(f"Не удалось получить баланс через get_balance_allowance(): {e}")

    # Старый SDK / кастомная версия
    try:
        if hasattr(client, "get_balance"):
            data = client.get_balance()
            balance = safe_float(pick(data, "balance", "available", "balance_available"), 0.0)
            if balance > 0:
                return balance
    except Exception as e:
        log.warning(f"Не удалось получить баланс через get_balance(): {e}")

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
    text = " ".join(
        [
            str(market.get("question", "")),
            str(market.get("description", "")),
            " ".join(market.get("_event_tags", [])),
        ]
    ).lower()
    return any(k in text for k in EXCLUDED)


def fetch_markets() -> list[dict]:
    url = (
        f"{GAMMA_HOST}/events"
        f"?active=true&closed=false"
        f"&limit={MARKETS_TO_SCAN}&offset=0"
    )
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        markets: list[dict] = []
        for event in resp.json():
            for market in event.get("markets", []):
                if market.get("enableOrderBook") is False:
                    continue
                market["_event_tags"] = [t.get("label", "") for t in event.get("tags", [])]
                market["_event_title"] = event.get("title", "")
                market["_end_date"] = event.get("endDate") or market.get("endDate")
                markets.append(market)
        log.info(f"Загружено рынков: {len(markets)}")
        return markets
    except Exception as e:
        log.error(f"Ошибка загрузки рынков: {e}")
        return []


def get_book_snapshot(client: ClobClient, token_id: str) -> Optional[dict[str, Any]]:
    try:
        book = client.get_order_book(token_id)
        asks = pick(book, "asks", default=[]) or []
        if not asks:
            return None

        prices = [safe_float(pick(a, "price", default=None), -1.0) for a in asks]
        prices = [p for p in prices if p > 0]
        if not prices:
            return None

        best_ask = min(prices)
        return {
            "best_ask": best_ask,
            "tick_size": str(pick(book, "tick_size", default="0.01") or "0.01"),
            "neg_risk": bool(pick(book, "neg_risk", default=False)),
            "min_order_size": safe_float(pick(book, "min_order_size", default=MIN_SIZE), MIN_SIZE),
        }
    except Exception:
        return None


def build_outcome_candidates(market: dict) -> list[dict[str, str]]:
    token_ids = parse_token_ids(market.get("clobTokenIds"))
    outcomes = parse_outcomes(market.get("outcomes"))

    if len(token_ids) == 2 and len(outcomes) < 2:
        outcomes = ["Yes", "No"]

    candidates: list[dict[str, str]] = []
    for idx, token_id in enumerate(token_ids):
        outcome = outcomes[idx] if idx < len(outcomes) else ""
        normalized_outcome = normalize_outcome_name(outcome, idx)
        candidates.append(
            {
                "token_id": str(token_id),
                "outcome": normalized_outcome,
            }
        )
    return candidates


# ════════════════════════════════════════════════════════════════
#  БОТ
# ════════════════════════════════════════════════════════════════
class PolymarketBot:
    def __init__(self):
        from eth_account import Account

        if not TRADE_YES and not TRADE_NO:
            raise ValueError("Отключены и TRADE_YES, и TRADE_NO — торговать нечем.")

        signer = Account.from_key(PRIVATE_KEY)
        funder = FUNDER_ADDRESS if FUNDER_ADDRESS else signer.address

        log.info(f"Signature type : {SIGNATURE_TYPE}")
        log.info(f"Funder address : {funder}")
        log.info(f"Trade sides    : YES={TRADE_YES} NO={TRADE_NO}")
        log.info(f"Run mode       : {RUN_MODE}")

        self.client = ClobClient(
            host=CLOB_HOST,
            key=PRIVATE_KEY,
            chain_id=CHAIN_ID,
            signature_type=SIGNATURE_TYPE,
            funder=funder,
        )

        if API_KEY and API_SECRET and API_PASSPHRASE:
            self.client.set_api_creds(
                ApiCreds(
                    api_key=API_KEY,
                    api_secret=API_SECRET,
                    api_passphrase=API_PASSPHRASE,
                )
            )
            log.info("API-ключи загружены из .env")
        else:
            log.info("Деривируем API-ключи...")
            creds = self.client.create_or_derive_api_creds()
            self.client.set_api_creds(creds)
            log.info(f"API KEY: {creds.api_key}")

        self.positions: dict[str, dict[str, Any]] = self._load_positions()
        self.open_orders: dict[str, dict[str, Any]] = {}
        self.stop_event = threading.Event()
        self.heartbeat_thread: Optional[threading.Thread] = None
        self.heartbeat_id = ""

        self.refresh_open_orders(log_empty=False)

    # ── Состояние ───────────────────────────────────────────────
    def _load_positions(self) -> dict[str, dict[str, Any]]:
        if not STATE_FILE.exists():
            return {}
        try:
            with STATE_FILE.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                log.info(f"Загружено записей из {STATE_FILE}: {len(data)}")
                return data
        except Exception as e:
            log.warning(f"Не удалось прочитать {STATE_FILE}: {e}")
        return {}

    def save_positions(self) -> None:
        try:
            with STATE_FILE.open("w", encoding="utf-8") as f:
                json.dump(self.positions, f, indent=2, ensure_ascii=False)
        except Exception as e:
            log.error(f"Не удалось сохранить {STATE_FILE}: {e}")

    def refresh_open_orders(self, log_empty: bool = True) -> None:
        try:
            params = OpenOrderParams() if OpenOrderParams is not None else None
            orders = self.client.get_orders(params) if params is not None else self.client.get_orders()
            fresh: dict[str, dict[str, Any]] = {}
            for order in orders or []:
                token_id = str(pick(order, "asset_id", "assetId", default="") or "")
                if not token_id:
                    continue
                fresh[token_id] = dict(order)
            self.open_orders = fresh
            if self.open_orders:
                log.info(f"Открытых ордеров на аккаунте: {len(self.open_orders)}")
            elif log_empty:
                log.info("Открытых ордеров на аккаунте нет")
        except Exception as e:
            log.warning(f"Не удалось получить open orders: {e}")

    def known_market_keys(self) -> set[str]:
        keys: set[str] = set()
        for position in self.positions.values():
            condition_id = str(position.get("condition_id", "") or "")
            if condition_id:
                keys.add(condition_id)
        for order in self.open_orders.values():
            market = str(pick(order, "market", default="") or "")
            if market:
                keys.add(market)
        return keys

    def category_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for position in self.positions.values():
            category = str(position.get("category", "other") or "other").lower()
            counts[category] = counts.get(category, 0) + 1
        return counts

    # ── Heartbeat ──────────────────────────────────────────────
    def start_heartbeat_worker(self) -> None:
        if not HEARTBEAT_ENABLED:
            return
        if self.heartbeat_thread and self.heartbeat_thread.is_alive():
            return
        self.heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            name="polybot-heartbeat",
            daemon=True,
        )
        self.heartbeat_thread.start()

    def _heartbeat_loop(self) -> None:
        while not self.stop_event.wait(HEARTBEAT_INTERVAL_SEC):
            if not self.open_orders:
                continue
            try:
                resp = self.client.post_heartbeat(self.heartbeat_id)
                next_id = pick(resp, "heartbeat_id", "heartbeatId", default="")
                if next_id:
                    self.heartbeat_id = str(next_id)
                log.debug(f"Heartbeat OK, активных ордеров: {len(self.open_orders)}")
            except Exception as e:
                log.warning(f"Heartbeat ошибка: {e}")
                # Если id протух — даём SDK/серверу шанс пересоздать цепочку с пустым id.
                self.heartbeat_id = ""

    def stop(self) -> None:
        self.stop_event.set()
        if self.heartbeat_thread and self.heartbeat_thread.is_alive():
            self.heartbeat_thread.join(timeout=2)

    # ── Логика торговли ────────────────────────────────────────
    def should_trade_outcome(self, outcome: str) -> bool:
        outcome = outcome.upper()
        if outcome == "YES":
            return TRADE_YES
        if outcome == "NO":
            return TRADE_NO
        return False

    def already_tracking_token(self, token_id: str) -> bool:
        return token_id in self.positions or token_id in self.open_orders

    def place_order(
        self,
        token_id: str,
        outcome: str,
        order_price: float,
        size: float,
        tick_size: str,
        neg_risk: bool,
        question: str,
    ) -> Optional[dict[str, Any]]:
        cost = round(order_price * size, 4)
        log.info(
            f"  → LIMIT BUY {outcome:<3} | {question[:60]}"
            f" | price={order_price:.4f} | size={size} | cost=${cost}"
        )

        order_args = OrderArgs(
            token_id=token_id,
            price=order_price,
            size=size,
            side=BUY,
        )

        try:
            try:
                if PartialCreateOrderOptions is not None:
                    options = PartialCreateOrderOptions(
                        tick_size=str(tick_size),
                        neg_risk=bool(neg_risk),
                    )
                    signed = self.client.create_order(order_args, options)
                else:
                    signed = self.client.create_order(order_args)
            except TypeError:
                signed = self.client.create_order(order_args)

            resp = self.client.post_order(signed, OrderType.GTC)

            if isinstance(resp, dict) and resp.get("success") is False:
                log.error(f"  ✗ Биржа отклонила ордер: {resp.get('errorMsg') or resp}")
                return None

            order_id = pick(resp, "orderID", "order_id", default=None)
            status = str(pick(resp, "status", default="unknown") or "unknown").lower()
            log.info(f"  ✓ Ордер принят: {order_id} | status={status}")
            return {
                "order_id": order_id or "unknown",
                "status": status,
                "raw": resp,
            }
        except Exception as e:
            log.error(f"  ✗ Ошибка отправки ордера: {e}")
            return None

    def run_once(self) -> int:
        log.info("=" * 70)
        log.info(f"Запуск: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

        self.refresh_open_orders(log_empty=False)

        total_portfolio = get_portfolio_value(self.client)
        free_balance = total_portfolio
        log.info(f"Портфель (или fallback): ${total_portfolio:.2f}")

        markets = fetch_markets()
        placed = 0
        known_markets = self.known_market_keys()
        seen_cats = self.category_counts()

        for market in markets:
            token_candidates = build_outcome_candidates(market)
            if not token_candidates:
                continue

            question = str(market.get("question") or market.get("_event_title", "")).strip()
            condition_id = str(market.get("conditionId") or market.get("condition_id") or market.get("id") or "")
            days = days_until(market.get("_end_date") or market.get("endDate"))
            cat = str((market.get("_event_tags") or ["other"])[0] or "other").lower()

            if not question:
                continue
            if days is None or days < MIN_DAYS or days > MAX_DAYS:
                continue
            if is_election(market):
                continue
            if seen_cats.get(cat, 0) >= MAX_PER_CAT:
                continue
            if condition_id and not ALLOW_BOTH_SIDES_SAME_MARKET and condition_id in known_markets:
                continue

            viable_candidates: list[dict[str, Any]] = []
            for candidate in token_candidates:
                token_id = candidate["token_id"]
                outcome = candidate["outcome"]

                if not self.should_trade_outcome(outcome):
                    continue
                if self.already_tracking_token(token_id):
                    continue

                book = get_book_snapshot(self.client, token_id)
                if not book or book["best_ask"] is None:
                    continue
                if not (MIN_PRICE <= book["best_ask"] <= MAX_PRICE):
                    continue

                viable_candidates.append({**candidate, **book})

            if not viable_candidates:
                continue

            # По умолчанию берём только одну сторону рынка — самую дешёвую.
            viable_candidates.sort(key=lambda x: x["best_ask"])
            if not ALLOW_BOTH_SIDES_SAME_MARKET:
                viable_candidates = viable_candidates[:1]

            for candidate in viable_candidates:
                if seen_cats.get(cat, 0) >= MAX_PER_CAT:
                    break

                token_id = candidate["token_id"]
                outcome = candidate["outcome"]
                best_ask = safe_float(candidate["best_ask"], 0.0)
                tick_size = str(candidate["tick_size"])
                neg_risk = bool(candidate["neg_risk"])
                min_order_size = safe_float(candidate["min_order_size"], MIN_SIZE)
                order_price = calc_limit_price(best_ask, tick_size)
                target_size = calc_target_size(total_portfolio, order_price, min_order_size)

                max_affordable = round((free_balance / order_price), 2) if order_price > 0 else 0.0
                required_min_size = round(max(MIN_SIZE, min_order_size), 2)
                size = round(min(target_size, max_affordable), 2)

                if size < required_min_size:
                    log.info(
                        f"Пропуск [{outcome}] {question[:50]} — "
                        f"недостаточно свободного баланса для min_size={required_min_size}"
                    )
                    continue

                expected_cost = round(order_price * size, 4)
                log.info(
                    f"✔ [{cat.upper()}] [{outcome}] {question[:60]}"
                    f" | ask={best_ask:.4f} | limit={order_price:.4f}"
                    f" | days={days:.0f} | size={size} | free=${free_balance:.2f}"
                )

                result = self.place_order(
                    token_id=token_id,
                    outcome=outcome,
                    order_price=order_price,
                    size=size,
                    tick_size=tick_size,
                    neg_risk=neg_risk,
                    question=question,
                )

                if not result:
                    continue

                placed_at = datetime.now(timezone.utc).isoformat()
                status = str(result.get("status", "unknown"))
                order_id = str(result.get("order_id", "unknown"))

                self.positions[token_id] = {
                    "order_id": order_id,
                    "question": question,
                    "condition_id": condition_id,
                    "category": cat,
                    "outcome": outcome,
                    "ask_price": best_ask,
                    "order_price": order_price,
                    "size": size,
                    "neg_risk": neg_risk,
                    "status": status,
                    "placed_at": placed_at,
                }

                if status in {"live", "delayed", "unmatched"}:
                    self.open_orders[token_id] = {
                        "id": order_id,
                        "market": condition_id,
                        "asset_id": token_id,
                        "price": order_price,
                        "original_size": size,
                        "outcome": outcome,
                        "status": status,
                    }

                if condition_id:
                    known_markets.add(condition_id)
                seen_cats[cat] = seen_cats.get(cat, 0) + 1
                free_balance = max(round(free_balance - expected_cost, 4), 0.0)
                placed += 1
                self.save_positions()
                time.sleep(0.25)

        log.info("=" * 70)
        log.info(f"Готово! Новых ордеров размещено: {placed}")
        self.save_positions()
        self.refresh_open_orders(log_empty=False)
        return placed

    def run(self) -> None:
        self.start_heartbeat_worker()

        if RUN_MODE == "once":
            self.run_once()
            return

        while not self.stop_event.is_set():
            self.run_once()
            if self.stop_event.wait(SCAN_INTERVAL_SEC):
                break


# ════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    if not PRIVATE_KEY:
        log.error("PRIVATE_KEY не задан в .env!")
        raise SystemExit(1)

    if not FUNDER_ADDRESS:
        log.warning(
            "FUNDER_ADDRESS не задан! Если деньги лежат на proxy-кошельке Polymarket, "
            "добавь FUNDER_ADDRESS=0x... в .env"
        )

    bot = PolymarketBot()
    try:
        bot.run()
    except KeyboardInterrupt:
        log.info("Остановка по Ctrl+C...")
    finally:
        bot.stop()
