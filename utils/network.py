import platform
import subprocess
import logging

logger = logging.getLogger("LCP")


def get_network_info():
    """Obtiene información de red"""
    system = platform.system()
    broadcast_addresses = []

    try:
        if system == "Darwin":  # macOS
            output = subprocess.check_output(
                ["ifconfig en0 | grep broadcast | awk '{print $6}'"],
                universal_newlines=True,
                shell=True,
            )
            broadcast_addresses = output.splitlines()

    except Exception as e:
        logger.error(f"Error obteniendo información de red: {e}")
        broadcast_addresses.append("255.255.255.255")

    broadcast_addresses = list(set(broadcast_addresses))
    logger.info(f"Direcciones de broadcast detectadas: {broadcast_addresses}")
    return broadcast_addresses
