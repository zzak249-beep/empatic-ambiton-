# -*- coding: utf-8 -*-
"""
Ejecución de órdenes en BingX Perpetual Futures (demo VST o dinero real) para la estrategia Granville V5.

Salidas idénticas a la estrategia de TradingView:
  SL (stop) · TP1 parcial (TP1_FRAC) · tras TP1 el stop pasa a la entrada (MOVE_BE) · TP2 · salida por tiempo (MAX_BARS).
  Con PARTIAL_MODE=0: un único TP en TP_SINGLE_R.

Seguridad:
  * El stop y los objetivos viven en el EXCHANGE (STOP_MARKET / TAKE_PROFIT_MARKET): si el bot se cae, siguen activos.
  * Si no se puede colocar el stop, la posición se cierra al instante.
  * Si el stop desaparece, se repone; si no se puede, se cierra.
  * Límites: posiciones máximas, máximas por lado, pérdida diaria, precio ya movido, símbolos excluidos, lista de códigos.
  * /pausa, /reanudar, /cerrartodo desde Telegram.
"""
from __future__ import annotations

import hashlib
import hmac
import html
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Optional

import requests

from engine import CODES, TF_MS

log = logging.getLogger("granville.trader")

LIVE_URL = "https://open-api.bingx.com"
DEMO_URL = "https://open-api-vst.bingx.com"
CONFIRM_PHRASE = "SI_OPERAR_CON_DINERO_REAL"


class BingXError(Exception):
    def __init__(self, code, msg):
        super().__init__(f"BingX code={code} msg={msg}")
        self.code, self.msg = code, msg


def _floor(x: float, decimals: int) -> float:
    return float(Decimal(str(x)).quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_DOWN))


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


class BingXAuth:
    """Cliente firmado (HMAC-SHA256). Las ÓRDENES no se reintentan a ciegas (podrían duplicarse)."""

    def __init__(self, key: str, secret: str, base: str, timeout: float = 15.0):
        self.key, self.secret, self.base, self.timeout = key, secret, base.rstrip("/"), timeout
        self.s = requests.Session()
        self.lock = threading.Lock()
        self.last = 0.0

    def _throttle(self) -> None:
        with self.lock:
            wait = self.last + 0.12 - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self.last = time.monotonic()

    def call(self, method: str, path: str, params: Optional[dict] = None, signed: bool = True, retries: int = 1):
        last: Optional[Exception] = None
        for attempt in range(retries):
            self._throttle()
            p = {k: v for k, v in (params or {}).items() if v is not None}
            headers: dict = {}
            if signed:
                p["timestamp"] = int(time.time() * 1000)
                p["recvWindow"] = 10000
            qs = "&".join(f"{k}={p[k]}" for k in sorted(p))
            url = f"{self.base}{path}?{qs}"
            if signed:
                url += f"&signature={hmac.new(self.secret.encode(), qs.encode(), hashlib.sha256).hexdigest()}"
                headers["X-BX-APIKEY"] = self.key
            try:
                r = self.s.request(method, url, headers=headers, timeout=self.timeout)
                if r.status_code == 429:
                    time.sleep(2 + 2 * attempt)
                    last = RuntimeError("HTTP 429")
                    continue
                j = r.json()
            except (requests.RequestException, ValueError) as ex:
                last = ex
                time.sleep(0.5 * (attempt + 1))
                continue
            if j.get("code", 0) != 0:
                raise BingXError(j.get("code"), j.get("msg"))
            return j.get("data")
        raise RuntimeError(f"{path}: {last}")


class LiveTrader:
    def __init__(self, cfg, notifier):
        self.cfg, self.notifier = cfg, notifier
        self.mode = cfg.mode                                   # demo | live
        self.api = BingXAuth(cfg.api_key, cfg.api_secret, DEMO_URL if self.mode == "demo" else LIVE_URL)
        self.lock = threading.RLock()
        self.path = Path(cfg.live_file)
        self.trades: dict[str, dict] = {}                      # abiertas, por símbolo
        self.closed: list[dict] = []
        self.contracts: dict[str, dict] = {}
        self.paused = False
        self.hedge = True
        self.day: Optional[str] = None
        self.day_equity = 0.0
        self.halted_day: Optional[str] = None
        self.exclude = re.compile(cfg.live_exclude_re, re.I) if cfg.live_exclude_re else None
        self.codes = {c.upper() for c in cfg.live_codes}
        self._load()

    # ---------- persistencia ----------
    def _load(self) -> None:
        if self.path.exists():
            try:
                d = json.loads(self.path.read_text(encoding="utf-8"))
                self.trades = {t["symbol"]: t for t in d.get("open", [])}
                self.closed = d.get("closed", [])
            except Exception as ex:  # noqa: BLE001
                log.warning("No se pudo leer %s (%s)", self.path, ex)

    def _save(self) -> None:
        self.closed = self.closed[-2000:]
        _atomic_write(self.path, json.dumps({"open": list(self.trades.values()), "closed": self.closed}))

    def _notify(self, text: str) -> None:
        try:
            self.notifier.send(text)
        except Exception as ex:  # noqa: BLE001
            log.error("No se pudo notificar: %s", ex)

    # ---------- lecturas ----------
    def set_contracts(self, contracts: list[dict]) -> None:
        self.contracts = {c["symbol"]: c for c in contracts}

    def equity(self) -> float:
        d = self.api.call("GET", "/openApi/swap/v2/user/balance", retries=3)
        bal = d.get("balance", d) if isinstance(d, dict) else d
        if isinstance(bal, list):
            bal = next((b for b in bal if str(b.get("asset", "USDT")).upper() == "USDT"), bal[0])
        return float(bal.get("equity") or bal.get("balance") or 0.0)

    def last_price(self, sym: str) -> float:
        d = self.api.call("GET", "/openApi/swap/v2/quote/price", {"symbol": sym}, signed=False, retries=3)
        if isinstance(d, list):
            d = d[0]
        return float(d["price"])

    def _pos(self, sym: str, side: str) -> tuple[float, float]:
        d = self.api.call("GET", "/openApi/swap/v2/user/positions", {"symbol": sym}, retries=3) or []
        if isinstance(d, dict):
            d = [d]
        for p in d:
            amt = float(p.get("positionAmt", 0) or 0)
            ps = str(p.get("positionSide", "")).upper()
            if ps == side or (ps == "BOTH" and amt != 0 and ((amt > 0) == (side == "LONG"))):
                return abs(amt), float(p.get("avgPrice", 0) or 0)
        return 0.0, 0.0

    def _orders(self, sym: str) -> list[dict]:
        d = self.api.call("GET", "/openApi/swap/v2/trade/openOrders", {"symbol": sym}, retries=3) or {}
        return d.get("orders", []) if isinstance(d, dict) else d

    # ---------- escritura ----------
    def _place(self, sym: str, side: str, kind: str, qty: float, stop: Optional[float] = None,
               prec: int = 6, exit_order: bool = False) -> str:
        order_side = ("BUY" if side == "LONG" else "SELL") if not exit_order else ("SELL" if side == "LONG" else "BUY")
        params = {"symbol": sym, "side": order_side, "positionSide": side if self.hedge else "BOTH",
                  "type": kind, "quantity": qty,
                  "stopPrice": f"{stop:.{prec}f}" if stop is not None else None,
                  "workingType": "CONTRACT_PRICE" if stop is not None else None}
        if exit_order and not self.hedge:
            params["reduceOnly"] = "true"
        d = self.api.call("POST", "/openApi/swap/v2/trade/order", params, retries=1) or {}
        return str(d.get("order", d).get("orderId"))

    def _cancel(self, sym: str, oid) -> None:
        if not oid:
            return
        try:
            self.api.call("DELETE", "/openApi/swap/v2/trade/order", {"symbol": sym, "orderId": oid}, retries=2)
        except Exception as ex:  # noqa: BLE001
            log.debug("cancel %s %s: %s", sym, oid, ex)

    # ---------- arranque ----------
    def startup(self, contracts: list[dict]) -> None:
        self.set_contracts(contracts)
        try:
            d = self.api.call("GET", "/openApi/swap/v1/positionSide/dual", retries=3)
            self.hedge = str((d or {}).get("dualSidePosition", "true")).lower() == "true"
        except Exception as ex:  # noqa: BLE001
            log.warning("No se pudo leer el modo de posición (%s): asumo hedge", ex)
        eq = self.equity()                    # si las claves son incorrectas, falla aquí y no se opera
        with self.lock:
            for lt in list(self.trades.values()):
                self._check(lt)
            self._save()
        c = self.cfg
        self._notify(
            f"🤖 <b>Trading {'REAL' if self.mode == 'live' else 'DEMO'} activado</b>\n"
            f"Equity: {eq:.2f} USDT · riesgo {c.risk_pct:g}%/op · máx {c.live_max_pos} posiciones ({c.live_max_side} por lado) · "
            f"apalancamiento tope {min(c.max_lev, c.live_leverage):g}x\n"
            f"Códigos: {', '.join(sorted(self.codes)) or 'todos los de la estrategia'} · filtro de ventaja: <b>{c.edge}</b>\n"
            f"Modo posición: {'hedge' if self.hedge else 'one-way'} · pérdida diaria máx {c.live_daily_loss_pct:g}%\n"
            f"Comandos: /live · /pausa · /reanudar · /cerrartodo")
        if c.edge == "none":
            self._notify("⚠️ EDGE_FILTER=none: operará TODAS las señales sin exigir ventaja histórica demostrada.")

    # ---------- filtros de seguridad ----------
    def _daily(self) -> tuple[float, bool]:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self.day != today:
            self.day, self.day_equity = today, self.equity()
        start = datetime.strptime(today, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000
        pnl = sum(t.get("pnl", 0.0) for t in self.closed if t.get("exit_ms", 0) >= start)
        return pnl, bool(self.day_equity > 0 and pnl <= -self.day_equity * self.cfg.live_daily_loss_pct / 100)

    def _reject(self, cand: dict, sl: float, tp1: float) -> Optional[str]:
        c = self.cfg
        sym, side, name = cand["symbol"], cand["side"], CODES[cand["code"]]
        if self.paused:
            return "bot en pausa (/reanudar)"
        if self.codes and name not in self.codes:
            return f"código {name} no está en LIVE_CODES"
        if self.exclude and self.exclude.search(sym):
            return "símbolo excluido (LIVE_EXCLUDE_REGEX)"
        if sym not in self.contracts:
            return "contrato desconocido"
        if sym in self.trades:
            return "ya hay una posición en ese símbolo"
        if len(self.trades) >= c.live_max_pos:
            return f"máximo de {c.live_max_pos} posiciones abiertas"
        if sum(1 for t in self.trades.values() if t["side"] == side) >= c.live_max_side:
            return f"máximo de {c.live_max_side} posiciones {side}"
        pnl, halted = self._daily()
        if halted:
            if self.halted_day != self.day:
                self.halted_day = self.day
                self._notify(f"⛔ Límite de pérdida diaria alcanzado ({pnl:+.2f} USDT). No abro más hoy.")
            return "límite de pérdida diaria"
        px = self.last_price(sym)
        sgn = 1 if side == "LONG" else -1
        if abs(px - cand["price"]) > c.live_max_slip_r * cand["rk"]:
            return f"el precio se ha movido demasiado ({px} vs {cand['price']})"
        if sgn * (px - sl) <= 0 or sgn * (tp1 - px) <= 0:
            return "el precio ya está más allá del SL o del objetivo"
        return None

    # ---------- abrir ----------
    def on_signal(self, cand: dict, prec: int) -> None:
        """cand: symbol, tf, side (LONG/SHORT), code, price, rk, time. Devuelve nada; todo se notifica."""
        try:
            with self.lock:
                self._open(cand, prec)
        except Exception as ex:  # noqa: BLE001
            log.exception("Error al abrir %s", cand["symbol"])
            self._notify(f"⚠️ Error al abrir {html.escape(cand['symbol'])}: {html.escape(str(ex))}\n"
                         f"Comprueba en BingX que no ha quedado nada abierto.")

    def _levels(self, cand: dict, ref: float) -> tuple[float, float, float]:
        c, sd = self.cfg, 1 if cand["side"] == "LONG" else -1
        rk = cand["rk"]
        if c.partial_mode:
            return ref - sd * rk, ref + sd * rk * c.tp1_r, ref + sd * rk * c.tp2_r
        tp = ref + sd * rk * c.tp_single_r
        return ref - sd * rk, tp, tp

    def _open(self, cand: dict, prec: int) -> None:
        c, sym, side = self.cfg, cand["symbol"], cand["side"]
        sl, tp1, tp2 = self._levels(cand, cand["price"])
        why = self._reject(cand, sl, tp1)
        name = CODES[cand["code"]]
        if why:
            log.info("NO opero %s %s %s: %s", sym, side, name, why)
            self._notify(f"⏭ No opero {html.escape(sym)} {side} ({name}): {why}")
            return
        ct = self.contracts[sym]
        qprec = int(ct.get("quantityPrecision", 3) or 3)
        minq = float(ct.get("tradeMinQuantity", 0) or 0)
        eq = self.equity()
        mult = c.long_risk_mult if side == "LONG" else c.short_risk_mult
        risk_cash = eq * c.risk_pct / 100 * mult
        qty = risk_cash / cand["rk"]
        cap = eq * min(c.max_lev, c.live_leverage)
        if qty * cand["price"] > cap:
            qty = cap / cand["price"]
        qty = _floor(qty, qprec)
        if qty <= 0 or qty < minq:
            self._notify(f"⏭ No opero {html.escape(sym)}: tamaño {qty} por debajo del mínimo del contrato ({minq}).")
            return
        for path_, par in (("/openApi/swap/v2/trade/leverage", {"symbol": sym, "side": side, "leverage": int(min(c.max_lev, c.live_leverage))}),
                           ("/openApi/swap/v2/trade/marginType", {"symbol": sym, "marginType": "ISOLATED"})):
            try:
                self.api.call("POST", path_, par, retries=1)
            except Exception as ex:  # noqa: BLE001
                log.debug("%s %s: %s", path_, sym, ex)

        # 1) entrada a mercado (si hay fallo de red, comprobamos si llegó a ejecutarse)
        try:
            self._place(sym, side, "MARKET", qty, prec=prec)
        except BingXError:
            raise
        except RuntimeError:
            log.warning("Respuesta dudosa en la entrada de %s; compruebo posición", sym)
        time.sleep(0.7)
        filled, avg = self._pos(sym, side)
        if filled <= 0:
            raise RuntimeError("la orden de entrada no aparece como posición")
        entry = avg or cand["price"]
        # mantenemos la distancia de riesgo desde el precio de entrada REAL
        cand = dict(cand, price=entry)
        sl, tp1, tp2 = self._levels(cand, entry)

        # 2) stop en el exchange: si falla, se cierra todo
        lt = {"symbol": sym, "tf": cand["tf"], "side": side, "code": cand["code"], "qty": filled, "entry": entry,
              "sl_px": sl, "tp1": tp1, "tp2": tp2, "prec": prec, "stage": 1, "bars": 0, "last_ms": int(cand["time"]),
              "opened_ms": int(time.time() * 1000), "step": 10 ** (-qprec), "sl_id": None, "tp1_id": None,
              "tp2_id": None, "split": bool(c.partial_mode), "risk": cand["rk"]}
        try:
            lt["sl_id"] = self._place(sym, side, "STOP_MARKET", filled, sl, prec, exit_order=True)
        except Exception as ex:  # noqa: BLE001
            log.error("Sin stop en %s (%s): cierro la posición", sym, ex)
            self._flatten(sym, side, prec)
            self._notify(f"🚨 No pude colocar el stop de {html.escape(sym)} ({html.escape(str(ex))}). "
                         f"Posición cerrada a mercado por seguridad.")
            return

        # 3) objetivos
        q1 = _floor(filled * c.tp1_frac, qprec) if c.partial_mode else 0.0
        q2 = _floor(filled - q1, qprec)
        floor_q = max(minq, 10 ** (-qprec))
        if c.partial_mode and (q1 < floor_q or q2 < floor_q):
            lt["split"], q1, q2 = False, 0.0, filled
        lt["qty_tp1"], lt["qty_rest"] = q1, q2 if lt["split"] else filled
        try:
            if lt["split"]:
                lt["tp1_id"] = self._place(sym, side, "TAKE_PROFIT_MARKET", q1, tp1, prec, exit_order=True)
            lt["tp2_id"] = self._place(sym, side, "TAKE_PROFIT_MARKET", lt["qty_rest"], tp2, prec, exit_order=True)
        except Exception as ex:  # noqa: BLE001
            log.error("Fallo al colocar objetivos de %s: %s", sym, ex)
            self._notify(f"⚠️ {html.escape(sym)}: el stop está puesto pero falló un objetivo ({html.escape(str(ex))}). Se reintentará.")
        self.trades[sym] = lt
        self._save()
        self._notify(
            f"{'🟢' if side == 'LONG' else '🔴'} <b>OPERACIÓN {'REAL' if self.mode == 'live' else 'DEMO'} ABIERTA</b> · "
            f"<b>{side}</b> {html.escape(sym)} · {name} · {cand['tf']}\n"
            f"Cantidad {filled:g} · entrada {entry:.{prec}f} · riesgo ≈ {risk_cash:.2f} USDT ({c.risk_pct * mult:.2f}%)\n"
            f"🛑 SL {sl:.{prec}f} · 🎯 TP1 {tp1:.{prec}f} · TP2 {tp2:.{prec}f} · salida por tiempo a las {c.max_bars} velas")

    # ---------- gestión ----------
    def housekeeping(self) -> None:
        with self.lock:
            for lt in list(self.trades.values()):
                try:
                    self._check(lt)
                except Exception as ex:  # noqa: BLE001
                    log.warning("housekeeping %s: %s", lt["symbol"], ex)
            self._save()

    def on_bars(self, symbol: str, A: dict) -> None:
        """Llamado por el escáner con las velas cerradas del símbolo: cuenta velas y aplica la salida por tiempo."""
        with self.lock:
            lt = self.trades.get(symbol)
            if not lt:
                return
            try:
                new = int((A["time"] > lt["last_ms"]).sum())
                if new > 0:
                    lt["bars"] += new
                    lt["last_ms"] = int(A["time"][-1])
                self._check(lt)
                if lt["symbol"] in self.trades and lt["bars"] >= self.cfg.max_bars:
                    self._close_market(lt, "TIEMPO")
            except Exception as ex:  # noqa: BLE001
                log.exception("on_bars %s", symbol)
                self._notify(f"⚠️ Error gestionando {html.escape(symbol)}: {html.escape(str(ex))}")
            self._save()

    def _check(self, lt: dict) -> None:
        sym, side, prec = lt["symbol"], lt["side"], lt["prec"]
        qty, _ = self._pos(sym, side)
        if qty <= 0:
            self._finalize(lt, "EXCHANGE")
            return
        ids = {str(o.get("orderId")) for o in self._orders(sym)}
        if str(lt["sl_id"]) not in ids:                       # el stop debe existir siempre
            try:
                lt["sl_id"] = self._place(sym, side, "STOP_MARKET", qty, lt["sl_px"], prec, exit_order=True)
                log.warning("Stop de %s repuesto", sym)
            except Exception as ex:  # noqa: BLE001
                self._notify(f"🚨 {html.escape(sym)} sin stop y no se pudo reponer ({html.escape(str(ex))}). Cierro a mercado.")
                self._close_market(lt, "SIN_STOP")
                return
        if lt["stage"] == 1 and lt["split"] and qty < lt["qty"] - lt["step"] * 0.5:   # TP1 ejecutado
            lt["stage"] = 2
            if self.cfg.move_be:
                if not self._move_sl(lt, lt["entry"], qty):
                    self._notify(f"⚠️ {html.escape(sym)}: TP1 ejecutado pero no pude mover el stop a la entrada. Cierro el resto.")
                    self._close_market(lt, "BE_FALLO")
                    return
                self._notify(f"🎯 TP1 ejecutado · {html.escape(sym)} · stop movido a la entrada {lt['entry']:.{prec}f}")
            else:
                self._notify(f"🎯 TP1 ejecutado · {html.escape(sym)}")
        try:                                                  # reponer objetivos que falten
            if lt["stage"] == 1 and lt["split"] and str(lt["tp1_id"]) not in ids:
                lt["tp1_id"] = self._place(sym, side, "TAKE_PROFIT_MARKET", lt["qty_tp1"], lt["tp1"], prec, exit_order=True)
            if str(lt["tp2_id"]) not in ids:
                q = lt["qty_rest"] if lt["stage"] == 1 else qty
                lt["tp2_id"] = self._place(sym, side, "TAKE_PROFIT_MARKET", q, lt["tp2"], prec, exit_order=True)
        except Exception as ex:  # noqa: BLE001
            log.debug("reponer objetivos %s: %s", sym, ex)

    def _move_sl(self, lt: dict, new_px: float, qty: float) -> bool:
        """Coloca el stop nuevo ANTES de cancelar el viejo (la posición nunca queda sin stop)."""
        try:
            new_id = self._place(lt["symbol"], lt["side"], "STOP_MARKET", qty, new_px, lt["prec"], exit_order=True)
        except Exception as ex:  # noqa: BLE001
            log.warning("No se pudo mover el stop de %s: %s", lt["symbol"], ex)
            return False
        self._cancel(lt["symbol"], lt["sl_id"])
        lt["sl_id"], lt["sl_px"] = new_id, new_px
        return True

    def _flatten(self, sym: str, side: str, prec: int) -> None:
        qty, _ = self._pos(sym, side)
        if qty > 0:
            self._place(sym, side, "MARKET", qty, prec=prec, exit_order=True)

    def _close_market(self, lt: dict, reason: str) -> None:
        try:
            self._flatten(lt["symbol"], lt["side"], lt["prec"])
        except Exception as ex:  # noqa: BLE001
            self._notify(f"🚨 NO pude cerrar {html.escape(lt['symbol'])} a mercado ({html.escape(str(ex))}). Ciérralo a mano en BingX.")
            return
        time.sleep(0.7)
        self._finalize(lt, reason)

    def _finalize(self, lt: dict, reason: str) -> None:
        for k in ("sl_id", "tp1_id", "tp2_id"):
            self._cancel(lt["symbol"], lt.get(k))
        pnl = 0.0
        try:
            d = self.api.call("GET", "/openApi/swap/v2/user/income",
                              {"symbol": lt["symbol"], "incomeType": "REALIZED_PNL", "startTime": lt["opened_ms"], "limit": 100},
                              retries=2) or []
            pnl = sum(float(x.get("income", 0) or 0) for x in d)
        except Exception as ex:  # noqa: BLE001
            log.debug("income %s: %s", lt["symbol"], ex)
        lt.update(reason=reason, pnl=pnl, exit_ms=int(time.time() * 1000))
        self.trades.pop(lt["symbol"], None)
        self.closed.append(lt)
        self._save()
        self._notify(f"{'✅' if pnl > 0 else '🔻' if pnl < 0 else '⚪'} <b>Cerrada</b> · {html.escape(lt['symbol'])} {lt['side']} · "
                     f"{CODES[lt['code']]} · motivo {reason} · PnL realizado ≈ {pnl:+.2f} USDT (sin comisiones)")

    # ---------- comandos de Telegram ----------
    def pause(self) -> str:
        self.paused = True
        return "⏸ Trading en pausa: no abriré operaciones nuevas (las abiertas siguen protegidas por su stop)."

    def resume(self) -> str:
        self.paused = False
        return "▶️ Trading reanudado."

    def close_all(self) -> str:
        with self.lock:
            self.paused = True
            n = len(self.trades)
            for lt in list(self.trades.values()):
                self._close_market(lt, "MANUAL")
        return f"🛑 Cierre manual: {n} posiciones cerradas a mercado. Trading en pausa."

    def summary(self) -> str:
        with self.lock:
            try:
                eq = self.equity()
                pnl, halted = self._daily()
            except Exception as ex:  # noqa: BLE001
                return f"No pude leer la cuenta: {html.escape(str(ex))}"
            lines = [f"<b>Trading {self.mode.upper()}</b> {'⏸ PAUSA' if self.paused else '▶️ activo'}",
                     f"Equity {eq:.2f} USDT · PnL hoy {pnl:+.2f} USDT{' · LÍMITE DIARIO ALCANZADO' if halted else ''}",
                     f"Abiertas: {len(self.trades)}"]
            for t in self.trades.values():
                lines.append(f"{'🟢' if t['side'] == 'LONG' else '🔴'} {html.escape(t['symbol'])} {CODES[t['code']]} · "
                             f"ent {t['entry']:.{t['prec']}f} · SL {t['sl_px']:.{t['prec']}f} · "
                             f"{'tras TP1' if t['stage'] == 2 else 'esperando TP1'} · vela {t['bars']}/{self.cfg.max_bars}")
            lines.append(f"Cerradas: {len(self.closed)} · PnL acumulado ≈ {sum(t.get('pnl', 0.0) for t in self.closed):+.2f} USDT")
            return "\n".join(lines)


def make_trader(cfg, notifier) -> LiveTrader:
    if cfg.mode == "live" and cfg.live_confirm != CONFIRM_PHRASE:
        raise SystemExit(f"BOT_MODE=live exige LIVE_CONFIRM={CONFIRM_PHRASE} (confirmación explícita de dinero real).")
    if not (cfg.api_key and cfg.api_secret):
        raise SystemExit("Faltan BINGX_API_KEY / BINGX_API_SECRET.")
    return LiveTrader(cfg, notifier)
