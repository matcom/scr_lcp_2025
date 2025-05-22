# core/engine.py

import threading
import os
import sys

# Asegurar que PROJECT_ROOT esté en sys.path para importaciones relativas
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.discovery import Discovery
from core.messaging import Messaging
from persistence.peers_store import PeersStore
from persistence.history_store import HistoryStore


class Engine:
    """
    Orquesta Discovery, Messaging y persistencia.
    Filtra peers locales al cargar el JSON guardado.
    """

    def __init__(self,
                 user_id: bytes,
                 broadcast_interval: float = 1.0):
        # Normalizar user_id a 20 bytes
        if isinstance(user_id, str):
            user_id = user_id.encode('utf-8')
        raw_id = user_id
        self.user_id = raw_id.ljust(20, b'\x00')[:20]

        # Persistencia
        self.peers_store = PeersStore()      # persistence/peers.json
        self.history_store = HistoryStore()  # persistence/history.json

        # Discovery (broadcast periódico y persistencia)
        self.discovery = Discovery(
            user_id=self.user_id,
            broadcast_interval=broadcast_interval,
            peers_store=self.peers_store
        )

        # Cargar peers previos y filtrar la IP local
        loaded = self.peers_store.load()  # { uid_bytes: {'ip', 'last_seen'} }
        local_ip = self.discovery.local_ip
        filtered = {
            uid: info
            for uid, info in loaded.items()
            if info['ip'] != local_ip
        }
        self.discovery.peers.update(filtered)

        # Mensajería
        self.messaging = Messaging(
            user_id=self.user_id,
            discovery=self.discovery,
            history_store=self.history_store
        )

    def start(self):
        """
        Arranca el hilo de recepción de mensajes.
        Discovery ya inició broadcast y persistencia en su constructor.
        """
        recv_thread = threading.Thread(
            target=self.messaging.recv_loop,
            name="MessagingRecvLoop",
            daemon=True
        )
        recv_thread.start()
