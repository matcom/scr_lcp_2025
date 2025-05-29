# Este archivo implementa la interfaz gráfica del sistema de chat usando Streamlit.
# El flujo de trabajo consiste en manejar la interacción con el usuario, mostrar mensajes
# y archivos, y coordinar las acciones con el motor principal del sistema. La interfaz
# se actualiza automáticamente y mantiene el estado de la sesión.

import os
import sys
import streamlit as st
from datetime import datetime, UTC
from streamlit_autorefresh import st_autorefresh

# Configuración del path para importaciones
# Asegura acceso a los módulos core y persistence
SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.engine import Engine

# Configuración de constantes del sistema
# Estos valores son críticos para:
# 1. Determinar el estado de conexión de peers
# 2. Limitar el tamaño de archivos
# 3. Mantener la interfaz actualizada
OFFLINE_THRESHOLD = 20.0  # Tiempo máximo sin respuesta (segundos)
MAX_UPLOAD_SIZE = 100 * 1024 * 1024  # Límite de tamaño de archivo (100 MB)
REFRESH_INTERVAL = 3000  # Intervalo de actualización de UI (ms)

# Sistema de autenticación
# Maneja el ingreso del identificador de usuario
# y mantiene la sesión activa
if 'user_id' not in st.session_state or not st.session_state['user_id']:
    st.title("LCP Chat Interface")
    with st.form("login_form"):
        st.text_input(
            "Ingresa tu User ID (max 20 caracteres):",
            key="input_user_id",
            max_chars=20
        )
        if st.form_submit_button("Confirmar") and st.session_state["input_user_id"]:
            st.session_state['user_id'] = st.session_state["input_user_id"]
    st.stop()

user = st.session_state['user_id']

# Inicialización del motor de comunicación
# Esta sección es crítica porque:
# 1. Establece la conexión con la red
# 2. Configura los componentes del sistema
# 3. Maneja errores de inicialización
if 'engine' not in st.session_state:
    try:
        engine = Engine(user_id=user)
        engine.start()
        st.session_state['engine'] = engine
    except Exception as e:
        st.error(f"Error al inicializar el chat: {e}")
        st.stop()
else:
    engine = st.session_state['engine']

# Configuración de actualización automática
# Mantiene la interfaz sincronizada con el estado del sistema
st_autorefresh(interval=REFRESH_INTERVAL, key="auto_refresh")

# Panel lateral de control
# Esta sección es importante porque:
# 1. Muestra información del usuario
# 2. Indica el estado de la conexión
# 3. Permite acciones de sistema
st.sidebar.title(f"Usuario: {user}")
st.sidebar.markdown(
    f"<p style='font-size:12px; color:gray;'>IP: {engine.discovery.local_ip}</p>",
    unsafe_allow_html=True
)

# Monitoreo del estado TCP
# Indica si el sistema puede recibir archivos
tcp_status = "🟢 TCP Activo" if engine.messaging.tcp_sock else "🔴 TCP Inactivo"
st.sidebar.markdown(f"<p style='font-size:12px;'>{tcp_status}</p>", unsafe_allow_html=True)

# Botón de descubrimiento manual de peers
# Permite forzar una búsqueda inmediata
if st.sidebar.button("🔍 Buscar Peers"):
    with st.sidebar.status("Buscando peers..."):
        engine.discovery.force_discover()
        st.sidebar.success("Búsqueda de peers completada")

# Gestión de peers y mapeo de identificadores
# Esta sección es fundamental porque:
# 1. Procesa la información de peers activos
# 2. Maneja la conversión de formatos de ID
# 3. Clasifica peers por estado de conexión
now = datetime.now(UTC)

raw_peers = engine.discovery.get_peers()  # Obtiene diccionario de peers activos

# Proceso de unificación de formatos de ID
# Esta sección es crítica porque:
# 1. Normaliza IDs en bytes y strings
# 2. Mantiene la consistencia de datos
# 3. Facilita la búsqueda y comparación
peers = []
for uid_key, info in raw_peers.items():
    if isinstance(uid_key, bytes):
        trimmed = uid_key.rstrip(b'\x00')
        name_str = trimmed.decode('utf-8', errors='ignore')
        uid_bytes = trimmed.ljust(20, b'\x00')
    else:
        name_str = uid_key
        b = name_str.encode('utf-8')
        trimmed = b[:20]
        uid_bytes = trimmed.ljust(20, b'\x00')
    peers.append((name_str, uid_bytes, info))

# Mapeo inverso para búsqueda rápida
# Permite convertir nombres a IDs binarios
reverse_map = {name: uid for name, uid, _ in peers}

# Clasificación de peers por estado
# Separa peers en:
# 1. Actuales (conectados recientemente)
# 2. Anteriores (sin actividad reciente)
current_peers = [
    name for name, _, info in peers
    if (now - info['last_seen']).total_seconds() < OFFLINE_THRESHOLD
]
previous_peers = [
    name for name, _, info in peers
    if (now - info['last_seen']).total_seconds() >= OFFLINE_THRESHOLD
]

# Interfaz de selección de peers
# Esta sección es importante porque:
# 1. Permite elegir destinatario
# 2. Separa peers activos e inactivos
# 3. Mantiene la selección en sesión
st.sidebar.subheader("Peers Conectados")
selected_current = st.sidebar.selectbox(
    "Selecciona un peer actual",
    sorted(current_peers) if current_peers else ["Ninguno"]
)
if selected_current == "Ninguno":
    selected_current = None

st.sidebar.subheader("Peers Anteriores")
selected_previous = st.sidebar.selectbox(
    "Selecciona un peer anterior",
    sorted(previous_peers) if previous_peers else ["Ninguno"]
)
if selected_previous == "Ninguno":
    selected_previous = None

peer_name = selected_current or selected_previous

# Sistema de mensajería global
# Esta sección implementa:
# 1. Campo de entrada de mensaje
# 2. Botón de envío
# 3. Manejo de errores y confirmaciones
st.sidebar.subheader("Mensaje Global")
msg_global = st.sidebar.text_area("Escribe tu mensaje global aquí:")
if st.sidebar.button("Enviar Mensaje Global"):
    if msg_global:
        with st.sidebar.status("Enviando mensaje global..."):
            try:
                engine.messaging.send_all(msg_global.encode('utf-8'))
                engine.history_store.append_message(
                    sender=user,
                    recipient="*global*",
                    message=msg_global,
                    timestamp=datetime.now(UTC)
                )
                st.sidebar.success("Mensaje global enviado")
            except Exception as e:
                st.sidebar.error(f"Error al enviar mensaje global: {e}")
    else:
        st.sidebar.error("Por favor escribe algo antes de enviar")

# Sistema de transferencia de archivos
# Esta sección es crítica porque:
# 1. Maneja la selección de archivos
# 2. Valida tamaños y formatos
# 3. Coordina la transferencia TCP
st.sidebar.subheader("Enviar Archivo")
if peer_name:
    uploaded = st.sidebar.file_uploader(
        "Selecciona un archivo",
        key="file_uploader",
        help=f"Tamaño máximo: {MAX_UPLOAD_SIZE/1024/1024:.1f} MB"
    )
    
    # Validación y procesamiento de archivo
    # Esta sección es importante porque:
    # 1. Verifica límites de tamaño
    # 2. Maneja la transferencia TCP
    # 3. Actualiza el historial
    if uploaded is not None:
        file_size = len(uploaded.getvalue())
        if file_size > MAX_UPLOAD_SIZE:
            st.sidebar.error(f"Archivo demasiado grande ({file_size/1024/1024:.1f} MB)")
        elif st.sidebar.button("Enviar Archivo"):
            with st.sidebar.status(f"Enviando archivo {uploaded.name}...") as status:
                try:
                    data = uploaded.getvalue()
                    uid_bytes = reverse_map[peer_name]
                    
                    status.update(label="Estableciendo conexión TCP...")
                    engine.messaging.send_file(uid_bytes, data, uploaded.name)
                    
                    engine.history_store.append_file(
                        sender=user,
                        recipient=peer_name,
                        filename=uploaded.name,
                        timestamp=datetime.now(UTC)
                    )
                    st.sidebar.success(f"Archivo '{uploaded.name}' enviado correctamente")
                except ConnectionError as e:
                    st.sidebar.error(f"Error de conexión: {e}")
                except TimeoutError as e:
                    st.sidebar.error(f"Timeout al enviar archivo: {e}")
                except Exception as e:
                    st.sidebar.error(f"Error al enviar archivo: {e}")
else:
    st.sidebar.info("Selecciona un peer para enviar archivos")

# Interfaz principal de chat
# Esta sección implementa:
# 1. Visualización de mensajes
# 2. Historial de conversaciones
# 3. Entrada de mensajes
st.header("Chat")

# Sección de mensajes globales
# Muestra todos los mensajes broadcast
# con formato especial para identificación
st.subheader("Mensajes Globales")
global_msgs = engine.history_store.get_conversation("*global*")
for e in global_msgs:
    is_me = (e['sender'] == user)
    left, right = st.columns([3, 3])
    if is_me:
        with right, st.chat_message("user"):
            st.write(f"[Global] {e['message']}")
    else:
        with left, st.chat_message(e['sender']):
            st.write(f"[Global] {e['message']}")

# Sección de chat privado
# Esta sección es crítica porque:
# 1. Muestra conversaciones individuales
# 2. Diferencia mensajes y archivos
# 3. Indica estados de transferencia
if peer_name:
    st.subheader(f"Chat con {peer_name}")
    private = engine.history_store.get_conversation(peer_name)
    
    # Filtrado de mensajes
    # Excluye mensajes globales ya mostrados
    private = [msg for msg in private if msg.get('recipient') != "*global*"]
    
    # Visualización de mensajes y archivos
    # Con formato diferenciado por tipo y origen
    for e in private:
        is_me = (e['sender'] == user)
        left, right = st.columns([3, 3])
        if is_me:
            with right, st.chat_message("user"):
                if e['type'] == 'message':
                    st.write(e['message'])
                else:
                    st.write(f"[Archivo] {e['filename']}")
                    # Indicador de transferencia reciente
                    if (now - e['timestamp']).total_seconds() < 30:
                        st.caption("✅ Transferido por TCP")
        else:
            with left, st.chat_message(e['sender']):
                if e['type'] == 'message':
                    st.write(e['message'])
                else:
                    st.write(f"[Archivo] {e['filename']}")
                    if (now - e['timestamp']).total_seconds() < 30:
                        st.caption("✅ Transferido por TCP")

    # Sistema de entrada de mensajes
    # Esta sección es importante porque:
    # 1. Maneja la entrada de texto
    # 2. Procesa el envío asíncrono
    # 3. Actualiza la interfaz en tiempo real
    txt = st.chat_input("Escribe tu mensaje...")
    if txt:
        st.session_state["__msg_pending__"] = txt

    # Procesamiento de mensajes pendientes
    # Maneja el envío y actualización del chat
    if "__msg_pending__" in st.session_state:
        m = st.session_state["__msg_pending__"]
        try:
            uid_bytes = reverse_map[peer_name]
            engine.messaging.send(uid_bytes, m.encode('utf-8'))
            engine.history_store.append_message(
                sender=user,
                recipient=peer_name,
                message=m,
                timestamp=datetime.now(UTC)
            )
            left, right = st.columns([3, 3])
            with right, st.chat_message("user"):
                st.write(m)
        except ConnectionError as e:
            st.error(f"Error de conexión: {e}")
        except TimeoutError as e:
            st.error(f"Timeout al enviar mensaje: {e}")
        except Exception as e:
            st.error(f"Error al enviar mensaje: {e}")
        finally:
            del st.session_state["__msg_pending__"]

else:
    st.write("Selecciona un peer en la barra lateral para comenzar a chatear.")
