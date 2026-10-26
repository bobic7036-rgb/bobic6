import time
from bot import db


def fmt_price(p: float) -> str:
    if p >= 1000:
        return f"{p:,.2f}"
    if p >= 10:
        return f"{p:.3f}".rstrip("0").rstrip(".")
    if p >= 0.1:
        return f"{p:.4f}".rstrip("0").rstrip(".")
    return f"{p:.6f}".rstrip("0").rstrip(".")


def side_emoji(side: str) -> str:
    return "🟢 LONG" if side == "LONG" else "🔴 SHORT"


def signal_text(t, lev=None, margin=None) -> str:
    coin = t["symbol"].replace("USDT", "")
    lines = [
        f"⚡️ <b>ЗДЕЛКА: {coin}USDT</b>",
        "",
        f"Напрямок: <b>{side_emoji(t['side'])}</b>",
        f"Шанс (за аналізом): <b>{t['score']}%</b>",
        f"📍 Вхід: <code>{fmt_price(t['entry'])}</code>",
        f"🛑 Стоп-лос: <code>{fmt_price(t['sl'])}</code>",
        f"✅ Тейк 1: <code>{fmt_price(t['tp1'])}</code>",
        f"✅ Тейк 2: <code>{fmt_price(t['tp2'])}</code>",
    ]
    if lev:
        lines += [
            "",
            f"🎯 Рекомендоване плече: <b>x{lev}</b>",
            f"💰 Маржа на зделку: <b>{fmt_price(margin)} USDT</b> (~50% на TP1, ~50% на TP2)",
        ]
    lines += ["", f"🧠 <i>Чому так піде:</i> {t['reason']}"]
    return "\n".join(lines)


def result_text(t, status: str, price: float) -> str:
    coin = t["symbol"].replace("USDT", "")
    emoji = {"TP1": "✅", "TP2": "🏆", "SL": "🛑"}[status]
    label = {"TP1": "тейк-профіт 1", "TP2": "тейк-профіт 2", "SL": "СТОП-ЛОС"}[status]
    pnl = t["pnl"]
    pnl_str = f"+{pnl:.2f}" if pnl >= 0 else f"{pnl:.2f}"
    return (
        f"{emoji} <b>РЕЗУЛЬТАТ: {coin}USDT ({t['side']})</b>\n"
        f"Ціна дійшла до {label}: <code>{fmt_price(price)}</code>\n"
        f"Вхід був: <code>{fmt_price(t['entry'])}</code>, шанс {t['score']}%\n"
        f"PnL по зделці: <b>{pnl_str} USDT</b>"
    )
