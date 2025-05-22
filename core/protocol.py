# core/protocol.py

import struct

# Puertos por defecto (pueden parametrizarse si se desea)
UDP_PORT = 9990
TCP_PORT = 9990

# Formatos de cabecera
HEADER_FMT = '!20s20sB I Q 50s'   # user_from, user_to, op_code, body_id, body_len, padding
HEADER_SIZE = struct.calcsize(HEADER_FMT)

# Formato de respuesta
RESPONSE_FMT = '!B20s4s'          # status, responder_id, padding
RESPONSE_SIZE = struct.calcsize(RESPONSE_FMT)

# Dirección “broadcast” de UID (20 bytes de 0xFF)
BROADCAST_UID = b'\xff' * 20


def pack_header(user_from: bytes,
                user_to: bytes = BROADCAST_UID,
                op_code: int = 0,
                body_id: int = 0,
                body_len: int = 0) -> bytes:
    """
    Empaqueta un Echo-Request o Message-Request.
    """
    padding = b'\x00' * 50
    return struct.pack(
        HEADER_FMT,
        user_from,
        user_to,
        op_code,
        body_id,
        body_len,
        padding
    )


def unpack_header(data: bytes) -> dict:
    """
    Desempaqueta la cabecera de  HEADER_SIZE bytes.
    """
    user_from, user_to, op_code, body_id, body_len, _ = struct.unpack(
        HEADER_FMT,
        data[:HEADER_SIZE]
    )
    return {
        'user_from': user_from.rstrip(b'\x00'),
        'user_to':   user_to.rstrip(b'\x00'),
        'op_code':   op_code,
        'body_id':   body_id,
        'body_len':  body_len
    }


def pack_response(status: int,
                  responder: bytes) -> bytes:
    """
    Empaqueta una respuesta a un Echo-Request.
    """
    padding = b'\x00' * 4
    return struct.pack(
        RESPONSE_FMT,
        status,
        responder,
        padding
    )


def unpack_response(data: bytes) -> dict:
    """
    Desempaqueta una respuesta de tamaño RESPONSE_SIZE.
    """
    status, responder, _ = struct.unpack(
        RESPONSE_FMT,
        data[:RESPONSE_SIZE]
    )
    return {
        'status':    status,
        'responder': responder.rstrip(b'\x00')
    }
