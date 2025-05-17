from protocol import *
import socket
import threading
import time
from datetime import datetime, timedelta
import queue
import random
import logging
import os
from utils import get_optimal_thread_count, get_network_info

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(threadName)s] [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("LCP")


class Peer:
    def __init__(self, user_id):

        self._expected_message_bodies = {}
        self._expected_bodies_lock = threading.Lock()

        self.user_id_str, self.user_id = self._ensure_20_bytes_id(user_id)

        if len(self.user_id) != 20:
            logger.warning(
                f"Error crítico: ID no tiene exactamente 20 bytes (tiene {len(self.user_id)} bytes)"
            )
            if len(self.user_id) < 20:
                self.user_id = self.user_id + b" " * (20 - len(self.user_id))
            else:
                self.user_id = self.user_id[:20]
            try:
                self.user_id_str = self.user_id.decode("utf-8")
            except UnicodeDecodeError:
                self.user_id_str = "Unknown".ljust(20)
                self.user_id = self.user_id_str.encode("utf-8")

        original_id = user_id.strip()

        logger.info(
            f"Inicializando peer LCP con ID: '{original_id}' (ID normalizado: '{self.user_id_str}')"
        )
        logger.debug(
            f"ID codificado en bytes ({len(self.user_id)} bytes): {self.user_id.hex()}"
        )

        (
            self.message_workers_count,
            self.file_workers_count,
            self.max_concurrent_transfers,
        ) = get_optimal_thread_count()

        logger.info(
            f"Configurando {self.message_workers_count} hilos para mensajes (operaciones de red UDP)"
        )
        logger.info(
            f"Configurando {self.file_workers_count} hilos para transferencias (operaciones TCP y archivos)"
        )
        logger.info(
            f"Límite de transferencias concurrentes: {self.max_concurrent_transfers}"
        )

        self.peers = {}
        self._peers_lock = threading.Lock()

        self._udp_socket_lock = threading.Lock()
        self._tcp_socket_lock = threading.Lock()
        self._callback_lock = threading.Lock()
        self._conversation_locks = {}
        self._conversation_locks_lock = threading.Lock()

        self.udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self.udp_socket.bind(("0.0.0.0", UDP_PORT))
        logger.info(f"Socket UDP inicializado en 0.0.0.0:{UDP_PORT}")

        self.tcp_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.tcp_socket.bind(("0.0.0.0", TCP_PORT))
        self.tcp_socket.listen(self.max_concurrent_transfers)
        logger.info(f"Socket TCP inicializado en 0.0.0.0:{TCP_PORT}")

        udp_thread = threading.Thread(
            target=self._udp_listener, daemon=True, name="UDP-Listener"
        )
        udp_thread.start()
        logger.info("Hilo UDP-Listener iniciado")

        tcp_thread = threading.Thread(
            target=self._tcp_listener, daemon=True, name="TCP-Listener"
        )
        tcp_thread.start()
        logger.info("Hilo TCP-Listener iniciado")

        discovery_thread = threading.Thread(
            target=self._discovery_service, daemon=True, name="Discovery"
        )
        discovery_thread.start()
        logger.info("Servicio de autodescubrimiento iniciado")

        self.message_callbacks = []
        self.file_callbacks = []
        self.peer_discovery_callbacks = []

        self.file_progress_callbacks = []

        self.message_queue = queue.Queue()

        self.file_send_queue = queue.Queue()

        self.active_file_transfers = 0
        self._transfers_lock = threading.Lock()

        for i in range(self.message_workers_count):
            worker_name = f"Worker-{i+1}"
            threading.Thread(
                target=self._message_worker, daemon=True, name=worker_name
            ).start()
        logger.info(f"Worker de mensajes {worker_name} iniciado")

        for i in range(self.file_workers_count):
            worker_name = f"FileSender-{i+1}"
            threading.Thread(
                target=self._file_send_worker, daemon=True, name=worker_name
            ).start()
        logger.info(f"Worker de envío de archivos {worker_name} iniciado")

    def _build_header(self, user_to, operation, body_id=0, body_length=0):
        """Construye el header"""
        header = bytearray(100)
        header[0:20] = self.user_id
        if user_to is None:
            header[20:40] = BROADCAST_ID
            logger.debug(f"Configurando destino como BROADCAST en header")
        else:
            header[20:40] = user_to.encode("utf-8").ljust(20, b"\x00")
            logger.debug(f"Configurando destino como {user_to} en header)")

        header[40] = operation
        header[41] = body_id
        header[42:50] = body_length.to_bytes(8, "big")

        return header

    def _parse_header(self, data):
        """Parsea un header"""
        if len(data) < 100 or len(data) > 100:
            return None

        user_to_bytes = data[20:40]
        is_broadcast = all(b == 0xFF for b in user_to_bytes)

        if is_broadcast:
            user_to = BROADCAST_ID
        else:
            try:
                user_to = user_to_bytes.decode("utf-8").rstrip("\x00")
            except UnicodeDecodeError:
                logger.warning(f"Error decodificando campo user_to")
                user_to = user_to_bytes.decode("utf-8", errors="replace")

        try:
            user_from = data[0:20].decode("utf-8").rstrip("\x00")
        except UnicodeDecodeError:
            logger.warning(f"Error decodificando campo user_from")
            user_from = data[0:20].decode("utf-8", errors="replace")

        return {
            "user_from": user_from,
            "user_to": user_to,
            "operation": data[40],
            "body_id": data[41],
            "body_length": int.from_bytes(data[42:50], "big"),
        }

    def _send_response(self, addr, status, reason=None):
        """Envía una respuesta

        Args:
            addr: Tuple (IP, puerto) del destinatario
            status: Código de estado (0=OK, 1=Bad Request, 2=Internal Error)
            reason: Razón del error
        """
        response = bytearray(25)
        response[0] = status
        response[1:21] = self.user_id

        status_text = {
            RESPONSE_OK: "OK",
            RESPONSE_BAD_REQUEST: "BAD REQUEST",
            RESPONSE_INTERNAL_ERROR: "INTERNAL ERROR",
        }.get(status, f"UNKNOWN STATUS ({status})")

        if reason and status != RESPONSE_OK:
            logger.warning(
                f"Enviando respuesta {status_text} a {addr[0]}:{addr[1]} - Razón: {reason}"
            )
        else:
            logger.debug(f"Enviando respuesta {status_text} a {addr[0]}:{addr[1]}")

        logger.debug(
            f"Datos de respuesta: status={status}, userId={self.user_id_str.strip()} (bytes={response[1:21].hex()[:20]})"
        )

        try:
            self.udp_socket.sendto(response, addr)
        except Exception as e:
            logger.error(f"Error enviando respuesta a {addr[0]}:{addr[1]}: {e}")

    def _build_response(self, status, reason=None):
        """Construye respuesta

        Args:
            status: Código de estado (0=OK, 1=Bad Request, 2=Internal Error)
            reason: Razón del error (solo para logs, no se envía en el protocolo)

        Returns:
            bytearray: Respuesta formateada de 25 bytes
        """
        response = bytearray(25)
        response[0] = status
        response[1:21] = self.user_id

        status_text = {
            RESPONSE_OK: "OK",
            RESPONSE_BAD_REQUEST: "BAD REQUEST",
            RESPONSE_INTERNAL_ERROR: "INTERNAL ERROR",
        }.get(status, f"UNKNOWN STATUS ({status})")

        if reason and status != RESPONSE_OK:
            logger.warning(f"Construyendo respuesta {status_text} - Razón: {reason}")

        return response

    def _discovery_service(self):
        """Servicio periódico de autodescubrimiento"""
        while True:
            try:
                self.send_echo()
                self._cleanup_inactive_peers()
                time.sleep(10)
            except Exception as e:
                logger.error(
                    f"Error en servicio de autodescubrimiento: {e}", exc_info=True
                )
                time.sleep(5)

    def _cleanup_inactive_peers(self):
        """Limpia peers inactivos de la lista de peers conocidos y consolida duplicados"""
        now = datetime.now()
        inactive_peers = []
        consolidated_peers = []

        with self._peers_lock:
            normalized_peers = {}
            for user_id, (ip, last_seen) in list(self.peers.items()):
                normalized_id = self._normalize_user_id(user_id)

                if normalized_id in normalized_peers:
                    existing_ip, existing_time = normalized_peers[normalized_id]
                    if last_seen > existing_time:
                        normalized_peers[normalized_id] = (ip, last_seen)
                    self.peers.pop(user_id, None)
                    consolidated_peers.append((user_id, normalized_id))
                else:
                    normalized_peers[normalized_id] = (ip, last_seen)

            inactive = {
                normalized_id: (ip, last_seen)
                for normalized_id, (ip, last_seen) in normalized_peers.items()
                if now - last_seen > timedelta(seconds=90)
            }

            for normalized_id, (ip, last_seen) in inactive.items():
                to_remove = []
                for peer_id in self.peers.keys():
                    if self._normalize_user_id(peer_id) == normalized_id:
                        to_remove.append(peer_id)

                for peer_id in to_remove:
                    logger.info(
                        f"Peer inactivo eliminado: {normalized_id} (sin actividad por >90s)"
                    )
                    self.peers.pop(peer_id, None)
                    inactive_peers.append(normalized_id)

        for old_id, new_id in consolidated_peers:
            logger.debug(f"Consolidado peer duplicado: '{old_id}' -> '{new_id}'")

        if inactive_peers:
            with self._callback_lock:
                for user_id in inactive_peers:
                    for callback in self.peer_discovery_callbacks:
                        callback(user_id, False)

    def send_echo(self):
        """Operación 0: Echo-Reply para descubrimiento"""
        header = self._build_header(None, 0)

        logger.debug(
            f"Header ECHO construido manualmente: {header[0:20].hex()[:20]}... -> {header[20:40].hex()[:20]}... op={header[40]}"
        )
        logger.debug(
            f"Enviando ECHO con ID origen: '{self.user_id_str.strip()}' (bytes: {header[0:20].hex()[:20]})"
        )

        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as echo_socket:
            echo_socket.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            echo_socket.bind(("0.0.0.0", 0))

            for i in get_network_info():
                logger.info(f"Enviando ECHO (broadcast) a {i}:{UDP_PORT}")
                echo_socket.sendto(header, (i, UDP_PORT))

            echo_socket.settimeout(5)
            logger.info(f"Esperando respuestas al ECHO durante 5 segundos...")

            start_time = time.time()

            try:
                while time.time() - start_time < 5:
                    try:
                        resp_data, resp_addr = echo_socket.recvfrom(25)
                        if len(resp_data) == 25:
                            raw_status = resp_data[0]
                            user_id_bytes = resp_data[0:20]

                            if raw_status == 0:
                                user_id_bytes = resp_data[1:21]
                                logger.debug(
                                    f"Respuesta ECHO con formato correcto: status=0, ID sigue después"
                                )

                            user_id_bytes = user_id_bytes.rstrip(b"\x00")

                            try:
                                user_id = user_id_bytes.decode("utf-8")
                                user_id = self._normalize_user_id(user_id)
                            except UnicodeDecodeError:
                                logger.warning(f"Error decodificando ID de usuario")
                                continue

                            my_id = self._normalize_user_id(self.user_id_str)
                            if user_id == my_id:
                                logger.debug(
                                    f"Ignorando respuesta ECHO de nosotros mismos: {user_id}"
                                )
                                continue

                            logger.info(
                                f"Recibida respuesta ECHO de '{user_id}' desde {resp_addr[0]}:{resp_addr[1]}"
                            )
                            logger.debug(
                                f"Datos completos de respuesta: {resp_data.hex()}"
                            )

                            with self._peers_lock:
                                existing_peer = False

                                for existing_id in list(self.peers.keys()):
                                    if self._normalize_user_id(existing_id) == user_id:
                                        existing_peer = True
                                        self.peers[existing_id] = (
                                            resp_addr[0],
                                            datetime.now(),
                                        )
                                        break

                                if not existing_peer:
                                    is_new = True

                                    user_id_bytes_final = user_id.encode("utf-8")

                                    if len(user_id_bytes_final) > 20:
                                        user_id_bytes_final = user_id_bytes_final[:20]
                                        while True:
                                            try:
                                                user_id_final = (
                                                    user_id_bytes_final.decode("utf-8")
                                                )
                                                break
                                            except UnicodeDecodeError:
                                                user_id_bytes_final = (
                                                    user_id_bytes_final[:-1]
                                                )
                                                if len(user_id_bytes_final) == 0:
                                                    user_id_final = (
                                                        f"Unknown-{resp_addr[0]}"
                                                    )
                                                    user_id_bytes_final = (
                                                        user_id_final.encode("utf-8")[
                                                            :20
                                                        ]
                                                    )
                                                    break
                                    else:
                                        user_id_final = user_id.ljust(20)[:20]

                                    self.peers[user_id_final] = (
                                        resp_addr[0],
                                        datetime.now(),
                                    )

                                    logger.info(f"Nuevo peer descubierto: {user_id}")
                                    with self._callback_lock:
                                        for callback in self.peer_discovery_callbacks:
                                            callback(user_id.strip(), True)

                    except socket.timeout:
                        break

            except Exception as e:
                logger.error(f"Error procesando respuestas ECHO: {e}", exc_info=True)

            finally:
                logger.info(
                    f"Finalizada espera de respuestas ECHO. Tiempo total: {time.time() - start_time:.2f}s"
                )

        return

    def _udp_listener(self):
        """Escucha mensajes UDP de control"""
        logger.info("Iniciando escucha de mensajes UDP en puerto %d", UDP_PORT)
        while True:
            try:
                self.udp_socket.settimeout(None)

                data, addr = self.udp_socket.recvfrom(1024)
                logger.info(
                    f"UDP recibido: {len(data)} bytes desde {addr[0]}:{addr[1]}"
                )
                handler_thread = threading.Thread(
                    target=self._handle_udp_message,
                    args=(data, addr),
                    daemon=True,
                    name=f"UDPHandler-{addr[0]}:{addr[1]}",
                )
                handler_thread.start()
                logger.debug(
                    f"Lanzado hilo {handler_thread.name} para procesar mensaje UDP"
                )
            except socket.timeout:
                continue
            except Exception as e:
                logger.error(f"Error en UDP listener: {e}")
                time.sleep(0.1)

    def _handle_udp_message(self, data, addr):
        """Maneja un mensaje UDP en un hilo separado"""
        try:
            # Primero verificar si este paquete es un cuerpo de mensaje que estamos esperando
            # Para eso el tamaño debe ser mayor a 8 (tamaño del body_id)
            if len(data) > 8:
                try:
                    body_id = int.from_bytes(data[:8], "big")

                    with self._expected_bodies_lock:
                        key = f"{addr[0]}:{body_id}"
                        if key in self._expected_message_bodies:
                            self._expected_message_bodies[key]["data"] = data
                            self._expected_message_bodies[key]["received"] = True
                            self._expected_message_bodies[key]["event"].set()

                            thread_name = threading.current_thread().name
                            logger.debug(
                                f"{thread_name} recibió cuerpo de mensaje con ID {body_id} de {addr[0]}, notificando al hilo de procesamiento"
                            )
                            return

                except Exception as e:
                    logger.debug(f"Error verificando si es un cuerpo de mensaje: {e}")

            if len(data) > 100:
                return self._send_response(addr, RESPONSE_BAD_REQUEST)
            elif len(data) < 100:
                logger.warning(
                    f"Recibido mensaje UDP con formato desconocido desde {addr[0]}:{addr[1]} (tamaño: {len(data)} bytes)"
                )
                return self._send_response(addr, RESPONSE_BAD_REQUEST)

            header = self._parse_header(data)

            sender_id = self._normalize_user_id(header["user_from"])
            my_id = self._normalize_user_id(self.user_id_str)

            if sender_id == my_id:
                logger.debug(f"Ignorando mensaje propio desde {addr[0]}:{addr[1]}")
                return

            with self._peers_lock:
                clean_id_exists = False
                for existing_id in list(self.peers.keys()):
                    if self._normalize_user_id(existing_id) == sender_id:
                        self.peers[self._normalize_user_id(existing_id)] = (
                            addr[0],
                            datetime.now(),
                        )
                        clean_id_exists = True
                        break
                is_new = not clean_id_exists
                if is_new:

                    sender_bytes = header["user_from"].encode("utf-8")

                    if len(sender_bytes) > 20:
                        sender_bytes = sender_bytes[:20]
                        while True:
                            try:
                                normalized_id = sender_bytes.decode("utf-8")
                                break
                            except UnicodeDecodeError:
                                sender_bytes = sender_bytes[:-1]
                                if len(sender_bytes) == 0:
                                    normalized_id = f"Unknown-{addr[0]}".ljust(20)[:20]
                                    break
                    else:
                        normalized_id = header["user_from"].ljust(20)[:20]

                    self.peers[normalized_id] = (addr[0], datetime.now())
                status_text = "nuevo" if is_new else "existente"
                logger.info(
                    f"Peer {status_text} registrado: {sender_id} en {addr[0]}:{addr[1]}"
                )

            if is_new:
                for callback in self.peer_discovery_callbacks:
                    callback(header["user_from"], True)

            operation_type = "desconocida"
            if header["operation"] == 0:
                operation_type = "ECHO"
                self._process_echo(header, addr)
            elif header["operation"] == 1:
                operation_type = "MENSAJE"
                self.message_queue.put(
                    {
                        "type": "message",
                        "header": header,
                        "addr": addr,
                    }
                )
            elif header["operation"] == 2:
                operation_type = "ARCHIVO"
                self.message_queue.put(
                    {
                        "type": "file",
                        "header": header,
                        "addr": addr,
                    }
                )

            logger.info(
                f"Recibido mensaje de tipo {operation_type} (op={header['operation']}) de {header['user_from']}"
            )

            if header["operation"] > 0:
                logger.debug(
                    f"Cola de mensajes: aproximadamente {self.message_queue.qsize()} tareas pendientes"
                )

        except Exception as e:
            logger.error(f"Error procesando mensaje UDP: {e}")

    def _tcp_listener(self):
        """Escucha conexiones TCP para transferencia de archivos"""
        logger.info(f"Iniciando escucha de conexiones TCP en puerto {TCP_PORT}")
        while True:
            try:
                conn, addr = self.tcp_socket.accept()
                logger.info(f"Nueva conexión TCP desde {addr[0]}:{addr[1]}")
                handler_thread = threading.Thread(
                    target=self._handle_file_transfer,
                    args=(conn, addr),
                    daemon=True,
                    name=f"FileHandler-{addr[0]}:{addr[1]}",
                )
                handler_thread.start()
                logger.info(
                    f"Lanzado hilo {handler_thread.name} para manejar transferencia de archivo"
                )
            except Exception as e:
                logger.error(f"Error en TCP listener: {e}", exc_info=True)

    def _process_echo(self, header, addr):
        """Procesa operación 0: Echo-Reply para autodescubrimiento"""
        user_from = header["user_from"]
        worker_name = threading.current_thread().name

        logger.info(
            f"{worker_name} procesando ECHO de {user_from} desde {addr[0]}:{addr[1]}"
        )

        expected_recipient = self.user_id_str.rstrip("\x00")
        user_to = header["user_to"]

        if user_to != BROADCAST_ID and user_to != expected_recipient:
            logger.debug(
                f"{worker_name} ignorando ECHO para otro destinatario: {user_to}"
            )
            return

        with self._udp_socket_lock:
            logger.debug(f"{worker_name} enviando respuesta a ECHO de {user_from}")

            echo_response = self._build_response(RESPONSE_OK)

            logger.debug(
                f"{worker_name} enviando respuesta ECHO: status={RESPONSE_OK}, ID={self.user_id_str.strip()} (bytes: {echo_response.hex()})"
            )

            try:
                self.udp_socket.sendto(echo_response, addr)
                logger.info(f"{worker_name} respuesta a ECHO enviada a {user_from}")
            except Exception as e:
                logger.error(f"{worker_name} error enviando respuesta ECHO: {e}")

    def _process_message(self, header, addr):
        """Procesa operación 1: Message-Response"""
        user_from = header["user_from"]
        worker_name = threading.current_thread().name
        logger.info(
            f"{worker_name} iniciando procesamiento de mensaje de {user_from} desde {addr[0]}:{addr[1]}"
        )

        with self._conversation_locks_lock:
            if user_from not in self._conversation_locks:
                logger.debug(f"Creando nuevo lock de conversación para {user_from}")
                self._conversation_locks[user_from] = threading.Lock()
            user_lock = self._conversation_locks[user_from]

        with user_lock:
            logger.debug(
                f"{worker_name} adquirió lock de conversación para {user_from}"
            )

            if not all(
                key in header
                for key in [
                    "user_from",
                    "user_to",
                    "operation",
                    "body_id",
                    "body_length",
                ]
            ):
                with self._udp_socket_lock:
                    logger.warning(
                        f"{worker_name} rechazando header de {user_from} por formato incorrecto"
                    )
                    self._send_response(
                        addr, RESPONSE_BAD_REQUEST, "Header incompleto o malformado"
                    )
                return

            expected_recipient = self.user_id_str.rstrip("\x00")
            if (
                header["user_to"] != expected_recipient
                and header["user_to"] != BROADCAST_ID
            ):
                with self._udp_socket_lock:
                    logger.warning(
                        f"{worker_name} rechazando mensaje para destinatario incorrecto: {header['user_to']}"
                    )
                    self._send_response(
                        addr,
                        RESPONSE_BAD_REQUEST,
                        f"Destinatario incorrecto: esperaba {expected_recipient}",
                    )
                return

            # Fase 1: Enviar confirmación del header
            with self._udp_socket_lock:
                logger.debug(
                    f"{worker_name} enviando confirmación de header (phase 1) a {addr[0]}:{addr[1]}"
                )
                self._send_response(addr, RESPONSE_OK)
                logger.info(f"{worker_name} confirmó recepción de header a {user_from}")

            try:
                # Fase 2: Recibir cuerpo del mensaje
                timeout_secs = 5
                expected_body_id = header["body_id"]
                expected_length = header["body_length"]

                message_wait_event = threading.Event()
                key = f"{addr[0]}:{expected_body_id}"

                with self._expected_bodies_lock:
                    self._expected_message_bodies[key] = {
                        "data": None,
                        "received": False,
                        "event": message_wait_event,
                        "timestamp": time.time(),
                    }
                    logger.debug(
                        f"{worker_name} registrando espera de cuerpo de mensaje con ID {expected_body_id} de {addr[0]}"
                    )

                # Fase 2: Esperamos por el evento de recepción del cuerpo
                received = message_wait_event.wait(timeout_secs)

                if not received:
                    logger.error(
                        f"{worker_name} timeout esperando cuerpo con ID {expected_body_id} de {addr[0]}"
                    )
                    with self._expected_bodies_lock:
                        if key in self._expected_message_bodies:
                            del self._expected_message_bodies[key]
                    raise socket.timeout("Timeout esperando cuerpo del mensaje")

                # Obtenemos los datos del cuerpo
                with self._expected_bodies_lock:
                    if key not in self._expected_message_bodies:
                        raise Exception(
                            f"Mensaje con ID {expected_body_id} no encontrado en el registro"
                        )

                    message_data = self._expected_message_bodies[key]
                    body_data = message_data["data"]
                    del self._expected_message_bodies[key]

                logger.info(
                    f"{worker_name} recibido cuerpo de mensaje: {len(body_data)} bytes"
                )
                msg_addr = (
                    addr[0],
                    addr[1],
                )

                logger.info(
                    f"{worker_name} recibió {len(body_data)} bytes de datos desde {msg_addr[0]}:{msg_addr[1]}"
                )

                if msg_addr[0] != addr[0]:
                    logger.warning(
                        f"{worker_name} detectó IP diferente en mensaje de datos: esperaba {addr[0]}, recibió {msg_addr[0]}"
                    )
                    with self._udp_socket_lock:
                        self._send_response(
                            addr,
                            RESPONSE_BAD_REQUEST,
                            "Origen del mensaje no coincide con el header",
                        )
                    return

                received_body_id = (
                    int.from_bytes(body_data[:8], "big") if len(body_data) >= 8 else -1
                )

                if received_body_id == expected_body_id.to_bytes(8, "big"):
                    logger.debug(
                        f"{worker_name} verificó BodyId correcto: {received_body_id}"
                    )

                    actual_length = len(body_data) - 8

                    if actual_length != expected_length:
                        logger.warning(
                            f"{worker_name} tamaño de mensaje incorrecto: esperaba {expected_length}, recibió {actual_length}"
                        )
                        with self._udp_socket_lock:
                            self._send_response(
                                addr,
                                RESPONSE_BAD_REQUEST,
                                "Tamaño de mensaje incorrecto",
                            )
                        return

                    try:
                        try:
                            message = body_data[8:].decode("utf-8")
                        except UnicodeDecodeError:
                            message = body_data[8:].decode("utf-8", errors="replace")
                            logger.warning(
                                f"{worker_name} mensaje con caracteres inválidos de {user_from}"
                            )

                        log_len = min(50, len(message))
                        log_preview = message[:log_len] + (
                            "..." if len(message) > log_len else ""
                        )
                        logger.info(
                            f"{worker_name} decodificó mensaje de {user_from}: {log_preview}"
                        )

                        if not message.strip():
                            logger.warning(
                                f"{worker_name} mensaje vacío recibido de {user_from}, ignorando"
                            )
                            with self._udp_socket_lock:
                                self._send_response(addr, RESPONSE_OK)
                            return

                        callbacks_count = len(self.message_callbacks)
                        if callbacks_count == 0:
                            logger.debug(
                                f"{worker_name} no hay callbacks registrados, mensaje ignorado"
                            )
                        else:
                            safe_user_from = user_from.strip()
                            with self._callback_lock:
                                logger.debug(
                                    f"{worker_name} notificando mensaje a {callbacks_count} callbacks"
                                )
                                for i, callback in enumerate(self.message_callbacks):
                                    try:
                                        callback(safe_user_from, message)
                                        if i == 0 or i == callbacks_count - 1:
                                            logger.debug(
                                                f"{worker_name} callback {i+1}/{callbacks_count} completado"
                                            )
                                    except Exception as cb_e:
                                        logger.error(
                                            f"{worker_name} error en callback {i+1}: {cb_e}",
                                            exc_info=True,
                                        )

                        # Fase 3: Confirmar recepción
                        with self._udp_socket_lock:
                            logger.debug(
                                f"{worker_name} enviando confirmación final (phase 3) a {addr[0]}:{addr[1]}"
                            )
                            self._send_response(addr, RESPONSE_OK)
                            logger.info(
                                f"{worker_name} completó procesamiento de mensaje de {user_from}"
                            )
                    except UnicodeDecodeError as ude:
                        logger.error(
                            f"{worker_name} error decodificando mensaje como UTF-8: {ude}"
                        )
                        with self._udp_socket_lock:
                            self._send_response(
                                addr,
                                RESPONSE_BAD_REQUEST,
                                "Error de codificación del mensaje",
                            )
                else:
                    logger.warning(
                        f"{worker_name} error de BodyId: esperaba {expected_body_id}, recibió {received_body_id}"
                    )
                    with self._udp_socket_lock:
                        self._send_response(
                            addr,
                            RESPONSE_BAD_REQUEST,
                            f"BodyId incorrecto: esperaba {expected_body_id}, recibió {received_body_id}",
                        )

            except socket.timeout:
                logger.error(
                    f"{worker_name} timeout esperando datos de mensaje de {user_from}"
                )
                with self._udp_socket_lock:
                    self._send_response(
                        addr,
                        RESPONSE_INTERNAL_ERROR,
                        "Timeout esperando datos del mensaje",
                    )
            except Exception as e:
                logger.error(
                    f"{worker_name} error procesando mensaje de {user_from}: {e}",
                    exc_info=True,
                )
                with self._udp_socket_lock:
                    self._send_response(
                        addr, RESPONSE_INTERNAL_ERROR, f"Error interno: {str(e)}"
                    )
            finally:
                if random.random() < 0.1:
                    logger.debug(
                        f"{worker_name} iniciando limpieza de locks de conversación antiguas"
                    )
                    self._cleanup_conversation_locks()

    def _process_file_request(self, header, addr):
        """Procesa operación 2: Send File-Ack"""
        user_from = header["user_from"]
        worker_name = threading.current_thread().name

        logger.info(f"{worker_name} procesando solicitud de archivo de {user_from}")

        if not all(
            key in header
            for key in ["user_from", "user_to", "operation", "body_id", "body_length"]
        ):
            with self._udp_socket_lock:
                logger.warning(
                    f"{worker_name} rechazando header de archivo de {user_from} por formato incorrecto"
                )
                self._send_response(
                    addr, RESPONSE_BAD_REQUEST, "Header incompleto o malformado"
                )
            return

        expected_recipient = self.user_id_str.rstrip("\x00")
        if header["user_to"] != expected_recipient:
            with self._udp_socket_lock:
                logger.warning(
                    f"{worker_name} rechazando archivo para destinatario incorrecto: {header['user_to']}"
                )
                self._send_response(
                    addr,
                    RESPONSE_BAD_REQUEST,
                    f"Destinatario incorrecto: esperaba {expected_recipient}",
                )
            return

        file_size = header["body_length"]
        if file_size <= 0:
            with self._udp_socket_lock:
                logger.warning(
                    f"{worker_name} rechazando archivo de tamaño inválido: {file_size} bytes"
                )
                self._send_response(
                    addr,
                    RESPONSE_BAD_REQUEST,
                    f"Tamaño de archivo inválido: {file_size} bytes",
                )
            return

        with self._peers_lock:
            expected_file_id = header["body_id"]
            if not hasattr(self, "_expected_file_transfers"):
                self._expected_file_transfers = {}

            peer_ip = addr[0]
            self._expected_file_transfers[peer_ip] = {
                "body_id": expected_file_id,
                "file_size": file_size,
                "user_from": user_from,
                "timestamp": time.time(),
            }
            logger.info(
                f"{worker_name} registrando transferencia esperada de {user_from} con ID {expected_file_id}"
            )

        with self._udp_socket_lock:
            logger.info(
                f"{worker_name} aceptando solicitud de archivo de {user_from}, tamaño: {file_size} bytes, ID: {expected_file_id}"
            )
        logger.info(
            f"{worker_name} esperando conexión TCP de {user_from} para transferencia de archivo con ID {expected_file_id}"
        )

    def _handle_file_transfer(self, conn, addr):
        """Maneja la transferencia de archivo por TCP"""
        worker_name = threading.current_thread().name
        logger.info(
            f"{worker_name} iniciando manejo de transferencia de archivo desde {addr[0]}:{addr[1]}"
        )

        try:
            file_id_bytes = conn.recv(8)
            if len(file_id_bytes) < 8:
                logger.error(
                    f"{worker_name} recibió identificador de archivo incompleto: {len(file_id_bytes)} bytes"
                )
                conn.send(
                    self._build_response(
                        RESPONSE_BAD_REQUEST, "ID de archivo incompleto"
                    )
                )
                conn.close()
                return

            file_id = int.from_bytes(file_id_bytes, "big")
            logger.info(f"{worker_name} recibió identificador de archivo: {file_id}")

            expected_transfer_info = None
            peer_id = None

            with self._peers_lock:
                if (
                    hasattr(self, "_expected_file_transfers")
                    and addr[0] in self._expected_file_transfers
                ):
                    expected_transfer_info = self._expected_file_transfers[addr[0]]

                for user_id, (ip, _) in self.peers.items():
                    if ip == addr[0]:
                        peer_id = user_id
                        break

            if not expected_transfer_info:
                logger.warning(
                    f"{worker_name} no hay transferencia esperada desde IP {addr[0]}, rechazando conexión"
                )
                conn.send(
                    self._build_response(
                        RESPONSE_BAD_REQUEST, "Transferencia no autorizada"
                    )
                )
                conn.close()
                return

            if expected_transfer_info["body_id"] != file_id:
                logger.warning(
                    f"{worker_name} ID de archivo incorrecto: esperado {expected_transfer_info['body_id']}, recibido {file_id}"
                )
                conn.send(
                    self._build_response(
                        RESPONSE_BAD_REQUEST, "ID de archivo incorrecto"
                    )
                )
                conn.close()
                return

            if not peer_id:
                logger.warning(
                    f"{worker_name} no pudo identificar peer con IP {addr[0]}, cerrando conexión"
                )
                conn.send(
                    self._build_response(RESPONSE_BAD_REQUEST, "Peer no identificado")
                )
                conn.close()
                return

            logger.info(
                f"{worker_name} identificó peer como {peer_id} para la transferencia de archivo con ID {file_id}"
            )

            timestamp = int(time.time())
            temp_file = f"lcp_file_{timestamp}_{peer_id}.dat"
            expected_size = expected_transfer_info["file_size"]

            try:
                logger.info(
                    f"{worker_name} creando archivo temporal: {temp_file}, tamaño esperado: {expected_size} bytes"
                )

                bytes_recibidos = 0
                with open(temp_file, "wb") as f:
                    logger.info(
                        f"{worker_name} iniciando recepción de datos de archivo"
                    )

                    while bytes_recibidos < expected_size:
                        bytes_restantes = expected_size - bytes_recibidos
                        chunk_size = bytes_restantes

                        data = conn.recv(chunk_size)
                        if not data:
                            logger.debug(
                                f"{worker_name} fin de transmisión detectado antes de completar"
                            )
                            break

                        f.write(data)
                        bytes_recibidos += len(data)

                        if bytes_recibidos % (1024 * 1024) < 4096:
                            progress = (
                                int((bytes_recibidos / expected_size) * 100)
                                if expected_size > 0
                                else 0
                            )
                            logger.info(
                                f"{worker_name} progreso: {bytes_recibidos/1024:.1f} KB ({progress}%) recibidos de {peer_id}"
                            )

            except IOError as e:
                logger.error(f"{worker_name} error de I/O escribiendo archivo: {e}")
                conn.send(
                    self._build_response(
                        RESPONSE_INTERNAL_ERROR, f"Error de I/O: {str(e)}"
                    )
                )
                return

            try:
                received_size = os.path.getsize(temp_file)
                if received_size != expected_size:
                    logger.error(
                        f"{worker_name} tamaño de archivo incorrecto: esperado {expected_size}, recibido {received_size}"
                    )
                    conn.send(
                        self._build_response(
                            RESPONSE_BAD_REQUEST,
                            f"Tamaño incorrecto: esperado {expected_size}, recibido {received_size}",
                        )
                    )
                    return

                logger.info(
                    f"{worker_name} transferencia completa: {bytes_recibidos} bytes recibidos en {temp_file}"
                )

                with self._peers_lock:
                    if (
                        hasattr(self, "_expected_file_transfers")
                        and addr[0] in self._expected_file_transfers
                    ):
                        del self._expected_file_transfers[addr[0]]

                logger.info(
                    f"{worker_name} notificando recepción de archivo a {len(self.file_callbacks)} callbacks"
                )
                for callback in self.file_callbacks:
                    callback(peer_id, temp_file)

                logger.debug(
                    f"{worker_name} enviando confirmación de recepción exitosa a {peer_id}"
                )
                conn.send(self._build_response(RESPONSE_OK))
                logger.info(
                    f"{worker_name} transferencia de archivo finalizada correctamente"
                )

            except Exception as e:
                logger.error(f"{worker_name} error verificando archivo recibido: {e}")
                conn.send(
                    self._build_response(
                        RESPONSE_INTERNAL_ERROR, f"Error interno: {str(e)}"
                    )
                )

        except ConnectionError as e:
            logger.error(f"{worker_name} error de conexión: {e}")
            try:
                conn.send(
                    self._build_response(
                        RESPONSE_INTERNAL_ERROR, f"Error de conexión: {str(e)}"
                    )
                )
            except:
                pass
        except Exception as e:
            logger.error(
                f"{worker_name} error en transferencia de archivo: {e}", exc_info=True
            )
            try:
                conn.send(
                    self._build_response(RESPONSE_INTERNAL_ERROR, f"Error: {str(e)}")
                )
            except:
                pass
        finally:
            conn.close()
            logger.debug(f"{worker_name} conexión TCP cerrada")

    def _cleanup_conversation_locks(self):
        """Limpia locks de conversaciones antiguas"""
        with self._conversation_locks_lock:
            if len(self._conversation_locks) > 100:
                keys_to_remove = random.sample(
                    list(self._conversation_locks.keys()),
                    len(self._conversation_locks) - 50,
                )
                for key in keys_to_remove:
                    del self._conversation_locks[key]

    def _message_worker(self):
        """Procesa mensajes de la cola"""
        worker_name = threading.current_thread().name

        while True:
            try:
                task = self.message_queue.get()
                header = task["header"]
                addr = task["addr"]

                logger.info(
                    f"{worker_name} procesando tarea de tipo '{task['type']}' de {header['user_from']} ({addr[0]}:{addr[1]})"
                )

                start_time = time.time()

                if task["type"] == "message":
                    logger.info(
                        f"{worker_name} procesando mensaje de {header['user_from']}"
                    )
                    self._process_message(header, addr)

                elif task["type"] == "file":
                    logger.info(
                        f"{worker_name} procesando solicitud de archivo de {header['user_from']}"
                    )
                    self._process_file_request(header, addr)

                process_time = time.time() - start_time
                logger.info(
                    f"{worker_name} completó procesamiento en {process_time:.3f} segundos"
                )

            except Exception as e:
                logger.error(f"{worker_name} error: {e}")
            finally:
                self.message_queue.task_done()
                logger.debug(
                    f"{worker_name} listo para siguiente tarea. Cola: aprox. {self.message_queue.qsize()} pendientes"
                )

    def _file_send_worker(self):
        """Procesa envíos de archivos de la cola"""
        worker_name = threading.current_thread().name

        while True:
            try:
                task = self.file_send_queue.get()
                user_to = task["user_to"]
                file_path = task["file_path"]

                logger.info(
                    f"{worker_name} procesando envío de archivo '{file_path}' a {user_to}"
                )

                start_time = time.time()

                with self._transfers_lock:
                    if self.active_file_transfers >= self.max_concurrent_transfers:
                        logger.warning(
                            f"{worker_name} alcanzó límite de transferencias concurrentes ({self.max_concurrent_transfers})"
                        )
                        self.file_send_queue.put(task)
                        time.sleep(1)
                        self.file_send_queue.task_done()
                        continue

                    self.active_file_transfers += 1
                    logger.info(
                        f"{worker_name} inicia transferencia ({self.active_file_transfers}/{self.max_concurrent_transfers} activas)"
                    )

                try:
                    success = self._send_file(user_to, file_path)

                    if success:
                        logger.info(
                            f"{worker_name} archivo enviado exitosamente a {user_to}"
                        )
                        with self._callback_lock:
                            for callback in self.file_progress_callbacks:
                                callback(
                                    user_to, file_path, 100, "completado"
                                )  # 100% completado
                    else:
                        logger.error(
                            f"{worker_name} error enviando archivo a {user_to}"
                        )
                        with self._callback_lock:
                            for callback in self.file_progress_callbacks:
                                callback(
                                    user_to, file_path, -1, "error"
                                )  # -1 indica error

                finally:
                    with self._transfers_lock:
                        self.active_file_transfers -= 1
                        logger.info(
                            f"{worker_name} finaliza transferencia ({self.active_file_transfers}/{self.max_concurrent_transfers} activas)"
                        )

                process_time = time.time() - start_time
                logger.info(
                    f"{worker_name} completó procesamiento en {process_time:.3f} segundos"
                )

            except Exception as e:
                logger.error(f"{worker_name} error: {e}", exc_info=True)
            finally:
                self.file_send_queue.task_done()
                logger.debug(
                    f"{worker_name} listo para siguiente tarea. Cola: aprox. {self.file_send_queue.qsize()} pendientes"
                )

    def send_message(self, user_to, message):
        """Envía un mensaje a otro peer"""
        logger.info(f"Intentando enviar mensaje a '{user_to}': {message[:50]}...")

        found_peer = None
        peer_addr = None

        with self._peers_lock:
            normalized_to = self._normalize_user_id(user_to)

            for peer_id, (ip, _) in self.peers.items():
                if self._normalize_user_id(peer_id) == normalized_to:
                    found_peer = peer_id
                    peer_addr = (ip, 9990)
                    break

            if not found_peer:
                logger.error(
                    f"No se puede enviar mensaje: peer '{user_to}' no encontrado"
                )
                return False

            logger.info(
                f"Peer '{user_to}' encontrado como '{found_peer}' en {peer_addr[0]}:{peer_addr[1]}"
            )

        message_id = int(time.time() * 1000) % 256
        message_bytes = message.encode("utf-8")
        expected_user_id = found_peer.encode("utf-8").ljust(20)[:20]

        with self._conversation_locks_lock:
            if found_peer not in self._conversation_locks:
                logger.debug(
                    f"Creando nuevo lock de conversación para envío a {found_peer}"
                )
                self._conversation_locks[found_peer] = threading.Lock()
            user_lock = self._conversation_locks[found_peer]

        with user_lock:
            logger.debug(f"Adquirido lock de conversación para envío a {found_peer}")
            with socket.socket(
                socket.AF_INET, socket.SOCK_DGRAM
            ) as conversation_socket:
                conversation_socket.bind(("0.0.0.0", 0))
                conversation_socket.settimeout(5)
                local_port = conversation_socket.getsockname()[1]
                logger.debug(
                    f"Socket temporal creado en puerto {local_port} para conversación con {found_peer}"
                )

                try:
                    # Fase 1: Enviar header
                    header = self._build_header(
                        found_peer, MESSAGE, message_id, len(message_bytes)
                    )
                    logger.info(
                        f"FASE 1: Enviando header LCP a {peer_addr[0]}:{peer_addr[1]} desde puerto {local_port}"
                    )

                    conversation_socket.sendto(header, peer_addr)
                    logger.debug(f"Header enviado, esperando respuesta (timeout: 5s)")

                    resp_data, resp_addr = conversation_socket.recvfrom(25)
                    logger.debug(
                        f"Respuesta recibida desde {resp_addr[0]}:{resp_addr[1]} ({len(resp_data)} bytes)"
                    )

                    if resp_data[0] != 0:
                        logger.error(
                            f"Respuesta negativa recibida: status={resp_data[0]}"
                        )
                        return False

                    logger.info(f"FASE 1 completada: header aceptado por {found_peer}")

                    # Fase 2: Enviar cuerpo del mensaje
                    body = message_id.to_bytes(8, "big") + message_bytes
                    logger.info(
                        f"FASE 2: Enviando cuerpo del mensaje a {peer_addr[0]}:{peer_addr[1]} ({len(body)} bytes) desde puerto {local_port}"
                    )

                    conversation_socket.sendto(body, peer_addr)
                    logger.debug(
                        f"Cuerpo enviado, esperando confirmación final (timeout: 5s)"
                    )

                    resp_data, resp_addr = conversation_socket.recvfrom(25)
                    logger.debug(
                        f"Confirmación recibida desde {resp_addr[0]}:{resp_addr[1]}"
                    )

                    if resp_data[0] == 0:
                        logger.info(
                            f"FASE 2 completada: mensaje entregado exitosamente a {found_peer}"
                        )
                    else:
                        logger.error(
                            f"Error en confirmación final: status={resp_data[0]}"
                        )

                    return resp_data[0] == 0

                except socket.timeout:
                    logger.error(f"Timeout esperando respuesta de {found_peer}")
                    return False
                except Exception as e:
                    logger.error(
                        f"Error enviando mensaje a {found_peer}: {e}", exc_info=True
                    )
                    return False

    def send_file(self, user_to, file_path):
        """Envía un archivo a otro peer"""
        logger.info(f"Intentando enviar archivo '{file_path}' a '{user_to}'")

        found_peer = None
        peer_addr = None

        with self._peers_lock:
            normalized_to = self._normalize_user_id(user_to)

            for peer_id, (ip, _) in self.peers.items():
                if self._normalize_user_id(peer_id) == normalized_to:
                    found_peer = peer_id
                    peer_addr = (ip, UDP_PORT)
                    break

        if not found_peer:
            logger.error(f"No se puede enviar archivo: peer '{user_to}' no encontrado")
            return False

        logger.info(
            f"Peer '{user_to}' encontrado como '{found_peer}' en {peer_addr[0]}:{peer_addr[1]}"
        )

        if not os.path.exists(file_path):
            logger.error(f"No se puede enviar archivo: '{file_path}' no existe")
            return False

        self.file_send_queue.put({"user_to": found_peer, "file_path": file_path})
        logger.info(f"Archivo '{file_path}' añadido a la cola de envío")
        return True

    def _send_file(self, user_to, file_path):
        """Realiza el envío de un archivo a otro peer"""
        file_id = int(time.time() * 1000) % 256
        file_size = os.path.getsize(file_path)

        found_peer = None
        peer_addr = None

        with self._peers_lock:
            normalized_to = self._normalize_user_id(user_to)

            for peer_id, (ip, _) in self.peers.items():
                if self._normalize_user_id(peer_id) == normalized_to:
                    found_peer = peer_id
                    peer_addr = (ip, UDP_PORT)
                    break

        if not found_peer or not peer_addr:
            logger.error(
                f"No se puede enviar archivo: peer '{user_to}' no encontrado en el momento de envío"
            )
            return False

        worker_name = threading.current_thread().name

        with self._callback_lock:
            for callback in self.file_progress_callbacks:
                callback(user_to, file_path, 0, "iniciando")

        logger.info(
            f"{worker_name} enviando archivo a {user_to}: '{file_path}' (file_id: {file_id}, tamaño: {file_size} bytes)"
        )

        try:
            # Fase 1: Enviar header
            header = self._build_header(found_peer, 2, file_id, file_size)
            logger.info(
                f"{worker_name} FASE 1: Enviando header de archivo a {peer_addr[0]}:{peer_addr[1]}"
            )
            with self._udp_socket_lock:
                self.udp_socket.sendto(header, peer_addr)

            logger.info(
                f"{worker_name} FASE 1 completada: header de archivo aceptado por {found_peer}"
            )

            # Fase 2: Enviar archivo por TCP
            logger.info(
                f"{worker_name} FASE 2: Iniciando transferencia TCP con {peer_addr[0]}:{peer_addr[1]}"
            )
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                logger.debug(
                    f"{worker_name} Conectando a {peer_addr[0]}:{peer_addr[1]} para transferencia de archivo"
                )
                s.connect(peer_addr)

                # Enviar identificador de archivo
                logger.debug(
                    f"{worker_name} Enviando identificador de archivo: {file_id}"
                )
                s.send(file_id.to_bytes(8, "big"))

                # Transferir contenido del archivo
                bytes_enviados = 0
                with open(file_path, "rb") as f:
                    logger.info(
                        f"{worker_name} Iniciando transferencia de datos del archivo"
                    )
                    last_progress_update = 0

                    while True:
                        chunk = f.read(4096)
                        if not chunk:
                            break
                        s.send(chunk)
                        bytes_enviados += len(chunk)

                        if file_size > 0:
                            progress = min(100, int((bytes_enviados * 100) / file_size))

                            if (progress - last_progress_update >= 5) or (
                                bytes_enviados % (1024 * 1024) < 4096
                            ):
                                last_progress_update = progress
                                logger.info(
                                    f"{worker_name} Progreso: {bytes_enviados/1024:.1f} KB ({progress}%) enviados a {found_peer}"
                                )
                                with self._callback_lock:
                                    for callback in self.file_progress_callbacks:
                                        callback(
                                            found_peer, file_path, progress, "progreso"
                                        )

                logger.info(
                    f"{worker_name} Transferencia completa: {bytes_enviados} bytes enviados a {found_peer}"
                )

                logger.debug(
                    f"{worker_name} Esperando confirmación final de transferencia"
                )
                resp_data = s.recv(25)

                if resp_data[0] == 0:
                    logger.info(
                        f"{worker_name} FASE 2 completada: archivo entregado exitosamente a {found_peer}"
                    )
                    with self._callback_lock:
                        for callback in self.file_progress_callbacks:
                            callback(found_peer, file_path, 100, "completado")
                    return True
                else:
                    logger.error(
                        f"{worker_name} Error en confirmación final de archivo: status={resp_data[0]}"
                    )
                    return False

        except socket.timeout:
            logger.error(f"{worker_name} Timeout esperando respuesta de {found_peer}")
            with self._callback_lock:
                for callback in self.file_progress_callbacks:
                    callback(found_peer, file_path, 0, "error")
            return False
        except ConnectionError as e:
            logger.error(f"{worker_name} Error de conexión con {found_peer}: {e}")
            with self._callback_lock:
                for callback in self.file_progress_callbacks:
                    callback(found_peer, file_path, 0, "error")
            return False
        except Exception as e:
            logger.error(
                f"{worker_name} Error enviando archivo a {found_peer}: {e}",
                exc_info=True,
            )
            with self._callback_lock:
                for callback in self.file_progress_callbacks:
                    callback(found_peer, file_path, 0, "error")
            return False
        finally:
            self.udp_socket.settimeout(None)
            logger.debug(f"{worker_name} Socket UDP restaurado a modo no bloqueante")

    def broadcast_message(self, message):
        """Envía un mensaje a todos los peers con una única transmisión.
        Esto implementa la funcionalidad de mensajería uno-a-muchos.

        Args:
            message: Mensaje a enviar a todos los peers

        Returns:
            bool: True si el mensaje fue enviado, False en caso de error
        """
        logger.info(f"Iniciando envío de mensaje broadcast: {message[:50]}...")
        message_id = int(time.time() * 1000) % 256
        message_bytes = message.encode("utf-8")

        logger.info(
            f"Enviando broadcast (message_id: {message_id}, tamaño: {len(message_bytes)} bytes)"
        )

        try:
            # Fase 1: Enviar header con destino broadcast
            header = self._build_header(None, MESSAGE, message_id, len(message_bytes))
            logger.info("FASE 1: Enviando header LCP broadcast")

            broadcast_addresses = get_network_info()
            success = False

            with self._udp_socket_lock:
                for broadcast_addr in broadcast_addresses:
                    try:
                        self.udp_socket.sendto(header, (broadcast_addr, UDP_PORT))
                        logger.info(
                            f"Header broadcast enviado a {broadcast_addr}:{UDP_PORT}"
                        )
                        success = True
                    except Exception as e:
                        logger.error(f"Error enviando header a {broadcast_addr}: {e}")

                if not success:
                    logger.error(
                        "No se pudo enviar el header a ninguna dirección de broadcast"
                    )
                    return False

            # Fase 2: Enviar cuerpo del mensaje a broadcast
            body = message_id.to_bytes(8, "big") + message_bytes
            logger.info(
                f"FASE 2: Enviando cuerpo del mensaje broadcast ({len(body)} bytes)"
            )

            with self._udp_socket_lock:
                for broadcast_addr in broadcast_addresses:
                    try:
                        self.udp_socket.sendto(body, (broadcast_addr, UDP_PORT))
                        logger.info(
                            f"Cuerpo broadcast enviado a {broadcast_addr}:{UDP_PORT}"
                        )
                        success = True
                    except Exception as e:
                        logger.error(f"Error enviando cuerpo a {broadcast_addr}: {e}")

            logger.info("Mensaje broadcast enviado correctamente")
            return True

        except Exception as e:
            logger.error(f"Error enviando mensaje broadcast: {e}", exc_info=True)
            return False
        finally:
            self.udp_socket.settimeout(None)

    def register_message_callback(self, callback):
        """Registra una función para recibir mensajes"""
        self.message_callbacks.append(callback)

    def register_file_callback(self, callback):
        """Registra una función para recibir archivos"""
        self.file_callbacks.append(callback)

    def register_peer_discovery_callback(self, callback):
        """Registra una función para notificar cambios en pares"""
        self.peer_discovery_callbacks.append(callback)

    def register_file_progress_callback(self, callback):
        """Registra una función para recibir actualizaciones del progreso de transferencias de archivos.
        El callback debe aceptar (user_id, file_path, progress, status) donde:
        - user_id: ID del usuario remoto
        - file_path: ruta del archivo
        - progress: porcentaje de progreso (0-100) o -1 si hay error
        - status: cadena con el estado ('iniciando', 'progreso', 'completado', 'error')
        """
        self.file_progress_callbacks.append(callback)

    def get_peers(self):
        """Devuelve la lista de pares conocidos"""
        unique_peers = set()
        with self._peers_lock:
            my_normalized_id = self._normalize_user_id(self.user_id_str)

            for peer_id in self.peers.keys():
                normalized_id = self._normalize_user_id(peer_id)
                if normalized_id != my_normalized_id:
                    unique_peers.add(normalized_id)

        return list(unique_peers)

    def _normalize_user_id(self, user_id):
        """Normaliza un ID de usuario para comparaciones consistentes.

        Args:
            user_id: String con el ID de usuario a normalizar

        Returns:
            String con el ID normalizado (sin espacios en los extremos)
        """
        if not user_id:
            return ""

        return user_id.strip().rstrip("\x00")

    def _ensure_20_bytes_id(self, user_id):
        """Asegura que un ID tenga exactamente 20 bytes en UTF-8.

        Args:
            user_id: String con el ID de usuario a procesar

        Returns:
            Tuple[str, bytes]: (ID procesado como string, ID procesado como bytes)
        """
        clean_id = self._normalize_user_id(user_id)

        id_bytes = clean_id.encode("utf-8")

        if len(id_bytes) < 20:
            result_str = clean_id.ljust(20)[:20]
            result_bytes = result_str.encode("utf-8")
            if len(result_bytes) > 20:
                result_bytes = result_bytes[:20]
                while True:
                    try:
                        result_str = result_bytes.decode("utf-8")
                        break
                    except UnicodeDecodeError:
                        result_bytes = result_bytes[:-1]
                        if len(result_bytes) == 0:
                            result_str = "Unknown"
                            result_bytes = result_str.encode("utf-8").ljust(20, b" ")[
                                :20
                            ]
                            break

        elif len(id_bytes) > 20:
            result_bytes = id_bytes[:20]

            try:
                result_str = result_bytes.decode("utf-8")
            except UnicodeDecodeError:
                while True:
                    result_bytes = result_bytes[:-1]
                    try:
                        result_str = result_bytes.decode("utf-8")
                        break
                    except UnicodeDecodeError:
                        if len(result_bytes) == 0:
                            result_str = "Unknown"
                            result_bytes = result_str.encode("utf-8").ljust(20, b" ")[
                                :20
                            ]
                            break

            if len(result_bytes) < 20:
                padding = b" " * (20 - len(result_bytes))
                result_bytes = result_bytes + padding
                result_str = result_bytes.decode("utf-8")
        else:
            result_str = clean_id
            result_bytes = id_bytes

        return result_str, result_bytes

    def close(self):
        """Cierra las conexiones"""
        self.udp_socket.close()
        self.tcp_socket.close()
