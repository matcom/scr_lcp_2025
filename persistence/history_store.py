# Este archivo maneja el almacenamiento persistente del historial de mensajes y archivos intercambiados
# entre usuarios. El flujo de trabajo consiste en almacenar cada interacción en un archivo JSON,
# manteniendo un registro cronológico de todas las comunicaciones. Cada entrada incluye metadatos
# como tipo (mensaje/archivo), remitente, destinatario y timestamp. Los timestamps se manejan
# consistentemente en UTC para evitar problemas de zonas horarias.

import os
import json
from datetime import datetime, UTC
from typing import List, Dict, Any

# Clase principal para gestionar el almacenamiento del historial de comunicaciones
# Esta clase es fundamental para:
# 1. Mantener un registro persistente de todas las interacciones
# 2. Gestionar conversaciones privadas y globales
# 3. Manejar tanto mensajes de texto como transferencias de archivos
class HistoryStore:
    # Inicializa el almacén de historial con la ruta al archivo JSON
    # Crea el archivo y directorio si no existen para garantizar la persistencia
    def __init__(self, filename: str = "history.json"):
        folder = os.path.dirname(os.path.abspath(__file__))
        self.path = os.path.join(folder, filename)
        os.makedirs(folder, exist_ok=True)
        if not os.path.exists(self.path):
            with open(self.path, 'w', encoding='utf-8') as f:
                json.dump([], f)

    # Método interno para agregar una nueva entrada al historial
    # Maneja la conversión de timestamps y asegura la consistencia de los datos
    def _append(self, entry: Dict[str, Any]):
        try:
            history = self.load_raw()
        except Exception:
            history = []

        # Normalización del timestamp a formato ISO con zona horaria UTC
        # Esto es crucial para mantener consistencia temporal en la aplicación
        if isinstance(entry['timestamp'], datetime):
            if entry['timestamp'].tzinfo is None:
                entry['timestamp'] = entry['timestamp'].replace(tzinfo=UTC)
            entry['timestamp'] = entry['timestamp'].isoformat()

        history.append(entry)
        with open(self.path, 'w', encoding='utf-8') as f:
            json.dump(history, f, ensure_ascii=False, indent=2)

    # Agrega un mensaje de texto al historial
    # Los parámetros incluyen remitente, destinatario, contenido y timestamp
    def append_message(self, sender: str, recipient: str, message: str, timestamp: datetime):
        # Aseguramos consistencia temporal con UTC
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
            
        entry = {
            'type': 'message',
            'sender': sender,
            'recipient': recipient,
            'message': message,
            'timestamp': timestamp.isoformat()
        }
        self._append(entry)

    # Agrega un registro de transferencia de archivo al historial
    # Similar a append_message pero para archivos
    def append_file(self, sender: str, recipient: str, filename: str, timestamp: datetime):
        # Aseguramos consistencia temporal con UTC
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
            
        entry = {
            'type': 'file',
            'sender': sender,
            'recipient': recipient,
            'filename': filename,
            'timestamp': timestamp.isoformat()
        }
        self._append(entry)

    # Recupera la conversación completa con un peer específico
    # Esta función es crucial porque:
    # 1. Maneja tanto mensajes privados como globales
    # 2. Convierte timestamps a objetos datetime
    # 3. Filtra mensajes relevantes según el contexto
    def get_conversation(self, peer: str) -> List[Dict[str, Any]]:
        try:
            history = self.load_raw()
        except Exception as e:
            print(f"Error cargando historial: {e}")
            return []

        # Procesamiento de timestamps: convertimos strings ISO a datetime UTC
        # Esto es necesario para poder realizar operaciones temporales con los mensajes
        for item in history:
            if isinstance(item.get('timestamp'), str):
                try:
                    dt = datetime.fromisoformat(item['timestamp'])
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=UTC)
                    item['timestamp'] = dt
                except ValueError as e:
                    print(f"Error parseando timestamp: {e}")
                    item['timestamp'] = datetime.now(UTC)

        # Manejo especial para mensajes globales
        if peer == "*global*":
            return [
                item for item in history
                if item.get('recipient') == "*global*"
            ]

        # Filtrado de mensajes para conversaciones privadas
        # Incluye tanto mensajes directos como globales relevantes
        return [
            item for item in history
            if (
                # Mensajes privados con el peer
                item.get('sender') == peer or 
                item.get('recipient') == peer or
                # Mensajes globales (excepto los míos)
                (item.get('recipient') == "*global*" and item.get('sender') != peer)
            )
        ]

    # Carga el historial completo sin procesar
    # Esta función es importante porque:
    # 1. Proporciona acceso directo a los datos raw
    # 2. Mantiene los timestamps en formato ISO
    # 3. Maneja casos de archivo vacío o inexistente
    def load_raw(self) -> List[Dict[str, Any]]:
        if not os.path.exists(self.path):
            return []
        with open(self.path, 'r', encoding='utf-8') as f:
            content = f.read().strip()
            if not content:
                return []
            return json.loads(content)
