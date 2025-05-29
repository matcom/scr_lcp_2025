import socket
import psutil

# Este archivo proporciona utilidades para la configuración de red del sistema. Su función principal
# es obtener la dirección IP local y la dirección de broadcast correspondiente. El flujo de trabajo
# consiste en examinar todas las interfaces de red disponibles, filtrar las interfaces no deseadas
# (como las virtuales o desconectadas), y finalmente calcular la dirección de broadcast basándose
# en la IP y máscara de red seleccionada.

# Obtiene la dirección IP local válida y su dirección de broadcast correspondiente
# Esta función es crucial para establecer la comunicación en la red local, ya que:
# 1. Filtra interfaces no deseadas (virtuales, desconectadas)
# 2. Encuentra una IP válida que no sea localhost
# 3. Calcula matemáticamente la dirección de broadcast
def get_local_ip_and_broadcast():
    for iface, addrs in psutil.net_if_addrs().items():
        stats = psutil.net_if_stats().get(iface)

        if not stats or not stats.isup:
            continue

        # Se excluyen adaptadores virtuales y otros no deseados para asegurar una conexión física real
        excluded = ["virtualbox", "vmware", "loopback", "bluetooth", "ethernet 2", "conexión de red bluetooth"]
        if any(x.lower() in iface.lower() for x in excluded):
            continue

        for addr in addrs:
            if addr.family == socket.AF_INET and not addr.address.startswith("127."):
                ip = addr.address
                netmask = addr.netmask
                if not ip or not netmask:
                    continue

                # Cálculo de la dirección de broadcast mediante operaciones bit a bit:
                # Se realiza un OR entre la IP y el complemento de la máscara para obtener
                # la dirección más alta posible en la subred
                ip_parts = list(map(int, ip.split('.')))
                mask_parts = list(map(int, netmask.split('.')))
                broadcast_parts = [(ip_parts[i] | (~mask_parts[i] & 0xff)) for i in range(4)]
                broadcast = '.'.join(map(str, broadcast_parts))

                print(f"🧪 IP usada: {ip}, Broadcast: {broadcast}, Interfaz: {iface}")
                return ip, broadcast

    raise RuntimeError("❌ No se encontró una interfaz de red válida.")
