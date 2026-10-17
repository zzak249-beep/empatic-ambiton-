# Granville V5 → BingX → Telegram

Port a Python de la estrategia de TradingView **"Granville V5 — ESTRATEGIA"**: escanea los futuros perpetuos de BingX,
detecta las señales **G1–G8** al cierre de cada vela de 15m, las filtra con los libros **Base / V2 / V3 / V4**,
avisa por **Telegram** y, si tú lo activas, **opera en BingX** (demo o dinero real).

> ⚠️ **Esto no es una promesa de beneficios.** El bot automatiza una estrategia; no garantiza que sea rentable.
> Empieza en modo `signals`, pasa a `demo`, y solo después, con poco dinero, a `live`. Puedes perder todo lo que pongas.
> No es asesoramiento financiero.

## Cómo decide (igual que el Pine)
1. **Señales** (vela cerrada de 15m): cruces (G1/G5), falsas pérdidas/rupturas de la media (G2/G6), rebotes (G3/G7) y extremos (g4/g8).
2. **Cuatro libros** que se calculan siempre en paralelo:
   `Base` (original) · `V2` (TF mayor 4h, RVOL, sesión, VWAP, stop estructural) ·
   `V3` (+ RSI/MACD, volumen de gatillo, TF 1h, filtro de coste) · `V4` (+ régimen HaP EMA Strength).
3. **Autoevaluación**: cada señal de cada libro se simula como operación virtual con las mismas salidas que la real
   (SL · TP1 parcial · stop a la entrada · TP2 · salida a las 18 velas, con costes). De ahí salen, por código y libro, el
   *posterior* de R y su *límite inferior*.
4. **Filtro de ventaja** (`EDGE_FILTER`): solo se opera un código si tiene **n ≥ 12 y límite inferior > 0** (`good`).
5. **Operación** (libro `BOOK`): entrada a mercado, stop en el exchange, TP1 (50%), stop a la entrada, TP2, salida por tiempo.

## Archivos
| Archivo | Para qué |
|---|---|
| `bot.py` | Escáner, Telegram, comandos, histórico (`--warmup`, `--backtest`) y bucle principal |
| `engine.py` | Todo el cálculo de la estrategia (indicadores, 4 libros, simulación, estadísticas). Sin red, probado |
| `trader.py` | Ejecución en BingX con salvaguardas (solo se carga si `BOT_MODE` ≠ `signals`) |
| `_env.example` / `_env.railway.example` | Variables (la segunda, sin comentarios, para pegar en Railway) |
| `railway.json`, `requirements.txt`, `.python-version` | Despliegue |
| `tests/`, `run_tests.sh`, `.github/workflows/tests.yml` | Tests (exchange y reloj simulados, no tocan tu cuenta) |

## 1. Instalar y probar en local
```bash
pip install -r requirements.txt
cp _env.example .env          # rellena TELEGRAM_TOKEN y TELEGRAM_CHAT_ID
python bot.py --get-chat-id   # tras escribir /start a tu bot
python bot.py --test-telegram
./run_tests.sh                # opcional
```

## 2. Precargar la autoevaluación (importante)
Con `EDGE_FILTER=good` el bot no opera un código hasta tener historial con ventaja. Se precarga con el histórico real de BingX:
```bash
python bot.py --backtest top:60 --days 45      # solo informe (no guarda)
python bot.py --warmup   top:60 --days 45      # informe + guarda stats.json (el bot lo usa al arrancar)
```
Lee el informe con espíritu crítico: con `n < 30` no hay conclusiones, y al mirar muchos códigos y libros **alguno saldrá
bien por pura casualidad**. Un ★ es una pista, no una prueba. Desconfía de lo que solo funciona en un libro o en un periodo.

## 3. Escalera de seguridad (no te saltes pasos)
| Paso | `BOT_MODE` | Qué hace | Cuánto tiempo |
|---|---|---|---|
| 1 | `signals` | Solo avisos. Compara con tu gráfico | 1–2 semanas |
| 2 | `demo` | Opera en la cuenta virtual VST de BingX | 2–4 semanas |
| 3 | `live` | Dinero real, `RISK_PCT=0.25–0.5`, cuenta pequeña | vigilado a mano |

Para `demo`/`live` crea en BingX una clave API con permisos de **lectura y trading, SIN retirada**, y limítala a la IP de Railway si puedes.
`live` exige `LIVE_CONFIRM=SI_OPERAR_CON_DINERO_REAL`.

## 4. Desplegar en Railway
1. Sube este repo a GitHub (**el `.gitignore` ya excluye `.env` y el estado; nunca subas claves**).
2. Railway → *New Project → Deploy from GitHub repo*.
3. *Variables → Raw Editor*: pega `_env.railway.example` y rellena (Railway no admite comentarios en la línea).
4. *Volume*: añade uno montado en `/data` y deja `DATA_DIR=/data` (guarda estadísticas, simulaciones y operaciones entre redeploys).
5. Para precargar estadísticas: ejecútalo en local con `--warmup` y sube `stats.json` al volumen, o deja que se acumule solo.
6. Revisa los logs: debe aparecer "Granville V5 en marcha" en Telegram.

## Comandos de Telegram
`/stats` (tabla del libro operado) · `/todos` (los 4 libros) · `/codigos` (qué códigos están permitidos) · `/abiertas` ·
`/estado` · y en demo/live: `/live` · `/pausa` · `/reanudar` · `/cerrartodo` (cierra todo a mercado y pausa).

## Salvaguardas de la ejecución
- Stop y objetivos **en el exchange**: si el bot se cae, la posición sigue protegida.
- Si no se puede colocar el stop → se cierra la posición al instante. Si el stop desaparece → se repone o se cierra.
- Límites: posiciones máximas, máximas por lado, **pérdida diaria**, no persigue precio (`LIVE_MAX_SLIP_R`), símbolos excluidos.
- Una sola posición por símbolo. Tope de apalancamiento. Aislado (isolated margin).
- Un stop **no garantiza** el precio de salida: con saltos o poca liquidez puedes perder más de 1R.

## Diferencias respecto al Pine (léelas)
- **Estadísticas globales**: el Pine evalúa cada código en *un* símbolo; el bot las agrega **entre todos los símbolos escaneados**
  (por timeframe y libro). Es lo único viable con cientos de contratos, pero no es idéntico.
- **Filtro de delta de volumen (1m)**: no implementado (en el Pine está apagado por defecto).
- **Entrada**: el Pine entra al cierre de la vela; el bot, a mercado unos segundos después → hay slippage.
- **Posiciones simultáneas**: el Pine tiene una sola; el bot puede tener varias (límites arriba).
- **HaP EMA Strength** está reconstruido a partir de su guía de uso, no es el código original (igual que en el Pine).
- El calentamiento del HaP se mide en velas descargadas; los timeframes superiores se piden con 400 velas.
- Indicadores (SMA/EMA/RMA/RSI/MACD), alineación sin repintado con el TF superior y reglas de salida están verificados con tests,
  pero **no se ha comparado vela a vela contra TradingView**: hazlo tú con `python bot.py --debug SIMBOLO` frente al gráfico antes de fiarte.

## Qué NO hace
No garantiza resultados, no gestiona la correlación entre posiciones (varias señales a la vez suelen ser la misma apuesta),
no modela el slippage real ni la liquidez, y no sabe nada de noticias. Las acciones tokenizadas (`NCSK…`) se excluyen de la ejecución real por defecto.
