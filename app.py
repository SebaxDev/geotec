import streamlit as st
import pandas as pd
from google.oauth2 import service_account
import gspread
from streamlit_geolocation import streamlit_geolocation
from datetime import datetime
import pytz
import time

# --- CONFIGURACIÓN ---
st.set_page_config(page_title="Fusión - GPS Técnico", page_icon="📍", layout="centered")

SHEET_ID = "13R_3Mdr25Jd-nGhK7CxdcbKkFWLc0LPdYrOLOY8sZJo"
WORKSHEET_CLIENTES = "Clientes"
WORKSHEET_CAJAS = "Cajas"

def ahora_argentina():
    tz = pytz.timezone('America/Argentina/Cordoba')
    return datetime.now(tz).strftime("%d/%m/%Y %H:%M:%S")

# --- CONEXIÓN A SHEETS ---
@st.cache_resource(show_spinner="Conectando a Fusión...")
def get_sheet_client():
    creds_dict = dict(st.secrets["gcp_service_account"])
    creds = service_account.Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    )
    return gspread.authorize(creds)

# OPTIMIZACIÓN 1: Subimos el TTL a 600 (10 minutos)
@st.cache_data(ttl=600, show_spinner="Actualizando base (optimizado)...")
def load_data():
    client = get_sheet_client()
    doc = client.open_by_key(SHEET_ID)
    
    # OPTIMIZACIÓN 2: Usar get_all_values() en lugar de get_all_records()
    ws_clientes = doc.worksheet(WORKSHEET_CLIENTES)
    data_clientes = ws_clientes.get_all_values()
    # Convertimos la lista cruda en un DataFrame usando la primera fila como columnas
    df_clientes = pd.DataFrame(data_clientes[1:], columns=data_clientes[0])
    
    ws_cajas = doc.worksheet(WORKSHEET_CAJAS)
    data_cajas = ws_cajas.get_all_values()
    df_cajas = pd.DataFrame(data_cajas[1:], columns=data_cajas[0])
    
    return df_clientes, df_cajas

try:
    df_clientes, df_cajas = load_data()
    client = get_sheet_client()
    ws_clientes = client.open_by_key(SHEET_ID).worksheet(WORKSHEET_CLIENTES)
except Exception as e:
    st.error(f"Error de conexión. Contactá a oficina. Detalle: {e}")
    st.stop()

# --- INTERFAZ MÓVIL ---
st.title("📍 Fusión Campo")
st.markdown("Captura de coordenadas de Clientes")
st.markdown("---")

# Inicializamos el estado para la búsqueda si no existe
if "busqueda_cliente" not in st.session_state:
    st.session_state.busqueda_cliente = ""

# ==========================================
# GESTIÓN DE CLIENTES
# ==========================================
busqueda = st.text_input(
    "🔍 Buscar Cliente (Nombre o Nº)", 
    placeholder="Ej: Juan Perez o 9944", 
    key="busqueda_cliente"
).strip().upper()

if busqueda:
    mask = df_clientes["Nº Cliente"].astype(str).str.contains(busqueda, case=False) | \
           df_clientes["Nombre"].astype(str).str.contains(busqueda, case=False)
    resultados = df_clientes[mask]
    
    if resultados.empty:
        st.warning("No se encontraron clientes.")
    else:
        opciones = resultados.apply(lambda x: f"{x['Nº Cliente']} - {x['Nombre']}", axis=1).tolist()
        cliente_sel = st.selectbox("Seleccioná el cliente correcto:", opciones)
        
        idx_sel = opciones.index(cliente_sel)
        cliente_row = resultados.iloc[idx_sel]
        
        # ID para controlar el reseteo
        id_cliente = str(cliente_row.get('Nº Cliente', idx_sel))
        row_idx = cliente_row.name + 2 
        
        caja_actual = str(cliente_row.get("Caja NAP", "")).replace("nan", "")
        
        st.info(f"**Dirección:** {cliente_row.get('Dirección', 'S/D')} \n\n"
                f"**Caja actual:** {caja_actual if caja_actual else 'Ninguna'} | **Precinto:** {cliente_row.get('N° de Precinto', 'Ninguno')}")
        
        lat_actual = str(cliente_row.get("Latitud", "")).strip()
        lon_actual = str(cliente_row.get("Longitud", "")).strip()
        
        if lat_actual and lon_actual and lat_actual not in ["nan", "None", ""] and lon_actual not in ["nan", "None", ""]:
            st.warning(f"⚠️ **Atención:** Este cliente ya tiene una ubicación guardada ({lat_actual}, {lon_actual}). Si guardás ahora, la ubicación anterior será reemplazada.")
            st.markdown(f"🗺️ **[Ver ubicación actual en Google Maps](https://www.google.com/maps?q={lat_actual},{lon_actual})**")
        
        if "ultimo_cliente" not in st.session_state:
            st.session_state.ultimo_cliente = id_cliente
            st.session_state.refresh_gps = False

        if st.session_state.ultimo_cliente != id_cliente:
            st.session_state.ultimo_cliente = id_cliente
            st.session_state.refresh_gps = True
            st.rerun()

        if st.session_state.refresh_gps:
            st.session_state.refresh_gps = False
            st.info("🔄 Preparando satélite para el nuevo cliente...")
            st.rerun()
        else:
            st.markdown("### 1. 📡 OBTENER UBICACIÓN")
            st.markdown("**Presioná el botón debajo para leer el GPS de tu celular ⬇️**")
            
            loc = streamlit_geolocation() 
            
            if loc and 'latitude' in loc and loc['latitude'] is not None:
                st.success(f"📍 ¡Ubicación capturada lista para guardar!\n\nLat: {loc['latitude']} | Lon: {loc['longitude']}")
            else:
                st.error("❌ Esperando captura de GPS... Toca el botón de arriba.")
            
            with st.form(key=f"form_{id_cliente}"):
                st.markdown("### 2. Datos Complementarios (Opcional)")
                
                cajas_validas = [""] + sorted([str(c).strip() for c in df_cajas["N De Caja"].dropna().unique() if str(c).strip()])
                caja_idx = cajas_validas.index(caja_actual) if caja_actual in cajas_validas else 0
                
                precinto_actual = str(cliente_row.get("N° de Precinto", "")).replace("nan", "")
                nuevo_precinto = st.text_input("🔒 Precinto (Solo 7 números)", value=precinto_actual)
                nueva_caja = st.selectbox("📦 Caja NAP", options=cajas_validas, index=caja_idx)
                
                submit = st.form_submit_button("💾 Guardar Datos", use_container_width=True)
                
                if submit:
                    duplicado = False
                    if nuevo_precinto and nuevo_precinto != precinto_actual and len(nuevo_precinto) == 7:
                        duplicados_df = df_clientes[df_clientes["N° de Precinto"].astype(str).str.strip() == nuevo_precinto]
                        if not duplicados_df.empty:
                            cliente_dup = duplicados_df.iloc[0]
                            st.error(f"❌ El precinto {nuevo_precinto} ya lo tiene el cliente: {cliente_dup['Nº Cliente']} - {cliente_dup['Nombre']}.")
                            duplicado = True

                    if not loc or 'latitude' not in loc or loc['latitude'] is None:
                        st.error("❌ No podés guardar sin presionar el botón de ubicación primero.")
                    elif nuevo_precinto and (not nuevo_precinto.isdigit() or len(nuevo_precinto) != 7):
                        st.error("❌ El precinto debe tener exactamente 7 números.")
                    elif not duplicado:
                        updates = [
                            {"range": f"J{row_idx}", "values": [[str(loc['latitude'])]]},
                            {"range": f"K{row_idx}", "values": [[str(loc['longitude'])]]},
                            {"range": f"H{row_idx}", "values": [[ahora_argentina()]]}
                        ]
                        if nuevo_precinto != precinto_actual:
                            updates.append({"range": f"F{row_idx}", "values": [[nuevo_precinto]]})
                        if nueva_caja != caja_actual:
                            updates.append({"range": f"M{row_idx}", "values": [[nueva_caja]]})
                            
                        guardado_exitoso = False
                        max_intentos = 3
                        
                        for intento in range(max_intentos):
                            try:
                                ws_clientes.batch_update(updates, value_input_option='USER_ENTERED')
                                guardado_exitoso = True
                                break
                            except Exception as e:
                                if intento < max_intentos - 1:
                                    time.sleep(2)
                                else:
                                    st.error(f"❌ Error de red al guardar: {str(e)[:50]}... Intentá nuevamente.")
                        
                        if guardado_exitoso:
                            st.cache_data.clear()
                            st.success("✅ Datos guardados correctamente. Actualizando pantalla...")
                            
                            time.sleep(1.5)
                            st.session_state.busqueda_cliente = ""
                            st.session_state.ultimo_cliente = None
                            st.rerun()