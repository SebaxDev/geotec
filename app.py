import threading
import time
import unicodedata
from datetime import datetime

import gspread
import pandas as pd
import pytz
import streamlit as st
from google.oauth2 import service_account

# =====================================================================
# CONFIGURACIÓN
# =====================================================================
st.set_page_config(page_title="Fusión - GPS Técnico", page_icon="📍", layout="centered")

SHEET_ID = "13R_3Mdr25Jd-nGhK7CxdcbKkFWLc0LPdYrOLOY8sZJo"
WORKSHEET_CLIENTES = "Clientes"
WORKSHEET_CAJAS = "Cajas"

# Columnas de la hoja "Clientes" donde se escribe
COL_PRECINTO = "F"
COL_FECHA = "H"
COL_LAT = "J"
COL_LON = "K"
COL_CAJA = "M"

# Nombres de encabezados
H_NUM = "Nº Cliente"
H_NOMBRE = "Nombre"
H_DIR = "Dirección"
H_CAJA = "Caja NAP"
H_PRECINTO = "N° de Precinto"
H_LAT = "Latitud"
H_LON = "Longitud"
H_CAJAS_NUM = "N De Caja"

TTL_DATOS = 600          # segundos de validez de los datos en caché
TTL_FALLA = 60           # reintento rápido tras un fallo
MIN_CHARS = 3            # mínimo de caracteres para buscar
MAX_RESULTADOS = 30      # máximo de resultados en el selector
TIMEOUT_API = (5, 20)    # (conexión, lectura) en segundos


def ahora_argentina():
    tz = pytz.timezone("America/Argentina/Cordoba")
    return datetime.now(tz).strftime("%d/%m/%Y %H:%M:%S")


def normalizar(texto):
    """Minúsculas, sin acentos, sin espacios repetidos."""
    t = unicodedata.normalize("NFKD", str(texto)).encode("ascii", "ignore").decode()
    return " ".join(t.lower().split())


# =====================================================================
# CONEXIÓN A GOOGLE SHEETS (se crea UNA sola vez por servidor)
# =====================================================================
@st.cache_resource(show_spinner=False)
def get_doc():
    creds_dict = dict(st.secrets["gcp_service_account"])
    creds = service_account.Credentials.from_service_account_info(
        creds_dict,
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ],
    )
    client = gspread.authorize(creds)
    # Evita que la app quede colgada si se corta la red
    try:
        client.set_timeout(TIMEOUT_API)
    except Exception:
        pass
    return client.open_by_key(SHEET_ID)


@st.cache_resource(show_spinner=False)
def get_ws_clientes():
    return get_doc().worksheet(WORKSHEET_CLIENTES)


# =====================================================================
# CACHÉ EN MEMORIA (compartido entre sesiones del mismo proceso)
# =====================================================================
_LOCK = threading.RLock()
_CACHE = {"ts": 0.0, "clientes": None, "cajas": [], "headers": []}


def _a_df(values):
    if not values:
        return pd.DataFrame()
    headers = [str(h).strip() for h in values[0]]
    n = len(headers)
    filas = [(list(r) + [""] * (n - len(r)))[:n] for r in values[1:]]
    return pd.DataFrame(filas, columns=headers)


@st.cache_data(ttl=TTL_DATOS, show_spinner=False)
def _fetch_remote():
    """Una sola ejecución para TODOS los usuarios dentro del TTL.

    st.cache_data deduplica: si 20 técnicos entran a la vez con la caché
    vencida, se hace 1 sola llamada a Google y los otros 19 esperan el
    resultado ya calculado (en vez de 20 llamadas simultáneas que se
    pelean la cuota y dan timeout).
    """
    doc = get_doc()
    resp, err = None, None
    for _ in range(2):
        try:
            resp = doc.values_batch_get([WORKSHEET_CLIENTES, WORKSHEET_CAJAS])
            break
        except Exception as e:
            err = e
            time.sleep(0.7)
    if resp is None:
        raise err

    rangos = resp.get("valueRanges", [])
    df_clientes = _a_df(rangos[0].get("values", []) if len(rangos) > 0 else [])
    df_cajas = _a_df(rangos[1].get("values", []) if len(rangos) > 1 else [])

    for col in (H_NUM, H_NOMBRE):
        if col not in df_clientes.columns:
            raise ValueError(f"No encontré la columna '{col}' en la hoja {WORKSHEET_CLIENTES}.")

    # Columna precalculada para buscar rápido (sin acentos ni mayúsculas)
    df_clientes["_buscar"] = (
        (df_clientes[H_NUM].astype(str) + " " + df_clientes[H_NOMBRE].astype(str))
        .map(normalizar)
    )

    cajas = []
    if H_CAJAS_NUM in df_cajas.columns:
        cajas = sorted({str(c).strip() for c in df_cajas[H_CAJAS_NUM] if str(c).strip()})

    return df_clientes, cajas, list(df_clientes.columns)


def get_data(force=False):
    """Devuelve (df_clientes, cajas, headers, aviso).

    Si hay datos guardados, NUNCA se queda colgado: sirve lo que hay y
    reintenta después. Solo bloquea (pocos segundos) la primera carga real.
    """
    with _LOCK:
        fresco = _CACHE["clientes"] is not None and (time.time() - _CACHE["ts"]) < TTL_DATOS
        if not force and fresco:
            return _CACHE["clientes"], _CACHE["cajas"], _CACHE["headers"], None

    if force:
        _fetch_remote.clear()

    try:
        df, cajas, headers = _fetch_remote()
    except Exception as e:
        with _LOCK:
            if _CACHE["clientes"] is not None:
                # Falló la actualización pero tenemos datos viejos: los servimos
                # y reintentamos dentro de 1 minuto, sin bloquear la pantalla.
                _CACHE["ts"] = time.time() - TTL_DATOS + TTL_FALLA
                return _CACHE["clientes"], _CACHE["cajas"], _CACHE["headers"], \
                    f"No pude actualizar la base ({str(e)[:60]}). Uso datos anteriores."
        raise

    with _LOCK:
        _CACHE["clientes"], _CACHE["cajas"], _CACHE["headers"] = df, cajas, headers
        _CACHE["ts"] = time.time()
    return df, cajas, headers, None


def parchear_fila(row_pos, cambios):
    """Actualiza en memoria la fila guardada, para no recargar toda la hoja."""
    with _LOCK:
        df = _CACHE["clientes"]
        if df is None or row_pos not in df.index:
            return
        for col, val in cambios.items():
            if col in df.columns:
                df.at[row_pos, col] = val


# =====================================================================
# GPS: cada cliente tiene su propio componente (evita coords viejas)
# =====================================================================
# La librería fija internamente key="loc", por eso llamamos al componente
# interno con una clave propia por cliente: así se reinicia solo.
GEO_DEFAULT = {
    "latitude": None, "longitude": None, "altitude": None, "accuracy": None,
    "altitudeAccuracy": None, "heading": None, "speed": None,
}
try:
    from streamlit_geolocation import _streamlit_geolocation as _geo_component
except Exception:
    _geo_component = None


def loc_valida(loc):
    return bool(loc) and isinstance(loc, dict) and loc.get("latitude") is not None and loc.get("longitude") is not None


def widget_gps(id_cliente):
    nonce = st.session_state.get("geo_nonce", 0)
    if _geo_component is not None:
        return _geo_component(key=f"geo_{id_cliente}_{nonce}", default=GEO_DEFAULT)

    # Plan B: si el componente no cargó (algunos navegadores móviles lo
    # bloquean), se permiten ingresar las coordenadas manualmente.
    st.info(
        "⚠️ El GPS automático no está disponible en este navegador. "
        "Ingresá las coordenadas manualmente (podés copiarlas desde Google Maps: "
        "mantené presionado el punto en el mapa y tocá las coordenadas que aparecen)."
    )
    c1, c2 = st.columns(2)
    lat_txt = c1.text_input("Latitud", key=f"mlat_{id_cliente}", placeholder="-31.420083")
    lon_txt = c2.text_input("Longitud", key=f"mlon_{id_cliente}", placeholder="-64.188776")
    if st.button("📌 Usar estas coordenadas", key=f"mset_{id_cliente}", use_container_width=True):
        try:
            lat = float(lat_txt.replace(",", "."))
            lon = float(lon_txt.replace(",", "."))
        except ValueError:
            st.error("Formato inválido. Usá números, con punto o coma decimal.")
        else:
            if -90 <= lat <= 90 and -180 <= lon <= 180:
                st.session_state[f"loc_manual_{id_cliente}"] = {
                    "latitude": lat, "longitude": lon, "accuracy": None,
                }
            else:
                st.error("Coordenadas fuera de rango.")
    return st.session_state.get(f"loc_manual_{id_cliente}")


# =====================================================================
# GUARDADO
# =====================================================================
def localizar_fila(ws, headers, numero, fila_esperada):
    """Confirma que la fila de la hoja sigue siendo del cliente (por si alguien reordenó)."""
    col_num = headers.index(H_NUM) + 1
    try:
        valor = ws.cell(fila_esperada, col_num).value
        if str(valor or "").strip() == str(numero).strip():
            return fila_esperada
    except Exception:
        pass
    try:
        valores = ws.col_values(col_num)
        for i, v in enumerate(valores, start=1):
            if i > 1 and str(v).strip() == str(numero).strip():
                return i
    except Exception:
        pass
    return None


def guardar(row_pos, numero, loc, nuevo_precinto, precinto_actual, nueva_caja, caja_actual, headers):
    ws = get_ws_clientes()
    fila_esperada = row_pos + 2
    fila = None

    for intento in range(3):
        try:
            if fila is None:
                fila = localizar_fila(ws, headers, numero, fila_esperada)
                if fila is None:
                    return False, "No encontré al cliente en la planilla. Recargá los datos e intentá de nuevo."

            fecha = ahora_argentina()
            updates = [
                {"range": f"{COL_LAT}{fila}", "values": [[str(loc["latitude"])]]},
                {"range": f"{COL_LON}{fila}", "values": [[str(loc["longitude"])]]},
                {"range": f"{COL_FECHA}{fila}", "values": [[fecha]]},
            ]
            if nuevo_precinto != precinto_actual:
                # El apóstrofe fuerza texto y conserva ceros iniciales
                valor_prec = f"'{nuevo_precinto}" if nuevo_precinto.startswith("0") else nuevo_precinto
                updates.append({"range": f"{COL_PRECINTO}{fila}", "values": [[valor_prec]]})
            if nueva_caja != caja_actual:
                updates.append({"range": f"{COL_CAJA}{fila}", "values": [[nueva_caja]]})

            ws.batch_update(updates, value_input_option="USER_ENTERED")

            if fila == fila_esperada:
                cambios = {H_LAT: str(loc["latitude"]), H_LON: str(loc["longitude"])}
                if nuevo_precinto != precinto_actual:
                    cambios[H_PRECINTO] = nuevo_precinto
                if nueva_caja != caja_actual:
                    cambios[H_CAJA] = nueva_caja
                parchear_fila(row_pos, cambios)
            else:
                with _LOCK:
                    _CACHE["ts"] = 0.0  # la hoja cambió de orden: forzar recarga
                _fetch_remote.clear()
            return True, None

        except Exception as e:
            if intento < 2:
                get_ws_clientes.clear()
                time.sleep(0.8)
                try:
                    ws = get_ws_clientes()
                except Exception:
                    pass
            else:
                return False, f"Error de red al guardar: {str(e)[:80]}. Intentá nuevamente."
    return False, "No se pudo guardar."


# =====================================================================
# PANEL DEL CLIENTE
# =====================================================================
def panel_cliente(row_pos, df_clientes, cajas, headers):
    row = df_clientes.loc[row_pos]
    numero = str(row.get(H_NUM, "")).strip()
    id_cliente = f"{row_pos}_{numero}"

    caja_actual = str(row.get(H_CAJA, "")).strip()
    precinto_actual = str(row.get(H_PRECINTO, "")).strip()

    st.info(
        f"**Dirección:** {row.get(H_DIR, 'S/D')}  \n\n"
        f"**Caja actual:** {caja_actual or 'Ninguna'} | **Precinto:** {precinto_actual or 'Ninguno'}"
    )

    lat_actual = str(row.get(H_LAT, "")).strip()
    lon_actual = str(row.get(H_LON, "")).strip()
    if lat_actual and lon_actual and lat_actual.lower() not in ("nan", "none") and lon_actual.lower() not in ("nan", "none"):
        st.warning(
            f"⚠️ **Atención:** Este cliente ya tiene una ubicación guardada ({lat_actual}, {lon_actual}). "
            "Si guardás ahora, la ubicación anterior será reemplazada."
        )
        st.markdown(f"🗺️ **[Ver ubicación actual en Google Maps](https://www.google.com/maps?q={lat_actual},{lon_actual})**")

    st.markdown("### 1. 📡 OBTENER UBICACIÓN")
    if _geo_component is not None:
        st.markdown("**Presioná el botón debajo para leer el GPS de tu celular ⬇️**")

    loc = widget_gps(id_cliente)

    if loc_valida(loc):
        precision = loc.get("accuracy")
        txt = f"📍 ¡Ubicación capturada lista para guardar!\n\nLat: {loc['latitude']} | Lon: {loc['longitude']}"
        if precision is not None:
            try:
                txt += f"\n\nPrecisión: ±{float(precision):.0f} m"
            except (TypeError, ValueError):
                pass
        st.success(txt)
        try:
            if precision is not None and float(precision) > 100:
                st.warning("⚠️ La precisión del GPS es baja. Si podés, esperá unos segundos y volvé a presionar el botón.")
        except (TypeError, ValueError):
            pass
    else:
        if _geo_component is not None:
            st.error("❌ Esperando captura de GPS... Tocá el botón de arriba.")

    # Si la caja actual no está en la lista, la agregamos para no borrarla sin querer
    opciones_caja = [""] + cajas
    if caja_actual and caja_actual not in opciones_caja:
        opciones_caja.append(caja_actual)
    caja_idx = opciones_caja.index(caja_actual) if caja_actual in opciones_caja else 0

    with st.form(key=f"form_{id_cliente}"):
        st.markdown("### 2. Datos Complementarios (Opcional)")
        nuevo_precinto = st.text_input(
            "🔒 Precinto (Solo 7 números)",
            value=precinto_actual,
            max_chars=7,
            key=f"prec_{id_cliente}",
        ).strip()
        nueva_caja = st.selectbox(
            "📦 Caja NAP", options=opciones_caja, index=caja_idx, key=f"caja_{id_cliente}"
        )
        submit = st.form_submit_button("💾 Guardar Datos", use_container_width=True)

    if not submit:
        return

    # ---- Validaciones ----
    if not loc_valida(loc):
        st.error("❌ No podés guardar sin obtener la ubicación primero (botón de GPS o coordenadas manuales).")
        return
    if nuevo_precinto and (not nuevo_precinto.isdigit() or len(nuevo_precinto) != 7):
        st.error("❌ El precinto debe tener exactamente 7 números.")
        return
    if nuevo_precinto and nuevo_precinto != precinto_actual and H_PRECINTO in df_clientes.columns:
        coincide = df_clientes[H_PRECINTO].astype(str).str.strip() == nuevo_precinto
        coincide &= df_clientes.index != row_pos
        if coincide.any():
            dup = df_clientes[coincide].iloc[0]
            st.error(f"❌ El precinto {nuevo_precinto} ya lo tiene el cliente: {dup[H_NUM]} - {dup[H_NOMBRE]}.")
            return

    # ---- Guardado ----
    with st.spinner("Guardando..."):
        ok, error = guardar(
            row_pos, numero, loc, nuevo_precinto, precinto_actual, nueva_caja, caja_actual, headers
        )

    if ok:
        st.session_state["msg_ok"] = f"✅ Datos de {row.get(H_NOMBRE, 'cliente')} guardados correctamente."
        st.session_state["reset_busqueda"] = True
        st.session_state["geo_nonce"] = st.session_state.get("geo_nonce", 0) + 1
        st.rerun()
    else:
        st.error(f"❌ {error}")


# =====================================================================
# INTERFAZ
# =====================================================================
def main():
    st.title("📍 Fusión Campo")
    st.markdown("Captura de coordenadas de Clientes")
    st.markdown("---")

    # El reseteo se hace ANTES de crear el widget (si no, Streamlit da error)
    if st.session_state.pop("reset_busqueda", False):
        st.session_state["busqueda_cliente"] = ""

    mensaje_ok = st.session_state.pop("msg_ok", None)
    if mensaje_ok:
        st.success(mensaje_ok)

    # La pantalla se dibuja PRIMERO: el técnico ve la app al instante,
    # aunque los datos todavía estén cargando abajo.
    busqueda = st.text_input(
        "🔍 Buscar Cliente (Nombre o Nº)",
        placeholder="Ej: Juan Perez o 9944",
        key="busqueda_cliente",
    ).strip()

    # ---- Carga de datos (con caché es casi instantáneo; nunca cuelga la app) ----
    try:
        with st.spinner("Cargando datos..."):
            df_clientes, cajas, headers, aviso = get_data()
    except Exception as e:
        st.error(f"Error de conexión con la planilla. Detalle: {str(e)[:200]}")
        if st.button("🔄 Reintentar"):
            _fetch_remote.clear()
            st.rerun()
        st.stop()

    if aviso:
        st.warning(aviso)

    edad_min = int(time.time() - _CACHE["ts"]) // 60
    st.caption(f"🕒 Datos actualizados hace {edad_min} min.")

    # ---- Búsqueda ----
    if busqueda:
        q = normalizar(busqueda)
        if len(q) < MIN_CHARS:
            st.caption(f"Escribí al menos {MIN_CHARS} caracteres para buscar.")
        else:
            resultados = df_clientes[df_clientes["_buscar"].str.contains(q, regex=False)]
            total = len(resultados)

            if total == 0:
                st.warning("No se encontraron clientes.")
            else:
                if total > MAX_RESULTADOS:
                    st.caption(f"Mostrando {MAX_RESULTADOS} de {total} resultados. Escribí más para afinar la búsqueda.")
                    resultados = resultados.head(MAX_RESULTADOS)

                etiquetas = dict(
                    zip(resultados.index, resultados[H_NUM].astype(str) + " - " + resultados[H_NOMBRE].astype(str))
                )
                row_pos = st.selectbox(
                    "Seleccioná el cliente correcto:",
                    options=list(etiquetas.keys()),
                    format_func=lambda i: etiquetas.get(i, str(i)),
                )
                panel_cliente(row_pos, df_clientes, cajas, headers)

    st.markdown("---")
    if st.button("🔄 Recargar datos de la planilla"):
        with st.spinner("Actualizando..."):
            try:
                get_data(force=True)
            except Exception as e:
                st.error(f"No pude actualizar: {str(e)[:100]}")
        st.rerun()


main()