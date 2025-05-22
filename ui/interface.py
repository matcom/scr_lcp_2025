# ui/interface.py

import os
import sys
import streamlit as st

# --- Ajuste de sys.path para importar core/ y persistence/ ---
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.engine import Engine

# --- Paso 1: pedir User ID con formulario ---
if 'user_id' not in st.session_state or not st.session_state['user_id']:
    st.title("LCP Chat Interface")
    with st.form("login_form"):
        st.text_input(
            "Ingresa tu User ID (max 20 caracteres):",
            key="input_user_id",
            max_chars=20
        )
        submitted = st.form_submit_button("Confirmar")
        if submitted and st.session_state["input_user_id"]:
            st.session_state['user_id'] = st.session_state["input_user_id"]
    st.stop()

user = st.session_state['user_id']

# --- Paso 2: arrancar Engine una sola vez ---
if 'engine' not in st.session_state:
    engine = Engine(user_id=user)
    engine.start()
    st.session_state['engine'] = engine
else:
    engine = st.session_state['engine']

# Obtener la IP usada
local_ip = engine.discovery.local_ip

# --- Paso 3: barra lateral ---
st.sidebar.title(f"Usuario: {user}")
# Mostrar IP en tamaño pequeño
st.sidebar.markdown(f"<p style='font-size:12px; color:gray;'>IP: {local_ip}</p>", unsafe_allow_html=True)

# Peers actualmente conectados
current_peers = [uid.decode('utf-8') for uid in engine.discovery.get_peers().keys()]

# Peers con historial (excluyendo al propio y actuales)
history = engine.history_store.load()
all_senders = {e['sender'] for e in history if e['sender'] != user}
previous_peers = sorted(all_senders - set(current_peers))

# Selección de peer
if 'selected_peer' not in st.session_state:
    st.session_state['selected_peer'] = (current_peers or previous_peers or [None])[0]

def on_select_current():
    st.session_state['selected_peer'] = st.session_state['current_dropdown']

def on_select_previous():
    st.session_state['selected_peer'] = st.session_state['previous_dropdown']

st.sidebar.subheader("Peers Conectados")
if current_peers:
    st.sidebar.selectbox(
        "Selecciona un peer actual",
        current_peers,
        key='current_dropdown',
        on_change=on_select_current
    )
else:
    st.sidebar.write("Ninguno")

st.sidebar.subheader("Peers Anteriores")
if previous_peers:
    st.sidebar.selectbox(
        "Selecciona un peer anterior",
        previous_peers,
        key='previous_dropdown',
        on_change=on_select_previous
    )
else:
    st.sidebar.write("Ninguno")

# --- Nueva sección: Mensaje Global ---
st.sidebar.subheader("Mensaje Global")
msg_global = st.sidebar.text_area(
    "Escribe tu mensaje global aquí:",
    key="global_message_input"
)
if st.sidebar.button("Enviar Mensaje Global"):
    if msg_global:
        engine.messaging.send_all(msg_global.encode('utf-8'))
        st.sidebar.success("Mensaje global enviado a todos los peers conectados")
        st.session_state["global_message_input"] = ""
    else:
        st.sidebar.error("Por favor escribe algo antes de enviar")

# --- Paso 4: cargar conversación histórica automáticamente ---
peer = st.session_state['selected_peer']
if peer:
    st.header(f"Chateando con: {peer}")

    # Obtener solo la conversación con este peer
    conv = engine.history_store.get_conversation(peer)

    for entry in conv:
        if entry['type'] == 'message':
            if entry['sender'] == peer:
                with st.chat_message(peer):
                    st.write(entry['message'])
            else:
                with st.chat_message("Yo", is_user=True):
                    st.write(entry['message'])
        elif entry['type'] == 'file':
            if entry['sender'] == peer:
                with st.chat_message(peer):
                    st.write(f"[Archivo recibido] {entry['filename']}")
            else:
                with st.chat_message("Yo", is_user=True):
                    st.write(f"[Archivo enviado] {entry['filename']}")

    # Entrada de nuevo mensaje fija al fondo
    msg = st.chat_input("Escribe tu mensaje...", key="new_message")
    if msg:
        engine.messaging.send(
            peer.encode('utf-8'),
            msg.encode('utf-8')
        )

else:
    st.write("Selecciona un peer en la barra lateral para comenzar a chatear.")
