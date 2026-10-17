# -*- coding: utf-8 -*-
"""
Motor Granville V5 (port fiel de la estrategia de TradingView "Granville V5 — ESTRATEGIA").

Contiene SOLO lógica pura (sin red, sin Telegram, sin órdenes) para poder probarla:
  * indicadores (SMA/EMA/RMA como en Pine, RSI, MACD, VWAP diario, HaP EMA Strength)
  * las señales de los 4 libros:  0 Base · 1 V2 (filtros) · 2 V3 (+momentum/volumen) · 3 V4 (+HaP)
  * la simulación de operaciones virtuales (autoevaluación) y sus estadísticas (posterior, límite inferior)
Códigos: 0 G1 · 1 G2 · 2 G3 · 3 g4 (LONG) · 4 G5 · 5 G6 · 6 G7 · 7 g8 (SHORT)
"""
from __future__ import annotations

import json
import math
import os
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Optional

import numpy as np
import pandas as pd

TF_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000, "6h": 21_600_000,
    "8h": 28_800_000, "12h": 43_200_000, "1d": 86_400_000,
}
CODES = ["G1", "G2", "G3", "g4", "G5", "G6", "G7", "g8"]
BOOKS = ["Base", "V2", "V3", "V4"]
KIND_LABEL = {
    "G1": "Cruce alcista", "G2": "Falsa pérdida de la media (spring)", "G3": "Rebote en la media",
    "g4": "Sobreventa extrema", "G5": "Cruce bajista", "G6": "Falsa ruptura de la media",
    "G7": "Rechazo en la media", "g8": "Sobrecompra extrema",
}
BOOK_ALIASES = {"base": 0, "v2": 1, "v3": 2, "v4": 3}


def is_ext(code: int) -> bool:
    return code in (3, 7)


# ============================================================
# CONFIGURACIÓN (mismos nombres y valores por defecto que los inputs del Pine V5)
# ============================================================
def make_cfg() -> SimpleNamespace:
    e = os.getenv

    def s(name, d):
        return e(name, str(d)).split(" #")[0].strip()      # tolera comentarios al final de la línea

    def f(name, d):
        return float(s(name, d))

    def i(name, d):
        return int(float(s(name, d)))

    def b(name, d):
        return s(name, d).lower() in ("1", "true", "yes", "si", "sí", "on")

    def lst(name, d):
        return [x.strip() for x in s(name, d).split(",") if x.strip()]

    book = s("BOOK", "V2").lower()
    if book not in BOOK_ALIASES:
        raise SystemExit("BOOK debe ser Base, V2, V3 o V4")
    edge = s("EDGE_FILTER", "good").lower()
    if edge not in ("none", "good", "notbad"):
        raise SystemExit("EDGE_FILTER debe ser none, good o notbad")
    direction = s("DIRECTION", "both").lower()
    data_dir = s("DATA_DIR", ".")
    p = lambda name, fn: str(Path(data_dir) / s(name, fn))  # noqa: E731
    return SimpleNamespace(
        # ---- qué escanear
        timeframes=lst("TIMEFRAMES", "15m"), quotes=[q.upper() for q in lst("QUOTE_ASSETS", "USDT")],
        min_volume=f("MIN_VOLUME_24H_USDT", 2_000_000), include_re=s("INCLUDE_REGEX", ""), exclude_re=s("EXCLUDE_REGEX", ""),
        candles=i("CANDLES", 500), htf_candles=i("HTF_CANDLES", 400),
        # ---- señales base
        ma_length=i("MA_LENGTH", 50), ma_type=s("MA_TYPE", "SMA").upper(), atr_length=i("ATR_LENGTH", 14),
        strength_mult=f("STRENGTH_MULT", 1.2), dev_mult=f("DEV_MULT", 2.5),
        show_buys=direction in ("both", "long"), show_sells=direction in ("both", "short"),
        show_ext=b("SHOW_EXTREMES", 1),
        # ---- autoevaluación
        stop_mult=f("STOP_MULT", 1.5), tp1_r=f("TP1_R", 1.0), tp2_r=f("TP2_R", 2.0), tp1_frac=f("TP1_FRAC", 0.5),
        move_be=b("MOVE_BE", 1), max_bars=i("MAX_BARS", 18), cost_pct=f("COST_PCT", 0.12),
        prior_n=f("PRIOR_N", 20.0), prior_mean=f("PRIOR_MEAN", 0.0), min_n=i("MIN_N", 12),
        good_thr=f("GOOD_THR", 0.0), z_conf=f("Z_CONF", 0.5), half_life=f("HALF_LIFE", 40.0),
        # ---- stop estructural y G2/G6
        use_struct=b("USE_STRUCT", 1), struct_buf=f("STRUCT_BUF", 0.3), min_risk_atr=f("MIN_RISK_ATR", 0.7),
        max_risk_atr=f("MAX_RISK_ATR", 3.0), spring_bars=i("SPRING_BARS", 3),
        # ---- filtros V2
        use_htf=b("USE_HTF", 1), htf_tf=s("HTF_TF", "4h"), htf_flat_thr=f("HTF_FLAT_THR", 0.10),
        use_rvol=b("USE_RVOL", 1), rvol_len=i("RVOL_LEN", 20), rvol_min=f("RVOL_MIN", 1.2),
        use_atr_pct=b("USE_ATR_PCT", 0), atr_pct_len=i("ATR_PCT_LEN", 200), atr_pct_min=f("ATR_PCT_MIN", 20.0),
        use_er=b("USE_ER", 0), er_len=i("ER_LEN", 10), er_min=f("ER_MIN", 0.30),
        use_btc=b("USE_BTC", 0), btc_symbol=s("BTC_SYMBOL", "BTC-USDT"),
        use_vwap_ext=b("USE_VWAP_EXT", 1), use_active_hrs=b("USE_ACTIVE_HRS", 0),
        allow_asia=b("ALLOW_ASIA", 1), allow_eu=b("ALLOW_EU", 1), allow_us=b("ALLOW_US", 1),
        # ---- capa V3
        use_mom=b("USE_MOM", 1), use_rsi=b("USE_RSI", 1), rsi_len=i("RSI_LEN", 14), rsi_trend_min=f("RSI_TREND_MIN", 50.0),
        rsi_chase_max=f("RSI_CHASE_MAX", 75.0), rsi_os=f("RSI_OS", 30.0), rsi_look=i("RSI_LOOK", 3),
        use_macd=b("USE_MACD", 1), macd_fast=i("MACD_FAST", 12), macd_slow=i("MACD_SLOW", 26), macd_sig=i("MACD_SIG", 9),
        use_rvol2=b("USE_RVOL2", 1), rvol2_min=f("RVOL2_MIN", 1.4), rvol2_bars=i("RVOL2_BARS", 2),
        use_clv=b("USE_CLV", 1), clv_min=f("CLV_MIN", 0.65),
        use_htf2=b("USE_HTF2", 1), htf2_tf=s("HTF2_TF", "1h"),
        use_cost_gate=b("USE_COST_GATE", 1), max_cost_r=f("MAX_COST_R", 0.25),
        # ---- HaP
        hap_first=i("HAP_FIRST", 3), hap_step=i("HAP_STEP", 3), hap_smooth=i("HAP_SMOOTH", 2),
        hap_w_slope=f("HAP_W_SLOPE", 0.35), hap_w_period=f("HAP_W_PERIOD", 0.35), hap_w_align=f("HAP_W_ALIGN", 0.30),
        hap_warm=f("HAP_WARM", 1.5), hap_tf1=s("HAP_TF1", "1h"), hap_tf2=s("HAP_TF2", "4h"),
        # ---- capa V4
        use_hap=b("USE_HAP", 1), hap_min_score=f("HAP_MIN_SCORE", 55.0), hap_max_score=f("HAP_MAX_SCORE", 90.0),
        use_hap_mom=b("USE_HAP_MOM", 1), hap_mom_len=i("HAP_MOM_LEN", 5),
        use_hap_mtf1=b("USE_HAP_MTF1", 1), hap_mtf1_min=f("HAP_MTF1_MIN", 50.0),
        use_hap_mtf2=b("USE_HAP_MTF2", 1), hap_mtf2_min=f("HAP_MTF2_MIN", 50.0),
        hap_ext_floor=f("HAP_EXT_FLOOR", 40.0), hap_ignore_missing=s("HAP_IF_MISSING", "block").lower() == "ignore",
        # ---- estrategia
        book=BOOK_ALIASES[book], edge=edge, risk_pct=f("RISK_PCT", 1.0), max_lev=f("MAX_LEV", 10.0),
        partial_mode=b("PARTIAL_MODE", 1), tp_single_r=f("TP_SINGLE_R", 1.5),
        long_risk_mult=f("LONG_RISK_MULT", 1.0), short_risk_mult=f("SHORT_RISK_MULT", 1.0),
        account=f("ACCOUNT_USDT", 1000),
        # ---- ejecución del bot
        mode=s("BOT_MODE", "signals").lower(), live_confirm=s("LIVE_CONFIRM", ""),
        api_key=s("BINGX_API_KEY", ""), api_secret=s("BINGX_API_SECRET", ""),
        live_max_pos=i("LIVE_MAX_POSITIONS", 3), live_max_side=i("LIVE_MAX_PER_SIDE", 2),
        live_leverage=f("LIVE_LEVERAGE", 5), live_daily_loss_pct=f("LIVE_DAILY_LOSS_PCT", 2.0),
        live_codes=[x.upper() for x in lst("LIVE_CODES", "")], live_exclude_re=s("LIVE_EXCLUDE_REGEX", "^NCS"),
        live_max_slip_r=f("LIVE_MAX_SLIP_R", 0.25),
        # ---- ficheros y avisos
        token=s("TELEGRAM_TOKEN", ""), chat_id=s("TELEGRAM_CHAT_ID", ""), commands=b("TELEGRAM_COMMANDS", 1),
        notify_only_edge=b("NOTIFY_ONLY_EDGE", 0), notify_books=lst("NOTIFY_BOOKS", ""),
        summary_hour=i("SUMMARY_HOUR_UTC", 8), max_per_scan=i("MAX_SIGNALS_PER_SCAN", 10),
        scan_delay=f("SCAN_DELAY_SEC", 6), max_age_factor=f("MAX_SIGNAL_AGE_FACTOR", 1.5),
        rps=f("RATE_LIMIT_RPS", 8), workers=i("WORKERS", 8),
        data_dir=data_dir, state_file=p("STATE_FILE", "state.json"), sims_file=p("SIMS_FILE", "sims.json"),
        stats_file=p("STATS_FILE", "stats.json"), live_file=p("LIVE_FILE", "live_trades.json"),
    )


# ============================================================
# INDICADORES (equivalentes a Pine v6)
# ============================================================
def sma(a: np.ndarray, n: int) -> np.ndarray:
    return pd.Series(a).rolling(n).mean().to_numpy()


def ema(a: np.ndarray, n: int) -> np.ndarray:
    return pd.Series(a).ewm(span=n, adjust=False).mean().to_numpy()


def rma(a: np.ndarray, n: int) -> np.ndarray:
    """Media móvil de Wilder con semilla SMA, igual que ta.rma de Pine."""
    a = np.asarray(a, dtype=float)
    out = np.full(len(a), np.nan)
    sm = pd.Series(a).rolling(n).mean().to_numpy()
    valid = np.flatnonzero(~np.isnan(sm))
    if len(valid) == 0:
        return out
    i0 = int(valid[0])
    out[i0] = sm[i0]
    for i in range(i0 + 1, len(a)):
        out[i] = (a[i] + (n - 1) * out[i - 1]) / n if not np.isnan(a[i]) else np.nan
    return out


def true_range(h: np.ndarray, l: np.ndarray, c: np.ndarray) -> np.ndarray:
    pc = np.r_[np.nan, c[:-1]]
    return np.fmax(np.fmax(h - l, np.abs(h - pc)), np.abs(l - pc))


def rsi_pine(c: np.ndarray, n: int) -> np.ndarray:
    chg = np.r_[np.nan, np.diff(c)]
    up, dn = np.maximum(chg, 0.0), np.maximum(-chg, 0.0)
    ru, rd = rma(up, n), rma(dn, n)
    with np.errstate(all="ignore"):
        r = 100.0 - 100.0 / (1.0 + ru / rd)
    r = np.where(rd == 0, 100.0, np.where(ru == 0, 0.0, r))
    return np.where(np.isnan(ru) | np.isnan(rd), np.nan, r)


def shift(a: np.ndarray, k: int = 1) -> np.ndarray:
    out = np.full(len(a), np.nan)
    if k < len(a):
        out[k:] = a[:-k] if k > 0 else a
    return out


def barssince(cond: np.ndarray) -> np.ndarray:
    n = len(cond)
    idx = np.where(cond, np.arange(n), -1)
    last = np.maximum.accumulate(idx)
    return np.where(last >= 0, np.arange(n) - last, 999)


def percentrank(a: np.ndarray, length: int) -> np.ndarray:
    out = np.full(len(a), np.nan)
    for i in range(length, len(a)):
        prev = a[i - length:i]
        out[i] = np.sum(prev <= a[i]) / length * 100.0
    return out


def hap_score(src: np.ndarray, cfg) -> tuple[np.ndarray, np.ndarray]:
    """HaP EMA Strength reconstruido: 30 EMAs fijas. Devuelve (score suavizado 0-100 con calentamiento, alineación %)."""
    n = len(src)
    periods = [cfg.hap_first] + [cfg.hap_step * k for k in range(1, 30)]
    E = np.vstack([ema(src, p) for p in periods])                    # (30, n)
    prev = np.hstack([np.full((30, 1), np.nan), E[:, :-1]])
    with np.errstate(all="ignore"):
        U = (E > prev).astype(float)                                 # NaN -> False
        rise = U.sum(axis=0)
        w = np.sqrt(np.arange(1, 30, dtype=float))
        w_inner = (w[:, None] * U[1:]).sum(axis=0)
        w_rise = math.sqrt(cfg.hap_first) * U[0] + math.sqrt(cfg.hap_step) * w_inner
        pair_up = (E[:-1] > E[1:]).sum(axis=0).astype(float)
    w_tot = math.sqrt(cfg.hap_first) + math.sqrt(cfg.hap_step) * 106.605620   # constante del Pine
    slope_pct = rise / 30.0 * 100.0
    wtd_pct = w_rise / w_tot * 100.0
    align_pct = pair_up / 29.0 * 100.0
    w_sum = max(cfg.hap_w_slope + cfg.hap_w_period + cfg.hap_w_align, 0.0001)
    raw = (cfg.hap_w_slope * slope_pct + cfg.hap_w_period * wtd_pct + cfg.hap_w_align * align_pct) / w_sum
    sm = ema(raw, cfg.hap_smooth)
    longest = max(cfg.hap_first, cfg.hap_step * 29)
    warm_bars = int(math.floor(longest * cfg.hap_warm + 0.5))        # math.round de Pine (mitad hacia arriba)
    sm = np.where(np.arange(n) >= warm_bars, sm, np.nan)
    return sm, align_pct


def htf_frame(df: pd.DataFrame, cfg) -> pd.DataFrame:
    """Indicadores de un timeframe superior (velas YA cerradas): ma, atr, score HaP."""
    h, l, c = (df[k].to_numpy(dtype=float) for k in ("high", "low", "close"))
    ma = sma(c, cfg.ma_length) if cfg.ma_type == "SMA" else ema(c, cfg.ma_length)
    atr = rma(true_range(h, l, c), cfg.atr_length)
    hs, _ = hap_score(c, cfg)
    return pd.DataFrame({"time": df["time"].to_numpy(dtype=np.int64), "ma": ma, "atr": atr, "hap": hs})


def _lookup(times: np.ndarray, frame: Optional[pd.DataFrame], col: str, tf: str, back: int) -> np.ndarray:
    """Valor del TF superior en la vela H-back, siendo H la vela superior que contiene cada vela base
    (equivale a expr[back] con request.security(lookahead_on) del Pine: nunca mira el futuro)."""
    if frame is None or frame.empty:
        return np.full(len(times), np.nan)
    ms = TF_MS[tf]
    s = pd.Series(frame[col].to_numpy(dtype=float), index=frame["time"].to_numpy(dtype=np.int64))
    s = s[~s.index.duplicated()]
    keys = (times // ms - back) * ms
    return s.reindex(keys).to_numpy(dtype=float)


# ============================================================
# SEÑALES DE LOS 4 LIBROS
# ============================================================
def _pick(conds: list[tuple[np.ndarray, int]], n: int) -> np.ndarray:
    out = np.full(n, -1, dtype=int)
    for arr, code in reversed(conds):          # el primero de la lista tiene prioridad
        out = np.where(arr, code, out)
    return out


def compute_signals(df: pd.DataFrame, tf: str, ctx: dict, cfg) -> Optional[dict]:
    """
    df: velas CERRADAS del timeframe base (time = apertura en ms).
    ctx: {"frames": {tf: htf_frame}, "btc": pd.Series(slope) | None}
    Devuelve arrays por vela: sig[4][n] (código o -1), rk[4][n] (riesgo), costok[4][n], más contexto.
    """
    n = len(df)
    if n < cfg.ma_length + 30:
        return None
    t = df["time"].to_numpy(dtype=np.int64)
    o, h, l, c, v = (df[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close", "volume"))
    frames = ctx.get("frames", {})
    with np.errstate(all="ignore"):
        ma = sma(c, cfg.ma_length) if cfg.ma_type == "SMA" else ema(c, cfg.ma_length)
        atr = rma(true_range(h, l, c), cfg.atr_length)
        ma1 = shift(ma)
        ma_slope = ma - ma1
        body = np.abs(c - o)
        strong = body > sma(body, 20) * cfg.strength_mult
        dist = (c - ma) / atr
        bull, bear = c > o, c < o
        c1 = shift(c)

        # ---- TF mayor (pendiente de la media, vela cerrada anterior)
        fr_h = frames.get(cfg.htf_tf)
        htf_slope = np.nan_to_num(_lookup(t, fr_h, "ma", cfg.htf_tf, 1) - _lookup(t, fr_h, "ma", cfg.htf_tf, 2))
        htf_atr = _lookup(t, fr_h, "atr", cfg.htf_tf, 1)
        htf_up, htf_dn = htf_slope > 0, htf_slope < 0
        htf_flat = np.abs(htf_slope) < cfg.htf_flat_thr * np.where(np.isnan(htf_atr), atr, htf_atr)
        # ---- TF intermedio
        fr_2 = frames.get(cfg.htf2_tf)
        h2_slope = np.nan_to_num(_lookup(t, fr_2, "ma", cfg.htf2_tf, 1) - _lookup(t, fr_2, "ma", cfg.htf2_tf, 2))
        h2_up, h2_dn = h2_slope > 0, h2_slope < 0

        # ---- HaP
        hs, hap_align = hap_score(c, cfg)
        m1 = _lookup(t, frames.get(cfg.hap_tf1), "hap", cfg.hap_tf1, 1)
        m2 = _lookup(t, frames.get(cfg.hap_tf2), "hap", cfg.hap_tf2, 1)
        sh = shift(hs, cfg.hap_mom_len)
        hap_mom = hs - np.where(np.isnan(sh), hs, sh)
        ign = cfg.hap_ignore_missing
        na_s = np.isnan(hs)
        sc_l = np.where(na_s, ign, (hs >= cfg.hap_min_score) & (hs <= cfg.hap_max_score))
        sc_s = np.where(na_s, ign, (hs <= 100.0 - cfg.hap_min_score) & (hs >= 100.0 - cfg.hap_max_score))
        mom_l = (not cfg.use_hap_mom) | np.where(np.isnan(hap_mom), ign, hap_mom >= 0)
        mom_s = (not cfg.use_hap_mom) | np.where(np.isnan(hap_mom), ign, hap_mom <= 0)
        m1_l = (not cfg.use_hap_mtf1) | np.where(np.isnan(m1), ign, m1 >= cfg.hap_mtf1_min)
        m1_s = (not cfg.use_hap_mtf1) | np.where(np.isnan(m1), ign, m1 <= 100.0 - cfg.hap_mtf1_min)
        m2_l = (not cfg.use_hap_mtf2) | np.where(np.isnan(m2), ign, m2 >= cfg.hap_mtf2_min)
        m2_s = (not cfg.use_hap_mtf2) | np.where(np.isnan(m2), ign, m2 <= 100.0 - cfg.hap_mtf2_min)
        hap_trend_l = (not cfg.use_hap) | (sc_l & mom_l & m1_l & m2_l)
        hap_trend_s = (not cfg.use_hap) | (sc_s & mom_s & m1_s & m2_s)
        hs1 = shift(hs)
        turn_up = np.where(na_s, ign, hs > np.where(np.isnan(hs1), hs, hs1))
        turn_dn = np.where(na_s, ign, hs < np.where(np.isnan(hs1), hs, hs1))
        floor_l = (not cfg.use_hap_mtf2) | np.where(np.isnan(m2), ign, m2 >= cfg.hap_ext_floor)
        floor_s = (not cfg.use_hap_mtf2) | np.where(np.isnan(m2), ign, m2 <= 100.0 - cfg.hap_ext_floor)
        hap_ext_l = (not cfg.use_hap) | (turn_up & floor_l)
        hap_ext_s = (not cfg.use_hap) | (turn_dn & floor_s)

        # ---- volumen, ATR percentil, ER
        rvol = v / sma(v, cfg.rvol_len)
        atr_pct = percentrank(atr / c, cfg.atr_pct_len) if cfg.use_atr_pct else np.full(n, np.nan)
        er_den = pd.Series(np.abs(c - c1)).rolling(cfg.er_len).sum().to_numpy()
        er = np.where(er_den > 0, np.abs(c - shift(c, cfg.er_len)) / er_den, 0.0)

        # ---- BTC (hipótesis, apagado por defecto)
        btc = ctx.get("btc")
        btc_s = np.nan_to_num(btc.reindex(t).to_numpy(dtype=float)) if (cfg.use_btc and btc is not None) else np.zeros(n)
        btc_l = (not cfg.use_btc) | (btc_s >= 0)
        btc_sh = (not cfg.use_btc) | (btc_s <= 0)

        # ---- VWAP diario (UTC), sesión
        day = t // 86_400_000
        tp = (h + l + c) / 3.0
        vwap = (pd.Series(tp * v).groupby(day).cumsum() / pd.Series(v).groupby(day).cumsum()).to_numpy()
        hr = (t // 3_600_000) % 24
        sess = np.where((hr >= 7) & (hr < 13), 1, np.where((hr >= 13) & (hr < 21), 2, 0))
        sess_ok = np.where(sess == 0, cfg.allow_asia, np.where(sess == 1, cfg.allow_eu, cfg.allow_us))
        active_hr = ((hr >= 10) & (hr <= 14)) | (hr == 21)

        # ---- momentum (V3)
        rsi = rsi_pine(c, cfg.rsi_len)
        ema_f, ema_s = ema(c, cfg.macd_fast), ema(c, cfg.macd_slow)
        macd_line = ema_f - ema_s
        macd_h = macd_line - ema(macd_line, cfg.macd_sig)
        rsi1, mh1 = shift(rsi), shift(macd_h)
        rsi_rise = rsi > np.where(np.isnan(rsi1), rsi, rsi1)
        rsi_fall = rsi < np.where(np.isnan(rsi1), rsi, rsi1)
        hist_up = macd_h > np.where(np.isnan(mh1), macd_h, mh1)
        hist_dn = macd_h < np.where(np.isnan(mh1), macd_h, mh1)
        mom_trend_l = ((not cfg.use_rsi) | ((rsi >= cfg.rsi_trend_min) & (rsi <= cfg.rsi_chase_max) & rsi_rise)) & ((not cfg.use_macd) | hist_up)
        mom_trend_s = ((not cfg.use_rsi) | ((rsi <= 100 - cfg.rsi_trend_min) & (rsi >= 100 - cfg.rsi_chase_max) & rsi_fall)) & ((not cfg.use_macd) | hist_dn)
        lo_rsi = pd.Series(rsi).rolling(cfg.rsi_look).min().to_numpy()
        hi_rsi = pd.Series(rsi).rolling(cfg.rsi_look).max().to_numpy()
        mom_ext_l = ((not cfg.use_rsi) | ((lo_rsi <= cfg.rsi_os) & rsi_rise)) & ((not cfg.use_macd) | hist_up)
        mom_ext_s = ((not cfg.use_rsi) | ((hi_rsi >= 100 - cfg.rsi_os) & rsi_fall)) & ((not cfg.use_macd) | hist_dn)
        rvol_hi = pd.Series(rvol).rolling(cfg.rvol2_bars).max().to_numpy()
        clv = np.where(h > l, (c - l) / (h - l), 0.5)
        rvol_trig = (not cfg.use_rvol2) | (rvol_hi >= cfg.rvol2_min)
        vol_l = rvol_trig & ((not cfg.use_clv) | (clv >= cfg.clv_min))
        vol_s = rvol_trig & ((not cfg.use_clv) | (clv <= 1.0 - cfg.clv_min))
        l2_trend_l = (not cfg.use_mom) | (mom_trend_l & vol_l & ((not cfg.use_htf2) | h2_up))
        l2_trend_s = (not cfg.use_mom) | (mom_trend_s & vol_s & ((not cfg.use_htf2) | h2_dn))
        l2_ext_l = (not cfg.use_mom) | (mom_ext_l & vol_l)
        l2_ext_s = (not cfg.use_mom) | (mom_ext_s & vol_s)

        # ---- señales base (definición original)
        cross_up = (c > ma) & (c1 <= ma1)
        cross_dn = (c < ma) & (c1 >= ma1)
        buy_cross_b = cross_up & (ma_slope > 0) & strong & bull
        buy_bounce = (l <= ma * 1.002) & (c > ma) & bull & (ma_slope > 0) & strong & ~buy_cross_b
        buy_ext = (dist < -cfg.dev_mult) & bull & strong & (ma_slope >= 0)
        sell_cross_b = cross_dn & (ma_slope < 0) & strong & bear
        sell_bounce = (h >= ma * 0.998) & (c < ma) & bear & (ma_slope < 0) & strong & ~sell_cross_b
        sell_ext = (dist > cfg.dev_mult) & bear & strong & (ma_slope <= 0)
        bars_under = barssince(cross_dn)
        bars_over = barssince(cross_up)
        buy_spring = buy_cross_b & (bars_under <= cfg.spring_bars)
        sell_spring = sell_cross_b & (bars_over <= cfg.spring_bars)
        buy_cross_n = buy_cross_b & ~buy_spring
        sell_cross_n = sell_cross_b & ~sell_spring

        # ---- V2
        vol_ok = (not cfg.use_rvol) | (rvol >= cfg.rvol_min)
        atr_ok = (not cfg.use_atr_pct) | (atr_pct >= cfg.atr_pct_min)
        common_l = vol_ok & atr_ok & sess_ok & btc_l
        common_s = vol_ok & atr_ok & sess_ok & btc_sh
        er_ok = (not cfg.use_er) | (er >= cfg.er_min)
        ext_htf_ok = (not cfg.use_htf) | htf_flat
        ext_hrs_ok = (not cfg.use_active_hrs) | active_hr
        htf_l, htf_s = (not cfg.use_htf) | htf_up, (not cfg.use_htf) | htf_dn
        f_buy_spring = buy_spring & common_l & htf_l
        f_buy_cross = buy_cross_n & common_l & htf_l & er_ok
        f_buy_bounce = buy_bounce & common_l & htf_l
        f_buy_ext = buy_ext & common_l & ext_htf_ok & ext_hrs_ok & ((not cfg.use_vwap_ext) | (c < vwap))
        f_sell_spring = sell_spring & common_s & htf_s
        f_sell_cross = sell_cross_n & common_s & htf_s & er_ok
        f_sell_bounce = sell_bounce & common_s & htf_s
        f_sell_ext = sell_ext & common_s & ext_htf_ok & ext_hrs_ok & ((not cfg.use_vwap_ext) | (c > vwap))
        # ---- V3
        m_buy = (f_buy_spring & l2_trend_l, f_buy_cross & l2_trend_l, f_buy_bounce & l2_trend_l, f_buy_ext & l2_ext_l)
        m_sell = (f_sell_spring & l2_trend_s, f_sell_cross & l2_trend_s, f_sell_bounce & l2_trend_s, f_sell_ext & l2_ext_s)
        # ---- V4
        h_buy = (m_buy[0] & hap_trend_l, m_buy[1] & hap_trend_l, m_buy[2] & hap_trend_l, m_buy[3] & hap_ext_l)
        h_sell = (m_sell[0] & hap_trend_s, m_sell[1] & hap_trend_s, m_sell[2] & hap_trend_s, m_sell[3] & hap_ext_s)

        sb, ss, se = cfg.show_buys, cfg.show_sells, cfg.show_ext

        def book(bs, sl):  # prioridad: spring, cruce, rebote, extremo (LONG) y luego (SHORT)
            return _pick([
                (sb & bs[0], 1), (sb & bs[1], 0), (sb & bs[2], 2), (sb & bs[3] & se, 3),
                (ss & sl[0], 5), (ss & sl[1], 4), (ss & sl[2], 6), (ss & sl[3] & se, 7)], n)

        f_buy = (f_buy_spring, f_buy_cross, f_buy_bounce, f_buy_ext)
        f_sell = (f_sell_spring, f_sell_cross, f_sell_bounce, f_sell_ext)
        sig_b = _pick([(sb & buy_cross_b, 0), (sb & buy_bounce, 2), (sb & buy_ext & se, 3),
                       (ss & sell_cross_b, 4), (ss & sell_bounce, 6), (ss & sell_ext & se, 7)], n)
        sig = np.vstack([sig_b, book(f_buy, f_sell), book(m_buy, m_sell), book(h_buy, h_sell)])

        # ---- riesgo por libro y filtro de coste (solo V3 y V4)
        rk_fix = cfg.stop_mult * atr
        rk_long = c - (np.fmin(ma, l) - cfg.struct_buf * atr)
        rk_short = (np.fmax(ma, h) + cfg.struct_buf * atr) - c
        rk = np.zeros((4, n))
        cost_ok = np.ones((4, n), dtype=bool)
        for b in range(4):
            sg = sig[b]
            is_long = (sg >= 0) & (sg < 4)
            ext = (sg == 3) | (sg == 7)
            rk_s = np.clip(np.where(is_long, rk_long, rk_short), cfg.min_risk_atr * atr, cfg.max_risk_atr * atr)
            rk[b] = np.where((b >= 1) and cfg.use_struct, np.where(ext, rk_fix, rk_s), rk_fix)
            if b >= 2 and cfg.use_cost_gate:
                cost_r = cfg.cost_pct / (rk[b] / c * 100.0)
                cost_ok[b] = cost_r <= cfg.max_cost_r

    i = n - 1
    data_ok = bool(not np.isnan(_lookup(t[-1:], fr_h, "ma", cfg.htf_tf, 1)[0])
                   and not np.isnan(_lookup(t[-1:], fr_h, "ma", cfg.htf_tf, 2)[0])
                   and not np.isnan(_lookup(t[-1:], fr_2, "ma", cfg.htf2_tf, 1)[0])
                   and not np.isnan(_lookup(t[-1:], fr_2, "ma", cfg.htf2_tf, 2)[0]))
    return {
        "time": t, "open": o, "high": h, "low": l, "close": c, "ma": ma, "atr": atr, "sess": sess,
        "sig": sig, "rk": rk, "costok": cost_ok, "data_ok": data_ok,
        "diag": {"hap": float(hs[i]) if not np.isnan(hs[i]) else None, "hap_m1": None if np.isnan(m1[i]) else float(m1[i]),
                 "hap_m2": None if np.isnan(m2[i]) else float(m2[i]), "rvol": float(rvol[i]) if not np.isnan(rvol[i]) else None,
                 "rsi": float(rsi[i]) if not np.isnan(rsi[i]) else None, "htf_slope": float(htf_slope[i]),
                 "h2_slope": float(h2_slope[i]), "vwap": float(vwap[i])},
    }


# ============================================================
# OPERACIONES VIRTUALES (autoevaluación): mismas reglas que el Pine
# ============================================================
def vstep(tr: dict, high: float, low: float, close: float, cfg) -> Optional[float]:
    """Avanza una operación virtual con una vela cerrada. Devuelve R neto de costes si se cierra, o None."""
    side, e, rk, stp, half = tr["side"], tr["entry"], tr["risk"], tr["stop"], tr["half"]
    bars = tr["bars"] + 1
    f = cfg.tp1_frac
    tp1 = e + side * rk * cfg.tp1_r
    tp2 = e + side * rk * cfg.tp2_r
    hit_stop = low <= stp if side == 1 else high >= stp
    hit_tp1 = high >= tp1 if side == 1 else low <= tp1
    hit_tp2 = high >= tp2 if side == 1 else low <= tp2
    rest_stop = 0.0 if cfg.move_be else -1.0
    res = None
    if half == 0:
        if hit_stop:
            res = -1.0                                   # si una vela toca stop y objetivo, cuenta el stop
        elif hit_tp2:
            res = f * cfg.tp1_r + (1.0 - f) * cfg.tp2_r
        elif hit_tp1:
            half = 1
            stp = e if cfg.move_be else stp
    else:
        if hit_stop:
            res = f * cfg.tp1_r + (1.0 - f) * rest_stop
        elif hit_tp2:
            res = f * cfg.tp1_r + (1.0 - f) * cfg.tp2_r
    if res is None and bars >= cfg.max_bars:
        mtm = side * (close - e) / rk
        res = f * cfg.tp1_r + (1.0 - f) * mtm if half == 1 else mtm
    tr.update(half=half, stop=stp, bars=bars)
    if res is None:
        return None
    return res - (cfg.cost_pct / 100.0) * e / rk


def vstep_single(tr: dict, high: float, low: float, close: float, cfg) -> Optional[float]:
    """Salida simple (PARTIAL_MODE=0): un solo TP en TP_SINGLE_R y stop fijo."""
    side, e, rk, stp = tr["side"], tr["entry"], tr["risk"], tr["stop"]
    tr["bars"] += 1
    tp = e + side * rk * cfg.tp_single_r
    if (low <= stp) if side == 1 else (high >= stp):
        res = -1.0
    elif (high >= tp) if side == 1 else (low <= tp):
        res = cfg.tp_single_r
    elif tr["bars"] >= cfg.max_bars:
        res = side * (close - e) / rk
    else:
        return None
    return res - (cfg.cost_pct / 100.0) * e / rk


def process_bar(state: dict, A: dict, i: int, tf: str, symbol: str, cfg, spawn: bool = True,
                on_close: Optional[Callable[[list], None]] = None) -> tuple[list, list]:
    """
    Procesa la vela cerrada i en el MISMO orden que el Pine:
      1) resolver las operaciones virtuales abiertas  2) (callback: actualizar estadísticas)
      3) señales nuevas de los 4 libros; se abre operación virtual si no hay otra de ese libro y dirección.
    Devuelve (operaciones cerradas, señales de esta vela).
    """
    t_open = int(A["time"][i])
    hi, lo, cl = float(A["high"][i]), float(A["low"][i]), float(A["close"][i])
    closed, keep = [], []
    for tr in state["open"]:
        if tr["last_ms"] >= t_open:
            keep.append(tr)
            continue
        res = vstep(tr, hi, lo, cl, cfg)
        tr["last_ms"] = t_open
        if res is None:
            keep.append(tr)
        else:
            tr.update(R=res, exit_ms=t_open + TF_MS[tf])
            closed.append(tr)
    state["open"] = keep
    if closed and on_close:
        on_close(closed)
    sigs: list = []
    if not spawn:
        return closed, sigs
    cnt: dict = {}
    for tr in state["open"]:
        cnt[(tr["book"], tr["side"])] = cnt.get((tr["book"], tr["side"]), 0) + 1
    for b in range(4):
        sg = int(A["sig"][b][i])
        rk = float(A["rk"][b][i])
        if sg < 0 or not bool(A["costok"][b][i]) or not math.isfinite(rk) or rk <= 0:
            continue
        side = 1 if sg < 4 else -1
        busy = cnt.get((b, side), 0) > 0
        sigs.append({"book": b, "code": sg, "side": side, "rk": rk, "price": cl, "busy": busy, "time": t_open,
                     "sess": int(A["sess"][i]), "symbol": symbol, "tf": tf, "idx": i})
        if not busy:
            state["open"].append({"id": f"{symbol}|{tf}|{b}|{side}|{t_open}", "symbol": symbol, "tf": tf, "book": b,
                                  "code": sg, "side": side, "sess": int(A["sess"][i]), "entry": cl, "risk": rk,
                                  "stop": cl - side * rk, "half": 0, "bars": 0, "last_ms": t_open, "sig_ms": t_open})
    return closed, sigs


def advance(state: dict, A: dict, tf: str, symbol: str, cfg, stats: "Stats", fresh: bool) -> tuple[list, list]:
    """Modo en vivo: procesa las velas nuevas. Solo la ÚLTIMA genera señales (y solo si es reciente)."""
    t = A["time"]
    n = len(t)
    last = state.get("last_ms")
    start = n - 1 if last is None else int(np.searchsorted(t, last, side="right"))
    closed_all: list = []
    sigs: list = []

    def on_close(trs):
        for tr in trs:
            stats.update(tf, tr["book"], tr["code"], tr["sess"], tr["R"])

    for i in range(start, n):
        is_last = i == n - 1
        c, s = process_bar(state, A, i, tf, symbol, cfg, spawn=(is_last and fresh and A["data_ok"]), on_close=on_close)
        closed_all += c
        if is_last:
            sigs = s
        state["last_ms"] = int(t[i])
    return closed_all, sigs


# ============================================================
# ESTADÍSTICAS (posterior, límite inferior, por sesión) — idénticas a f_stats del Pine
# ============================================================
class Stats:
    KEYS = ("n", "win", "sumr", "wn", "ws", "wq")

    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.RLock()
        self.tf: dict = {}
        self.decay = 0.5 ** (1.0 / cfg.half_life) if cfg.half_life > 0 else 1.0

    def _get(self, tf: str) -> dict:
        d = self.tf.get(tf)
        if d is None:
            d = {k: np.zeros((4, 8)) for k in self.KEYS}
            d["ssn"], d["sss"] = np.zeros((4, 8, 3)), np.zeros((4, 8, 3))
            self.tf[tf] = d
        return d

    def update(self, tf: str, book: int, code: int, sess: int, R: float) -> None:
        with self.lock:
            d = self._get(tf)
            d["n"][book, code] += 1
            d["win"][book, code] += 1 if R > 0 else 0
            d["sumr"][book, code] += R
            d["wn"][book, code] = d["wn"][book, code] * self.decay + 1.0
            d["ws"][book, code] = d["ws"][book, code] * self.decay + R
            d["wq"][book, code] = d["wq"][book, code] * self.decay + R * R
            d["ssn"][book, code, sess] += 1
            d["sss"][book, code, sess] += R

    def get(self, tf: str, book: int, code: int) -> dict:
        c = self.cfg
        with self.lock:
            d = self._get(tf)
            n, wn, ws, wq = (float(d[k][book, code]) for k in ("n", "wn", "ws", "wq"))
            win = float(d["win"][book, code])
        mean = ws / wn if wn > 0 else 0.0
        post = (ws + c.prior_n * c.prior_mean) / (wn + c.prior_n)
        vr = max(wq / wn - mean * mean, 0.0) if wn > 0 else 0.0
        se = math.sqrt(vr / (wn + c.prior_n))
        lo = post - c.z_conf * se
        known = n >= c.min_n
        return {"n": n, "mean": mean, "post": post, "wr": win / n * 100.0 if n > 0 else 0.0, "lo": lo,
                "known": known, "good": known and lo > c.good_thr, "bad": known and post < c.good_thr}

    def total(self, tf: str, book: int) -> dict:
        with self.lock:
            d = self._get(tf)
            n, win, r = float(d["n"][book].sum()), float(d["win"][book].sum()), float(d["sumr"][book].sum())
        return {"n": n, "wr": win / n * 100.0 if n > 0 else 0.0, "avg": r / n if n > 0 else 0.0, "sum": r}

    def session(self, tf: str, book: int, code: int, sess: int) -> tuple[float, float]:
        c = self.cfg
        with self.lock:
            d = self._get(tf)
            n, s = float(d["ssn"][book, code, sess]), float(d["sss"][book, code, sess])
        return n, (s + c.prior_n * c.prior_mean) / (n + c.prior_n)

    def edge_ok(self, tf: str, book: int, code: int) -> bool:
        if self.cfg.edge == "none":
            return True
        st = self.get(tf, book, code)
        return st["good"] if self.cfg.edge == "good" else not st["bad"]

    def save(self, path: str) -> None:
        with self.lock:
            out = {tf: {k: v.tolist() for k, v in d.items()} for tf, d in self.tf.items()}
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(out), encoding="utf-8")
        os.replace(tmp, p)

    def load(self, path: str) -> None:
        p = Path(path)
        if not p.exists():
            return
        raw = json.loads(p.read_text(encoding="utf-8"))
        with self.lock:
            for tf, d in raw.items():
                self.tf[tf] = {k: np.array(v, dtype=float) for k, v in d.items()}


# ============================================================
# RESUMEN ESTADÍSTICO DE UNA LISTA DE R (para backtest e informes)
# ============================================================
def summarize(rs_list: list[float]) -> Optional[dict]:
    if not rs_list:
        return None
    rs = np.array(rs_list, dtype=float)
    n = len(rs)
    win = rs > 0
    gp, gl = float(rs[win].sum()), float(-rs[~win].sum())
    sd = float(rs.std(ddof=1)) if n > 1 else 0.0
    eq = np.r_[0.0, np.cumsum(rs)]
    return {"n": n, "wr": float(win.mean()), "avg": float(rs.mean()), "sum": float(rs.sum()),
            "pf": gp / gl if gl > 0 else float("inf"),
            "t": float(rs.mean() / (sd / np.sqrt(n))) if sd > 1e-9 else 0.0,
            "maxdd": float((np.maximum.accumulate(eq) - eq).max())}


def verdict(sm: Optional[dict]) -> str:
    if not sm:
        return "sin datos"
    if sm["n"] < 30:
        return "muestra insuficiente (<30 ops)"
    if sm["t"] >= 2.0:
        return "ventaja probable"
    if sm["t"] <= -2.0:
        return "ventaja NEGATIVA"
    return "sin evidencia de ventaja"
