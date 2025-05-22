# core/discovery.py

import socket
import time
import threading
from datetime import datetime

from core.protocol import (
    UDP_PORT,
    BROADCAST_UID,
    pack_header,
    unpack_header,
    pack_response,
    HEADER_SIZE
)


class Discovery:
    """
    Envía Echo-Request periódicos y mantiene el mapa de peers.
    La recepción de Echo-Request se procesa en handle_echo().
    """

    def __init__(self,
                 user_id: bytes,
                 broadcast_interval: float = 1.0,
                 peers_store=None):
        # UID sin padding y UID padded
        self.raw_id = user_id.rstrip(b'\x00')
        self.user_id = self.raw_id.ljust(20, b'\x00')

        self.broadcast_interval = broadcast_interval
        self.peers_store = peers_store

        # IP de la interfaz principal (normalmente Wi-Fi)
        hostname = socket.gethostname()
        self.local_ip = socket.gethostbyname(hostname)

        # Diccionario de peers descubiertos
        # { uid_bytes: {'ip': str, 'last_seen': datetime} }
        self.peers = {}

        # Socket UDP ligado a la IP local
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self.sock.bind((self.local_ip, UDP_PORT))

        # Hilo de broadcast
        threading.Thread(target=self._broadcast_loop, daemon=True).start()
        # Hilo de persistencia (si se proporcionó peers_store)
        if self.peers_store:
            threading.Thread(target=self._persist_loop, daemon=True).start()

    def _broadcast_loop(self):
        """Envía un Echo-Request de broadcast cada intervalo."""
        while True:
            pkt = pack_header(
                user_from=self.user_id,
                user_to=BROADCAST_UID,
                op_code=0
            )
            self.sock.sendto(pkt, ('<broadcast>', UDP_PORT))
            time.sleep(self.broadcast_interval)

    def _persist_loop(self):
        """Guarda el mapa de peers a disco cada 5 segundos."""
        while True:
            time.sleep(5)
            # Persistir sin limpiar; el Engine carga y filtra local_ip
            self.peers_store.save(self.peers)

    def handle_echo(self, data: bytes, addr):
        """
        Procesa un Echo-Request (op_code=0):
        - Responde con Echo-Reply.
        - Actualiza self.peers (ignorando a uno mismo y duplicados).
        """
        hdr = unpack_header(data[:HEADER_SIZE])
        peer_id = hdr['user_from']
        peer_ip = addr[0]

        # Ignorar si es uno mismo
        if peer_id == self.raw_id or peer_ip == self.local_ip:
            return

        # Responder Echo-Reply
        reply = pack_response(status=0, responder=self.user_id)
        self.sock.sendto(reply, addr)

        # Ignorar duplicados por IP
        if any(info['ip'] == peer_ip for info in self.peers.values()):
            return

        # Registrar nuevo peer
        self.peers[peer_id] = {
            'ip': peer_ip,
            'last_seen': datetime.utcnow()
        }

    def get_peers(self) -> dict:
        """Devuelve una copia del mapa actual de peers."""
        return self.peers.copy()
