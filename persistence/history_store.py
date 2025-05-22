# persistence/history_store.py

import os
import json
from datetime import datetime
from typing import List, Dict

class HistoryStore:
    """
    Historial de mensajes y archivos.
    - Cada entrada incluye sender, recipient, message/filename, timestamp.
    - Permite recuperar conversación con un peer específico.
    """

    def __init__(self, filename: str = "history.json"):
        folder = os.path.dirname(os.path.abspath(__file__))
        self.path = os.path.join(folder, filename)
        os.makedirs(folder, exist_ok=True)
        if not os.path.exists(self.path):
            with open(self.path, 'w', encoding='utf-8') as f:
                json.dump([], f)

    def _append(self, entry: Dict):
        history = self.load_raw()
        history.append(entry)
        with open(self.path, 'w', encoding='utf-8') as f:
            json.dump(history, f, ensure_ascii=False, indent=2)

    def append_message(self, sender: str, recipient: str, message: str, timestamp: datetime):
        entry = {
            'type': 'message',
            'sender': sender,
            'recipient': recipient,
            'message': message,
            'timestamp': timestamp.isoformat()
        }
        self._append(entry)

    def append_file(self, sender: str, recipient: str, filename: str, timestamp: datetime):
        entry = {
            'type': 'file',
            'sender': sender,
            'recipient': recipient,
            'filename': filename,
            'timestamp': timestamp.isoformat()
        }
        self._append(entry)

    def load_raw(self) -> List[Dict]:
        """Carga el JSON sin parsear timestamps."""
        with open(self.path, 'r', encoding='utf-8') as f:
            return json.load(f)

    def load(self) -> List[Dict]:
        """Carga y parsea timestamps a datetime."""
        data = self.load_raw()
        for e in data:
            e['timestamp'] = datetime.fromisoformat(e['timestamp'])
        return data

    def get_conversation(self, peer: str) -> List[Dict]:
        """
        Devuelve la conversación completa (mensajes y archivos)
        con `peer`, ordenada cronológicamente.
        """
        conv = [
            e for e in self.load()
            if (
                (e['type'] in ('message', 'file')) and
                (e['sender'] == peer or e['recipient'] == peer)
            )
        ]
        return sorted(conv, key=lambda e: e['timestamp'])
