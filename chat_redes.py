import socket
import threading
import time
import os
import json
from queue import Queue, Empty

# Configuración del sistema
UDP_PORT = 9990
TCP_PORT = 9990
USER_NAME = "Mat"
USER_ID = USER_NAME.encode("utf-8").ljust(20, b'\x00')[:20]
BROADCAST_IP = '192.168.186.255'  # Ajustar según tu red
MAX_HISTORY = 10

# Códigos de operación
ECHO_CODE = 0
MESSAGE_CODE = 1
FILE_CODE = 2
GROUP_CREATE_CODE = 3
GROUP_JOIN_CODE = 4
GROUP_MSG_CODE = 5

# Estructuras thread-safe
discovered_users = {}
groups = {}
message_queue = Queue()
input_queue = Queue()
print_queue = Queue()
history = {}
discovery_lock = threading.Lock()

# ================== FUNCIONES BÁSICAS ==================

def safe_print(message):
    print_queue.put(message)

def console_output():
    while True:
        try:
            msg = print_queue.get_nowait()
            print(f"\n{msg}\n> ", end='', flush=True)
        except Empty:
            time.sleep(0.1)

def build_packet(user_to, op_code, body_id=0, body_length=0):
    # Asegurar que user_to sea bytes (sin encode innecesario)
    if isinstance(user_to, str):
        user_to = user_to.encode('utf-8')  # Solo si es string
    user_to_padded = user_to.ljust(20, b'\x00')[:20]
    
    # Construir header
    header = (
        USER_ID +
        user_to_padded +
        bytes([op_code]) +
        bytes([body_id]) +
        body_length.to_bytes(8, 'big') +
        b'\x00'*50
    )
    
    return header
def build_echo_packet():
    return build_packet(b'\xFF'*20, ECHO_CODE)

# ================== MANEJO DE RED ==================

def udp_listener():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(('', UDP_PORT))

    while True:
        try:
            data, addr = sock.recvfrom(1024)
            
            if len(data) < 100:
                continue  # Ignorar paquetes inválidos

            # Decodificar USER_IDFrom eliminando bytes basura
            user_from_bytes = data[:20]
            user_from = user_from_bytes.decode('utf-8', errors='ignore').split('\x00')[0].strip() or "Desconocido"
            
            op_code = data[40]

            # Descubrimiento de usuarios (ECHO_CODE)
            if op_code == ECHO_CODE:
                with discovery_lock:
                    if user_from != USER_NAME and addr[0] not in discovered_users:
                        discovered_users[addr[0]] = user_from
                        safe_print(f"[+] Usuario detectado: {user_from} ({addr[0]})")
                        sock.sendto(bytes([0]) + USER_ID + b'\x00'*4, addr)

            # Mensajes privados (MESSAGE_CODE)
            elif op_code == MESSAGE_CODE:
                if len(data) == 100:  # Fase 1: Header
                    body_id = data[41]
                    body_length = int.from_bytes(data[42:50], 'big')
                    
                    # Responder OK al header
                    sock.sendto(bytes([0]) + USER_ID + b'\x00'*4, addr)
                    
                    try:
                        # Recibir cuerpo
                        sock.settimeout(7)
                        body_data, addr_body = sock.recvfrom(1024)
                        
                        # Validar y decodificar
                        if len(body_data) < 8:
                            raise ValueError("Cuerpo demasiado corto")
                            
                        message_id = body_data[:8]
                        message = body_data[8:].decode('utf-8', errors='ignore').split('\x00')[0].strip()
                        
                        # Verificar BodyId
                        if message_id[-1] != body_id:
                            raise ValueError("BodyId no coincide")
                        
                        safe_print(f"[MSG] {user_from}: {message}")
                        sock.sendto(bytes([0]), addr_body)
                        
                    except Exception as e:
                        safe_print(f"[!] Error: {str(e)}")
                        sock.sendto(bytes([2]), addr_body)
                    finally:
                        sock.settimeout(None)

        except Exception as e:
            safe_print(f"[!] Error UDP: {e}")

def tcp_file_server():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(('', TCP_PORT))
    sock.listen(5)

    while True:
        conn, addr = sock.accept()
        try:
            # Recibir FileId y datos
            file_id = int.from_bytes(conn.recv(8), 'big')
            file_data = b''
            
            # Recibir datos en bloques de 1024 bytes
            while True:
                chunk = conn.recv(1024)
                if not chunk:
                    break
                file_data += chunk
            
            # Crear nombre de archivo único
            filename = f"recibido_{addr[0]}_{file_id}.bin"
            with open(filename, 'wb') as f:
                f.write(file_data)
            
            safe_print(f"[FILE] Archivo recibido: {filename}")
            
            # Confirmar recepción (ResponseStatus=0)
            conn.send(b'\x00')
            
        except Exception as e:
            safe_print(f"[!] Error TCP: {e}")
            # Enviar ResponseStatus=2 si hay error
            conn.send(b'\x02')
        finally:
            conn.close()

def broadcast_discovery():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    
    while True:
        try:
            # Usar bytes directamente (sin encode)
            packet = build_packet(b'\xff'*20, ECHO_CODE)  # <--- ¡Corrección aquí!
            sock.sendto(packet, (BROADCAST_IP, UDP_PORT))
            time.sleep(5)
        except Exception as e:
            safe_print(f"[!] Error en broadcast discovery: {str(e)}")

# ================== GRUPOS ==================

def handle_group_packet(data, addr):
    try:
        group_name = data[41:61].decode('utf-8').strip('\x00')
        op_code = data[40]
        user_from = data[:20].decode('utf-8').strip('\x00')

        if op_code == GROUP_CREATE_CODE:
            if group_name not in groups:
                groups[group_name] = {
                    "admin": user_from,
                    "members": {user_from},
                    "ip_admin": addr[0]
                }
                safe_print(f"[+] Nuevo grupo: {group_name}")

        elif op_code == GROUP_JOIN_CODE:
            if group_name in groups:
                groups[group_name]["members"].add(user_from)
                safe_print(f"[+] {user_from} se unió al grupo")

        elif op_code == GROUP_MSG_CODE:
            message = data[61:].decode('utf-8').strip('\x00')
            safe_print(f"[GRUPO {group_name}] {user_from}: {message}")

    except Exception as e:
        safe_print(f"[!] Error en grupo: {e}")

# ================== INTERFAZ DE USUARIO ==================

def input_handler():
    while True:
        try:
            cmd = input("\n> ")
            input_queue.put(cmd)
        except:
            pass

def process_command(cmd):
    try:
        cmd = cmd.strip()
        if not cmd:
            return

        if cmd.lower() == 'exit':
            with open('history.json', 'w') as f:
                json.dump(history, f)
            os._exit(0)

        elif cmd.lower() == 'users':
            with discovery_lock:
                users = [f"- {name} ({ip})" for ip, name in discovered_users.items()]
                safe_print("Usuarios detectados:\n" + "\n".join(users))

        elif cmd.startswith('msg '):
            parts = cmd.split(' ', 2)
            if len(parts) < 3:
                safe_print("Formato: msg <usuario> <mensaje>")
                return
            _, user, message = parts
            send_message(user, message)

        elif cmd.startswith('file '):
            parts = cmd.split(' ', 2)
            if len(parts) < 3:
                safe_print("Formato: file <usuario> <ruta>")
                return
            _, user, path = parts
            send_file(user, path)

        elif cmd.startswith('crear_grupo '):
            group_name = cmd.split(' ', 1)[1]
            if group_name not in groups:
                header = build_packet(b'\xFF'*20, GROUP_CREATE_CODE, body_id=1, body_length=len(group_name), body=group_name.encode('utf-8'))
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.sendto(header, (BROADCAST_IP, UDP_PORT))
                groups[group_name] = {
                    "admin": USER_NAME,
                    "members": {USER_NAME},
                    "ip_admin": socket.gethostbyname(socket.gethostname())
                }
                safe_print(f"Grupo '{group_name}' creado")
            else:
                safe_print("[!] El grupo ya existe")

        elif cmd.startswith('unirse_grupo '):
            group_name = cmd.split(' ', 1)[1]
            if group_name in groups:
                header = build_packet(groups[group_name]["ip_admin"].encode('utf-8'), GROUP_JOIN_CODE, body_id=1, body_length=len(group_name), body=group_name.encode('utf-8'))
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.sendto(header, (groups[group_name]["ip_admin"], UDP_PORT))
                groups[group_name]["members"].add(USER_NAME)
                safe_print(f"Unido al grupo '{group_name}'")
            else:
                safe_print("[!] Grupo no encontrado")

        elif cmd.startswith('grupo '):
            parts = cmd.split(' ', 2)
            if len(parts) < 3:
                safe_print("Formato: grupo <nombre> <mensaje>")
                return
            group_name = parts[1]
            message = parts[2]
            if group_name in groups:
                header = build_packet(b'\xFF'*20, GROUP_MSG_CODE, body_id=2, body_length=len(message), body=message.encode('utf-8'))
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                sock.sendto(header, (BROADCAST_IP, UDP_PORT))
                safe_print(f"[Grupo {group_name}] Mensaje enviado")
            else:
                safe_print("[!] No estás en este grupo")

        elif cmd.startswith('broadcast '):
            message = cmd.split(' ', 1)[1]
            send_broadcast(message)

        else:
            safe_print("Comandos válidos: users | msg | file | crear_grupo | unirse_grupo | grupo | exit")

    except Exception as e:
        safe_print(f"[!] Error: {e}")

def send_message(receiver, message):
    try:
        with discovery_lock:
            ip = next(ip for ip, name in discovered_users.items() if name == receiver)

        # Generar IDs
        message_id = int(time.time() * 1000).to_bytes(8, 'big')
        body_id = message_id[-1]  # Último byte como BodyId
        msg_bytes = message.encode('utf-8')
        body_length = 8 + len(msg_bytes)  # 8 bytes ID + mensaje

        # Construir header
        header = build_packet(
            receiver.encode('utf-8'), 
            MESSAGE_CODE, 
            body_id=body_id, 
            body_length=body_length
        )

        # Configurar socket con reintentos
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.settimeout(7)  # Timeout aumentado

        # Fase 1: Header (3 reintentos)
        header_sent = False
        for attempt in range(3):
            try:
                sock.sendto(header, (ip, UDP_PORT))
                response, _ = sock.recvfrom(25)
                if response[0] == 0:
                    header_sent = True
                    break
                else:
                    safe_print(f"Error en header (intento {attempt+1}/3)")
            except socket.timeout:
                safe_print(f"Timeout header (intento {attempt+1}/3)")

        if not header_sent:
            raise Exception("Fallo en fase 1")

        # Fase 2: Cuerpo (3 reintentos)
        body = message_id + msg_bytes
        body_sent = False
        for attempt in range(3):
            try:
                sock.sendto(body, (ip, UDP_PORT))
                response, _ = sock.recvfrom(1)
                if response[0] == 0:
                    body_sent = True
                    break
                else:
                    safe_print(f"Error en cuerpo (intento {attempt+1}/3)")
            except socket.timeout:
                safe_print(f"Timeout cuerpo (intento {attempt+1}/3)")

        if not body_sent:
            raise Exception("Fallo en fase 2")

        safe_print(f"[✓] Mensaje a {receiver} entregado")

    except Exception as e:
        safe_print(f"[!] Error: {str(e)}")
    finally:
        if 'sock' in locals():
            sock.close()
        
def send_file(receiver, file_path):
    try:
        with discovery_lock:
            ip = next(ip for ip, name in discovered_users.items() if name == receiver)

        if not os.path.exists(file_path):
            raise FileNotFoundError("Archivo no existe")

        # Generar FileId de 8 bytes
        file_id = int(time.time() * 1000).to_bytes(8, 'big')
        file_size = os.path.getsize(file_path)

        # Fase 1: Header UDP
        sock_udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock_udp.settimeout(7)
        header = build_packet(
            receiver.encode('utf-8'), 
            FILE_CODE, 
            body_id=file_id[-1], 
            body_length=file_size
        )
        
        # 3 reintentos para header
        for _ in range(3):
            try:
                sock_udp.sendto(header, (ip, UDP_PORT))
                response, _ = sock_udp.recvfrom(25)
                if response[0] == 0: break
            except socket.timeout: continue

        # Fase 2: Datos TCP
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock_tcp:
            sock_tcp.settimeout(10)
            sock_tcp.connect((ip, TCP_PORT))
            sock_tcp.sendall(file_id)  # Enviar 8 bytes
            
            with open(file_path, 'rb') as f:
                while True:
                    chunk = f.read(1024)
                    if not chunk: break
                    sock_tcp.sendall(chunk)
            
            # Confirmación final
            if sock_tcp.recv(1) != b'\x00':
                raise Exception("Error en confirmación TCP")

        safe_print(f"[✓] Archivo enviado a {receiver}")

    except Exception as e:
        safe_print(f"[!] Error archivo: {str(e)}")

def send_broadcast(message):
    try:
        message_id = int(time.time() * 1000).to_bytes(8, 'big')
        body_id = message_id[-1]
        msg_bytes = message.encode('utf-8')
        body_length = 8 + len(msg_bytes)

        header = build_packet(
            b'\xFF'*20, 
            MESSAGE_CODE, 
            body_id=body_id, 
            body_length=body_length
        )
        body = message_id + msg_bytes

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.sendto(header + body, (BROADCAST_IP, UDP_PORT))
        safe_print("[✓] Broadcast enviado")

    except Exception as e:
        safe_print(f"[!] Error broadcast: {str(e)}")

# ================== INICIALIZACIÓN ==================

if __name__ == "__main__":
    # Cargar historial
    try:
        with open('history.json', 'r') as f:
            history = json.load(f)
    except FileNotFoundError:
        history = {}

    # Iniciar servicios
    threading.Thread(target=udp_listener, daemon=True).start()
    threading.Thread(target=tcp_file_server, daemon=True).start()
    threading.Thread(target=broadcast_discovery, daemon=True).start()
    threading.Thread(target=console_output, daemon=True).start()

    # Interfaz principal
    print(f"\n=== Chat LCP - {USER_NAME} ===")
    print("Comandos disponibles:")
    print("- users: Listar usuarios")
    print("- msg <usuario> <mensaje>: Mensaje privado")
    print("- file <usuario> <ruta>: Enviar archivo")
    print("- crear_grupo <nombre>: Crear grupo")
    print("- unirse_grupo <nombre>: Unirse a grupo")
    print("- grupo <nombre> <mensaje>: Mensaje grupal")
    print("- exit: Salir\n")

    threading.Thread(target=input_handler, daemon=True).start()

    while True:
        try:
            cmd = input_queue.get(timeout=1)
            process_command(cmd)
        except Empty:
            continue