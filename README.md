# Polytrade — Polymarket Trading Bot

Бот для автоматической торговли контрактами на **Polymarket** через **CLOB API**.  
Сканирует активные рынки, фильтрует их по заданным условиям, рассчитывает размер позиции и выставляет **лимитные ордера**.

Текущая версия ориентирована на торговлю **YES** и **NO** контрактами, а также на безопасную работу через `FUNDER_ADDRESS` и корректный `SIGNATURE_TYPE`.

---

## Возможности

- подключение к **Polymarket CLOB**
- загрузка активных рынков из **Gamma API**
- фильтрация рынков по цене, сроку экспирации и категории
- выставление **лимитных ордеров**
- поддержка аккаунтов с:
  - обычным кошельком (**EOA**)
  - Polymarket proxy
  - **MetaMask / Rabby / Gnosis Safe** через `SIGNATURE_TYPE=2`
- хранение открытых позиций в `positions.json`
- логирование в `bot.log`
- настройка стратегии через `.env`

---

## Стек

- Python 3.11+
- `py-clob-client`
- `eth-account`
- `requests`
- `python-dotenv`

---

## Структура проекта

```text
Polytrade/
├── bot.py                # основной бот
├── requirements.txt      # Python-зависимости
├── README.md             # документация проекта
├── .env                  # секреты и настройки (создаётся вручную)
├── positions.json        # открытые позиции (создаётся автоматически)
└── bot.log               # лог работы (создаётся автоматически)
```

---

## Как работает бот

1. Загружает активные события и рынки из Gamma API
2. Извлекает `clobTokenIds`
3. Для каждого рынка:
   - получает лучший ask из стакана
   - проверяет цену, срок и категорию
   - исключает нежелательные рынки
4. Рассчитывает размер позиции
5. Создаёт и отправляет лимитный ордер через CLOB
6. Сохраняет информацию о позиции в `positions.json`

---

## Поддерживаемые режимы подписи

В `.env` используется переменная:

```env
SIGNATURE_TYPE=
```

Значения:

- `0` — **EOA**
- `1` — **POLY_PROXY**
- `2` — **GNOSIS_SAFE / MetaMask / Rabby**

Если вы работаете через обычный Polymarket-аккаунт, где деньги лежат на **proxy-адресе**, нужен:

```env
SIGNATURE_TYPE=2
FUNDER_ADDRESS=0x...
```

---

## Установка

### 1. Клонирование репозитория

```bash
git clone https://github.com/Obebe11/Polytrade.git
cd Polytrade
```

### 2. Создание виртуального окружения

Linux / VPS:

```bash
python3 -m venv venv
source venv/bin/activate
```

Windows PowerShell:

```powershell
python -m venv venv
venv\Scripts\Activate.ps1
```

### 3. Установка зависимостей

Если есть `requirements.txt`:

```bash
pip install -r requirements.txt
```

Если файла нет или он неполный:

```bash
pip install py-clob-client eth-account requests python-dotenv
```

---

## Настройка `.env`

Создайте файл `.env` в корне проекта.

Пример:

```env
# Polygon private key
PRIVATE_KEY=0x...

# Proxy-адрес Polymarket, где лежат средства
FUNDER_ADDRESS=0x...

# Тип подписи
SIGNATURE_TYPE=2

# API credentials Polymarket (опционально)
POLY_API_KEY=
POLY_API_SECRET=
POLY_API_PASSPHRASE=

# Fallback размер портфеля, если баланс не читается из API
PORTFOLIO_VALUE=100

# Сколько рынков сканировать за один проход
MARKETS_TO_SCAN=200

# Торговать ли YES / NO сторонами
TRADE_YES=true
TRADE_NO=true

# Разрешать ли одновременно YES и NO по одному рынку
ALLOW_BOTH_SIDES_SAME_MARKET=false

# Режим работы
RUN_MODE=loop

# Heartbeat для GTC-ордеров
HEARTBEAT_ENABLED=true
HEARTBEAT_INTERVAL=30
```

---

## Обязательные переменные

### `PRIVATE_KEY`
Приватный ключ кошелька Polygon.

### `FUNDER_ADDRESS`
Адрес, на котором реально лежат деньги на Polymarket.  
Это часто **не адрес MetaMask**, а отдельный proxy-адрес из настроек аккаунта Polymarket.

### `SIGNATURE_TYPE`
Для MetaMask / Rabby / Safe чаще всего нужен:

```env
SIGNATURE_TYPE=2
```

---

## Установка на VPS

Пример для Debian / Ubuntu:

### 1. Подключение к серверу

```bash
ssh root@YOUR_SERVER_IP
```

### 2. Установка системных пакетов

```bash
apt update
apt install -y python3 python3-venv python3-pip git
```

Если Python 3.11 установлен некорректно, может понадобиться:

```bash
apt install -y python3.11-full
```

### 3. Клонирование проекта

```bash
cd /root
git clone https://github.com/Obebe11/Polytrade.git
cd Polytrade
```

### 4. Виртуальное окружение

```bash
python3 -m venv venv
source venv/bin/activate
```

### 5. Установка зависимостей

```bash
pip install --upgrade pip
pip install -r requirements.txt || pip install py-clob-client eth-account requests python-dotenv
```

### 6. Создание `.env`

```bash
nano .env
```

Вставьте свои настройки и сохраните файл.

### 7. Первый запуск

```bash
python bot.py
```

---

## Запуск

### Одноразовый проход

```bash
python bot.py
```

### Фоновый запуск через `screen`

```bash
screen -S polytrade
source venv/bin/activate
python bot.py
```

Отключиться от screen:

```bash
Ctrl+A, затем D
```

Вернуться:

```bash
screen -r polytrade
```

### Фоновый запуск через `nohup`

```bash
nohup venv/bin/python bot.py > run.log 2>&1 &
```

---

## Автозапуск через systemd

Создайте сервис:

```bash
nano /etc/systemd/system/polytrade.service
```

Содержимое:

```ini
[Unit]
Description=Polytrade Polymarket Bot
After=network.target

[Service]
Type=simple
WorkingDirectory=/root/Polytrade
ExecStart=/root/Polytrade/venv/bin/python /root/Polytrade/bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Применение:

```bash
systemctl daemon-reload
systemctl enable polytrade
systemctl start polytrade
systemctl status polytrade
```

Логи:

```bash
journalctl -u polytrade -f
```

---

## Логика стратегии

Основные фильтры:

- цена контракта в диапазоне `MIN_PRICE` — `MAX_PRICE`
- до экспирации от `MIN_DAYS` до `MAX_DAYS`
- ограничение числа позиций на категорию
- исключение некоторых ключевых слов
- пропуск уже открытых позиций
- контроль риска через `RISK_PCT`

Пример параметров:

```python
MIN_PRICE = 0.001
MAX_PRICE = 0.030
RISK_PCT = 0.02
MIN_DAYS = 3
MAX_DAYS = 365
MAX_PER_CAT = 2
MIN_SIZE = 5
PRICE_OFFSET = 0.001
```

---

## Торговля YES и NO

Для рынка Polymarket обычно есть две стороны:

- **YES**
- **NO**

Важно:
- покупка **YES** — это `BUY` по `YES token_id`
- покупка **NO** — это **тоже** `BUY`, но по `NO token_id`
- `SELL` используется для закрытия уже купленной позиции, а не для открытия NO

Если бот ранее брал только `token_ids[0]`, это значит, что он фактически торговал только одну сторону рынка.

Для поддержки обеих сторон в конфиге удобно использовать:

```env
TRADE_YES=true
TRADE_NO=true
```

---

## Файлы, которые создаёт бот

### `positions.json`
Хранит открытые позиции, например:

```json
{
  "token_id_here": {
    "order_id": "12345",
    "question": "Will BTC hit $100k?",
    "category": "crypto",
    "price": 0.015,
    "size": 10,
    "placed_at": "2026-03-26T12:00:00"
  }
}
```

### `bot.log`
Файл с логами запуска, фильтрации и ордеров.

---

## Типичный рабочий процесс

1. Пополнить баланс на Polymarket
2. Уточнить `FUNDER_ADDRESS`
3. Указать `PRIVATE_KEY`
4. Настроить `.env`
5. Запустить бота
6. Проверять:
   - `bot.log`
   - `positions.json`
   - открытые ордера в аккаунте Polymarket

---

## Типичные ошибки и решения

### `PRIVATE_KEY не задан`
Проверьте наличие `.env` и правильность имени переменной.

### `not enough balance/allowance`
Обычно причина в неверном `FUNDER_ADDRESS` или `SIGNATURE_TYPE`.

### `422 Unprocessable Entity`
Проверьте параметры запроса к Gamma API.

### `No orderbook exists`
У рынка нет активного стакана. Такой рынок нужно пропустить.

### Ошибка импорта `BUY`
Используйте правильный импорт:

```python
from py_clob_client.order_builder.constants import BUY
```

### Ошибка `create_or_derive_api_key`
Правильный метод:

```python
client.create_or_derive_api_creds()
```

---

## Безопасность

Никогда не коммитьте в Git:

- `.env`
- приватные ключи
- API credentials
- `positions.json`
- `bot.log`

Пример `.gitignore`:

```gitignore
.env
positions.json
bot.log
venv/
__pycache__/
```

---

## Рекомендации по эксплуатации

- сначала тестируйте с маленьким балансом
- проверяйте `FUNDER_ADDRESS` дважды
- не включайте одновременно агрессивную торговлю без лимитов риска
- добавьте heartbeat для долгоживущих GTC-ордеров
- периодически синхронизируйте открытые ордера через API
- продумайте отдельную логику выхода из позиции

---

## Что стоит добавить дальше

- закрытие позиций (`SELL`)
- тейк-профит и стоп-лосс
- Telegram-уведомления
- учёт PnL
- heartbeat worker
- синхронизацию `positions.json` с реальными ордерами
- cron / loop режим
- healthcheck и алерты

---

## Пример быстрого старта

```bash
git clone https://github.com/Obebe11/Polytrade.git
cd Polytrade
python3 -m venv venv
source venv/bin/activate
pip install py-clob-client eth-account requests python-dotenv
cp .env.example .env 2>/dev/null || touch .env
nano .env
python bot.py
```

---

## Отказ от ответственности

Этот бот торгует реальными контрактами и может привести к потере средств.  
Используйте его только если понимаете, как работают Polymarket, CLOB, лимитные ордера и риск-менеджмент.

---

## Репозиторий

GitHub: `https://github.com/Obebe11/Polytrade`
