# Este archivo implementa el mecanismo de descubrimiento de peers en la red local.
# El flujo de trabajo consiste en enviar periódicamente mensajes de Echo-Request por broadcast UDP,
# procesar las respuestas, y mantener un mapa actualizado de peers activos. El sistema filtra
# automáticamente las IPs locales para evitar auto-descubrimiento y maneja la persistencia de
# la información de peers.

import socket
import time
import threading
from datetime import datetime, UTC
import ipaddress

from core.protocol import (
    UDP_PORT,
    BROADCAST_UID,
    pack_header,
    unpack_header,
    pack_response,
    unpack_response,
    HEADER_SIZE,
    RESPONSE_SIZE
)

# Tiempo en segundos después del cual un peer se considera desconectado
# Este valor es crucial para la limpieza de peers inactivos
OFFLINE_THRESHOLD = 20.0

# Clase principal para el descubrimiento y seguimiento de peers en la red
# Esta clase es fundamental porque:
# 1. Mantiene un registro actualizado de peers activos
# 2. Maneja la comunicación UDP para descubrimiento
# 3. Gestiona la persistencia de información de peers
class Discovery:
    # Inicializa el sistema de descubrimiento
    # Parámetros:
    # - user_id: Identificador único del usuario
    # - broadcast_interval: Frecuencia de envío de Echo-Request
    # - peers_store: Componente para persistencia de peers
    def __init__(self,
                 user_id: bytes,
                 broadcast_interval: float = 1.0,
                 peers_store=None):
        # Preparación del identificador de usuario
        # Mantenemos versión raw y padded para diferentes usos
        self.raw_id   = user_id.rstrip(b'\x00')
        self.user_id  = self.raw_id.ljust(20, b'\x00')
        self.broadcast_interval = broadcast_interval
        self.peers_store       = peers_store

        # Detección y configuración de IPs locales
        # Intentamos usar preferentemente IPs en la subred 192.168.1.x
        hostname  = socket.gethostname()
        all_addrs = socket.gethostbyname_ex(hostname)[2]
        
        # Selección de IP principal para broadcast
        # Prioridad: 192.168.1.x > otras no-loopback > primera disponible
        self.local_ip = next(
            (ip for ip in all_addrs if ip.startswith("192.168.1.")),
            next((ip for ip in all_addrs if not ip.startswith("127.")), all_addrs[0])
        )
        print(f"IP seleccionada para broadcast: {self.local_ip}")
        
        # Registro de todas las IPs locales incluyendo loopback
        # Esto es importante para filtrar auto-descubrimiento
        self.local_ips = set(all_addrs) | {"127.0.0.1"}

        # Mapa de peers conocidos con su información
        # Estructura: {padded_peer_id: {'ip': str, 'last_seen': datetime}}
        self.peers = {}

        # Configuración del socket UDP para broadcast
        # Habilitamos reuso de dirección y capacidad de broadcast
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        
        # Intento de bind a la IP local seleccionada
        # Si falla, fallback a 0.0.0.0 (todas las interfaces)
        try:
            self.sock.bind((self.local_ip, UDP_PORT))
            print(f"Socket UDP vinculado a {self.local_ip}")
        except Exception as e:
            print(f"Error al vincular a {self.local_ip}, intentando con 0.0.0.0: {e}")
            self.sock.bind(('0.0.0.0', UDP_PORT))

        # Inicio de hilos de broadcast y persistencia
        threading.Thread(target=self._broadcast_loop, daemon=True).start()
        if self.peers_store:
            threading.Thread(target=self._persist_loop, daemon=True).start()

    # Obtiene información detallada de las interfaces de red
    # Esta función es importante para:
    # 1. Detectar todas las interfaces disponibles
    # 2. Obtener IPs y máscaras de red
    # 3. Manejar casos especiales de Windows
    def _get_network_interfaces(self):
        interfaces = []
        
        try:
            import subprocess
            output = subprocess.check_output('ipconfig /all', shell=True).decode('latin1')
            
            current_if = None
            for line in output.split('\n'):
                line = line.strip()
                
                if not line:
                    continue
                    
                if not line.startswith(' '):
                    current_if = {'name': line, 'ip': None, 'mask': None}
                    continue
                    
                if 'IPv4' in line and 'Address' in line:
                    try:
                        current_if['ip'] = line.split(':')[-1].strip()
                        interfaces.append(current_if)
                    except:
                        pass
                        
        except Exception as e:
            print(f"Error obteniendo interfaces: {e}")
            
        return interfaces

    # Bucle principal de broadcast
    # Envía Echo-Request periódicamente según broadcast_interval
    def _broadcast_loop(self):
        while True:
            self._do_broadcast()
            time.sleep(self.broadcast_interval)

    # Realiza el envío de un Echo-Request por broadcast
    # Esta función es crítica porque:
    # 1. Empaqueta el mensaje según el protocolo
    # 2. Maneja errores de envío
    # 3. Registra la actividad para debugging
    def _do_broadcast(self):
        pkt = pack_header(
            user_from=self.user_id,
            user_to=BROADCAST_UID,
            op_code=0
        )
        try:
            # Broadcast a toda la red local
            self.sock.sendto(pkt, ('255.255.255.255', UDP_PORT))
            print(f"Broadcast enviado desde {self.local_ip} con ID {self.raw_id}")
        except Exception as e:
            print(f"Error al enviar broadcast: {e}")

    # Fuerza un descubrimiento inmediato
    # Útil para actualizar rápidamente el estado de la red
    def force_discover(self):
        self._do_broadcast()

    # Bucle de persistencia de información de peers
    # Esta función es importante porque:
    # 1. Filtra peers locales periódicamente
    # 2. Actualiza el estado conectado/desconectado
    # 3. Persiste la información actualizada
    def _persist_loop(self):
        while True:
            time.sleep(5)
            now = datetime.now(UTC)
            to_save = {}
            for uid, info in self.peers.items():
                ip = info['ip']
                if ip in self.local_ips:
                    continue
                age = (now - info['last_seen']).total_seconds()
                status = 'connected' if age < OFFLINE_THRESHOLD else 'disconnected'
                
                # Conversión de identificador para almacenamiento
                key = uid.decode('utf-8', errors='ignore') if isinstance(uid, bytes) else uid
                
                to_save[key] = {
                    'ip':         ip,
                    'last_seen':  info['last_seen'],
                    'status':     status
                }
            try:
                self.peers_store.save(to_save)
            except Exception as e:
                print(f"Error guardando peers: {e}")

    # Procesa un Echo-Request recibido
    # Esta función es crítica porque:
    # 1. Filtra mensajes de IPs locales y propios
    # 2. Envía Echo-Reply al remitente
    # 3. Actualiza el mapa de peers
    def handle_echo(self, data: bytes, addr):
        try:
            hdr      = unpack_header(data[:HEADER_SIZE])
            raw_id   = hdr['user_from']                    # ID sin padding
            raw_peer = raw_id.ljust(20, b'\x00')           # ID con padding
            peer_ip  = addr[0]

            print(f"Echo recibido de {peer_ip} con ID {raw_id}")

            # Filtrado de auto-descubrimiento
            if peer_ip in self.local_ips or raw_id == self.raw_id:
                print(f"Ignorando echo de IP local o self: {peer_ip}")
                return

            # Envío de Echo-Reply
            try:
                resp = pack_response(0, self.user_id)
                self.sock.sendto(resp, addr)
                print(f"Respuesta echo enviada a {peer_ip}")
            except Exception as e:
                print(f"Error al enviar respuesta echo: {e}")
                return

            # Limpieza de registros antiguos para la misma IP
            for uid in list(self.peers):
                if self.peers[uid]['ip'] == peer_ip and uid != raw_peer:
                    del self.peers[uid]

            # Actualización del mapa de peers
            self.peers[raw_peer] = {
                'ip':        peer_ip,
                'last_seen': datetime.now(UTC)
            }
            print(f"Peer actualizado: {peer_ip}")
        except Exception as e:
            print(f"Error procesando echo: {e}")

    # Procesa un Echo-Reply recibido
    # Esta función es similar a handle_echo pero:
    # 1. Usa formato de respuesta específico
    # 2. Verifica el estado de la respuesta
    # 3. Actualiza el mapa de peers
    def handle_response(self, data: bytes, addr):
        try:
            resp     = unpack_response(data[:RESPONSE_SIZE])
            resp_id  = resp['responder']                   # ID sin padding
            raw_peer = resp_id.ljust(20, b'\x00')          # ID con padding
            peer_ip  = addr[0]

            print(f"Respuesta recibida de {peer_ip} con ID {resp_id}")

            # Filtrado de respuestas inválidas o propias
            if resp['status'] != 0 or peer_ip in self.local_ips or resp_id == self.raw_id:
                print(f"Ignorando respuesta de IP local o self: {peer_ip}")
                return

            # Limpieza de registros antiguos
            for uid in list(self.peers):
                if self.peers[uid]['ip'] == peer_ip and uid != raw_peer:
                    del self.peers[uid]

            # Actualización del mapa de peers
            self.peers[raw_peer] = {
                'ip':        peer_ip,
                'last_seen': datetime.now(UTC)
            }
            print(f"Peer actualizado desde respuesta: {peer_ip}")
        except Exception as e:
            print(f"Error procesando respuesta: {e}")

    # Obtiene el mapa filtrado de peers activos
    # Esta función es importante porque:
    # 1. Excluye peers con IPs locales
    # 2. Proporciona información actualizada
    # 3. Es la interfaz principal para otros módulos
    def get_peers(self) -> dict:
        return {
            uid: info
            for uid, info in self.peers.items()
            if info['ip'] not in self.local_ips
        }
