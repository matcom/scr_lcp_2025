# persistence/peers_store.py

import os
import json
from datetime import datetime
from typing import Dict, Set


class PeersStore:
    """
    Guarda y carga el mapa de peers en JSON.
    Ahora incluye un método `clean()` que:
      - Elimina entradas cuya IP coincida con ips locales
      - Agrupa por IP y conserva sólo el peer con el último `last_seen`
    """

    def __init__(self, filename: str = "peers.json"):
        folder = os.path.dirname(os.path.abspath(__file__))
        self.path = os.path.join(folder, filename)
        os.makedirs(folder, exist_ok=True)
        if not os.path.exists(self.path):
            with open(self.path, 'w', encoding='utf-8') as f:
                json.dump({}, f)

    def save(self, peers: Dict[bytes, dict]):
        """Serializa y guarda peers (bytes→info) en JSON (str→info)."""
        serial = {}
        for uid, info in peers.items():
            uid_str = uid.decode('utf-8')
            serial[uid_str] = {
                'ip': info['ip'],
                'last_seen': info['last_seen'].isoformat()
            }
        with open(self.path, 'w', encoding='utf-8') as f:
            json.dump(serial, f, ensure_ascii=False, indent=2)

    def load(self) -> Dict[bytes, dict]:
        """Carga JSON y devuelve peers con claves en bytes y `last_seen` en datetime."""
        if not os.path.exists(self.path):
            return {}
        with open(self.path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        result = {}
        for uid_str, info in data.items():
            result[uid_str.encode('utf-8')] = {
                'ip': info['ip'],
                'last_seen': datetime.fromisoformat(info['last_seen'])
            }
        return result

    def clean(self, local_ips: Set[str]):
        """
        Filtra el fichero JSON para:
          1) Eliminar entradas cuya IP esté en local_ips.
          2) De duplicados (mismo IP), conservar sólo el peer
             con `last_seen` más reciente.
        Reescribe `peers.json` con el resultado limpio.
        """
        # Cargar raw JSON
        with open(self.path, 'r', encoding='utf-8') as f:
            raw: Dict[str, dict] = json.load(f)

        # Agrupar por IP, eligiendo el timestamp más reciente
        grouped: Dict[str, dict] = {}
        for uid_str, info in raw.items():
            ip = info.get('ip')
            ts = datetime.fromisoformat(info.get('last_seen'))
            # 1) Omitir IPs locales
            if ip in local_ips:
                continue
            # 2) Conservar sólo el más reciente para cada IP
            if ip not in grouped or ts > grouped[ip]['timestamp']:
                grouped[ip] = {'uid_str': uid_str, 'timestamp': ts}

        # Reconstruir dict limpio y guardar
        cleaned: Dict[str, dict] = {}
        for ip, data in grouped.items():
            cleaned[data['uid_str']] = {
                'ip': ip,
                'last_seen': data['timestamp'].isoformat()
            }

        with open(self.path, 'w', encoding='utf-8') as f:
            json.dump(cleaned, f, ensure_ascii=False, indent=2)
