import streamlit as st
import pandas as pd
from google.oauth2 import service_account
import gspread
from streamlit_geolocation import streamlit_geolocation
from datetime import datetime
import pytz

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
def init_connection():
    creds = service_account.Credentials.from_service_account_info(
        st.secrets["gcp_service_account"],
        scopes=["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    )
    client = gspread.authorize(creds)
    return client.open_by_key(SHEET_ID)

@st.cache_data(ttl=60, show_spinner="Actualizando base...")
def load_data():
    doc = init_connection()
    ws_clientes = doc.worksheet(WORKSHEET_CLIENTES)
    ws_cajas = doc.worksheet(WORKSHEET_CAJAS)
    
    df_clientes = pd.DataFrame(ws_clientes.get_all_records(default_blank=""))
    df_cajas = pd.DataFrame(ws_cajas.get_all_records(default_blank=""))
    return ws_clientes, ws_cajas, df_clientes, df_cajas

try:
    ws_clientes, ws_cajas, df_clientes, df_cajas = load_data()
except Exception as e:
    st.error("Error de conexión. Contactá a oficina.")
    st.stop()

# --- INTERFAZ MÓVIL ---
st.title("📍 Fusión Campo")
st.markdown("Captura de coordenadas en terreno")

tipo_accion = st.radio("¿Qué vas a ubicar?", ["👤 Cliente", "📦 Caja NAP"], horizontal=True)
st.markdown("---")

# ==========================================
# GESTIÓN DE CLIENTES
# ==========================================
if tipo_accion == "👤 Cliente":
    busqueda = st.text_input("🔍 Buscar Cliente (Nombre o Nº)", placeholder="Ej: Juan Perez o 9944").strip().upper()
    
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
            
            # gspread usa índice base 1, +1 por el header = +2
            row_idx = cliente_row.name + 2 
            
            st.info(f"**Dirección:** {cliente_row.get('Dirección', 'S/D')} \n\n"
                    f"**Caja actual:** {cliente_row.get('Caja NAP', 'Ninguna')} | **Precinto:** {cliente_row.get('N° de Precinto', 'Ninguno')}")
            
            st.markdown("### 1. Capturar Satélite")
            loc = streamlit_geolocation()
            
            with st.form("form_cliente"):
                st.markdown("### 2. Datos Complementarios (Opcional)")
                
                cajas_validas = [""] + sorted([str(c).strip() for c in df_cajas["N De Caja"].dropna().unique() if str(c).strip()])
                
                precinto_actual = str(cliente_row.get("N° de Precinto", "")).replace("nan", "")
                caja_actual = str(cliente_row.get("Caja NAP", "")).replace("nan", "")
                caja_idx = cajas_validas.index(caja_actual) if caja_actual in cajas_validas else 0
                
                nuevo_precinto = st.text_input("🔒 Precinto (Solo 7 números)", value=precinto_actual)
                nueva_caja = st.selectbox("📦 Caja NAP", options=cajas_validas, index=caja_idx)
                
                submit = st.form_submit_button("💾 Guardar Ubicación", use_container_width=True)
                
                if submit:
                    if not loc or 'latitude' not in loc or loc['latitude'] is None:
                        st.error("❌ Presioná el botón de ubicación primero.")
                    elif nuevo_precinto and (not nuevo_precinto.isdigit() or len(nuevo_precinto) != 7):
                        st.error("❌ El precinto debe tener exactamente 7 números.")
                    else:
                        updates = [
                            {"range": f"J{row_idx}", "values": [[str(loc['latitude'])]]},
                            {"range": f"K{row_idx}", "values": [[str(loc['longitude'])]]},
                            {"range": f"H{row_idx}", "values": [[ahora_argentina()]]}
                        ]
                        if nuevo_precinto != precinto_actual:
                            updates.append({"range": f"F{row_idx}", "values": [[nuevo_precinto]]})
                        if nueva_caja != caja_actual:
                            updates.append({"range": f"M{row_idx}", "values": [[nueva_caja]]})
                            
                        ws_clientes.batch_update(updates)
                        st.cache_data.clear()
                        st.success("✅ Datos guardados correctamente.")

# ==========================================
# GESTIÓN DE CAJAS NAP
# ==========================================
elif tipo_accion == "📦 Caja NAP":
    busqueda_caja = st.text_input("🔍 Buscar Caja NAP", placeholder="Ej: NAP-001").strip().upper()
    
    if busqueda_caja:
        mask_caja = df_cajas["N De Caja"].astype(str).str.contains(busqueda_caja, case=False)
        resultados_cajas = df_cajas[mask_caja]
        
        if resultados_cajas.empty:
            st.warning("Caja NAP no encontrada. Comunicate con oficina.")
        else:
            opciones_cajas = resultados_cajas["N De Caja"].tolist()
            caja_sel = st.selectbox("Seleccioná la Caja:", opciones_cajas)
            
            idx_caja = opciones_cajas.index(caja_sel)
            caja_row = resultados_cajas.iloc[idx_caja]
            row_idx_caja = caja_row.name + 2
            
            st.info(f"**Barrio:** {caja_row.get('Barrio', 'S/D')} | **Splitter:** {caja_row.get('Splitter', 'S/D')}")
            
            st.markdown("### Capturar Satélite")
            loc_caja = streamlit_geolocation()
            
            if st.button("💾 Guardar Ubicación Caja", use_container_width=True):
                if not loc_caja or 'latitude' not in loc_caja or loc_caja['latitude'] is None:
                    st.error("❌ Presioná el botón de ubicación primero.")
                else:
                    updates_caja = [
                        {"range": f"D{row_idx_caja}", "values": [[str(loc_caja['latitude'])]]},
                        {"range": f"E{row_idx_caja}", "values": [[str(loc_caja['longitude'])]]}
                    ]
                    ws_cajas.batch_update(updates_caja)
                    st.cache_data.clear()
                    st.success("✅ Ubicación de Caja NAP guardada.")