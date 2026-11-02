#!/usr/bin/env python3
"""
Bot de alertas para ClubOferta – Reacondicionados.

Revisa https://cluboferta.cl/categoria-reacondicionados/ y avisa por Telegram
cuando aparece un producto nuevo (o cuando vuelve uno que había desaparecido).

Uso:
    python monitor.py                         # revisa una vez
    python monitor.py --cada 60 --durante 270 # revisa cada 60 s durante 270 s (GitHub Actions)
    python monitor.py --loop 10               # revisa cada 10 minutos sin parar (modo PC local)

Variables de entorno:
    TELEGRAM_TOKEN    token del bot (de @BotFather)
    TELEGRAM_CHAT_ID  tu chat id (de @userinfobot)
    Si no están, el bot solo imprime los avisos en consola.
"""

import html
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

URL = os.environ.get("CATEGORY_URL", "https://cluboferta.cl/categoria-reacondicionados/")
STATE_FILE = Path(os.environ.get("STATE_FILE", "productos_vistos.json"))
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
MAX_PAGES = 20
PRODUCTOS_POR_MENSAJE = 5      # productos por mensaje
PAUSA_ENTRE_MENSAJES = 1       # segundos

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "es-CL,es;q=0.9",
}


# ---------------------------------------------------------------- scraping --

def fetch(url):
    """Descarga una página con reintentos. Devuelve None si es 404."""
    for intento in range(3):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.text
        except requests.RequestException as e:
            if intento == 2:
                raise
            print(f"  Error descargando ({e}), reintentando…")
            time.sleep(5 * (intento + 1))


def limpiar(texto):
    return re.sub(r"\s+", " ", texto or "").strip()


def extraer_precios(contenedor):
    """Devuelve (precio_actual, precio_original) como texto, p.ej. ('$67.000', '$189.990')."""
    precio_el = contenedor.select_one(".price")
    if not precio_el:
        return "", ""
    original = precio_el.select_one("del .woocommerce-Price-amount, del .amount")
    actual = precio_el.select_one("ins .woocommerce-Price-amount, ins .amount")
    if not actual:
        montos = precio_el.select(".woocommerce-Price-amount, .amount")
        actual = montos[-1] if montos else None
    return (
        limpiar(actual.get_text()) if actual else limpiar(precio_el.get_text()),
        limpiar(original.get_text()) if original else "",
    )


def parsear_productos(page_html, base_url):
    soup = BeautifulSoup(page_html, "html.parser")
    productos = {}

    # Formato estándar de WooCommerce: <li class="product">…</li>
    for li in soup.select("li.product"):
        link = li.select_one('a[href*="/producto/"]')
        if not link:
            continue
        url = urljoin(base_url, link["href"]).split("?")[0]
        titulo_el = li.select_one(".woocommerce-loop-product__title, h2, h3")
        nombre = limpiar(titulo_el.get_text(" ")) if titulo_el else limpiar(link.get_text(" "))
        precio, precio_original = extraer_precios(li)
        productos[url] = {"nombre": nombre, "precio": precio, "precio_original": precio_original}

    # Respaldo por si cambian el diseño: cualquier enlace a /producto/
    if not productos:
        for link in soup.select('a[href*="/producto/"]'):
            url = urljoin(base_url, link["href"]).split("?")[0]
            nombre = limpiar(link.get_text(" "))
            if nombre and url not in productos:
                productos[url] = {"nombre": nombre, "precio": "", "precio_original": ""}

    siguiente = soup.select_one("a.next.page-numbers")
    siguiente_url = urljoin(base_url, siguiente["href"]) if siguiente else None
    return productos, siguiente_url


def obtener_todos_los_productos():
    productos, url, paginas = {}, URL, 0
    while url and paginas < MAX_PAGES:
        page_html = fetch(url)
        if page_html is None:
            break
        nuevos, url = parsear_productos(page_html, url)
        productos.update(nuevos)
        paginas += 1
    return productos


# ---------------------------------------------------------------- avisos ----

def enviar_telegram(texto):
    """Envía un mensaje a tu Telegram con tu bot."""
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        print("\n[AVISO]\n" + re.sub(r"<[^>]+>", "", texto) + "\n")
        return
    for intento in range(3):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": texto, "parse_mode": "HTML"},
                timeout=30,
            )
            if r.ok:
                return
            print(f"Telegram respondió {r.status_code}: {r.text[:300]}", file=sys.stderr)
            if r.status_code in (400, 401, 403):   # token o chat id malos: no sirve reintentar
                return
        except requests.RequestException as e:
            print(f"Error enviando a Telegram: {e}", file=sys.stderr)
        time.sleep(5 * (intento + 1))


def formatear_producto(p, url):
    texto = f"<b>{html.escape(p['nombre'])}</b>"
    if p.get("precio"):
        texto += f"\n💰 {html.escape(p['precio'])}"
        if p.get("precio_original") and p["precio_original"] != p["precio"]:
            texto += f" (antes {html.escape(p['precio_original'])})"
    return texto + f"\n{html.escape(url)}"


def enviar_alertas(titulo, items):
    """Agrupa varios productos por mensaje para no mandar demasiados seguidos."""
    for i in range(0, len(items), PRODUCTOS_POR_MENSAJE):
        grupo = items[i:i + PRODUCTOS_POR_MENSAJE]
        enviar_telegram(titulo + "\n\n" + "\n\n".join(grupo))
        if i + PRODUCTOS_POR_MENSAJE < len(items):
            time.sleep(PAUSA_ENTRE_MENSAJES)


# ---------------------------------------------------------------- estado ----

def cargar_estado():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return None


def guardar_estado(estado):
    STATE_FILE.write_text(json.dumps(estado, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------- lógica ----

def revisar():
    ahora = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"[{ahora}] Revisando {URL}")
    actuales = obtener_todos_los_productos()
    print(f"  {len(actuales)} productos en la página")

    estado = cargar_estado()

    # Primera ejecución: guardar lo que hay sin avisar uno por uno
    if estado is None:
        estado = {"productos": {}}
        for url, p in actuales.items():
            estado["productos"][url] = {**p, "visto_por_primera_vez": ahora, "activo": True}
        guardar_estado(estado)
        lista = "\n".join(f"• {html.escape(p['nombre'])}" for p in actuales.values())
        enviar_telegram(
            f"✅ <b>Bot de ClubOferta activo</b>\nHay {len(actuales)} reacondicionados ahora:\n"
            f"{lista}\n\nTe aviso cuando aparezca algo nuevo."
        )
        return

    # Si la página vino vacía puede ser un error temporal: no tocar el estado
    if not actuales:
        print("  La página no mostró productos; no se actualiza el estado.")
        return

    vistos = estado["productos"]
    nuevos, reaparecidos = [], []
    for url, p in actuales.items():
        if url not in vistos:
            nuevos.append(formatear_producto(p, url))
            vistos[url] = {**p, "visto_por_primera_vez": ahora, "activo": True}
        else:
            if not vistos[url].get("activo", True):
                reaparecidos.append(formatear_producto(p, url))
            vistos[url].update(p)
            vistos[url]["activo"] = True

    for url in vistos:
        if url not in actuales:
            vistos[url]["activo"] = False

    if nuevos:
        enviar_alertas("🆕 <b>Nuevo reacondicionado en ClubOferta</b>", nuevos)
    if reaparecidos:
        if nuevos:
            time.sleep(PAUSA_ENTRE_MENSAJES)
        enviar_alertas("🔁 <b>Volvió a aparecer en ClubOferta</b>", reaparecidos)

    print(f"  {len(nuevos)} nuevo(s), {len(reaparecidos)} reaparecido(s)")
    guardar_estado(estado)


def revisar_seguro():
    try:
        revisar()
    except Exception as e:
        print(f"  Error: {e}", file=sys.stderr)


def main():
    args = sys.argv[1:]
    if len(args) >= 2 and args[0] == "--loop":
        minutos = float(args[1])
        while True:
            revisar_seguro()
            time.sleep(minutos * 60)
    elif "--cada" in args and "--durante" in args:
        cada = float(args[args.index("--cada") + 1])
        durante = float(args[args.index("--durante") + 1])
        fin = time.monotonic() + durante
        while True:
            inicio = time.monotonic()
            revisar_seguro()
            espera = cada - (time.monotonic() - inicio)
            if time.monotonic() + max(espera, 0) > fin:
                break
            time.sleep(max(espera, 0))
    else:
        revisar()


if __name__ == "__main__":
    main()
