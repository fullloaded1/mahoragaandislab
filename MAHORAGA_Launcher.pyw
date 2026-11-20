"""
MAHORAGA Desktop GUI Launcher (AndisLab).

Runs without a black CMD window (via pythonw.exe) so Windows QuickEdit Mode
never freezes uvicorn or harvey run. Provides 1-click Start / Stop / Restart,
live port health indicators (OpenWA 2785, Dashboard 5555, Engine), and log viewer.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import scrolledtext

PROJECT_ROOT = Path(__file__).resolve().parent
PYTHON_EXE = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
TOKEN_FILE = PROJECT_ROOT / "data" / "dashboard-token"

CREATE_NO_WINDOW = 0x08000000


def _is_port_open(port: int) -> bool:
    for host in ("127.0.0.1", "::1"):
        try:
            family = socket.AF_INET6 if ":" in host else socket.AF_INET
            with socket.socket(family, socket.SOCK_STREAM) as s:
                s.settimeout(0.5)
                if s.connect_ex((host, port)) == 0:
                    return True
        except Exception:
            pass
    return False


class MahoragaLauncher(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("MAHORAGA Launcher — AndisLab AI Sales Engine")
        self.geometry("760x520")
        self.configure(bg="#0f1117")
        self.resizable(True, True)

        self.dash_proc: subprocess.Popen | None = None
        self.run_proc: subprocess.Popen | None = None

        self._build_ui()
        self._poll_status()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self) -> None:
        hdr = tk.Frame(self, bg="#161b26", padx=16, pady=12)
        hdr.pack(fill="x")

        tk.Label(
            hdr,
            text="MAHORAGA AI SALES ENGINE",
            fg="#f8fafc",
            bg="#161b26",
            font=("Segoe UI", 14, "bold"),
        ).pack(anchor="w")
        tk.Label(
            hdr,
            text="PT Andis Sentral Laboratorium — Desktop Control Center (Anti-Freeze, Tanpa CMD)",
            fg="#94a3b8",
            bg="#161b26",
            font=("Segoe UI", 9),
        ).pack(anchor="w", pady=(2, 0))

        # Status pills
        st_frame = tk.Frame(self, bg="#0f1117", padx=16, pady=10)
        st_frame.pack(fill="x")

        self.lbl_openwa = self._make_badge(st_frame, "OpenWA (2785): Checking...")
        self.lbl_dash = self._make_badge(st_frame, "Dashboard (5555): Stopped")
        self.lbl_engine = self._make_badge(st_frame, "Engine (harvey run): Stopped")

        # Action buttons
        btn_frame = tk.Frame(self, bg="#0f1117", padx=16, pady=4)
        btn_frame.pack(fill="x")

        self.btn_start = tk.Button(
            btn_frame,
            text="▶ Start Semua",
            command=self.start_all,
            bg="#2563eb",
            fg="#ffffff",
            activebackground="#1d4ed8",
            activeforeground="#ffffff",
            relief="flat",
            padx=14,
            pady=7,
            font=("Segoe UI", 10, "bold"),
            cursor="hand2",
        )
        self.btn_start.pack(side="left", padx=(0, 8))

        self.btn_restart = tk.Button(
            btn_frame,
            text="🔄 Restart",
            command=self.restart_all,
            bg="#1e293b",
            fg="#e2e8f0",
            activebackground="#334155",
            relief="flat",
            padx=12,
            pady=7,
            font=("Segoe UI", 10),
            cursor="hand2",
        )
        self.btn_restart.pack(side="left", padx=(0, 8))

        self.btn_stop = tk.Button(
            btn_frame,
            text="⏹ Stop Semua",
            command=self.stop_all,
            bg="#7f1d1d",
            fg="#fecaca",
            activebackground="#991b1b",
            relief="flat",
            padx=12,
            pady=7,
            font=("Segoe UI", 10),
            cursor="hand2",
        )
        self.btn_stop.pack(side="left", padx=(0, 8))

        self.btn_open = tk.Button(
            btn_frame,
            text="🌐 Buka Dashboard",
            command=self.open_dashboard,
            bg="#065f46",
            fg="#d1fae5",
            activebackground="#047857",
            relief="flat",
            padx=12,
            pady=7,
            font=("Segoe UI", 10, "bold"),
            cursor="hand2",
        )
        self.btn_open.pack(side="right")

        # Log area
        log_wrap = tk.Frame(self, bg="#0f1117", padx=16, pady=10)
        log_wrap.pack(fill="both", expand=True)

        self.log_box = scrolledtext.ScrolledText(
            log_wrap,
            bg="#0b0d12",
            fg="#cbd5e1",
            insertbackground="#ffffff",
            font=("Consolas", 9),
            relief="flat",
            wrap="word",
            state="disabled",
        )
        self.log_box.pack(fill="both", expand=True)
        self.append_log("Siap. Klik '▶ Start Semua' untuk menyalakan Dashboard + Engine MAHORAGA.")

    def _make_badge(self, parent: tk.Frame, text: str) -> tk.Label:
        lbl = tk.Label(
            parent,
            text=text,
            fg="#cbd5e1",
            bg="#1e293b",
            padx=10,
            pady=5,
            font=("Segoe UI", 9, "bold"),
        )
        lbl.pack(side="left", padx=(0, 8))
        return lbl

    def append_log(self, line: str) -> None:
        def _do():
            self.log_box.configure(state="normal")
            self.log_box.insert("end", line.rstrip() + "\n")
            # Keep last 500 lines to stay light
            lines = int(self.log_box.index("end-1c").split(".")[0])
            if lines > 500:
                self.log_box.delete("1.0", f"{lines - 500}.0")
            self.log_box.see("end")
            self.log_box.configure(state="disabled")

        self.after(0, _do)

    def _stream_output(self, proc: subprocess.Popen, prefix: str) -> None:
        if not proc.stdout:
            return
        try:
            for raw in proc.stdout:
                if raw:
                    self.append_log(f"[{prefix}] {raw.rstrip()}")
        except Exception:
            pass

    def _kill_old_processes(self) -> None:
        # Clean up any leftover harvey dashboard / harvey run processes cleanly
        cmd = (
            "Get-CimInstance Win32_Process -Filter \"name = 'python.exe'\" | "
            "Where-Object { $_.CommandLine -like '*-m harvey dashboard*' -or $_.CommandLine -like '*-m harvey run*' } | "
            "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; "
            "Stop-Process -Name cloudflared -Force -ErrorAction SilentlyContinue"
        )
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", cmd],
            creationflags=CREATE_NO_WINDOW,
            capture_output=True,
        )

    def _ensure_openwa(self) -> None:
        if _is_port_open(2785):
            self.append_log("[OpenWA] Gateway sudah aktif di port 2785.")
            return
        self.append_log("[OpenWA] Belum aktif, mencoba menyalakan container Docker 'openwa'...")
        subprocess.run(["docker", "start", "openwa"], creationflags=CREATE_NO_WINDOW, capture_output=True)

    def start_all(self) -> None:
        def _worker():
            if not PYTHON_EXE.exists():
                self.append_log(f"[ERROR] Tidak menemukan {PYTHON_EXE}")
                return
            self.append_log("[Launcher] Membersihkan sesi lama & memeriksa OpenWA...")
            self._kill_old_processes()
            self._ensure_openwa()

            env = os.environ.copy()
            env["PYTHONUNBUFFERED"] = "1"
            env["PYTHONIOENCODING"] = "utf-8"

            self.append_log("[Launcher] Menyalakan Dashboard MAHORAGA (Port 5555 + Tunnel)...")
            self.dash_proc = subprocess.Popen(
                [str(PYTHON_EXE), "-m", "harvey", "dashboard", "--allow-remote", "--tunnel", "--send-wa"],
                cwd=str(PROJECT_ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                creationflags=CREATE_NO_WINDOW,
            )
            threading.Thread(target=self._stream_output, args=(self.dash_proc, "DASH"), daemon=True).start()

            self.append_log("[Launcher] Menyalakan Engine Heartbeat (harvey run)...")
            self.run_proc = subprocess.Popen(
                [str(PYTHON_EXE), "-m", "harvey", "run"],
                cwd=str(PROJECT_ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                creationflags=CREATE_NO_WINDOW,
            )
            threading.Thread(target=self._stream_output, args=(self.run_proc, "ENGINE"), daemon=True).start()

        threading.Thread(target=_worker, daemon=True).start()

    def stop_all(self) -> None:
        def _worker():
            self.append_log("[Launcher] Menghentikan Dashboard & Engine...")
            for p in (self.dash_proc, self.run_proc):
                if p and p.poll() is None:
                    try:
                        p.terminate()
                    except Exception:
                        pass
            self._kill_old_processes()
            self.dash_proc = None
            self.run_proc = None
            self.append_log("[Launcher] Semua proses MAHORAGA berhenti dengan aman.")

        threading.Thread(target=_worker, daemon=True).start()

    def restart_all(self) -> None:
        def _worker():
            self.append_log("[Launcher] Merestart semua layanan...")
            self._kill_old_processes()
            self.dash_proc = None
            self.run_proc = None
            self.start_all()

        threading.Thread(target=_worker, daemon=True).start()

    def open_dashboard(self) -> None:
        tok = TOKEN_FILE.read_text(encoding="utf-8").strip() if TOKEN_FILE.exists() else ""
        url = f"http://127.0.0.1:5555/#token={tok}" if tok else "http://127.0.0.1:5555/"
        webbrowser.open(url)

    def _poll_status(self) -> None:
        def _check():
            wa_ok = _is_port_open(2785)
            dash_ok = _is_port_open(5555)
            eng_ok = self.run_proc is not None and self.run_proc.poll() is None

            def _apply():
                self.lbl_openwa.config(
                    text=f"OpenWA (2785): {'ONLINE' if wa_ok else 'OFFLINE'}",
                    bg="#064e3b" if wa_ok else "#7f1d1d",
                    fg="#6ee7b7" if wa_ok else "#fecaca",
                )
                self.lbl_dash.config(
                    text=f"Dashboard (5555): {'ONLINE' if dash_ok else 'STOPPED'}",
                    bg="#064e3b" if dash_ok else "#1e293b",
                    fg="#6ee7b7" if dash_ok else "#94a3b8",
                )
                self.lbl_engine.config(
                    text=f"Engine: {'RUNNING' if eng_ok else 'STOPPED'}",
                    bg="#064e3b" if eng_ok else "#1e293b",
                    fg="#6ee7b7" if eng_ok else "#94a3b8",
                )

            self.after(0, _apply)

        threading.Thread(target=_check, daemon=True).start()
        self.after(3000, self._poll_status)

    def _on_close(self) -> None:
        self.stop_all()
        self.after(400, self.destroy)


if __name__ == "__main__":
    app = MahoragaLauncher()
    app.mainloop()
