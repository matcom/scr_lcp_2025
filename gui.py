import customtkinter as ctk
from tkinter import filedialog, simpledialog
import time
import os
import logging
import concurrent.futures
import queue
from utils import get_optimal_thread_count
from main import Peer

ctk.set_appearance_mode("System")
ctk.set_default_color_theme("blue")

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(threadName)s] [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("LCP-GUI")


class LCPChat(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("🌐 ConnectiChat LAN 2025 🔮")
        self.geometry("1000x700")
        self.minsize(800, 600)

        self.bg_color = ("#f0f0f0", "#2b2b2b")
        self.text_color = ("#000000", "#ffffff")
        self.button_color = ("#4a90e2", "#1f6aa5")
        self.success_color = ("#4CAF50", "#2E7D32")
        self.warning_color = ("#FF9800", "#F57C00")
        self.error_color = ("#F44336", "#D32F2F")
        self.progress_color = ("#4a90e2", "#1f6aa5")

        self.sent_file_notifications = set()

        n, _, _ = get_optimal_thread_count()
        self.thread_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=n, thread_name_prefix="GUI-Worker"
        )

        self.update_queue = queue.Queue()

        username = self.get_username()

        self.peer = Peer(username)

        self.peer.register_message_callback(self.on_message)
        self.peer.register_file_callback(self.on_file)
        self.peer.register_peer_discovery_callback(self.on_peer_change)
        self.peer.register_file_progress_callback(self.on_file_progress)

        self.current_chat = None
        self.chat_history = {}
        self.selected_user = ctk.StringVar()

        self.show_history = True

        self.file_progress_bars = {}
        self.progress_window = None

        self.create_widgets()

        self.load_all_message_history()

        self.after(1000, self.update_ui)
        self.after(100, self.process_ui_updates)

        self.append_to_chat("Sistema", f"Bienvenido {username}!")
        self.append_to_chat(
            "Sistema", "Esperando descubrir otros usuarios en la red..."
        )

    def __del__(self):
        """Destructor para liberar recursos"""
        if hasattr(self, "thread_pool"):
            self.thread_pool.shutdown(wait=False)

        if hasattr(self, "progress_window") and self.progress_window is not None:
            try:
                self.progress_window.destroy()
            except:
                pass

    def get_username(self):
        """Solicita un nombre de usuario al iniciar la aplicación"""
        username = simpledialog.askstring(
            "LCP Chat",
            "Introduce tu nombre de usuario:",
            initialvalue="Albert",
        )
        if not username:
            username = f"User{int(time.time())%1000}"
        return username

    def create_progress_window(self):
        """Crea la ventana única para mostrar todas las transferencias de archivos"""
        if hasattr(self, "progress_window") and self.progress_window is not None:
            try:
                if self.progress_window.winfo_exists():
                    return
            except Exception:
                logger.warning(
                    "Error al verificar existencia de progress_window, creando nuevo"
                )
                pass

        try:
            self.progress_window = ctk.CTkToplevel(self)
            self.progress_window.withdraw()
        except Exception as e:
            logger.error(f"Error al crear progress_window: {e}")
            self.progress_window = None
            return

        self.progress_window.title("Transferencias de Archivos")
        self.progress_window.geometry("550x400")
        self.progress_window.resizable(True, True)
        self.progress_window.transient(self)
        self.progress_window.protocol("WM_DELETE_WINDOW", self.hide_progress_window)

        header_frame = ctk.CTkFrame(self.progress_window)
        header_frame.pack(fill="x", padx=10, pady=(10, 0))

        title_frame = ctk.CTkFrame(header_frame, fg_color="transparent")
        title_frame.pack(fill="x", pady=5)

        icon_label = ctk.CTkLabel(title_frame, text="📁", font=("Arial", 20))
        icon_label.pack(side="left", padx=(5, 5))

        title_label = ctk.CTkLabel(
            title_frame,
            text="Transferencias de Archivos",
            font=("Arial", 16, "bold"),
        )
        title_label.pack(side="left", pady=5)

        button_frame = ctk.CTkFrame(header_frame, fg_color="transparent")
        button_frame.pack(fill="x", pady=5)

        self.transfer_status = ctk.CTkLabel(
            button_frame,
            text="Sin transferencias activas",
            font=("Arial", 11),
            text_color=("gray60", "gray70"),
        )
        self.transfer_status.pack(side="left", padx=10)

        clear_button = ctk.CTkButton(
            button_frame,
            text="Limpiar Completadas",
            command=self.clear_completed_transfers,
            font=("Arial", 11),
            height=28,
            width=30,
            fg_color=("#3498db", "#2980b9"),
        )
        clear_button.pack(side="right", padx=10)

        separator = ctk.CTkFrame(
            self.progress_window, height=1, fg_color=("gray80", "gray30")
        )
        separator.pack(fill="x", padx=10, pady=(0, 5))

        self.transfers_container = ctk.CTkScrollableFrame(self.progress_window)
        self.transfers_container.pack(fill="both", expand=True, padx=10, pady=10)

        self.progress_window.update_idletasks()

        self.progress_window.deiconify()
        self.progress_window.focus_force()
        self.progress_window.attributes("-topmost", True)

    def hide_progress_window(self):
        """Oculta la ventana de progreso en lugar de destruirla"""
        if (
            hasattr(self, "progress_window")
            and self.progress_window
            and self.progress_window.winfo_exists()
        ):
            self.progress_window.withdraw()

    def show_progress_window(self):
        """Muestra la ventana de progreso si existe"""
        if (
            not hasattr(self, "progress_window")
            or self.progress_window is None
            or not self.progress_window.winfo_exists()
        ):
            self.create_progress_window()
        else:
            self.progress_window.deiconify()
            self.progress_window.focus_force()
            self.progress_window.attributes("-topmost", True)

    def create_progress_bar(self, user_id, filename):
        """Crea una barra de progreso visual para un archivo en transferencia"""

        self.show_progress_window()

        file_key = f"{user_id}_{filename}"

        if file_key in self.file_progress_bars:
            logger.debug(f"Eliminando barra de progreso existente para {file_key}")
            progress_data = self.file_progress_bars[file_key]
            if "frame" in progress_data and progress_data["frame"] is not None:
                progress_data["frame"].destroy()
            del self.file_progress_bars[file_key]

        transfer_frame = ctk.CTkFrame(
            self.transfers_container,
            corner_radius=6,
            border_width=1,
            border_color=("gray85", "gray25"),
            fg_color=("gray95", "gray15"),
        )
        transfer_frame.pack(fill="x", pady=5, padx=5)

        header_frame = ctk.CTkFrame(transfer_frame, fg_color="transparent")
        header_frame.pack(fill="x", pady=(8, 0), padx=5)

        icon_label = ctk.CTkLabel(header_frame, text="📄", font=("Arial", 18))
        icon_label.pack(side="left", padx=5)

        short_filename = filename
        if len(filename) > 30:
            short_filename = filename[:27] + "..."

        file_label = ctk.CTkLabel(
            header_frame,
            text=f"{short_filename} → {user_id}",
            font=("Arial", 12),
            anchor="w",
        )
        file_label.pack(side="left", fill="x", expand=True, padx=5)

        close_button = ctk.CTkButton(
            header_frame,
            text="×",
            width=22,
            height=22,
            font=("Arial", 14, "bold"),
            fg_color=("gray75", "gray40"),
            hover_color=("gray65", "gray30"),
            command=lambda: self.remove_progress_bar(user_id, filename),
        )
        close_button.pack(side="right", padx=(0, 5))

        percent_label = ctk.CTkLabel(
            header_frame,
            text="0%",
            font=("Arial", 12, "bold"),
        )
        percent_label.pack(side="right", padx=(0, 8))

        bar_container = ctk.CTkFrame(transfer_frame, fg_color="transparent")
        bar_container.pack(fill="x", pady=(5, 8), padx=10)

        progress_bar = ctk.CTkProgressBar(
            bar_container,
            orientation="horizontal",
            mode="determinate",
            height=15,
            fg_color=("#E0E0E0", "#3a3a3a"),
            progress_color=("#4a90e2", "#1f6aa5"),
            corner_radius=2,
        )
        progress_bar.pack(fill="x")
        progress_bar.set(0)

        self.file_progress_bars[file_key] = {
            "frame": transfer_frame,
            "bar": progress_bar,
            "label": percent_label,
            "file_label": file_label,
            "icon": icon_label,
            "close_button": close_button,
            "_last_value": 0,
        }

        self.progress_window.update_idletasks()

    def update_progress_bar(self, user_id, filename, progress):
        """Actualiza la barra de progreso sin parpadeos"""
        file_key = f"{user_id}_{filename}"

        if file_key in self.file_progress_bars:
            progress_data = self.file_progress_bars[file_key]
            self.show_progress_window()
            if abs(progress_data["_last_value"] - progress) >= 5 or progress in [
                0,
                25,
                50,
                75,
                100,
            ]:
                try:

                    progress_data["bar"].set(progress / 100)
                    progress_data["label"].configure(text=f"{progress}%")
                    progress_data["_last_value"] = progress

                    if progress < 25:
                        progress_bar_color = ("#e74c3c", "#c0392b") 
                    elif progress < 50:
                        progress_bar_color = ("#f39c12", "#d35400") 
                    elif progress < 75:
                        progress_bar_color = ("#f1c40f", "#f39c12") 
                    elif progress < 100:
                        progress_bar_color = ("#2ecc71", "#27ae60")  
                    else:
                        progress_bar_color = ("#27ae60", "#219653")  

                    progress_data["bar"].configure(progress_color=progress_bar_color)

                    if progress < 100:
                        progress_data["file_label"].configure(
                            text=f"{filename} → {user_id}"
                        )
                        progress_data["icon"].configure(text="🔄")
                    else:
                        progress_data["file_label"].configure(
                            text=f"{filename} → {user_id} (Completado)"
                        )
                        progress_data["icon"].configure(text="✅")
                        self.after(
                            3000, lambda: self.remove_progress_bar(user_id, filename)
                        )

                except Exception as e:
                    logger.debug(f"Error al actualizar barra: {e}")

            if self.progress_window and self.progress_window.winfo_exists():
                self.progress_window.update_idletasks()

    def remove_progress_bar(self, user_id, filename):
        """Elimina una barra de progreso específica"""
        file_key = f"{user_id}_{filename}"

        if file_key in self.file_progress_bars:
            progress_data = self.file_progress_bars[file_key]
            if "frame" in progress_data and progress_data["frame"] is not None:
                try:
                    progress_data["frame"].destroy()
                except Exception:
                    pass
            del self.file_progress_bars[file_key]

            if not self.file_progress_bars:

                self.after(1000, self.hide_progress_window)
            else:

                active_transfers = sum(
                    1
                    for k, v in self.file_progress_bars.items()
                    if v.get("_last_value", 0) < 100
                )
                if active_transfers > 0:
                    self.transfer_status.configure(
                        text=f"{active_transfers} transferencias activas"
                    )
                else:
                    self.transfer_status.configure(
                        text="Todas las transferencias completadas"
                    )

    def clear_completed_transfers(self):
        """Limpia las barras de progreso de transferencias completadas"""
        completed_keys = []

        for file_key, progress_data in self.file_progress_bars.items():
            if progress_data.get("_last_value", 0) >= 100:
                completed_keys.append(file_key)

        for file_key in completed_keys:
            parts = file_key.split("_", 1)
            if len(parts) == 2:
                user_id, filename = parts
                self.remove_progress_bar(user_id, filename)

        if hasattr(self, "sent_file_notifications"):
            self.sent_file_notifications.clear()

        if self.file_progress_bars:
            active_count = len(self.file_progress_bars)
            self.transfer_status.configure(
                text=f"{active_count} transferencias activas"
            )
        else:
            self.transfer_status.configure(text="Sin transferencias activas")
            self.after(500, self.hide_progress_window)

    def create_widgets(self):
        """Crea los widgets de la interfaz"""
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=1)

        self.sidebar_frame = ctk.CTkFrame(self, width=250, corner_radius=0)
        self.sidebar_frame.grid(row=0, column=0, sticky="nsew")
        self.sidebar_frame.grid_rowconfigure(1, weight=1)

        self.sidebar_label = ctk.CTkLabel(
            self.sidebar_frame, text="Usuarios Conectados", font=("Arial", 14, "bold")
        )
        self.sidebar_label.grid(row=0, column=0, padx=20, pady=10)

        self.users_list = ctk.CTkScrollableFrame(self.sidebar_frame)
        self.users_list.grid(row=1, column=0, padx=10, pady=(0, 10), sticky="nsew")

        self.refresh_button = ctk.CTkButton(
            self.sidebar_frame,
            text="Actualizar",
            command=self.refresh_users,
            font=("Arial", 12),
        )
        self.refresh_button.grid(row=2, column=0, padx=20, pady=10, sticky="ew")

        self.main_frame = ctk.CTkFrame(self, corner_radius=0)
        self.main_frame.grid(row=0, column=1, sticky="nsew")
        self.main_frame.grid_rowconfigure(0, weight=1)
        self.main_frame.grid_columnconfigure(0, weight=1)

        self.chat_container = ctk.CTkFrame(self.main_frame)
        self.chat_container.grid(row=0, column=0, sticky="nsew")
        self.chat_container.grid_rowconfigure(0, weight=1)
        self.chat_container.grid_columnconfigure(0, weight=1)

        self.chat_display = ctk.CTkTextbox(
            self.chat_container, wrap="word", state="disabled", font=("Arial", 14)
        )
        self.chat_display.grid(row=0, column=0, padx=10, pady=10, sticky="nsew")

        self.input_frame = ctk.CTkFrame(self.main_frame, height=50)
        self.input_frame.grid(row=1, column=0, padx=10, pady=(0, 10), sticky="ew")
        self.input_frame.grid_columnconfigure(0, weight=1)

        self.message_input = ctk.CTkEntry(
            self.input_frame,
            placeholder_text="Escribe tu mensaje aquí...",
            font=("Arial", 14),
        )
        self.message_input.grid(row=0, column=0, padx=(0, 5), sticky="ew")
        self.message_input.bind("<Return>", self.send_message)

        self.send_button = ctk.CTkButton(
            self.input_frame,
            text="Enviar",
            command=self.send_message,
            width=80,
            font=("Arial", 12),
        )
        self.send_button.grid(row=0, column=1, padx=(0, 5))

        self.buttons_frame = ctk.CTkFrame(self.main_frame)
        self.buttons_frame.grid(row=2, column=0, padx=10, pady=(0, 10), sticky="ew")

        self.broadcast_button = ctk.CTkButton(
            self.buttons_frame,
            text="Enviar a Todos",
            command=self.send_broadcast,
            fg_color=self.warning_color,
            font=("Arial", 12),
        )
        self.broadcast_button.pack(side="left", padx=5, pady=5, fill="x", expand=True)

        self.file_button = ctk.CTkButton(
            self.buttons_frame,
            text="Enviar Archivo",
            command=self.send_file,
            font=("Arial", 12),
        )
        self.file_button.pack(side="left", padx=5, pady=5, fill="x", expand=True)

        self.status_var = ctk.StringVar()
        self.status_var.set("Conectando...")
        self.status_bar = ctk.CTkLabel(
            self,
            textvariable=self.status_var,
            anchor="w",
            font=("Arial", 11),
            corner_radius=0,
        )
        self.status_bar.grid(row=1, column=0, columnspan=2, sticky="ew")

    def update_ui(self):
        """Actualiza periódicamente la interfaz"""
        current_peers = set(self.peer.get_peers())
        if hasattr(self, "_last_peers"):
            if current_peers != self._last_peers:
                self.refresh_users()
        else:
            self.refresh_users()

        self._last_peers = current_peers
        self.after(5000, self.update_ui)

    def refresh_users(self):
        """Actualiza la lista de usuarios conectados y contactos históricos"""

        all_peers = self.peer.get_peers()
        online_peers = [peer_id for peer_id in all_peers if peer_id.strip() != ""]

        with self.peer._message_history_lock:
            historical_peers = list(self.peer._message_history.keys())

        all_contacts = set(online_peers)
        for hist_peer in historical_peers:
            normalized_id = self.peer._normalize_user_id(hist_peer)
            if not any(
                self.peer._normalize_user_id(p) == normalized_id for p in all_contacts
            ):
                all_contacts.add(normalized_id)

        for widget in self.users_list.winfo_children():
            widget.destroy()

        for user_id, chat_info in self.chat_history.items():
            if isinstance(chat_info, dict) and "button" in chat_info:
                chat_info.pop("button", None)

        for peer_id in online_peers:
            is_selected = self.current_chat == peer_id

            user_btn = ctk.CTkButton(
                self.users_list,
                text=peer_id + " 🟢",
                command=lambda p=peer_id: self.select_user(p),
                anchor="w",
                font=("Arial", 12, "bold" if is_selected else "normal"),
                fg_color=("#4a90e2", "#1f6aa5") if is_selected else "transparent",
                text_color=("#ffffff", "#ffffff") if is_selected else None,
                hover_color=(
                    ("#3a80d2", "#155995") if is_selected else ("#e1e1e1", "#3a3a3a")
                ),
            )
            user_btn.pack(fill="x", pady=2)

            if peer_id in self.chat_history and isinstance(
                self.chat_history[peer_id], dict
            ):
                self.chat_history[peer_id]["button"] = user_btn
                self.chat_history[peer_id]["online"] = True

            if is_selected:
                self.selected_user_btn = user_btn

        for peer_id in historical_peers:
            normalized_id = self.peer._normalize_user_id(peer_id)
            if not any(
                self.peer._normalize_user_id(p) == normalized_id for p in online_peers
            ):
                is_selected = self.current_chat == normalized_id

                user_btn = ctk.CTkButton(
                    self.users_list,
                    text=normalized_id + " 📜",
                    command=lambda p=normalized_id: self.select_user(p),
                    anchor="w",
                    font=("Arial", 12, "bold" if is_selected else "normal"),
                    fg_color=(
                        ("#4a90e2", "#1f6aa5")
                        if is_selected
                        else ("#888888", "#555555")
                    ),
                    text_color=("#ffffff", "#ffffff") if is_selected else None,
                    hover_color=(
                        ("#3a80d2", "#155995")
                        if is_selected
                        else ("#999999", "#666666")
                    ),
                )
                user_btn.pack(fill="x", pady=2)

                if normalized_id in self.chat_history and isinstance(
                    self.chat_history[normalized_id], dict
                ):
                    self.chat_history[normalized_id]["button"] = user_btn
                    self.chat_history[normalized_id]["online"] = False

                if is_selected:
                    self.selected_user_btn = user_btn

        connected_count = len(online_peers)
        historical_count = len(historical_peers) - sum(
            1
            for h in historical_peers
            if any(
                self.peer._normalize_user_id(p) == self.peer._normalize_user_id(h)
                for p in online_peers
            )
        )
        self.status_var.set(
            f"Conectados: {connected_count} usuarios | Históricos: {historical_count} contactos"
        )

    def select_user(self, user_id):
        """Selecciona un usuario para chatear"""
        self.current_chat = user_id
        self.display_chat_history(user_id)

        self.refresh_users()

    def display_chat_history(self, user_id):
        """Muestra el historial de chat con un usuario"""
        try:
            if not hasattr(self, "chat_display"):
                logger.warning("chat_display no existe al intentar mostrar historial")
                return

            self.chat_display.grid_forget()
            for chat_info in self.chat_history.values():
                if isinstance(chat_info, dict) and "chat" in chat_info:
                    chat_info["chat"].grid_forget()

            if user_id in self.chat_history:
                if (
                    isinstance(self.chat_history[user_id], dict)
                    and "chat" in self.chat_history[user_id]
                ):
                    chat_area = self.chat_history[user_id]["chat"]
                    self.chat_container.grid_columnconfigure(0, weight=1)
                    self.chat_container.grid_rowconfigure(0, weight=1)
                    chat_area.grid(row=0, column=0, padx=10, pady=10, sticky="nsew")
                    chat_area.see("end")

                    if "unread" in self.chat_history[user_id]:
                        self.chat_history[user_id]["unread"] = 0

                    if "button" in self.chat_history[user_id]:
                        try:
                            button = self.chat_history[user_id]["button"]
                            if button.winfo_exists():
                                is_online = self.chat_history[user_id].get(
                                    "online", False
                                )
                                button.configure(
                                    fg_color=("#4a90e2", "#1f6aa5"),
                                    text=user_id + (" 🟢" if is_online else " 📜"),
                                )
                            else:
                                logger.warning(
                                    f"Botón para {user_id} no existe, regenerando en siguiente refresh"
                                )
                                self.chat_history[user_id].pop("button", None)
                        except Exception as e:
                            logger.warning(
                                f"Error configurando botón para {user_id}: {e}"
                            )
                            self.chat_history[user_id].pop("button", None)
                else:
                    self.chat_display.configure(state="normal")
                    self.chat_display.delete("1.0", "end")

                    if isinstance(self.chat_history[user_id], list):
                        history_text = "\n".join(self.chat_history[user_id]) + "\n"
                        self.chat_display.insert("end", history_text)

                    self.chat_display.configure(state="disabled")
                    self.chat_display.see("end")
                    self.chat_display.grid(
                        row=0, column=0, padx=10, pady=10, sticky="nsew"
                    )
            else:
                self.chat_display.configure(state="normal")
                self.chat_display.delete("1.0", "end")
                self.chat_display.configure(state="disabled")
                self.chat_display.grid(row=0, column=0, padx=10, pady=10, sticky="nsew")

            if hasattr(self, "status_var"):
                self.status_var.set(f"Chat con: {user_id}")

            if user_id != "Sistema" and user_id != "Broadcast" and user_id != "Tú":
                logger.info(f"Mensajes de {user_id} marcados como leídos")
        except Exception as e:
            logger.error(f"Error al mostrar historial de chat: {e}", exc_info=True)

    def append_to_chat(self, user_id, message, chat_id=None):
        """Añade un mensaje al historial y lo muestra si es el chat actual"""
        try:
            timestamp = time.strftime("%H:%M:%S")
            if user_id == "Tú":
                formatted_msg = f"[{timestamp}] {user_id}: {message}"
            elif user_id == "Sistema":
                formatted_msg = f"[{timestamp}] 🔔 {user_id}: {message}"
            elif user_id == "Histórico":
                formatted_msg = message
            else:
                formatted_msg = f"[{timestamp}] ➤ {user_id}: {message}"
            history_id = chat_id if chat_id else user_id

            if history_id in self.chat_history:
                if (
                    isinstance(self.chat_history[history_id], dict)
                    and "chat" in self.chat_history[history_id]
                ):
                    chat_area = self.chat_history[history_id]["chat"]
                    chat_area.configure(state="normal")

                    tag = None
                    if user_id == "Sistema":
                        tag = "sistema"

                    if tag:
                        chat_area.insert("end", f"{formatted_msg}\n", tag)
                    else:
                        chat_area.insert("end", f"{formatted_msg}\n")

                    chat_area.configure(state="disabled")
                    chat_area.see("end")
                else:
                    if isinstance(self.chat_history[history_id], list):
                        self.chat_history[history_id].append(formatted_msg)
            else:
                self.chat_history[history_id] = []
                logger.info(f"Creado nuevo historial para {history_id}")
                self.chat_history[history_id].append(formatted_msg)

            if (self.current_chat == history_id or user_id == "Sistema") and hasattr(
                self, "chat_display"
            ):
                try:
                    if (
                        self.current_chat == history_id
                        and history_id in self.chat_history
                    ):
                        if (
                            isinstance(self.chat_history[history_id], dict)
                            and "chat" in self.chat_history[history_id]
                        ):
                            pass
                        else:
                            self.chat_display.configure(state="normal")
                            self.chat_display.insert("end", f"{formatted_msg}\n")
                            self.chat_display.configure(state="disabled")
                            self.chat_display.see("end")
                    elif user_id == "Sistema":
                        self.chat_display.configure(state="normal")
                        self.chat_display.insert("end", f"{formatted_msg}\n")
                        self.chat_display.configure(state="disabled")
                        self.chat_display.see("end")
                    if (
                        user_id != "Tú"
                        and user_id != "Sistema"
                        and hasattr(self, "status_var")
                    ):
                        self.status_var.set(f"✉️ Mensaje nuevo de {user_id}")
                except Exception as e:
                    logger.error(f"Error al actualizar chat_display: {e}")
            else:
                if (
                    user_id != "Sistema"
                    and user_id != "Tú"
                    and hasattr(self, "status_var")
                ):
                    self.status_var.set(f"✉️ Mensaje nuevo sin leer de {user_id}")
                    logger.info(
                        f"Mensaje pendiente de {user_id} (chat actual: {self.current_chat})"
                    )

            logger.debug(
                f"Mensaje añadido al historial de {user_id}: {message[:30]}..."
            )
        except Exception as e:
            logger.error(f"Error añadiendo mensaje al chat: {e}", exc_info=True)

    def _direct_add_to_chat_area(self, user_id, message, tag=None):
        """Añade un mensaje directamente al área de chat de un usuario específico
        Args:
            user_id: El ID del usuario cuyo chat queremos actualizar
            message: El mensaje ya formateado para mostrar
            tag: Etiqueta opcional para aplicar estilos (solo para mensajes del sistema)
        """
        try:

            if (
                user_id in self.chat_history
                and isinstance(self.chat_history[user_id], dict)
                and "chat" in self.chat_history[user_id]
            ):
                chat_area = self.chat_history[user_id]["chat"]
                chat_area.configure(state="normal")

                if tag:
                    chat_area.insert("end", f"{message}\n", tag)
                else:
                    chat_area.insert("end", f"{message}\n")

                chat_area.configure(state="disabled")
                chat_area.see("end")
                if self.current_chat == user_id:
                    self.chat_display.grid_forget()
                    chat_area.grid(row=0, column=0, padx=10, pady=10, sticky="nsew")
                    chat_area.update_idletasks()

        except Exception as e:
            logger.error(
                f"Error añadiendo mensaje directo al chat de {user_id}: {e}",
                exc_info=True,
            )

    def send_message(self, event=None):
        """Envía un mensaje al usuario seleccionado"""
        if not self.current_chat:
            self.append_to_chat(
                "Sistema", "Por favor, selecciona un usuario para enviar mensajes."
            )
            return

        if self.current_chat.strip() == "":
            self.append_to_chat(
                "Sistema",
                "Error: No se puede enviar mensaje a un usuario con nombre vacío.",
            )
            return

        message = self.message_input.get().strip()
        if not message:
            return

        current_chat = self.current_chat
        self.message_input.delete(0, "end")

        self.status_var.set(f"Enviando mensaje a {current_chat}...")

        self.thread_pool.submit(self._send_message_thread, current_chat, message)

    def _send_message_thread(self, user_to, message):
        """Ejecuta el envío de mensajes en un hilo separado"""
        try:
            success = self.peer.send_message(user_to, message)

            if success:
                self.update_queue.put(
                    lambda: self.append_to_chat("Tú", message, user_to)
                )
                self.update_queue.put(
                    lambda: self.status_var.set("Mensaje enviado correctamente")
                )
            else:
                self.update_queue.put(
                    lambda: self.append_to_chat(
                        "Sistema", f"Error enviando mensaje a {user_to}."
                    )
                )
                self.update_queue.put(
                    lambda: self.status_var.set("Error al enviar mensaje")
                )

        except Exception as e:
            logger.error(f"Error enviando mensaje: {e}")
            self.update_queue.put(
                lambda: self.append_to_chat(
                    "Sistema", f"Error enviando mensaje: {str(e)}"
                )
            )
            self.update_queue.put(
                lambda: self.status_var.set("Error al enviar mensaje")
            )

    def send_broadcast(self):
        """Envía un mensaje a todos los usuarios conectados"""
        message = self.message_input.get().strip()
        if not message:
            return

        self.message_input.delete(0, "end")

        timestamp = time.strftime("%H:%M:%S")
        broadcast_msg = f"[{timestamp}] Tú (Broadcast): {message}"

        if "Broadcast" not in self.chat_history:
            self.chat_history["Broadcast"] = []
            self.chat_history["Broadcast"].append(broadcast_msg)
        else:

            if (
                isinstance(self.chat_history["Broadcast"], dict)
                and "chat" in self.chat_history["Broadcast"]
            ):

                chat_area = self.chat_history["Broadcast"]["chat"]
                chat_area.configure(state="normal")
                chat_area.insert("end", f"{broadcast_msg}\n")
                chat_area.configure(state="disabled")
                chat_area.see("end")
            elif isinstance(self.chat_history["Broadcast"], list):
                self.chat_history["Broadcast"].append(broadcast_msg)

        peers = self.peer.get_peers()
        for peer_id in peers:
            if peer_id not in self.chat_history:

                self.chat_history[peer_id] = []
                self.chat_history[peer_id].append(broadcast_msg)
            else:
                if (
                    isinstance(self.chat_history[peer_id], dict)
                    and "chat" in self.chat_history[peer_id]
                ):
                    chat_area = self.chat_history[peer_id]["chat"]
                    chat_area.configure(state="normal")
                    chat_area.insert("end", f"{broadcast_msg}\n")
                    chat_area.configure(state="disabled")
                    chat_area.see("end")
                elif isinstance(self.chat_history[peer_id], list):
                    self.chat_history[peer_id].append(broadcast_msg)

        if self.current_chat == "Broadcast" or self.current_chat == "Sistema":
            self.chat_display.configure(state="normal")
            self.chat_display.insert("end", f"{broadcast_msg}\n")
            self.chat_display.configure(state="disabled")
            self.chat_display.see("end")
        elif self.current_chat in peers:
            if (
                isinstance(self.chat_history[self.current_chat], dict)
                and "chat" in self.chat_history[self.current_chat]
            ):
                pass
            else:
                self.chat_display.configure(state="normal")
                self.chat_display.insert("end", f"{broadcast_msg}\n")
                self.chat_display.configure(state="disabled")
                self.chat_display.see("end")

        self.status_var.set("Enviando mensaje broadcast...")
        self.append_to_chat("Sistema", f'Enviando a todos: "{message}"')

        self.thread_pool.submit(self._send_broadcast_thread, message)

    def _send_broadcast_thread(self, message):
        """Ejecuta el envío de mensajes broadcast en un hilo separado"""
        try:
            success = self.peer.broadcast_message(message, max_retries=3)

            if success:
                self.update_queue.put(
                    lambda: self.status_var.set(
                        "Mensaje broadcast enviado correctamente"
                    )
                )
                self.update_queue.put(
                    lambda: self.append_to_chat(
                        "Sistema",
                        "✅ Mensaje enviado a todos los usuarios correctamente",
                    )
                )
            else:
                self.update_queue.put(
                    lambda: self.append_to_chat(
                        "Sistema",
                        "❌ Error enviando mensaje broadcast después de varios intentos",
                    )
                )
                self.update_queue.put(
                    lambda: self.status_var.set("Error al enviar mensaje broadcast")
                )

        except Exception as e:
            logger.error(f"Error enviando mensaje broadcast: {e}")
            self.update_queue.put(
                lambda: self.append_to_chat(
                    "Sistema", f"❌ Error enviando mensaje broadcast: {str(e)}"
                )
            )
            self.update_queue.put(
                lambda: self.status_var.set("Error al enviar mensaje broadcast")
            )

    def send_file(self):
        """Envía un archivo al usuario seleccionado"""
        if not self.current_chat:
            self.append_to_chat(
                "Sistema", "Por favor, selecciona un usuario para enviar archivos."
            )
            return

        filepath = filedialog.askopenfilename(title="Selecciona un archivo para enviar")
        if not filepath:
            return

        current_chat = self.current_chat
        filename = os.path.basename(filepath)

        peers = self.peer.get_peers()
        if current_chat not in peers:
            self.append_to_chat(
                "Sistema",
                f"Error: No se puede enviar archivo a {current_chat}, peer no encontrado.",
            )
            return

        self.status_var.set(f"Preparando envío de archivo a {current_chat}...")
        self.thread_pool.submit(self._send_file_thread, current_chat, filepath)

    def _send_file_thread(self, user_to, filepath):
        """Ejecuta el envío de archivos en un hilo separado"""
        try:
            filename = os.path.basename(filepath)

            clean_user_to = user_to.strip() if isinstance(user_to, str) else user_to

            notification_id = f"start_{clean_user_to}_{filename}"

            if notification_id not in self.sent_file_notifications:
                self.update_queue.put(
                    lambda: self.append_to_chat(
                        "Sistema",
                        f"Iniciando envío de archivo: {filename} a {clean_user_to}",
                    )
                )

                self.sent_file_notifications.add(notification_id)

            file_msg = f"Enviando archivo: {filename}"
            self.update_queue.put(
                lambda: self.append_to_chat("Tú", file_msg, clean_user_to)
            )

            success = self.peer.send_file(user_to, filepath)

            if not success:
                self.update_queue.put(
                    lambda: self.append_to_chat(
                        "Sistema", f"Error al iniciar el envío de archivo a {user_to}."
                    )
                )
                self.update_queue.put(
                    lambda: self.status_var.set("Error al enviar archivo")
                )
                self.update_queue.put(
                    lambda: self.remove_progress_bar(user_to, filename)
                )

        except Exception as e:
            logger.error(f"Error enviando archivo: {e}")
            self.update_queue.put(
                lambda: self.append_to_chat(
                    "Sistema", f"Error al enviar archivo: {str(e)}"
                )
            )
            self.update_queue.put(
                lambda: self.status_var.set("Error al enviar archivo")
            )
            self.update_queue.put(lambda: self.remove_progress_bar(user_to, filename))

    def on_message(self, user_from, message):
        """Callback para mensajes recibidos"""
        logger.info(f"MENSAJE RECIBIDO de {user_from}: {message[:50]}...")
        try:
            if hasattr(self, "status_var"):
                self.status_var.set(f"✉️ Nuevo mensaje de {user_from}")

            self.update_queue.put(
                lambda u=user_from, m=message: self.append_to_chat(u, m)
            )

            logger.info(f"Mensaje de {user_from} procesado correctamente")
        except Exception as e:
            logger.error(
                f"Error procesando mensaje recibido de {user_from}: {e}", exc_info=True
            )

    def on_file(self, user_from, file_path):
        """Callback para archivos recibidos"""
        try:
            clean_user_from = (
                user_from.strip() if isinstance(user_from, str) else user_from
            )
            filename = os.path.basename(file_path)

            self.status_var.set(f"✉️ Archivo recibido de {clean_user_from}: {filename}")

            file_key = f"{clean_user_from}_{filename}"

            def create_completed_bar():
                self.show_progress_window()
                self.create_progress_bar(clean_user_from, filename)
                self.update_progress_bar(clean_user_from, filename, 100)

            self.update_queue.put(create_completed_bar)

            notification_id = f"received_{clean_user_from}_{filename}"

            if notification_id not in self.sent_file_notifications:
                self.append_to_chat(
                    "Sistema", f"Archivo recibido de {clean_user_from}: {filename}"
                )
                self.append_to_chat("Sistema", f"Guardado como: {file_path}")
                file_msg = f"Archivo recibido: {filename}"
                self.append_to_chat(clean_user_from, file_msg)

                self.sent_file_notifications.add(notification_id)

        except Exception as e:
            logger.error(f"Error procesando archivo recibido: {e}", exc_info=True)

    def on_peer_change(self, user_id, added):
        """Callback para cambios en la lista de peers"""
        if user_id:
            clean_user_id = user_id.strip()
            if clean_user_id == "":
                return
        else:
            return

        action = "conectado" if added else "desconectado"
        self.append_to_chat("Sistema", f"Usuario '{clean_user_id}' se ha {action}")

        if added:
            self.thread_pool.submit(self._load_message_history, clean_user_id)

        self.update_queue.put(self.refresh_users)

    def _load_message_history(self, user_id):
        """Carga el historial de mensajes con un usuario que se acaba de conectar"""
        try:

            if (
                user_id in self.chat_history
                and isinstance(self.chat_history[user_id], dict)
                and "chat" in self.chat_history[user_id]
            ):

                logger.info(
                    f"Usuario {user_id} conectado: no se cargarán mensajes duplicados"
                )
                self.update_queue.put(self.refresh_users)
                return

            history = self.peer.get_message_history(user_id)

            if history:
                if (
                    not hasattr(self, "_loading_all_history")
                    or not self._loading_all_history
                ):
                    self.update_queue.put(
                        lambda: self.append_to_chat(
                            "Sistema",
                            f"Recuperados {len(history)} mensajes del historial con '{user_id}'",
                        )
                    )

                if not self.show_history:
                    return

                if user_id not in self.chat_history or not isinstance(
                    self.chat_history[user_id], dict
                ):
                    self.add_contact_to_ui(user_id)
                for msg in history:
                    time_str = msg["timestamp"].strftime("%H:%M:%S")

                    if msg["from"] == "self":
                        formatted_msg = f"[{time_str}] Tú: {msg['text']}"
                        self._direct_add_to_chat_area(user_id, formatted_msg)
                    else:
                        formatted_msg = f"[{time_str}] ➤ {user_id}: {msg['text']}"
                        self._direct_add_to_chat_area(user_id, formatted_msg)
        except Exception as e:
            logger.error(
                f"Error cargando historial de mensajes con {user_id}: {e}",
                exc_info=True,
            )

    def load_all_message_history(self):
        """Carga todo el historial de mensajes guardados y añade los contactos previos
        a la lista de usuarios."""
        try:
            self._loading_all_history = True

            self.peer.load_message_history()

            with self.peer._message_history_lock:
                past_contacts = list(self.peer._message_history.keys())

            if not past_contacts:
                logger.info("No se encontró historial de conversaciones previas")
                self._loading_all_history = False
                return

            logger.info(
                f"Encontrados {len(past_contacts)} contactos previos con historial"
            )

            for contact_id in past_contacts:
                if contact_id not in self.chat_history:
                    self.add_contact_to_ui(contact_id)

            for contact_id in past_contacts:
                history = self.peer.get_message_history(contact_id)
                if history and self.show_history:
                    for msg in history:
                        time_str = msg["timestamp"].strftime("%H:%M:%S")
                        if msg["from"] == "self":
                            formatted_msg = f"[{time_str}] Tú: {msg['text']}"
                            self._direct_add_to_chat_area(contact_id, formatted_msg)
                        else:
                            formatted_msg = (
                                f"[{time_str}] ➤ {contact_id}: {msg['text']}"
                            )
                            self._direct_add_to_chat_area(contact_id, formatted_msg)

            self.process_all_pending_updates()

            self.append_to_chat(
                "Sistema",
                f"Se han cargado {len(past_contacts)} conversaciones previas con historial",
            )

            self._loading_all_history = False

        except Exception as e:
            logger.error(
                f"Error cargando el historial de mensajes completo: {e}", exc_info=True
            )

    def on_file_progress(self, user_id, file_path, progress, status):
        """Callback para actualizaciones de progreso de transferencias"""
        try:
            clean_user_id = user_id.strip() if isinstance(user_id, str) else user_id
            filename = os.path.basename(file_path)
            file_key = f"{clean_user_id}_{filename}"

            if status == "iniciando":
                if not hasattr(self, "progress_window") or self.progress_window is None:
                    self.update_queue.put(lambda: self.create_progress_window())

                if file_key not in self.file_progress_bars:
                    self.update_queue.put(
                        lambda: self.create_progress_bar(clean_user_id, filename)
                    )

                    def update_status():
                        active_count = len(self.file_progress_bars)
                        if hasattr(self, "transfer_status"):
                            try:
                                self.transfer_status.configure(
                                    text=f"{active_count} transferencias activas"
                                )
                            except Exception as e:
                                logger.warning(
                                    f"Error al actualizar estado de transferencias: {e}"
                                )

                    self.update_queue.put(update_status)
                else:
                    self.update_queue.put(
                        lambda: self.update_progress_bar(clean_user_id, filename, 0)
                    )

            elif status == "progreso":
                if file_key not in self.file_progress_bars:

                    def create_and_update():
                        self.create_progress_bar(clean_user_id, filename)
                        self.update_progress_bar(clean_user_id, filename, progress)

                    self.update_queue.put(create_and_update)
                else:
                    self.update_queue.put(
                        lambda p=progress: self.update_progress_bar(
                            clean_user_id, filename, p
                        )
                    )

                self.status_var.set(
                    f"Enviando '{filename}' a {clean_user_id}: {progress}% completado"
                )

            elif status == "completado":
                if file_key not in self.file_progress_bars:

                    def create_and_complete():
                        self.create_progress_bar(clean_user_id, filename)
                        self.update_progress_bar(clean_user_id, filename, 100)

                    self.update_queue.put(create_and_complete)
                else:
                    self.update_queue.put(
                        lambda: self.update_progress_bar(clean_user_id, filename, 100)
                    )

                notification_id = f"complete_{clean_user_id}_{filename}"
                msg_text = (
                    f"Archivo '{filename}' enviado correctamente a {clean_user_id}"
                )

                if notification_id not in self.sent_file_notifications:
                    self.append_to_chat("Sistema", msg_text)
                    self.sent_file_notifications.add(notification_id)

                    if len(self.sent_file_notifications) > 50:
                        self.sent_file_notifications.clear()

                self.status_var.set("Listo")

            elif status == "error":
                self.append_to_chat(
                    "Sistema", f"Error enviando archivo '{filename}' a {clean_user_id}"
                )
                self.status_var.set("Error en la transferencia")
                if file_key in self.file_progress_bars:
                    self.update_queue.put(
                        lambda: self.remove_progress_bar(clean_user_id, filename)
                    )

        except Exception as e:
            logger.error(f"Error en callback de progreso: {e}", exc_info=True)

    def process_ui_updates(self):
        """Procesa las actualizaciones de la interfaz de usuario desde la cola"""
        processed = 0
        while not self.update_queue.empty() and processed < 10:
            try:
                update_func = self.update_queue.get_nowait()
                try:
                    update_func()
                except Exception as e:
                    logger.error(
                        f"Error al procesar actualización de UI: {e}", exc_info=True
                    )
                processed += 1
            except queue.Empty:
                break
            except Exception as e:
                logger.error(f"Error inesperado en cola de UI: {e}", exc_info=True)

        pending = self.update_queue.qsize()
        next_interval = 50 if pending > 10 else (100 if pending > 0 else 500)
        self.after(next_interval, self.process_ui_updates)

    def process_all_pending_updates(self):
        """Procesa todas las actualizaciones pendientes en la cola hasta vaciarla"""
        max_iterations = 100
        iterations = 0

        while not self.update_queue.empty() and iterations < max_iterations:
            try:
                update_func = self.update_queue.get_nowait()
                try:
                    update_func()
                except Exception as e:
                    logger.error(
                        f"Error al procesar actualización en lote: {e}", exc_info=True
                    )
            except queue.Empty:
                break
            except Exception as e:
                logger.error(
                    f"Error inesperado procesando cola en lote: {e}", exc_info=True
                )
            iterations += 1

        if iterations >= max_iterations:
            logger.warning(
                "Posible bucle en la cola de actualizaciones: se alcanzó el límite de iteraciones"
            )

    def add_contact_to_ui(self, user_id):
        """Añade un contacto a la interfaz aunque no esté conectado actualmente"""
        clean_user_id = user_id.strip()

        if clean_user_id not in self.chat_history:
            is_online = False
            with self.peer._peers_lock:
                for peer_id in self.peer.peers.keys():
                    if self.peer._normalize_user_id(
                        peer_id
                    ) == self.peer._normalize_user_id(clean_user_id):
                        is_online = True
                        break

            button = ctk.CTkButton(
                self.users_list,
                text=clean_user_id + (" 🟢" if is_online else " 📜"),
                command=lambda uid=clean_user_id: self.select_user(uid),
                font=("Arial", 12),
                fg_color=(
                    ("#4a90e2", "#1f6aa5") if is_online else ("#888888", "#555555")
                ),
            )
            button.pack(fill="x", padx=5, pady=2)

            chat_area = ctk.CTkTextbox(
                self.chat_container,
                font=("Arial", 12),
                wrap="word",
                state="disabled",
                width=800,
                height=600,
            )

            is_dark = ctk.get_appearance_mode().lower() == "dark"

            chat_area.tag_config("sistema", foreground=self.success_color[is_dark])
            chat_area.tag_config("error", foreground=self.error_color[is_dark])
            chat_area.tag_config("warning", foreground=self.warning_color[is_dark])

            self.chat_history[clean_user_id] = {
                "button": button,
                "chat": chat_area,
                "online": is_online,
                "unread": 0,
            }

            logger.debug(
                f"Creado nuevo contacto en la UI: {clean_user_id} (online: {is_online})"
            )


if __name__ == "__main__":
    app = LCPChat()
    app.mainloop()
