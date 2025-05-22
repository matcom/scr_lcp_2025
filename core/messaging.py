# core/messaging.py

import threading
import socket
import os
from datetime import datetime

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


class Messaging:
    """
    Único hilo de recepción que despacha:
     - op_code=0 → Discovery.handle_echo
     - op_code=1,2 → handshake completo y guardado en historial
    """

    def __init__(self, user_id: bytes, discovery, history_store):
        self.user_id = user_id
        self.discovery = discovery
        self.history_store = history_store
        self.sock = discovery.sock

    def _wait_for_ack(self, expected_from: bytes, timeout: float = 2.0):
        """Espera un ACK válido del peer esperado."""
        self.sock.settimeout(timeout)
        try:
            data, _ = self.sock.recvfrom(RESPONSE_SIZE)
            resp = unpack_response(data)
            if resp['status'] != 0 or resp['responder'] != expected_from:
                raise ValueError("ACK inválido")
        finally:
            self.sock.settimeout(None)

    def send(self, recipient: bytes, message: bytes):
        """Envía un mensaje de texto (op_code=1) con header→ACK→body→ACK."""
        info = self.discovery.get_peers().get(recipient)
        if not info:
            raise ValueError("Peer no encontrado")
        dest = (info['ip'], UDP_PORT)

        # Header
        header = pack_header(
            user_from=self.user_id,
            user_to=recipient,
            op_code=1,
            body_len=len(message)
        )
        self.sock.sendto(header, dest)
        self._wait_for_ack(expected_from=recipient)

        # Body
        self.sock.sendto(message, dest)
        self._wait_for_ack(expected_from=recipient)

        # Registrar en historial
        self.history_store.append_message(
            sender=self.user_id.decode(),
            recipient=recipient.decode(),
            message=message.decode(),
            timestamp=datetime.utcnow()
        )

    def send_all(self, message: bytes):
        """
        Envía un mensaje global (op_code=1) con un único broadcast.
        No espera ACKs.
        """
        header = pack_header(
            user_from=self.user_id,
            user_to=BROADCAST_UID,
            op_code=1,
            body_len=len(message)
        )
        pkt = header + message
        self.sock.sendto(pkt, ('<broadcast>', UDP_PORT))

        for peer_id in self.discovery.get_peers().keys():
            self.history_store.append_message(
                sender=self.user_id.decode(),
                recipient=peer_id.decode(),
                message=message.decode(),
                timestamp=datetime.utcnow()
            )

    def _handle_message_or_file(self, hdr, initial_data: bytes, addr):
        """
        Completa handshake del body, ACK body, y guarda mensaje o archivo.
        """
        peer_id = hdr['user_from']

        # Leer el resto del body
        body_len = hdr['body_len']
        data = initial_data
        while len(data) < body_len:
            chunk, _ = self.sock.recvfrom(body_len - len(data))
            data += chunk

        # ACK del body
        self.sock.sendto(pack_response(0, self.user_id), addr)

        # Procesar contenido
        if hdr['op_code'] == 1:
            text = data.decode('utf-8')
            self.history_store.append_message(
                sender=peer_id.decode(),
                recipient=self.user_id.decode(),
                message=text,
                timestamp=datetime.utcnow()
            )
        elif hdr['op_code'] == 2:
            name_len = int.from_bytes(data[:2], 'big')
            filename = data[2:2+name_len].decode('utf-8')
            file_data = data[2+name_len:]
            save_dir = 'received_files'
            os.makedirs(save_dir, exist_ok=True)
            save_path = os.path.join(save_dir, filename)
            with open(save_path, 'wb') as f:
                f.write(file_data)
            self.history_store.append_file(
                sender=peer_id.decode(),
                recipient=self.user_id.decode(),
                filename=filename,
                timestamp=datetime.utcnow()
            )

    def recv_loop(self):
        """
        Único bucle de recepción:
         - op_code=0 → discovery.handle_echo
         - op_code=1,2 → ACK header y despachar body en hilo
        """
        while True:
            data, addr = self.sock.recvfrom(max(HEADER_SIZE, RESPONSE_SIZE, 4096))
            if len(data) < HEADER_SIZE:
                continue

            hdr = unpack_header(data[:HEADER_SIZE])

            if hdr['op_code'] == 0:
                # Echo-Request → discovery
                self.discovery.handle_echo(data, addr)
                continue

            # ACK header
            self.sock.sendto(pack_response(0, self.user_id), addr)

            # Despachar lectura y procesamiento del body
            initial_body = data[HEADER_SIZE:]
            threading.Thread(
                target=self._handle_message_or_file,
                args=(hdr, initial_body, addr),
                daemon=True
            ).start()
