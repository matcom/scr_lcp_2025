import logging
import platform
import multiprocessing
import subprocess


logger = logging.getLogger("LCP")


def get_available_resources():
    """Determina los recursos disponibles en el sistema.

    Retorna:
        dict: Diccionario con información sobre CPUs, memoria, carga del sistema, etc.
    """
    resources = {
        "cpu_count": 0,
        "memory_gb": 0,
        "memory_available_gb": 0,
        "system_load": 0.0,
        "platform": platform.system(),
    }

    try:
        resources["cpu_count"] = multiprocessing.cpu_count()
        logger.info(f"CPUs lógicas detectadas: {resources['cpu_count']}")
    except Exception as e:
        resources["cpu_count"] = 4
        logger.warning(
            f"No se pudo detectar el número de CPUs, usando valor por defecto: 4. Error: {e}"
        )
    if resources["platform"] == "Darwin":
        try:
            output = subprocess.check_output(["sysctl", "-n", "hw.memsize"]).strip()
            total_memory_bytes = int(output)
            resources["memory_gb"] = round(total_memory_bytes / (1024**3), 2)

            # Obtener información de memoria disponible
            vm_stat = subprocess.check_output(["vm_stat"]).decode("utf-8").strip()
            lines = vm_stat.split("\n")
            memory_data = {}

            for line in lines[1:]:
                if ":" in line:
                    key, value = line.split(":", 1)
                    value = int(value.strip().replace(".", ""))
                    memory_data[key.strip()] = value

            page_size = 4096
            free_pages = memory_data.get("Pages free", 0)
            inactive_pages = memory_data.get("Pages inactive", 0)
            free_memory_bytes = (free_pages + inactive_pages) * page_size
            resources["memory_available_gb"] = round(free_memory_bytes / (1024**3), 2)

            load = (
                subprocess.check_output(["sysctl", "-n", "vm.loadavg"]).decode().strip()
            )
            load = load.replace("{", "").replace("}", "").split()[0]
            resources["system_load"] = float(load)

            logger.info(f"Memoria total: {resources['memory_gb']} GB")
            logger.info(f"Memoria disponible: {resources['memory_available_gb']} GB")
            logger.info(f"Carga del sistema: {resources['system_load']}")

        except Exception as e:
            logger.warning(f"Error obteniendo recursos en macOS: {e}")

    return resources


def get_optimal_thread_count():
    """Determina el número óptimo de hilos basado en los recursos disponibles del sistema."""
    resources = get_available_resources()

    load_factor = max(
        0.5, min(1.0, 1.0 - (resources["system_load"] / resources["cpu_count"] / 2))
    )
    logger.info(f"Factor de carga calculado: {load_factor:.2f}")

    memory_factor = 1.0
    if resources["memory_gb"] > 0:
        memory_percent = (
            resources["memory_available_gb"] / resources["memory_gb"]
            if resources["memory_gb"] > 0
            else 0.5
        )
        memory_factor = max(0.5, min(1.5, memory_percent * 2))
        logger.info(
            f"Memoria disponible: {int(memory_percent * 100)}% - Factor de memoria: {memory_factor:.2f}"
        )

    base_msg_per_cpu = 3.0
    base_file_per_cpu = 1.5
    base_transfer_per_cpu = 2.0

    effective_cpu = resources["cpu_count"] * load_factor * memory_factor

    msg_workers = max(5, int(effective_cpu * base_msg_per_cpu))
    file_workers = max(3, int(effective_cpu * base_file_per_cpu))
    max_transfers = max(4, int(effective_cpu * base_transfer_per_cpu))

    if resources["platform"] == "Darwin":
        msg_workers = int(msg_workers * 1.2)
        file_workers = int(file_workers * 1.1)

    msg_workers = min(msg_workers, 40)
    file_workers = min(file_workers, 20)
    max_transfers = min(max_transfers, 25)

    logger.info(f"Workers para mensajes calculados: {msg_workers}")
    logger.info(f"Workers para archivos calculados: {file_workers}")
    logger.info(f"Límite de transferencias concurrentes: {max_transfers}")

    return msg_workers, file_workers, max_transfers
