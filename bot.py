#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Granville V5 → BingX → Telegram.   Port de la estrategia de TradingView "Granville V5 — ESTRATEGIA".

BOT_MODE=signals  (por defecto) solo avisa por Telegram. No necesita claves de BingX.
BOT_MODE=demo     opera en la cuenta VIRTUAL (VST) de BingX.
BOT_MODE=live     opera con DINERO REAL (exige LIVE_CONFIRM=SI_OPERAR_CON_DINERO_REAL).

Uso rápido:
  python bot.py --test-telegram
  python bot.py --warmup top:60 --days 45          # calcula qué códigos tienen ventaja y lo guarda en stats.json
  python bot.py --backtest top:60 --days 45        # lo mismo sin guardar, con informe de la estrategia
  python bot.py --once --dry-run                   # un escaneo en consola
  python bot.py                                    # arranca el bot
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import math
import os
import re
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import requests

import engine as E
from engine import BOOKS, CODES, KIND_LABEL, TF_MS

BASE_URL = "https://open-api.bingx.com"
log = logging.getLogger("granville")
STATUS: dict = {"started": time.time(), "last_scan": {}, "universe": 0, "last_alert": 0.0}
TRADER = None


def load_env(path: str = ".env") -> None:
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.split(" #")[0].strip().strip('"').strip("'"))


def atomic_write(path: str, text: str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)


# ============================================================
# BINGX (datos públicos de mercado)
# ============================================================
class BingX:
    def __init__(self, rps: float, timeout: float = 15.0):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "granville-v5/1.0"})
        self.timeout = timeout
        self.min_interval = 1.0 / max(rps, 0.5)
        self.next_t = 0.0
        self.lock = threading.Lock()

    def _wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            t = max(now, self.next_t)
            self.next_t = t + self.min_interval
        if t > now:
            time.sleep(t - now)

    def get(self, path: str, params: Optional[dict] = None, retries: int = 4):
        last: Optional[Exception] = None
        for attempt in range(retries):
            self._wait()
            try:
                r = self.session.get(BASE_URL + path, params=params, timeout=self.timeout)
                if r.status_code == 429:
                    time.sleep(2 + 3 * attempt)
                    last = RuntimeError("HTTP 429 (límite de peticiones)")
                    continue
                r.raise_for_status()
                j = r.json()
                if j.get("code", 0) != 0:
                    raise RuntimeError(f"BingX code={j.get('code')} msg={j.get('msg')}")
                return j.get("data")
            except Exception as ex:  # noqa: BLE001
                last = ex
                time.sleep(0.5 * (attempt + 1))
        raise RuntimeError(f"Fallo en {path}: {last}")

    def contracts(self, quotes: list[str]) -> list[dict]:
        out = []
        for c in self.get("/openApi/swap/v2/quote/contracts") or []:
            sym = str(c.get("symbol", ""))
            if "-" in sym and str(c.get("status", "1")) == "1" and sym.split("-")[-1].upper() in quotes:
                out.append(c)
        return out

    def volumes_24h(self) -> dict[str, float]:
        vols = {}
        for t in self.get("/openApi/swap/v2/quote/ticker") or []:
            try:
                q = t.get("quoteVolume")
                vols[t["symbol"]] = float(q) if q not in (None, "") else float(t.get("volume", 0)) * float(t.get("lastPrice", 0))
            except Exception:  # noqa: BLE001
                continue
        return vols

    def klines(self, symbol: str, interval: str, limit: int, end_time: Optional[int] = None) -> pd.DataFrame:
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        if end_time:
            params["endTime"] = int(end_time)
        data = self.get("/openApi/swap/v3/quote/klines", params)
        if not data:
            return pd.DataFrame()
        df = pd.DataFrame(data)
        need = ["time", "open", "high", "low", "close", "volume"]
        if not all(k in df.columns for k in need):
            raise RuntimeError(f"Formato de klines inesperado: {list(df.columns)}")
        df = df[need].astype(float)
        df["time"] = df["time"].astype("int64")
        return df.sort_values("time").drop_duplicates("time").reset_index(drop=True)


def closed_only(df: pd.DataFrame, interval: str, now_ms: Optional[int] = None) -> pd.DataFrame:
    if df.empty:
        return df
    now_ms = now_ms or int(time.time() * 1000)
    return df[df["time"] + TF_MS[interval] <= now_ms].reset_index(drop=True)


def select_universe(api: BingX, cfg, contracts: list[dict]) -> list[dict]:
    vols = api.volumes_24h()
    inc = re.compile(cfg.include_re, re.I) if cfg.include_re else None
    exc = re.compile(cfg.exclude_re, re.I) if cfg.exclude_re else None
    out = []
    for c in contracts:
        s = c["symbol"]
        if vols.get(s, 0.0) < cfg.min_volume or (inc and not inc.search(s)) or (exc and exc.search(s)):
            continue
        out.append(c)
    return sorted(out, key=lambda c: -vols.get(c["symbol"], 0.0))


class DataHub:
    """Descarga y cachea los timeframes superiores (solo se renuevan cuando empieza una vela superior nueva)."""

    def __init__(self, api: BingX, cfg):
        self.api, self.cfg = api, cfg
        self.cache: dict = {}
        self.btc: dict = {}
        self.lock = threading.Lock()
        self.tfs = sorted({cfg.htf_tf, cfg.htf2_tf, cfg.hap_tf1, cfg.hap_tf2})
        for tf in self.tfs:
            if tf not in TF_MS:
                raise SystemExit(f"Timeframe desconocido: {tf}")

    def frame(self, symbol: str, tf: str, now_ms: int, force: bool = False):
        boundary = now_ms // TF_MS[tf] * TF_MS[tf]
        key = (symbol, tf)
        hit = self.cache.get(key)
        if hit and hit[0] == boundary and not force:
            return hit[1]
        df = closed_only(self.api.klines(symbol, tf, self.cfg.htf_candles), tf, now_ms)
        fr = E.htf_frame(df, self.cfg) if len(df) > 10 else None
        with self.lock:
            self.cache[key] = (boundary, fr)
        return fr

    def frames(self, symbol: str, now_ms: int, force: bool = False) -> dict:
        return {tf: self.frame(symbol, tf, now_ms, force) for tf in self.tfs}

    def btc_slope(self, tf: str, now_ms: int) -> Optional[pd.Series]:
        if not self.cfg.use_btc:
            return None
        boundary = now_ms // TF_MS[tf] * TF_MS[tf]
        with self.lock:
            hit = self.btc.get(tf)
        if hit and hit[0] == boundary:
            return hit[1]
        df = closed_only(self.api.klines(self.cfg.btc_symbol, tf, self.cfg.candles), tf, now_ms)
        c = df["close"].to_numpy(dtype=float)
        ma = E.sma(c, self.cfg.ma_length) if self.cfg.ma_type == "SMA" else E.ema(c, self.cfg.ma_length)
        s = pd.Series(ma - E.shift(ma), index=df["time"].to_numpy(dtype=np.int64))
        with self.lock:
            self.btc[tf] = (boundary, s)
        return s


# ============================================================
# TELEGRAM
# ============================================================
class Notifier:
    def __init__(self, cfg, dry: bool = False):
        self.cfg, self.dry = cfg, dry

    def send(self, text: str) -> bool:
        if self.dry or not (self.cfg.token and self.cfg.chat_id):
            print("\n" + re.sub(r"<[^>]+>", "", html.unescape(text)))
            return True
        url = f"https://api.telegram.org/bot{self.cfg.token}/sendMessage"
        for chunk in [text[i:i + 3900] for i in range(0, len(text), 3900)] or [""]:
            for attempt in range(3):
                try:
                    r = requests.post(url, json={"chat_id": self.cfg.chat_id, "text": chunk, "parse_mode": "HTML",
                                                 "disable_web_page_preview": True}, timeout=15)
                    if r.status_code == 429:
                        time.sleep(float(r.json().get("parameters", {}).get("retry_after", 3)) + 1)
                        continue
                    if r.ok:
                        break
                    log.error("Telegram %s: %s", r.status_code, r.text[:200])
                    return False
                except requests.RequestException as ex:
                    log.error("Telegram: %s", ex)
                    time.sleep(2)
            else:
                return False
        return True

    def alert_once(self, msg: str, every: float = 1800) -> None:
        if time.time() - STATUS["last_alert"] > every:
            STATUS["last_alert"] = time.time()
            self.send(f"⚠️ {html.escape(msg)}")


# ============================================================
# ESTADO PERSISTENTE
# ============================================================
class Store:
    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.RLock()
        self.seen: dict[str, int] = {}
        self.sims: dict[str, dict] = {}
        self.stats = E.Stats(cfg)
        self.summary_day = ""
        p = Path(cfg.state_file)
        if p.exists():
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
                self.seen, self.summary_day = d.get("seen", {}), d.get("summary_day", "")
            except Exception as ex:  # noqa: BLE001
                log.warning("state.json ilegible (%s)", ex)
        p = Path(cfg.sims_file)
        if p.exists():
            try:
                self.sims = json.loads(p.read_text(encoding="utf-8"))
            except Exception as ex:  # noqa: BLE001
                log.warning("sims.json ilegible (%s)", ex)
        self.stats.load(cfg.stats_file)

    def save(self) -> None:
        with self.lock:
            cutoff = int(time.time() * 1000) - 3 * 86_400_000
            self.seen = {k: v for k, v in self.seen.items() if v >= cutoff}
            atomic_write(self.cfg.state_file, json.dumps({"seen": self.seen, "summary_day": self.summary_day}))
            atomic_write(self.cfg.sims_file, json.dumps(self.sims, default=float))
        self.stats.save(self.cfg.stats_file)


# ============================================================
# MENSAJES
# ============================================================
def fmt_px(x: float, prec: int) -> str:
    return f"{x:.{prec}f}"


def edge_line(st: dict, cfg) -> str:
    if st["n"] == 0:
        return "📊 Sin historial de este código todavía"
    base = f"n={st['n']:.0f} · R posterior {st['post']:+.2f} · límite inf. {st['lo']:+.2f} · acierto {st['wr']:.0f}%"
    if st["good"]:
        return f"⭐ <b>VENTAJA demostrada</b> ({base})"
    if not st["known"]:
        return f"📊 Pocos datos para juzgar ({base}; mínimo {cfg.min_n})"
    if st["bad"]:
        return f"🚫 Historial NEGATIVO ({base})"
    return f"➖ Sin ventaja demostrada ({base})"


def build_signal_message(s: dict, A: dict, st: dict, cfg, prec: int, real_note: str) -> str:
    side = "LONG" if s["side"] == 1 else "SHORT"
    name = CODES[s["code"]]
    sd = s["side"]
    px, rk = s["price"], s["rk"]
    sl = px - sd * rk
    tp1 = px + sd * rk * (cfg.tp1_r if cfg.partial_mode else cfg.tp_single_r)
    tp2 = px + sd * rk * cfg.tp2_r
    mult = cfg.long_risk_mult if sd == 1 else cfg.short_risk_mult
    risk_cash = cfg.account * cfg.risk_pct / 100 * mult
    qty = min(risk_cash / rk, cfg.account * cfg.max_lev / px)
    sym = s["symbol"]
    d = A["diag"]
    sess = ("Asia/noche", "Europa", "EEUU")[s["sess"]]
    when = datetime.fromtimestamp((s["time"] + TF_MS[s["tf"]]) / 1000, tz=timezone.utc)
    exits = (f"🎯 TP1: {fmt_px(tp1, prec)} ({cfg.tp1_r:g}R, cierra {cfg.tp1_frac * 100:.0f}%) · TP2: {fmt_px(tp2, prec)} ({cfg.tp2_r:g}R)\n"
             f"↪ Tras TP1: stop a la entrada · salida por tiempo a las {cfg.max_bars} velas" if cfg.partial_mode else
             f"🎯 TP: {fmt_px(tp1, prec)} ({cfg.tp_single_r:g}R) · salida por tiempo a las {cfg.max_bars} velas")
    hap = f"HaP {d['hap']:.0f}" if d["hap"] is not None else "HaP calentando"
    link = f"https://www.tradingview.com/chart/?symbol=BINGX:{sym.replace('-', '')}.P&interval={TF_MS[s['tf']] // 60_000}"
    return (
        f"{'🟢' if sd == 1 else '🔴'} <b>{side}</b> · {html.escape(sym)} · {s['tf']}\n"
        f"<b>{name}</b> ({KIND_LABEL[name]}) · libro {BOOKS[s['book']]}\n\n"
        f"{edge_line(st, cfg)}\n\n"
        f"Precio ref. (cierre vela): {fmt_px(px, prec)}\n"
        f"🛑 SL: {fmt_px(sl, prec)} ({rk / px * 100:.2f}% · {rk / max(A['atr'][-1], 1e-12):.2f} ATR)\n"
        f"{exits}\n\n"
        f"Tamaño (riesgo {cfg.risk_pct * mult:g}% de {cfg.account:g} USDT = {risk_cash:.2f}):\n"
        f"{qty:.6g} {sym.split('-')[0]} ≈ {qty * px:.2f} USDT nominal\n\n"
        f"{hap} · RVOL {d['rvol']:.2f} · RSI {d['rsi']:.0f} · sesión {sess}\n"
        f"{real_note}"
        f"Vela cerrada {when:%H:%M} UTC\n"
        f"<a href=\"{link}\">Abrir gráfico</a>\n"
        f"<i>Señal orientativa, no es consejo financiero. Revisa el gráfico antes de entrar.</i>")


def book_table(stats: E.Stats, tf: str, book: int, cfg) -> str:
    lines = [f"<b>{BOOKS[book]}</b> · {tf}", "<pre>cód    n   acierto  media R  posterior  lím.inf</pre>"]
    rows = []
    for k in range(8):
        st = stats.get(tf, book, k)
        flag = "⭐" if st["good"] and not E.is_ext(k) else ("✅" if st["good"] else ("🚫" if st["bad"] else "  "))
        rows.append(f"{CODES[k]:<4s}{st['n']:5.0f}  {st['wr']:5.0f}%  {st['mean']:+7.2f}  {st['post']:+8.2f}  {st['lo']:+7.2f} {flag}")
    tot = stats.total(tf, book)
    rows.append(f"TOT {tot['n']:6.0f}  {tot['wr']:5.0f}%  {tot['avg']:+7.2f}")
    return "\n".join(lines) + "\n<pre>" + "\n".join(rows) + "</pre>"


# ============================================================
# ESCANEO
# ============================================================
def scan(api: BingX, hub: DataHub, cfg, store: Store, notifier: Notifier, universe: list[dict], tf: str, dry: bool) -> int:
    now_ms = int(time.time() * 1000)
    tfms = TF_MS[tf]
    btc = hub.btc_slope(tf, now_ms) if cfg.use_btc else None
    prec_of = {c["symbol"]: int(c.get("pricePrecision", 6) or 6) for c in universe}
    errors: list[str] = []

    def work(sym: str):
        try:
            df = closed_only(api.klines(sym, tf, cfg.candles), tf, now_ms)
            if len(df) < cfg.ma_length + 30 or int(df["time"].iloc[-1]) + tfms < now_ms - 3 * tfms:
                return sym, None
            ctx = {"frames": hub.frames(sym, now_ms), "btc": btc}
            A = E.compute_signals(df, tf, ctx, cfg)
            if A is not None and not A["data_ok"]:
                ctx["frames"] = hub.frames(sym, now_ms, force=True)
                A = E.compute_signals(df, tf, ctx, cfg)
            return sym, A
        except Exception as ex:  # noqa: BLE001
            errors.append(f"{sym}: {ex}")
            return sym, None

    with ThreadPoolExecutor(max_workers=cfg.workers) as ex:
        results = list(ex.map(work, [c["symbol"] for c in universe]))
    if errors and len(errors) > max(5, len(universe) // 4):
        notifier.alert_once(f"BingX falla en {len(errors)}/{len(universe)} símbolos (p. ej. {errors[0][:120]})")

    sent = rest = new_virtual = 0
    book = cfg.book
    with store.lock:
        for sym, A in results:
            if A is None:
                continue
            fresh = now_ms - (int(A["time"][-1]) + tfms) <= cfg.max_age_factor * tfms
            st = store.sims.setdefault(f"{sym}|{tf}", {"open": [], "last_ms": None})
            closed, sigs = E.advance(st, A, tf, sym, cfg, store.stats, fresh)
            new_virtual += len(closed)
            if TRADER is not None and sym in TRADER.trades:
                TRADER.on_bars(sym, A)
            for s in sigs:
                if s["book"] != book:
                    continue
                key = f"{sym}|{tf}|{book}|{s['time']}"
                if key in store.seen:
                    continue
                stats_now = store.stats.get(tf, book, s["code"])
                edge = store.stats.edge_ok(tf, book, s["code"])
                cand = {"symbol": sym, "tf": tf, "side": "LONG" if s["side"] == 1 else "SHORT", "code": s["code"],
                        "price": s["price"], "rk": s["rk"], "time": s["time"]}
                will_trade = bool(TRADER is not None and edge and not dry)
                notify = (not s["busy"]) or will_trade
                if cfg.notify_only_edge and not stats_now["good"]:
                    notify = False
                store.seen[key] = now_ms
                if notify:
                    if sent >= cfg.max_per_scan:
                        rest += 1
                    else:
                        note = ""
                        if TRADER is not None:
                            note = ("🤖 Operación " + ("REAL" if cfg.mode == "live" else "DEMO") + ": " +
                                    ("se intentará abrir (pasa el filtro de ventaja)\n" if will_trade else "NO (el filtro de ventaja no la permite)\n"))
                        if notifier.send(build_signal_message(s, A, stats_now, cfg, prec_of.get(sym, 6), note)):
                            sent += 1
                if will_trade:
                    TRADER.on_signal(cand, prec_of.get(sym, 6))
        # limpiar simulaciones de símbolos que ya no se escanean
        live_keys = {f"{c['symbol']}|{tf}" for c in universe}
        for k in [k for k, v in store.sims.items() if k.endswith(f"|{tf}") and k not in live_keys
                  and now_ms - (v.get("last_ms") or 0) > (cfg.max_bars + 3) * tfms]:
            store.sims.pop(k, None)
    if rest:
        notifier.send(f"… y {rest} señales más del libro {BOOKS[book]} en este escaneo (MAX_SIGNALS_PER_SCAN={cfg.max_per_scan}).")
    STATUS["last_scan"][tf] = {"t": time.time(), "symbols": len(universe), "errors": len(errors), "closed": new_virtual, "sent": sent}
    log.info("Escaneo %s: %d símbolos · %d avisos · %d operaciones virtuales cerradas · %d errores",
             tf, len(universe), sent, new_virtual, len(errors))
    return sent


# ============================================================
# HISTÓRICO: warm-up de estadísticas y backtest (mismo motor que en vivo)
# ============================================================
def fetch_history(api: BingX, symbol: str, interval: str, days: float, extra_bars: int = 450) -> pd.DataFrame:
    need = int(days * 86_400_000 // TF_MS[interval]) + extra_bars
    chunks, total, end = [], 0, None
    for _ in range(80):
        df = api.klines(symbol, interval, 1000, end)
        if df.empty:
            break
        chunks.append(df)
        total += len(df)
        if total >= need:
            break
        new_end = int(df["time"].iloc[0]) - 1
        if (end is not None and new_end >= end) or len(df) < 50:
            break
        end = new_end
    if not chunks:
        return pd.DataFrame()
    out = pd.concat(chunks).sort_values("time").drop_duplicates("time").reset_index(drop=True)
    return closed_only(out, interval).tail(need).reset_index(drop=True)


def sim_strategy_trade(A: dict, i: int, side: int, rk: float, cfg) -> tuple[float, int]:
    """Operación 'real' de la estrategia desde la vela i: mismas salidas que el Pine. Devuelve (R, índice de salida)."""
    px = float(A["close"][i])
    tr = {"side": side, "entry": px, "risk": rk, "stop": px - side * rk, "half": 0, "bars": 0}
    step = E.vstep if cfg.partial_mode else E.vstep_single
    n = len(A["time"])
    for j in range(i + 1, n):
        res = step(tr, float(A["high"][j]), float(A["low"][j]), float(A["close"][j]), cfg)
        if res is not None:
            return res, j
    return float("nan"), n


def history_for_symbol(api: BingX, cfg, sym: str, tf: str, days: float, btc: Optional[pd.Series]) -> Optional[dict]:
    df = fetch_history(api, sym, tf, days)
    if len(df) < cfg.ma_length + 200:
        return None
    frames = {}
    for htf in sorted({cfg.htf_tf, cfg.htf2_tf, cfg.hap_tf1, cfg.hap_tf2}):
        frames[htf] = E.htf_frame(fetch_history(api, sym, htf, days + 5, extra_bars=300), cfg)
    A = E.compute_signals(df, tf, {"frames": frames, "btc": btc}, cfg)
    if A is None:
        return None
    cutoff = int(time.time() * 1000) - int(days * 86_400_000)
    start = max(int(np.searchsorted(A["time"], cutoff)), cfg.ma_length + 30)
    state, closed, sel = {"open": []}, [], []
    for i in range(start, len(A["time"])):
        c, sg = E.process_bar(state, A, i, tf, sym, cfg, spawn=True)
        closed += c
        sel += [s for s in sg if s["book"] == cfg.book]
    strat = []
    for s in sel:
        r, j = sim_strategy_trade(A, s["idx"], s["side"], s["rk"], cfg)
        if not math.isnan(r):
            strat.append({**s, "R": r, "exit_ms": int(A["time"][j]) + TF_MS[tf]})
    return {"symbol": sym, "closed": closed, "strat": strat}


def replay_strategy(results: list[dict], tf: str, cfg) -> list[dict]:
    """Reproduce cronológicamente: las estadísticas crecen con el tiempo y el filtro de ventaja decide cada entrada."""
    stats = E.Stats(cfg)
    ev = []
    for r in results:
        for tr in r["closed"]:
            ev.append((tr["exit_ms"], 0, tr))
        for s in r["strat"]:
            ev.append((s["time"] + TF_MS[tf], 1, s))
    ev.sort(key=lambda x: (x[0], x[1]))
    taken, busy_until = [], {}
    for t, kind, p in ev:
        if kind == 0:
            stats.update(tf, p["book"], p["code"], p["sess"], p["R"])
        elif stats.edge_ok(tf, cfg.book, p["code"]) and busy_until.get(p["symbol"], 0) <= t:
            taken.append(p)
            busy_until[p["symbol"]] = p["exit_ms"]
    return taken


def fmt_pf(x: float) -> str:
    return "∞" if x == float("inf") else f"{x:.2f}"


def print_group(title: str, rows: list[dict], keyf) -> None:
    g: dict = {}
    for r in rows:
        g.setdefault(keyf(r), []).append(r["R"])
    print(f"\n{title}\n  {'':10s} {'n':>5s} {'acierto':>7s} {'R/op':>7s} {'PF':>5s} {'total R':>8s} {'t':>5s}  veredicto")
    for k, rs in sorted(g.items(), key=lambda kv: -len(kv[1])):
        s = E.summarize(rs)
        print(f"  {str(k):10s} {s['n']:5d} {s['wr'] * 100:6.0f}% {s['avg']:+7.2f} {fmt_pf(s['pf']):>5s} {s['sum']:+8.1f} {s['t']:5.1f}  {E.verdict(s)}")


def cmd_history(api: BingX, cfg, spec: str, days: float, tfs: list[str], save: bool) -> None:
    contracts = {c["symbol"]: c for c in api.contracts(cfg.quotes)}
    if spec.lower().startswith("top:"):
        vols = api.volumes_24h()
        syms = [s for s, _ in sorted(vols.items(), key=lambda kv: -kv[1]) if s in contracts][:int(spec.split(":")[1])]
    else:
        syms = [s.strip().upper() for s in spec.split(",") if s.strip()]
    if cfg.exclude_re:
        syms = [s for s in syms if not re.search(cfg.exclude_re, s, re.I)]
    print(f"Histórico: {len(syms)} contratos · {days:g} días · TF {', '.join(tfs)} · libro operado {BOOKS[cfg.book]} · filtro de ventaja {cfg.edge}")
    existing = E.Stats(cfg)
    if save:
        existing.load(cfg.stats_file)
    for tf in tfs:
        btc = None
        if cfg.use_btc:
            d = fetch_history(api, cfg.btc_symbol, tf, days)
            c = d["close"].to_numpy(dtype=float)
            ma = E.sma(c, cfg.ma_length) if cfg.ma_type == "SMA" else E.ema(c, cfg.ma_length)
            btc = pd.Series(ma - E.shift(ma), index=d["time"].to_numpy(dtype=np.int64))
        done = [0]

        def work(sym):
            try:
                return history_for_symbol(api, cfg, sym, tf, days, btc)
            except Exception as ex:  # noqa: BLE001
                log.warning("%s %s: %s", sym, tf, ex)
                return None
            finally:
                done[0] += 1
                if done[0] % 5 == 0:
                    print(f"  ... {done[0]}/{len(syms)}", file=sys.stderr)

        with ThreadPoolExecutor(max_workers=min(cfg.workers, 6)) as ex:
            results = [r for r in ex.map(work, syms) if r]
        allc = sorted((t for r in results for t in r["closed"]), key=lambda t: t["exit_ms"])
        stats = E.Stats(cfg)
        for t in allc:
            stats.update(tf, t["book"], t["code"], t["sess"], t["R"])
        print(f"\n================ {tf} · {len(results)} contratos con datos · {len(allc)} operaciones virtuales ================")
        for b in range(4):
            print(re.sub(r"</?(b|pre)>", "", book_table(stats, tf, b, cfg)))
        taken = replay_strategy(results, tf, cfg)
        everything = [s for r in results for s in r["strat"]]
        print(f"\n---- ESTRATEGIA (libro {BOOKS[cfg.book]}) ----")
        for label, rows in (("Todas las señales (sin filtro de ventaja)", everything), (f"Con filtro de ventaja '{cfg.edge}'", taken)):
            sm = E.summarize([r["R"] for r in rows])
            if not sm:
                print(f"{label}: sin operaciones")
                continue
            print(f"{label}: {sm['n']} ops · acierto {sm['wr'] * 100:.1f}% · {sm['avg']:+.3f} R/op · total {sm['sum']:+.1f}R · "
                  f"PF {fmt_pf(sm['pf'])} · racha máx. {sm['maxdd']:.1f}R · t={sm['t']:.2f} → {E.verdict(sm)}")
        if taken:
            print_group("Por código (con filtro)", taken, lambda r: CODES[r["code"]])
            print_group("Por lado (con filtro)", taken, lambda r: "LONG" if r["side"] == 1 else "SHORT")
        if save:
            existing.tf[tf] = stats.tf.get(tf, existing.tf.get(tf))
    print("\nLectura: con n < 30 no saques conclusiones. Costes ya restados (COST_PCT). Es histórico: no garantiza nada.\n"
          "Limitaciones: no modela el tope de posiciones simultáneas, ni el slippage de entrada, y solo incluye contratos que existen hoy.")
    if save:
        existing.save(cfg.stats_file)
        print(f"\n✅ Estadísticas guardadas en {cfg.stats_file}. El bot las usará como historial inicial.")


# ============================================================
# COMANDOS DE TELEGRAM
# ============================================================
def handle_command(cmd: str, cfg, store: Store, notifier: Notifier) -> None:
    tf = cfg.timeframes[0]
    if cmd == "/stats":
        notifier.send(book_table(store.stats, tf, cfg.book, cfg) + "\n\nTotales por libro: " + " · ".join(
            f"{BOOKS[b]} n={store.stats.total(tf, b)['n']:.0f} {store.stats.total(tf, b)['avg']:+.2f}R" for b in range(4)))
    elif cmd == "/todos":
        notifier.send("\n\n".join(book_table(store.stats, tf, b, cfg) for b in range(4)))
    elif cmd == "/codigos":
        lines = [f"<b>Códigos del libro {BOOKS[cfg.book]}</b> (filtro: {cfg.edge})"]
        for k in range(8):
            st = store.stats.get(tf, cfg.book, k)
            lines.append(f"{CODES[k]}: " + ("✅ operable" if store.stats.edge_ok(tf, cfg.book, k) else "⛔ bloqueado") +
                         f" · n={st['n']:.0f} · post {st['post']:+.2f} · lím.inf {st['lo']:+.2f}")
        notifier.send("\n".join(lines))
    elif cmd == "/abiertas":
        rows = [t for v in store.sims.values() for t in v["open"] if t["book"] == cfg.book]
        lines = [f"Operaciones virtuales abiertas del libro {BOOKS[cfg.book]}: {len(rows)}"]
        for t in rows[:20]:
            lines.append(f"{'🟢' if t['side'] == 1 else '🔴'} {html.escape(t['symbol'])} {CODES[t['code']]} · entrada {t['entry']:g} · SL {t['stop']:g} · vela {t['bars']}/{cfg.max_bars}")
        notifier.send("\n".join(lines))
    elif cmd == "/estado":
        up = (time.time() - STATUS["started"]) / 3600
        ls = " · ".join(f"{k}: hace {(time.time() - v['t']) / 60:.0f} min, {v['symbols']} símbolos, {v['errors']} errores" for k, v in STATUS["last_scan"].items())
        notifier.send(f"Bot activo {up:.1f} h · modo <b>{cfg.mode}</b> · libro {BOOKS[cfg.book]} · filtro {cfg.edge}\nÚltimo escaneo: {ls or 'aún ninguno'}")
    elif TRADER is not None and cmd == "/pausa":
        notifier.send(TRADER.pause())
    elif TRADER is not None and cmd == "/reanudar":
        notifier.send(TRADER.resume())
    elif TRADER is not None and cmd == "/cerrartodo":
        notifier.send(TRADER.close_all())
    elif TRADER is not None and cmd == "/live":
        notifier.send(TRADER.summary())
    elif cmd in ("/ayuda", "/help", "/start"):
        notifier.send("Comandos: /stats · /todos · /codigos · /abiertas · /estado" + (" · /live · /pausa · /reanudar · /cerrartodo" if TRADER else ""))


def command_loop(cfg, store: Store, notifier: Notifier, stop: threading.Event) -> None:
    url = f"https://api.telegram.org/bot{cfg.token}/getUpdates"
    offset = 0
    while not stop.is_set():
        try:
            r = requests.get(url, params={"timeout": 25, "offset": offset}, timeout=35).json()
            for u in r.get("result", []):
                offset = u["update_id"] + 1
                m = u.get("message") or {}
                if str(m.get("chat", {}).get("id")) != str(cfg.chat_id):
                    continue
                cmd = (m.get("text") or "").split()[0].split("@")[0].lower() if m.get("text") else ""
                if cmd.startswith("/"):
                    try:
                        handle_command(cmd, cfg, store, notifier)
                    except Exception as ex:  # noqa: BLE001
                        log.exception("comando %s", cmd)
                        notifier.send(f"Error en {html.escape(cmd)}: {html.escape(str(ex))}")
        except Exception as ex:  # noqa: BLE001
            log.debug("getUpdates: %s", ex)
            stop.wait(5)


def daily_summary(cfg, store: Store, notifier: Notifier) -> None:
    now = datetime.now(timezone.utc)
    day = now.strftime("%Y-%m-%d")
    if now.hour < cfg.summary_hour or store.summary_day == day:
        return
    store.summary_day = day
    tf = cfg.timeframes[0]
    codes = [CODES[k] for k in range(8) if store.stats.edge_ok(tf, cfg.book, k)]
    notifier.send("📅 <b>Resumen diario</b>\n" + "\n".join(
        f"{BOOKS[b]}: n={store.stats.total(tf, b)['n']:.0f} · {store.stats.total(tf, b)['wr']:.0f}% acierto · {store.stats.total(tf, b)['avg']:+.2f} R/op"
        for b in range(4)) + f"\nCódigos operables (libro {BOOKS[cfg.book]}, filtro {cfg.edge}): {', '.join(codes) or 'ninguno todavía'}"
        + (("\n\n" + TRADER.summary()) if TRADER else ""))


# ============================================================
# BUCLE PRINCIPAL
# ============================================================
def run_loop(api: BingX, cfg, store: Store, notifier: Notifier, once: bool, dry: bool) -> None:
    hub = DataHub(api, cfg)
    contracts = api.contracts(cfg.quotes)
    universe = select_universe(api, cfg, contracts)
    STATUS["universe"] = len(universe)
    if TRADER is not None and not dry:
        TRADER.startup(contracts)
    n_known = sum(1 for tf in cfg.timeframes for b in range(4) for k in range(8) if store.stats.get(tf, b, k)["n"] > 0)
    notifier.send(f"✅ <b>Granville V5 en marcha</b> · modo <b>{cfg.mode}</b> · libro {BOOKS[cfg.book]} · filtro de ventaja <b>{cfg.edge}</b>\n"
                  f"{len(universe)} contratos · TF {', '.join(cfg.timeframes)} · "
                  f"{'historial inicial cargado' if n_known else '⚠️ sin historial (usa --warmup para precargarlo)'}")
    if cfg.edge == "good" and not n_known:
        notifier.send("ℹ️ Con EDGE_FILTER=good no se operará ningún código hasta que acumule ≥ "
                      f"{cfg.min_n} operaciones con ventaja. Ejecuta <code>python bot.py --warmup top:60 --days 45</code> para precargar.")
    due = {tf: (int(time.time() * 1000) // TF_MS[tf] + 1) * TF_MS[tf] / 1000 + cfg.scan_delay for tf in cfg.timeframes}
    if once:
        for tf in cfg.timeframes:
            scan(api, hub, cfg, store, notifier, universe, tf, dry)
        store.save()
        return
    stop = threading.Event()
    if cfg.commands and cfg.token and cfg.chat_id and not dry:
        threading.Thread(target=command_loop, args=(cfg, store, notifier, stop), daemon=True).start()
    last_hk = 0.0
    last_refresh = time.time()
    try:
        while True:
            now = time.time()
            for tf in cfg.timeframes:
                if now >= due[tf]:
                    try:
                        if now - last_refresh > 6 * 3600:
                            contracts = api.contracts(cfg.quotes)
                            last_refresh = now
                            if TRADER is not None:
                                TRADER.set_contracts(contracts)
                        universe = select_universe(api, cfg, contracts)
                        STATUS["universe"] = len(universe)
                        scan(api, hub, cfg, store, notifier, universe, tf, dry)
                        store.save()
                        daily_summary(cfg, store, notifier)
                    except Exception as ex:  # noqa: BLE001
                        log.exception("Error en el escaneo %s", tf)
                        notifier.alert_once(f"Error en el escaneo {tf}: {ex}")
                    due[tf] = (int(time.time() * 1000) // TF_MS[tf] + 1) * TF_MS[tf] / 1000 + cfg.scan_delay
            if TRADER is not None and not dry and now - last_hk > 30:
                last_hk = now
                TRADER.housekeeping()
            time.sleep(1.0)
    except KeyboardInterrupt:
        stop.set()
        store.save()
        log.info("Detenido.")


# ============================================================
# DIAGNÓSTICO
# ============================================================
def cmd_debug(api: BingX, cfg, symbol: str, tf: str) -> None:
    hub = DataHub(api, cfg)
    now_ms = int(time.time() * 1000)
    df = closed_only(api.klines(symbol, tf, cfg.candles), tf, now_ms)
    A = E.compute_signals(df, tf, {"frames": hub.frames(symbol, now_ms), "btc": hub.btc_slope(tf, now_ms)}, cfg)
    if A is None:
        sys.exit("Datos insuficientes")
    i = len(df) - 1
    t = datetime.fromtimestamp(int(A["time"][i]) / 1000, tz=timezone.utc)
    print(f"{symbol} {tf} · última vela cerrada {t:%Y-%m-%d %H:%M} UTC · datos TF superior OK: {A['data_ok']}")
    for k, v in A["diag"].items():
        print(f"  {k:10s} {v}")
    print(f"  media {A['ma'][i]:.6g} · ATR {A['atr'][i]:.6g} · cierre {A['close'][i]:.6g}")
    for b in range(4):
        sg = int(A["sig"][b][i])
        print(f"  libro {BOOKS[b]:4s}: " + (f"{CODES[sg]} {'LONG' if sg < 4 else 'SHORT'} · riesgo {A['rk'][b][i]:.6g} · coste OK {bool(A['costok'][b][i])}" if sg >= 0 else "sin señal"))
    print("\nÚltimas 120 velas con señal del libro operado:")
    for j in range(max(0, len(df) - 120), len(df)):
        sg = int(A["sig"][cfg.book][j])
        if sg >= 0:
            print(f"  {datetime.fromtimestamp(int(A['time'][j]) / 1000, tz=timezone.utc):%m-%d %H:%M}  {CODES[sg]}  {'LONG' if sg < 4 else 'SHORT'}")


def cmd_chat_id(cfg) -> None:
    if not cfg.token:
        sys.exit("Falta TELEGRAM_TOKEN")
    r = requests.get(f"https://api.telegram.org/bot{cfg.token}/getUpdates", timeout=15).json()
    seen = {}
    for u in r.get("result", []):
        ch = (u.get("message") or u.get("channel_post") or {}).get("chat")
        if ch:
            seen[ch["id"]] = ch.get("username") or ch.get("title") or ch.get("first_name")
    if not seen:
        print("Sin mensajes. Escribe /start a tu bot en Telegram y repite este comando.")
    for cid, name in seen.items():
        print(f"chat_id={cid}  ({name})")


def _sigterm(*_a) -> None:
    raise KeyboardInterrupt


def main() -> None:
    global TRADER
    load_env()
    ap = argparse.ArgumentParser(description="Granville V5 → BingX → Telegram")
    ap.add_argument("--list", action="store_true", help="lista los contratos que se escanearían")
    ap.add_argument("--test-telegram", action="store_true")
    ap.add_argument("--get-chat-id", action="store_true")
    ap.add_argument("--debug", metavar="SIMBOLO")
    ap.add_argument("--warmup", metavar="LISTA", help="top:60 o BTC-USDT,ETH-USDT: calcula y GUARDA las estadísticas iniciales")
    ap.add_argument("--backtest", metavar="LISTA", help="igual que --warmup pero sin guardar")
    ap.add_argument("--days", type=float, default=45)
    ap.add_argument("--tf", default=None, help="timeframe(s) separados por coma")
    ap.add_argument("--once", action="store_true", help="un solo escaneo y salir")
    ap.add_argument("--dry-run", action="store_true", help="no envía a Telegram ni opera")
    a = ap.parse_args()
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    cfg = E.make_cfg()
    for tf in cfg.timeframes:
        if tf not in TF_MS:
            sys.exit(f"Timeframe desconocido: {tf}")
    if cfg.mode not in ("signals", "demo", "live"):
        sys.exit("BOT_MODE debe ser signals, demo o live")
    api = BingX(cfg.rps)
    notifier = Notifier(cfg, dry=a.dry_run)
    tfs = [x.strip() for x in a.tf.split(",")] if a.tf else cfg.timeframes
    if a.list:
        for c in select_universe(api, cfg, api.contracts(cfg.quotes)):
            print(c["symbol"])
        return
    if a.test_telegram:
        print("Enviado" if notifier.send("✅ Prueba: Telegram conectado con Granville V5.") else "Fallo: revisa TELEGRAM_TOKEN y TELEGRAM_CHAT_ID")
        return
    if a.get_chat_id:
        return cmd_chat_id(cfg)
    if a.debug:
        return cmd_debug(api, cfg, a.debug.upper(), tfs[0])
    if a.warmup or a.backtest:
        return cmd_history(api, cfg, a.warmup or a.backtest, a.days, tfs, save=bool(a.warmup))
    store = Store(cfg)
    if cfg.mode != "signals" and not a.dry_run:
        from trader import make_trader
        TRADER = make_trader(cfg, notifier)
    signal.signal(signal.SIGTERM, _sigterm)
    run_loop(api, cfg, store, notifier, a.once, a.dry_run)


if __name__ == "__main__":
    main()
