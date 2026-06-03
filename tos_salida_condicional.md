# thinkorswim — Salida condicional de opciones (vender a +X% de P/L)

**Idea clave:** en TOS las órdenes condicionan sobre **precio (prima)**, no sobre "P/L%".
Pero en una opción larga (CALL o PUT comprado) el P/L% sube monótono con la prima, así que:

```
+X% de P/L   ==   prima de venta = prima_entrada × (1 + X/100)
−Y% de P/L   ==   prima de stop  = prima_entrada × (1 − Y/100)
```

Esa equivalencia es **exacta**. Ej.: comprás un CALL a **$1.00** → vender a **+10%** = SELL LIMIT a **$1.10**.

> Usá `tos_exit_calc.py` para calcular el precio exacto (ver abajo).

---

## Opción 1 — Límite GTC (lo más simple, solo profit target)
1. Comprás el contrato (ej. a $1.00).
2. En la posición: clic derecho → **Create Closing Order** → SELL.
3. Tipo **LIMIT**, precio = `entrada × 1.10`, **TIF = GTC**.
4. Queda trabajando hasta que la prima toque el target y se ejecuta sola.

## Opción 2 — Bracket OCO al entrar (profit + stop juntos) ← recomendado
Equivale a tu lógica de "vender al ROI objetivo **o** cortar pérdida":
1. En el ticket de compra: **Advanced Order → "1st Trgs OCO"**.
2. Se agregan 2 órdenes hijas que se activan al llenarse la compra:
   - **SELL LIMIT** a `entrada × 1.10`  (tu +10%).
   - **SELL STOP** a `entrada × 0.80`  (tu −20%, o donde quieras cortar).
3. Cuando una se ejecuta, la otra se cancela automáticamente (OCO).

## Opción 3 — Trailing stop por %
Tipo de orden **TRStop**: seguís la prima con un trailing de X% o X$.
Deja correr la ganancia en vez de salir fijo a +10%.

## Opción 4 — Condición custom (engranaje ⚙ de la orden)
El ⚙ del ticket abre **Conditions**: condicionás por *Mark/Last/Bid/Ask* del contrato
o del **subyacente**, o por un *Study*. Útil para salir según el precio de la ACCIÓN
en vez de la prima.

---

## Advertencias para opciones
- **Los STOP sobre la prima son traicioneros**: bid/ask ancho y poca liquidez pueden
  disparar el stop con un *print* malo. Alternativa: poner el stop sobre el **precio del
  subyacente** (Opción 4), no sobre la prima.
- thinkScript **no manda órdenes** (solo estudios/alertas). Para automatización real
  programática → **Schwab Trader API** (ex-API de TD Ameritrade).
- Las **Alertas** de TOS por P/L o precio **solo avisan**, no venden.

## Recomendación práctica
- Solo profit → **Opción 1** (SELL LIMIT GTC).
- Profit + protección → **Opción 2** (OCO), con el stop sobre el subyacente si se puede.

---

## Calculadora — `tos_exit_calc.py`
```bash
# precio de salida para una entrada y un % objetivo
py tos_exit_calc.py 1.00 10            # entrada 1.00, +10%  -> 1.10
py tos_exit_calc.py 1.35 10 --stop 20  # + target +10% y stop -20%

# sin argumentos: imprime una tabla de referencia
py tos_exit_calc.py
```
