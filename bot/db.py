import time  # noqa: F401 (доступно як db.time)
import aiosqlite
from bot.config import DB_PATH, DEFAULTS

_db = None


async def init_db():
    global _db
    _db = await aiosqlite.connect(DB_PATH)
    _db.row_factory = aiosqlite.Row
    await _db.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        base_until INTEGER DEFAULT 0,
        pro_until INTEGER DEFAULT 0,
        deposit REAL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    );
    CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        symbol TEXT,
        side TEXT,            -- LONG / SHORT
        entry REAL,
        sl REAL,
        tp1 REAL,
        tp2 REAL,
        score INTEGER,        -- шанс %
        reason TEXT,
        leverage INTEGER DEFAULT 1,
        margin REAL DEFAULT 0,
        status TEXT DEFAULT 'OPEN',   -- OPEN / TP1 / TP2 / SL
        pnl REAL DEFAULT 0,
        created_at INTEGER,
        closed_at INTEGER
    );
    CREATE TABLE IF NOT EXISTS signal_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT,
        side TEXT,
        entry REAL,
        sl REAL,
        tp1 REAL,
        tp2 REAL,
        score INTEGER,
        status TEXT DEFAULT 'OPEN',   -- OPEN / TP1 / TP2 / SL / EXPIRED
        created_at INTEGER,
        closed_at INTEGER
    );
    CREATE TABLE IF NOT EXISTS promos (
        code TEXT PRIMARY KEY,
        plan TEXT,                 -- base / pro
        days INTEGER,              -- термін дії підписки
        max_uses INTEGER DEFAULT 1,-- скільки разів можна активувати
        used INTEGER DEFAULT 0,
        active INTEGER DEFAULT 1,
        created_at INTEGER
    );
    """)
    await _db.commit()
    # seed defaults
    for k, v in DEFAULTS.items():
        cur = await _db.execute("SELECT 1 FROM settings WHERE key=?", (k,))
        if not await cur.fetchone():
            await _db.execute("INSERT INTO settings(key,value) VALUES(?,?)", (k, str(v)))
    await _db.commit()


async def get_setting(key: str):
    cur = await _db.execute("SELECT value FROM settings WHERE key=?", (key,))
    row = await cur.fetchone()
    return row["value"] if row else str(DEFAULTS.get(key, ""))


async def set_setting(key: str, value):
    await _db.execute(
        "INSERT INTO settings(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )
    await _db.commit()


async def get_int(key: str) -> int:
    try:
        return int(float(await get_setting(key)))
    except (ValueError, TypeError):
        return int(DEFAULTS.get(key, 0))


async def ensure_user(user_id: int, username: str = ""):
    await _db.execute(
        "INSERT OR IGNORE INTO users(user_id, username) VALUES(?,?)", (user_id, username)
    )
    await _db.commit()


async def get_user(user_id: int):
    await ensure_user(user_id)
    cur = await _db.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
    return await cur.fetchone()


async def set_subscription(user_id: int, plan: str, days: int):
    col = "base_until" if plan == "base" else "pro_until"
    await ensure_user(user_id)   # захист: юзера може ще не бути в базі (напр. промокод/адмін-видача)
    cur = await _db.execute(f"SELECT {col} FROM users WHERE user_id=?", (user_id,))
    row = await cur.fetchone()
    now = int(time.time())
    base = max((row[col] or 0) if row else 0, now)
    await _db.execute(
        f"UPDATE users SET {col}=? WHERE user_id=?", (base + days * 86400, user_id)
    )
    await _db.commit()


async def revoke_subscription(user_id: int, plan: str):
    col = "base_until" if plan == "base" else "pro_until"
    await _db.execute(f"UPDATE users SET {col}=0 WHERE user_id=?", (user_id,))
    await _db.commit()


async def set_deposit(user_id: int, deposit: float):
    await _db.execute("UPDATE users SET deposit=? WHERE user_id=?", (deposit, user_id))
    await _db.commit()


async def active_subscribers(plan: str):
    col = "base_until" if plan == "base" else "pro_until"
    now = int(time.time())
    cur = await _db.execute(
        f"SELECT user_id, username FROM users WHERE {col} > ?", (now,)
    )
    return await cur.fetchall()


async def add_trade(t: dict):
    await _db.execute(
        """INSERT INTO trades(user_id,symbol,side,entry,sl,tp1,tp2,score,reason,
           leverage,margin,status,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            t["user_id"], t["symbol"], t["side"], t["entry"], t["sl"], t["tp1"],
            t["tp2"], t["score"], t["reason"], t.get("leverage", 1),
            t.get("margin", 0), "OPEN", int(time.time()),
        ),
    )
    await _db.commit()


async def open_trades(user_id: int = None):
    if user_id is None:
        cur = await _db.execute("SELECT * FROM trades WHERE status='OPEN'")
    else:
        cur = await _db.execute(
            "SELECT * FROM trades WHERE status='OPEN' AND user_id=?", (user_id,)
        )
    return await cur.fetchall()


async def recent_trades(user_id: int, limit: int = 10):
    cur = await _db.execute(
        "SELECT * FROM trades WHERE user_id=? ORDER BY id DESC LIMIT ?",
        (user_id, limit),
    )
    return await cur.fetchall()


def _calc_pnl(t, status: str, price: float) -> float:
    """Чесний PnL від ФАКТИЧНОЇ ціни виходу (не від «дійшла до рівня»).

    Позиція ділиться 50/50 між TP1 і TP2. На TP1 рахуємо рух до tp1,
    на TP2 — повний рух до tp2; при SL/EXPIRED — рух до фактичної ціни.
    Маржа не задана (базова підписка) → показуємо % руху як умовний PnL.
    """
    side = 1 if t["side"] == "LONG" else -1
    entry = t["entry"] or 0.0
    if entry <= 0:
        return 0.0
    margin = t["margin"] or 0.0
    lev = t["leverage"] or 1
    if status == "TP1":
        move = (min(price, t["tp2"]) - entry) / entry * side
    elif status == "TP2":
        move = (t["tp2"] - entry) / entry * side
    else:  # SL або EXPIRED — по факту
        move = (price - entry) / entry * side
    move = max(min(move, 3.0), -1.0)          # ліквідація: не більше -100% маржі
    if margin > 0:
        pnl = (margin / 2) * move * lev       # кожен тейк бере половину маржі
        return round(pnl, 4)
    return round(move * 100, 2)               # без депозиту — умовні $ (≈%)


async def close_trade(trade_id: int, status: str, price: float):
    cur = await _db.execute("SELECT * FROM trades WHERE id=?", (trade_id,))
    t = await cur.fetchone()
    if not t:
        return 0.0
    pnl = _calc_pnl(t, status, price)
    await _db.execute(
        "UPDATE trades SET status=?, pnl=pnl+?, closed_at=? WHERE id=?",
        (status, pnl, int(time.time()), trade_id),
    )
    await _db.commit()
    return pnl


async def day_pnl(user_id: int):
    """PnL за останні 24 години"""
    since = int(time.time()) - 86400
    cur = await _db.execute(
        "SELECT COALESCE(SUM(pnl),0) s, COUNT(*) c FROM trades "
        "WHERE user_id=? AND closed_at >= ?",
        (user_id, since),
    )
    return await cur.fetchone()


async def total_pnl(user_id: int):
    cur = await _db.execute(
        "SELECT COALESCE(SUM(pnl),0) s FROM trades WHERE user_id=?", (user_id,)
    )
    return await cur.fetchone()


async def all_users_with_open():
    cur = await _db.execute(
        "SELECT DISTINCT user_id FROM trades WHERE status IN ('OPEN','TP1')"
    )
    return await cur.fetchall()


async def recent_statuses(user_id: int, limit: int = 10):
    """Останні закриті зделки користувача (для стопа серій)."""
    cur = await _db.execute(
        "SELECT status FROM trades WHERE user_id=? AND status!='OPEN' "
        "ORDER BY closed_at DESC LIMIT ?", (user_id, limit))
    return [r["status"] for r in await cur.fetchall()]


async def winrate_stats(user_id: int = None, days: int = 30):
    """Статистика TP/SL за останні N днів (winrate і серії)."""
    since = int(time.time()) - days * 86400
    q = ("SELECT status, COUNT(*) c FROM trades WHERE status!='OPEN' AND closed_at>=?"
         + (" AND user_id=?" if user_id else "") + " GROUP BY status")
    args = (since,) if user_id is None else (since, user_id)
    cur = await _db.execute(q, args)
    rows = {r["status"]: r["c"] for r in await cur.fetchall()}
    tp = rows.get("TP1", 0) + rows.get("TP2", 0)
    sl = rows.get("SL", 0)
    total = tp + sl
    wr = round(tp / total * 100, 1) if total else 0
    return {"tp1": rows.get("TP1", 0), "tp2": rows.get("TP2", 0),
            "sl": sl, "total": total, "winrate": wr}


async def count_open_for_user(user_id: int) -> int:
    cur = await _db.execute(
        "SELECT COUNT(*) c FROM trades WHERE user_id=? AND status IN ('OPEN','TP1')",
        (user_id,))
    return (await cur.fetchone())["c"]


# ---------- нові функції: історія сигналів + статистика сроботності ----------

async def add_signal_log(symbol: str, side: str, entry: float, sl: float,
                         tp1: float, tp2: float, score: int):
    """Журнал усіх зделок, які видав бот (для /stats та self-learning AI)."""
    await _db.execute(
        "INSERT INTO signal_log(symbol,side,entry,sl,tp1,tp2,score,status,created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (symbol, side, entry, sl, tp1, tp2, score, "OPEN", int(time.time())))
    await _db.commit()


async def open_signal_ids(max_age_min: int = 720):
    """OPEN-сигнали, яким менше max_age хвилин (щоб не чекати вічно)."""
    since = int(time.time()) - max_age_min * 60
    cur = await _db.execute(
        "SELECT * FROM signal_log WHERE status='OPEN' AND created_at>=?", (since,))
    return await cur.fetchall()


async def close_signal(sig_id: int, status: str):
    await _db.execute("UPDATE signal_log SET status=?, closed_at=? WHERE id=?",
                      (status, int(time.time()), sig_id))
    await _db.commit()


async def signal_stats(days: int = 30):
    """Скільки сигналів спрацювало (TP) проти стопів — по всьому журналу."""
    since = int(time.time()) - days * 86400
    cur = await _db.execute(
        "SELECT status, COUNT(*) c FROM signal_log WHERE status!='OPEN' "
        "AND created_at>=? GROUP BY status", (since,))
    rows = {r["status"]: r["c"] for r in await cur.fetchall()}
    tp = rows.get("TP1", 0) + rows.get("TP2", 0)
    sl = rows.get("SL", 0)
    total = tp + sl
    wr = round(tp / total * 100, 1) if total else 0
    return {"tp1": rows.get("TP1", 0), "tp2": rows.get("TP2", 0),
            "sl": sl, "total": total, "winrate": wr}


async def symbol_winrate(symbol: str, days: int = 30):
    """Винрейт по конкретній монеті в журналі (для AI-штрафу токсичних пар)."""
    since = int(time.time()) - days * 86400
    cur = await _db.execute(
        "SELECT status, COUNT(*) c FROM signal_log WHERE symbol=? AND status!='OPEN' "
        "AND created_at>=? GROUP BY status", (symbol, since))
    rows = {r["status"]: r["c"] for r in await cur.fetchall()}
    tp = rows.get("TP1", 0) + rows.get("TP2", 0)
    sl = rows.get("SL", 0)
    total = tp + sl
    if total < 2:
        return None
    return round(tp / total * 100, 1)


# ------------------------- ПРОМОКОДИ -------------------------

async def create_promo(code: str, plan: str, days: int, max_uses: int = 1):
    """Створити/перезаписати промокод. Повертає True, якщо створено."""
    code = code.upper().strip()
    if not code or plan not in ("base", "pro") or days <= 0:
        return False
    await _db.execute(
        "INSERT INTO promos(code,plan,days,max_uses,used,active,created_at) "
        "VALUES(?,?,?,?,0,1,?) ON CONFLICT(code) DO UPDATE SET "
        "plan=excluded.plan, days=excluded.days, max_uses=excluded.max_uses, "
        "used=0, active=1",
        (code, plan, days, max(1, max_uses), int(time.time())))
    await _db.commit()
    return True


async def get_promo(code: str):
    cur = await _db.execute("SELECT * FROM promos WHERE code=?", (code.upper().strip(),))
    return await cur.fetchone()


async def use_promo(code: str) -> bool:
    """Атомарно збільшує лічильник використань; False — якщо код невалідний/вичерпаний."""
    cur = await _db.execute(
        "UPDATE promos SET used=used+1 WHERE code=? AND active=1 AND used<max_uses",
        (code.upper().strip(),))
    await _db.commit()
    return cur.rowcount > 0


async def list_promos():
    cur = await _db.execute("SELECT * FROM promos ORDER BY created_at DESC LIMIT 50")
    return await cur.fetchall()


async def toggle_promo(code: str, active: bool):
    cur = await _db.execute("UPDATE promos SET active=? WHERE code=?",
                            (1 if active else 0, code.upper().strip()))
    await _db.commit()
    return cur.rowcount > 0

