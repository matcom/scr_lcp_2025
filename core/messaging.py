# core/messaging.py

# Este archivo implementa el sistema de mensajería usando el protocolo LCP (Local Chat Protocol).
# El flujo de trabajo consiste en manejar la comunicación entre peers, incluyendo mensajes de texto
# y transferencia de archivos. Utiliza UDP para mensajes de control y TCP para transferencia de
# archivos. El sistema maneja reintentos, timeouts y confirmaciones para garantizar la entrega.

import threading
import socket
import os
from datetime import datetime, UTC
import queue
import time
import hashlib

from core.protocol import (
    UDP_PORT,
    BROADCAST_UID,
    pack_header,
    unpack_header,
    pack_response,
    unpack_response,
    HEADER_SIZE,
    RESPONSE_SIZE,
    TCP_PORT,
    pack_message_body,
    unpack_message_body,
    OP_MESSAGE,
    OP_FILE,
    RESP_OK,
    USER_ID_SIZE
)

# Clase principal para el manejo de mensajería entre peers
# Esta clase es fundamental porque:
# 1. Implementa el protocolo LCP para comunicación
# 2. Maneja tanto mensajes de texto como archivos
# 3. Garantiza la entrega confiable de mensajes
class Messaging:
    # Inicializa el sistema de mensajería
    # Parámetros:
    # - user_id: Identificador único del usuario
    # - discovery: Módulo de descubrimiento de peers
    # - history_store: Almacenamiento de historial
    def __init__(self, user_id: bytes, discovery, history_store):
        # Normalización del identificador de usuario
        # Aseguramos exactamente 20 bytes con padding
        if isinstance(user_id, str):
            user_id = user_id.encode('utf-8')
        self.raw_id = user_id.rstrip(b'\x00')[:USER_ID_SIZE]
        self.user_id = self.raw_id.ljust(USER_ID_SIZE, b'\x00')
        print(f"ID inicializado: raw={self.raw_id!r}, padded={self.user_id!r}")
        self.discovery = discovery
        self.history_store = history_store

        # Configuración del socket UDP para mensajes de control
        # Ajustamos timeouts y tamaños de buffer para mejor rendimiento
        self.sock = discovery.sock
        self.sock.setblocking(True)
        self.sock.settimeout(5.0)  # Timeout estándar de 5 segundos
        
        # Optimización de buffers para mejor rendimiento
        # 256KB para envío y recepción
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 262144)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 262144)

        # Configuración del socket TCP para archivos
        # Similar optimización de buffers que UDP
        self.tcp_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.tcp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 262144)
        self.tcp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 262144)
        self.tcp_sock.bind(('0.0.0.0', TCP_PORT))
        self.tcp_sock.listen(5)
        
        # Sistema de coordinación de confirmaciones (ACKs)
        # Usa eventos threading para sincronización
        self._acks = {}             # Mapeo de uid a evento de confirmación
        self._acks_lock = threading.Lock()
        
        # Gestión de IDs únicos para mensajes
        # Usa un contador cíclico de 0-255
        self._next_body_id = 0
        self._body_id_lock = threading.Lock()
        
        # Control de transferencias de archivos pendientes
        # Almacena headers temporalmente para validación
        self._pending_headers = {}  # Mapeo de body_id a (header, timestamp)
        self._pending_headers_lock = threading.Lock()
        
        # Cola de mensajes entrantes para procesamiento asíncrono
        self._message_queue = queue.Queue()
        
        # Inicio de hilos de mantenimiento
        # Limpieza de headers antiguos y procesamiento de mensajes
        threading.Thread(target=self._clean_pending_headers, daemon=True).start()
        threading.Thread(target=self._process_messages, daemon=True).start()

    # Limpieza periódica de headers pendientes
    # Esta función es importante porque:
    # 1. Evita acumulación de memoria
    # 2. Elimina headers obsoletos
    # 3. Mantiene el sistema limpio
    def _clean_pending_headers(self):
        while True:
            now = datetime.now(UTC)
            with self._pending_headers_lock:
                for body_id in list(self._pending_headers.keys()):
                    header, timestamp = self._pending_headers[body_id]
                    if (now - timestamp).total_seconds() > 30:
                        del self._pending_headers[body_id]
            threading.Event().wait(5)  # Pausa entre ciclos de limpieza

    # Genera un ID único para el cuerpo del mensaje
    # Esta función es crítica porque:
    # 1. Asegura IDs únicos para mensajes
    # 2. Mantiene el contador en rango válido
    # 3. Es thread-safe para uso concurrente
    def _get_next_body_id(self):
        with self._body_id_lock:
            body_id = self._next_body_id
            self._next_body_id = (self._next_body_id + 1) % 256  # Mantiene el ID en 1 byte
            return body_id

    # Envía datos y espera confirmación con reintentos
    # Esta función es fundamental porque:
    # 1. Implementa el mecanismo de confirmación
    # 2. Maneja reintentos y timeouts
    # 3. Garantiza la entrega confiable
    def _send_and_wait(self, data: bytes, recipient: bytes, timeout: float = 5.0, retries: int = 3):
        # Verificación del peer en el sistema de descubrimiento
        info = self.discovery.get_peers().get(recipient)
        if not info:
            raise ValueError("Peer no encontrado en discovery")
        dest = (info['ip'], UDP_PORT)

        # Preparación del evento de confirmación
        ev = threading.Event()
        key = recipient.rstrip(b'\x00')
        
        # Ciclo de reintentos de envío
        for attempt in range(retries):
            with self._acks_lock:
                self._acks[key] = ev
            
            try:
                self.sock.sendto(data, dest)
                received = ev.wait(timeout)
                
                if received:
                    return True
                    
            except socket.error as e:
                if attempt == retries - 1:
                    raise ConnectionError(f"Error de red al enviar a {recipient!r}: {e}")
            finally:
                with self._acks_lock:
                    self._acks.pop(key, None)
                    
            # Espera exponencial entre reintentos
            if attempt < retries - 1:
                threading.Event().wait(0.5 * (attempt + 1))
                
        raise TimeoutError(f"No se recibió ACK de {recipient!r} después de {retries} intentos")

    # Envía un mensaje de texto a un peer específico
    # Esta función es importante porque:
    # 1. Implementa el protocolo de mensajes
    # 2. Maneja la secuencia header-ACK-body-ACK
    # 3. Incluye reintentos automáticos
    def send(self, recipient: bytes, message: bytes, timeout: float = 5.0):
        # Preparación del mensaje y su identificador
        body_id = self._get_next_body_id()
        body = pack_message_body(body_id, message)
        
        # Construcción y envío del header
        header = pack_header(
            user_from=self.user_id,
            user_to=recipient,
            op_code=OP_MESSAGE,
            body_id=body_id,
            body_len=len(body)
        )
        try:
            self._send_and_wait(header, recipient, timeout)
            self._send_and_wait(body, recipient, timeout)
        except (TimeoutError, ConnectionError) as e:
            # Intento de redescubrimiento antes de fallar
            self.discovery.discover_peers()
            raise

    # Envía un archivo a un peer específico usando TCP
    # Esta función es crítica porque:
    # 1. Maneja transferencias grandes eficientemente
    # 2. Implementa el protocolo de archivos
    # 3. Coordina UDP (control) y TCP (datos)
    def send_file(self, recipient: bytes, file_bytes: bytes, filename: str, timeout: float = None):
        # Verificación del peer destino
        info = self.discovery.get_peers().get(recipient)
        if not info:
            raise ValueError("Peer no encontrado en discovery")
            
        # Preparación del identificador y datos del archivo
        body_id = self._get_next_body_id()
        print(f"Enviando archivo {filename} (body_id={body_id})")
        
        # Preparación y envío del header UDP
        # Según protocolo: BodyLength es el tamaño del archivo
        header = pack_header(
            user_from=self.user_id,
            user_to=recipient,
            op_code=OP_FILE,
            body_id=body_id,
            body_len=len(file_bytes)  # Tamaño total del archivo
        )
        
        try:
            # Envío del header y espera de confirmación
            self._send_and_wait(header, recipient, timeout or 5.0)
            print(f"Header UDP enviado, esperando ACK...")
            
            # Pausa para sincronización con receptor
            time.sleep(0.5)
            
            # Establecimiento de conexión TCP y transferencia
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                # Configuración del socket para transferencia eficiente
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 262144)  # Buffer optimizado
                sock.settimeout(timeout or 30.0)
                
                print(f"Conectando a {info['ip']}:{TCP_PORT}...")
                sock.connect((info['ip'], TCP_PORT))
                
                # Envío del ID del archivo (8 bytes)
                file_id_bytes = body_id.to_bytes(8, 'big')
                sock.send(file_id_bytes)
                
                # Transferencia del archivo en chunks para eficiencia
                sent = 0
                chunk_size = 32768  # 32KB
                while sent < len(file_bytes):
                    chunk = file_bytes[sent:sent + chunk_size]
                    bytes_sent = sock.send(chunk)
                    if bytes_sent == 0:
                        raise ConnectionError("Conexión cerrada durante envío")
                    sent += bytes_sent
                    print(f"Enviados {sent}/{len(file_bytes)} bytes")
                
                # Finalización de la transferencia y espera de confirmación
                print("Esperando ACK final...")
                sock.shutdown(socket.SHUT_WR)  # Señalización de fin de datos
                
                # Recepción de confirmación con timeout
                sock.settimeout(5.0)  # Timeout específico para ACK
                ack = sock.recv(RESPONSE_SIZE)
                if not ack or len(ack) != RESPONSE_SIZE:
                    raise ConnectionError(f"ACK inválido: recibidos {len(ack) if ack else 0} bytes")
                    
                try:
                    # Procesamiento de la confirmación
                    resp = unpack_response(ack)
                    print(f"ACK recibido: status={resp['status']}, responder={resp['responder']!r}")
                    
                    # Manejo de diferentes estados de respuesta
                    if resp['status'] == RESP_OK:
                        print("Archivo enviado correctamente")
                    elif resp['status'] == 1:  # Archivo existente
                        print("El archivo ya existe en el destino")
                    elif resp['status'] == 2:  # Error interno
                        raise ConnectionError("Error general en el receptor")
                    else:
                        raise ConnectionError(f"Estado de ACK desconocido: {resp['status']}")
                        
                except Exception as e:
                    print(f"Error decodificando ACK: {e}")
                    print(f"Bytes recibidos: {' '.join(f'{b:02x}' for b in ack)}")
                    raise
                    
        except Exception as e:
            print(f"Error en transferencia TCP: {e}")
            raise

    # Envía un mensaje a todos los peers excepto broadcast
    # Esta función es importante porque:
    # 1. Maneja la difusión de mensajes
    # 2. Ignora fallos individuales
    # 3. Excluye el UID de broadcast
    def broadcast(self, message: bytes):
        for peer_id in self.discovery.get_peers():
            if peer_id == BROADCAST_UID:
                continue
            try:
                self.send(peer_id, message)
            except:
                pass

    # Alias para envío de mensajes globales
    # Mantiene compatibilidad con versiones anteriores
    def send_all(self, message: bytes):
        return self.broadcast(message)

    # Inicia el hilo de escucha de mensajes
    # Configura el receptor como daemon para limpieza automática
    def start_listening(self):
        threading.Thread(target=self.recv_loop, daemon=True).start()

    # Procesa mensajes de la cola en segundo plano
    # Esta función es crítica porque:
    # 1. Maneja mensajes de forma asíncrona
    # 2. Evita bloqueos en el receptor
    # 3. Aísla errores de procesamiento
    def _process_messages(self):
        while True:
            try:
                hdr, body = self._message_queue.get()
                self._handle_message_or_file(hdr, body)
            except Exception as e:
                print(f"Error procesando mensaje de la cola: {e}")

    # Bucle principal de recepción de mensajes
    # Esta función es fundamental porque:
    # 1. Maneja todos los tipos de mensajes
    # 2. Coordina TCP y UDP
    # 3. Procesa confirmaciones
    def recv_loop(self):
        print("Iniciando loop de recepción de mensajes...")
        
        # Inicio del receptor TCP en hilo separado
        tcp_thread = threading.Thread(target=self._tcp_accept_loop, daemon=True)
        tcp_thread.start()
        
        while True:
            try:
                # Recepción de datos UDP
                data, addr = self.sock.recvfrom(4096)
                print(f"\nRecibidos {len(data)} bytes desde {addr[0]}")
                
                # Validación básica del paquete
                if len(data) < 1:
                    print("  - Paquete vacío, ignorando")
                    continue

                # Procesamiento de confirmaciones (ACKs)
                if len(data) == RESPONSE_SIZE:
                    try:
                        resp = unpack_response(data)
                        print(f"  - Es un ACK (status={resp['status']})")
                        if resp['status'] == 0:
                            r = resp['responder'].rstrip(b'\x00')
                            with self._acks_lock:
                                ev = self._acks.get(r)
                                if ev:
                                    print(f"  - ACK esperado de {r!r}, notificando")
                                    ev.set()
                                    continue
                                else:
                                    print(f"  - ACK no esperado de {r!r}")
                        self.discovery.handle_response(data, addr)
                    except Exception as e:
                        print(f"Error procesando ACK: {e}")
                    continue

                # Procesamiento de mensajes y archivos
                if len(data) < HEADER_SIZE:
                    print(f"  - Paquete demasiado corto para header ({len(data)} < {HEADER_SIZE})")
                    continue

                try:
                    # Decodificación y validación del header
                    hdr = unpack_header(data[:HEADER_SIZE])
                    print(f"  - Header decodificado: op={hdr['op_code']}, from={hdr['user_from']!r}, to={hdr['user_to']!r}")
                except Exception as e:
                    print(f"Error desempaquetando header: {e}")
                    continue

                # Manejo de pings de descubrimiento
                # Los pings son mensajes broadcast con op_code 0
                if hdr['op_code'] == 0 and hdr['user_to'] == BROADCAST_UID:
                    print("  - Es un ping de discovery")
                    self.discovery.handle_echo(data, addr)
                    continue

                # Validación de destinatario del mensaje
                # Determina si el mensaje es para este peer o es broadcast
                my_id = self.raw_id.rstrip(b' ')
                to_id = hdr['user_to'].rstrip(b' ')  # Sin padding nulo
                from_id = hdr['user_from'].rstrip(b' ')  # Sin padding nulo
                is_for_me = (to_id == my_id)
                is_broadcast = (to_id == BROADCAST_UID)
                
                # Logging detallado para debugging de IDs
                print(f"  - Destino: {'broadcast' if is_broadcast else ('para mí' if is_for_me else 'no es para mí')}")
                print(f"  - Mi ID (sin espacios): {my_id!r}")
                print(f"  - ID destino (sin espacios): {to_id!r}")
                print(f"  - ID origen (sin espacios): {from_id!r}")
                
                # Procesamiento de mensajes y archivos destinados a este peer
                if hdr['op_code'] in (OP_MESSAGE, OP_FILE) and (is_for_me or is_broadcast):
                    try:
                        print(f"Procesando mensaje de {addr[0]} tipo {hdr['op_code']} {'(broadcast)' if is_broadcast else ''}")
                        
                        # Envío de confirmación de recepción de header
                        self.sock.sendto(pack_response(0, self.user_id), addr)
                        print("  - ACK de header enviado")

                        # Manejo de mensajes de texto
                        if hdr['op_code'] == OP_MESSAGE:
                            # Preparación para recepción del cuerpo
                            body_len = hdr['body_len']
                            body = bytearray()
                            
                            try:
                                # Configuración de timeout para el cuerpo
                                self.sock.settimeout(5.0)
                                print(f"  - Esperando cuerpo del mensaje ({body_len} bytes)")
                                
                                # Recepción del cuerpo completo
                                # Buffer grande para minimizar fragmentación
                                chunk, _ = self.sock.recvfrom(65536)  # 64KB
                                if not chunk:
                                    raise ConnectionError("Conexión cerrada durante recepción")
                                    
                                print(f"    - Recibidos {len(chunk)} bytes")
                                
                                # Validación de integridad del mensaje
                                if len(chunk) != body_len:
                                    print(f"    - ADVERTENCIA: Tamaño recibido ({len(chunk)}) != esperado ({body_len})")
                                    
                                body.extend(chunk)
                                
                                # Confirmación de recepción del cuerpo
                                self.sock.sendto(pack_response(0, self.user_id), addr)
                                print("  - ACK de cuerpo enviado")
                                
                                # Encolado para procesamiento asíncrono
                                self._message_queue.put((hdr, bytes(body)))
                                print(f"  - Mensaje encolado para procesamiento")
                                
                            except socket.timeout:
                                print("Timeout recibiendo cuerpo del mensaje")
                                self.sock.sendto(pack_response(2, self.user_id), addr)
                            finally:
                                # Restauración del timeout por defecto
                                self.sock.settimeout(5.0)
                                
                        # Manejo de transferencias de archivos
                        elif hdr['op_code'] == OP_FILE:
                            # Rechazo de archivos broadcast por seguridad
                            if is_broadcast:
                                print("  - Ignorando archivo broadcast")
                                self.sock.sendto(pack_response(1, self.user_id), addr)
                                continue
                                
                            # Registro del header para la transferencia TCP
                            with self._pending_headers_lock:
                                self._pending_headers[hdr['body_id']] = (hdr, datetime.now(UTC))
                            print("  - Header guardado para transferencia TCP")
                            
                    except Exception as e:
                        print(f"Error procesando mensaje: {e}")
                        try:
                            self.sock.sendto(pack_response(2, self.user_id), addr)
                        except:
                            pass
                else:
                    print("  - Mensaje ignorado (no es para mí ni broadcast)")
            except socket.timeout:
                continue  # Timeout normal, continuar escuchando
            except Exception as e:
                print(f"Error en recv_loop: {e}")
                continue

    # Bucle de aceptación de conexiones TCP para archivos
    # Esta función es crítica porque:
    # 1. Maneja conexiones entrantes de archivos
    # 2. Crea hilos dedicados por transferencia
    # 3. Mantiene el sistema responsive
    def _tcp_accept_loop(self):
        while True:
            try:
                client_sock, addr = self.tcp_sock.accept()
                threading.Thread(
                    target=self._handle_tcp_file_transfer,
                    args=(client_sock, addr),
                    daemon=True
                ).start()
            except Exception as e:
                print(f"Error aceptando conexión TCP: {e}")
                continue

    # Sanitiza el nombre del archivo eliminando caracteres no válidos
    # Esta función es importante porque:
    # 1. Elimina caracteres nulos y no imprimibles
    # 2. Asegura nombres de archivo válidos
    # 3. Preserva el nombre original lo más posible
    def _sanitize_filename(self, filename: str) -> str:
        # Separar nombre y extensión
        name, ext = os.path.splitext(filename)
        
        # Si no tiene extensión, intentar detectar por contenido
        if not ext:
            ext = '.bin'  # Por defecto
            
        # Eliminar caracteres no imprimibles y nulos del nombre
        clean_name = ''.join(c for c in name if c.isalnum() or c in '- _')
        
        # Si el nombre quedó vacío, usar nombre genérico
        if not clean_name:
            clean_name = f"archivo_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}"
        
        # Asegurar que la extensión sea válida
        clean_ext = ext.lower()
        if not clean_ext.startswith('.'):
            clean_ext = '.' + clean_ext
            
        # Combinar nombre limpio y extensión
        return f"{clean_name}{clean_ext}"

    def _detect_file_type(self, data: bytes) -> str:
        """Detecta el tipo de archivo basado en sus bytes iniciales (magic numbers)"""
        # Diccionario de firmas de archivos comunes
        signatures = {
            # Documentos
            b'%PDF': '.pdf',
            b'\x50\x4B\x03\x04': '.docx',  # También puede ser .xlsx, .pptx, .zip
            bytes([0xD0, 0xCF, 0x11, 0xE0]): '.doc',  # Archivos antiguos de Office
            
            # Imágenes
            b'\xFF\xD8\xFF': '.jpg',
            b'\x89PNG\r\n\x1A\n': '.png',
            b'GIF87a': '.gif',
            b'GIF89a': '.gif',
            b'BM': '.bmp',
            
            # Texto y código
            b'<!DOCTYPE html': '.html',
            b'<html': '.html',
            b'<?xml': '.xml',
            b'{\n': '.json',  # Común en archivos JSON
            b'{\r\n': '.json',
            
            # Comprimidos
            b'\x1F\x8B\x08': '.gz',
            b'PK': '.zip',
            b'Rar!': '.rar',
            
            # Python
            b'#!/usr/bin/env python': '.py',
            b'# -*- coding': '.py'
        }
        
        # Convertir los primeros bytes a texto para detectar archivos de texto
        try:
            start = data[:min(len(data), 1024)].decode('utf-8')
            # Si se puede decodificar como UTF-8 y no tiene caracteres nulos, probablemente es texto
            if '\x00' not in start:
                return '.txt'
        except UnicodeDecodeError:
            pass
            
        # Revisar firmas conocidas
        for signature, extension in signatures.items():
            if data.startswith(signature):
                return extension
                
        # Si no se reconoce la firma, intentar detectar por contenido
        # Verificar si parece un archivo de texto a pesar de no estar en UTF-8
        text_chars = bytearray({7,8,9,10,12,13,27} | set(range(0x20, 0x100)) - {0x7f})
        is_binary = bool(data.translate(None, text_chars))
        if not is_binary:
            return '.txt'
            
        # Si no se puede determinar, usar .bin
        return '.bin'

    # Maneja la transferencia de un archivo por TCP
    # Esta función es fundamental porque:
    # 1. Implementa el protocolo de archivos
    # 2. Maneja la recepción por partes
    # 3. Valida la integridad de los datos
    def _handle_tcp_file_transfer(self, sock: socket.socket, addr):
        # Función auxiliar para lectura exacta de bytes
        def recv_exact(n):
            data = bytearray()
            while len(data) < n:
                chunk = sock.recv(n - len(data))
                if not chunk:
                    raise ConnectionError("Conexión cerrada durante recepción")
                data.extend(chunk)
            return bytes(data)

        try:
            # Recepción del identificador del archivo (8 bytes)
            file_id = int.from_bytes(recv_exact(8), 'big')
            print(f"ID de archivo recibido: {file_id}")
            
            # Validación contra headers pendientes
            with self._pending_headers_lock:
                if file_id not in self._pending_headers:
                    print(f"No hay header pendiente para file_id={file_id}")
                    sock.send(pack_response(2, self.user_id))  # Error
                    return
                    
                hdr, _ = self._pending_headers[file_id]
                del self._pending_headers[file_id]  # Limpiar header usado

            # Recepción del contenido del archivo
            # Según protocolo: BodyLength es el tamaño exacto del archivo
            body_len = hdr['body_len']  # Ya no restamos 8 bytes
            if body_len <= 0:
                print(f"Tamaño de archivo inválido: {body_len}")
                sock.send(pack_response(2, self.user_id))
                return

            # Recepción por chunks para archivos grandes
            body = bytearray()
            chunk_size = 32768  # 32KB
            received = 0
            
            print(f"Iniciando recepción de {body_len} bytes...")
            while received < body_len:
                remaining = body_len - received
                current_chunk = min(chunk_size, remaining)
                chunk = recv_exact(current_chunk)
                if not chunk:
                    raise ConnectionError("Conexión cerrada durante recepción")
                body.extend(chunk)
                received += len(chunk)
                if received % (1024 * 1024) == 0:  # Reportar progreso cada 1MB
                    print(f"Recibidos {received}/{body_len} bytes ({(received/body_len)*100:.1f}%)")

            body = bytes(body)  # Convertir a bytes inmutables
            print(f"Recepción completa: {len(body)} bytes")

            # Detectar el tipo de archivo
            extension = self._detect_file_type(body)
            print(f"Tipo de archivo detectado: {extension}")

            # Preparación del directorio de descargas
            downloads_dir = os.path.join(os.getcwd(), "Descargas")
            os.makedirs(downloads_dir, exist_ok=True)

            # Generar nombre de archivo con la extensión correcta
            timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
            filename = f"archivo_{timestamp}_{file_id & 0xFF}{extension}"
            path = os.path.join(downloads_dir, filename)

            # Guardar el archivo
            with open(path, 'wb') as f:
                f.write(body)
            print(f"Archivo guardado como {filename}")

            # Enviar confirmación según protocolo
            sock.send(pack_response(0, self.user_id))
            print("ACK enviado")

            # Registro en el historial de transferencias
            self.history_store.append_file(
                sender=addr[0],
                recipient=self.raw_id.decode('utf-8'),
                filename=filename,
                timestamp=datetime.now(UTC)
            )

        except Exception as e:
            print(f"Error en transferencia TCP: {e}")
            try:
                # Notificación de error al remitente
                sock.send(pack_response(2, self.user_id))  # Status 2 = Error
            except:
                pass
            raise
        finally:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except:
                pass
            sock.close()

    def _handle_message_or_file(self, hdr, body: bytes):
        """Procesa un mensaje o archivo recibido"""
        try:
            # Normalización de identificadores
            # Elimina padding y espacios para comparación
            peer_id = hdr['user_from'].rstrip(b'\x00')
            my_id = self.raw_id.rstrip(b' ')
            to_id = hdr['user_to'].rstrip(b' ')
            from_id = hdr['user_from'].rstrip(b' ')
            
            # Preparación de metadatos del mensaje
            peer = peer_id.decode('utf-8', errors='ignore')
            is_broadcast = (to_id == BROADCAST_UID)

            # Logging detallado para debugging
            print(f"Procesando mensaje/archivo de {peer} ({hdr['op_code']}) {'(broadcast)' if is_broadcast else ''}")
            print(f"  - ID origen: {peer_id!r}")
            print(f"  - ID destino: {to_id!r}")
            print(f"  - ID local: {my_id!r}")
            print(f"  - Longitud body: {len(body)} bytes")

            # Procesamiento de mensajes de texto
            if hdr['op_code'] == OP_MESSAGE:
                # Extracción y validación del contenido
                message_id, content = unpack_message_body(body)
                # Verificación de consistencia de IDs
                if (message_id & 0xFF) != hdr['body_id']:
                    print(f"  - Warning: ID de mensaje no coincide: header={hdr['body_id']}, body={message_id & 0xFF}")
                    
                # Decodificación del texto con manejo de errores
                text = content.decode('utf-8', errors='ignore')
                print(f"  - Mensaje decodificado ({len(text)} chars): {text[:50]}...")
                
                # Registro en el historial
                self.history_store.append_message(
                    sender=peer,
                    recipient="*global*" if is_broadcast else my_id.decode('utf-8'),
                    message=text,
                    timestamp=datetime.now(UTC)
                )
                print("  - Mensaje guardado en historial")
            else:
                # Rechazo de archivos broadcast por seguridad
                if is_broadcast:
                    print("  - Ignorando archivo broadcast")
                    return
                    
                # Procesamiento de archivo recibido
                file_id = int.from_bytes(body[:8], 'big')
                # Validación de consistencia del ID
                if (file_id & 0xFF) != hdr['body_id']:
                    print(f"  - Warning: ID de archivo no coincide: header={hdr['body_id']}, body={file_id & 0xFF}")
                    
                # Extracción del contenido binario
                file_data = body[8:]
                
                # Generación de nombre único para el archivo
                timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
                filename = f"archivo_{timestamp}_{file_id & 0xFF}.bin"
                print(f"  - Guardando archivo como: {filename} ({len(file_data)} bytes)")

                # Preparación del directorio de descargas
                downloads_dir = os.path.join(os.getcwd(), "Descargas")
                os.makedirs(downloads_dir, exist_ok=True)
                path = os.path.join(downloads_dir, filename)
                
                # Almacenamiento del archivo en disco
                with open(path, 'wb') as f:
                    f.write(file_data)

                # Registro en el historial de transferencias
                self.history_store.append_file(
                    sender=peer,
                    recipient=my_id.decode('utf-8'),
                    filename=filename,
                    timestamp=datetime.now(UTC)
                )
                print("  - Archivo guardado en Descargas/")
        except Exception as e:
            # Logging detallado de errores para debugging
            print(f"Error procesando mensaje/archivo: {e}")
            print(f"  - Header: {hdr}")
            print(f"  - Body length: {len(body)}")
            # Supresión de excepciones para mantener el sistema funcionando
