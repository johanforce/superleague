# -*- coding: utf-8 -*-
"""Super League Hub: one launcher for Simulator, Award Recalculator and data transfer."""
from __future__ import annotations

import os
import subprocess
import sys
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from season_archive import (
    SeasonArchiveError,
    load_season_archives,
    preview_season_folder,
    season_folder_summary,
    write_season_archive,
)
from sl_database import SuperLeagueDatabase

APP_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = APP_DIR / "src"
MEMORY_DIR = APP_DIR / "memory"


class SuperLeagueHub:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Super League · Control Center")
        self.root.geometry("820x650")
        self.root.minsize(720, 580)
        self.root.configure(bg="#101827")
        self.children: dict[str, subprocess.Popen] = {}
        self.last_import_file: Path | None = None
        self._setup_style()
        self._build_ui()
        self._refresh_status()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(1200, self._poll_children)

    def _setup_style(self):
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Hub.TButton", font=("Arial", 11, "bold"), padding=(14, 11))
        style.configure("Hub.Small.TButton", font=("Arial", 10), padding=(10, 7))
        style.configure("Hub.Treeview", rowheight=27, font=("Arial", 9))
        style.configure("Hub.Treeview.Heading", font=("Arial", 9, "bold"))

    def _build_ui(self):
        shell = tk.Frame(self.root, bg="#101827")
        shell.pack(fill="both", expand=True, padx=28, pady=24)

        header = tk.Frame(shell, bg="#101827")
        header.pack(fill="x", pady=(0, 18))
        tk.Label(header, text="SUPER LEAGUE", bg="#101827", fg="#F1F5F9",
                 font=("Arial", 25, "bold")).pack(anchor="w")
        tk.Label(header, text="CONTROL CENTER  ·  Quản lý mô phỏng và dữ liệu",
                 bg="#101827", fg="#94A3B8", font=("Arial", 10)).pack(anchor="w", pady=(4, 0))

        app_group = self._section(shell, "ỨNG DỤNG")
        app_grid = tk.Frame(app_group, bg="#172235")
        app_grid.pack(fill="x", padx=14, pady=(2, 14))
        self._action_card(
            app_grid, 0, 0, "⚽  MỞ TRÌNH MÔ PHỎNG", "Chạy Super League Simulator với sự nghiệp SQLite hiện tại.",
            "Mở Simulator", self.launch_simulator,
        )
        self._action_card(
            app_grid, 0, 1, "🏆  AWARD RECALCULATOR", "Chỉnh thông số, tính lại danh hiệu và xem Top 20.",
            "Mở Recalculator", self.launch_recalculator,
        )

        data_group = self._section(shell, "DỮ LIỆU MÔ PHỎNG")
        data_grid = tk.Frame(data_group, bg="#172235")
        data_grid.pack(fill="x", padx=14, pady=(2, 14))
        self._action_card(
            data_grid, 0, 0, "⇧  XUẤT DỮ LIỆU MÙA", "Xuất archive từ database ra file JSON trong thư mục ngoài do bạn chọn.",
            "Xuất dữ liệu mùa", self.export_data,
        )
        self._action_card(
            data_grid, 0, 1, "⇩  NẠP DỮ LIỆU MÙA", "Nạp các file JSON đã xuất vào database SQLite duy nhất.",
            "Nạp dữ liệu mùa", self.import_data,
        )

        info = tk.Frame(shell, bg="#172235", highlightbackground="#334155", highlightthickness=1)
        info.pack(fill="both", expand=True, pady=(14, 0))
        top = tk.Frame(info, bg="#172235")
        top.pack(fill="x", padx=16, pady=(14, 8))
        tk.Label(top, text="TRẠNG THÁI PROJECT", bg="#172235", fg="#E2E8F0",
                 font=("Arial", 11, "bold")).pack(side="left")
        ttk.Button(top, text="Mở thư mục", style="Hub.Small.TButton", command=self.open_project_folder).pack(side="right")

        self.project_var = tk.StringVar(value=str(APP_DIR))
        tk.Label(info, textvariable=self.project_var, bg="#172235", fg="#94A3B8",
                 font=("Arial", 9), wraplength=720, justify="left").pack(anchor="w", padx=16)
        self.status_var = tk.StringVar(value="Đang kiểm tra project...")
        tk.Label(info, textvariable=self.status_var, bg="#172235", fg="#A7F3D0",
                 font=("Arial", 10), justify="left", wraplength=720).pack(anchor="w", padx=16, pady=(10, 14))

        footer = tk.Frame(shell, bg="#101827")
        footer.pack(fill="x", pady=(13, 0))
        tk.Label(
            footer,
            text="Toàn bộ dữ liệu nền, sự nghiệp và lịch sử mùa được lưu trong memory/super_league.db. Xuất/nạp mùa chỉ tạo file ở thư mục ngoài do bạn chọn.",
            bg="#101827", fg="#FBBF24", font=("Arial", 9), wraplength=760, justify="left",
        ).pack(anchor="w")
        ttk.Button(footer, text="Làm mới trạng thái", style="Hub.Small.TButton",
                   command=self._refresh_status).pack(anchor="e", pady=(8, 0))

    def _section(self, parent, title: str):
        frame = tk.Frame(parent, bg="#172235", highlightbackground="#334155", highlightthickness=1)
        frame.pack(fill="x", pady=(0, 13))
        tk.Label(frame, text=title, bg="#172235", fg="#CBD5E1", font=("Arial", 10, "bold")).pack(
            anchor="w", padx=14, pady=(12, 8)
        )
        return frame

    def _action_card(self, parent, row, column, title, description, button_label, command):
        parent.grid_columnconfigure(column, weight=1, uniform="cards")
        card = tk.Frame(parent, bg="#1E2C42", highlightbackground="#3B4B63", highlightthickness=1)
        card.grid(row=row, column=column, sticky="nsew", padx=6, pady=6)
        tk.Label(card, text=title, bg="#1E2C42", fg="#F8FAFC", font=("Arial", 11, "bold")).pack(
            anchor="w", padx=13, pady=(13, 6)
        )
        tk.Label(card, text=description, bg="#1E2C42", fg="#A8B6C9", font=("Arial", 9),
                 justify="left", wraplength=320, height=3).pack(anchor="w", padx=13, fill="x")
        ttk.Button(card, text=button_label, style="Hub.TButton", command=command).pack(
            anchor="w", padx=13, pady=(9, 13)
        )

    def _active_children(self) -> list[str]:
        active = []
        for name, process in list(self.children.items()):
            if process.poll() is None:
                active.append(name)
        return active

    def _require_apps_closed(self) -> bool:
        active = self._active_children()
        if active:
            messagebox.showwarning(
                "Ứng dụng đang mở",
                "Hãy đóng ứng dụng đang chạy từ Control Center trước khi xuất/nạp dữ liệu:\n\n"
                + "\n".join(active),
                parent=self.root,
            )
            return False
        return messagebox.askyesno(
            "Xác nhận trạng thái",
            "Hãy chắc chắn bạn đã đóng mọi cửa sổ Simulator và Award Recalculator (kể cả cửa sổ mở ngoài Control Center).\n\nTiếp tục?",
            parent=self.root,
        )

    def _launch(self, key: str, script_name: str, label: str):
        existing = self.children.get(key)
        if existing and existing.poll() is None:
            messagebox.showinfo("Đang chạy", f"{label} đã được mở rồi.", parent=self.root)
            return
        script = SRC_DIR / script_name
        if not script.is_file():
            messagebox.showerror(
                "Thiếu file", f"Không tìm thấy {script_name} trong thư mục project:\n{APP_DIR}", parent=self.root
            )
            return
        try:
            process = subprocess.Popen([sys.executable, str(script)], cwd=str(APP_DIR))
            self.children[key] = process
            self.status_var.set(f"Đã mở {label} (PID {process.pid}).")
        except OSError as exc:
            messagebox.showerror("Không mở được ứng dụng", str(exc), parent=self.root)

    def launch_simulator(self):
        self._launch("Simulator", "main.py", "Simulator")

    def launch_recalculator(self):
        self._launch("Award Recalculator", "recalculate_awards.py", "Award Recalculator")

    def _database(self):
        return SuperLeagueDatabase(MEMORY_DIR / "super_league.db")

    def export_data(self):
        if not self._require_apps_closed():
            return
        try:
            records = self._database().load_season_archives()
            if not records:
                messagebox.showwarning("Chưa có mùa đã hoàn tất", "Database hiện chưa có archive mùa nào.", parent=self.root)
                return
            target = filedialog.askdirectory(
                parent=self.root, title="Chọn thư mục ngoài project để xuất các mùa", mustexist=False,
                initialdir=str(APP_DIR),
            )
            if not target:
                return
            target_path = Path(target)
            target_path.mkdir(parents=True, exist_ok=True)
            for record in records:
                write_season_archive(target_path, record)
            summary = season_folder_summary(target_path)
            season_names = ", ".join(item["label"] for item in summary["seasons"])
            message = (f"Đã xuất {summary['season_count']} mùa vào:\n{summary['folder']}\n\n"
                       f"Các mùa: {season_names}\n"
                       f"Dung lượng: {summary['total_bytes'] / (1024 * 1024):.2f} MiB\n\n"
                       "Các file chỉ được tạo trong thư mục bạn chọn; memory/ vẫn chỉ có super_league.db.")
            self.status_var.set(f"Đã xuất {summary['season_count']} mùa ra thư mục ngoài.")
            messagebox.showinfo("Xuất dữ liệu thành công", message, parent=self.root)
        except Exception as exc:
            messagebox.showerror("Không xuất được dữ liệu mùa", str(exc), parent=self.root)

    def import_data(self):
        if not self._require_apps_closed():
            return
        source = filedialog.askdirectory(
            parent=self.root, title="Chọn thư mục chứa các file mùa YYYY-YYYY.json", mustexist=True,
            initialdir=str(APP_DIR),
        )
        if not source:
            return
        try:
            preview = preview_season_folder(source)
            records, errors = load_season_archives(source, strict=True)
            if errors:
                raise SeasonArchiveError("\n".join(errors))
        except Exception as exc:
            messagebox.showerror("Thư mục mùa không hợp lệ", str(exc), parent=self.root)
            return
        lines = [f"{row['label']} · {row['match_count']} trận · {row['player_count']} hồ sơ cầu thủ · {row['file']}"
                 for row in preview["seasons"]]
        message = (
            f"Thư mục nguồn: {preview['folder']}\n"
            f"Số mùa tìm thấy: {preview['season_count']}\n"
            f"Tổng dung lượng: {preview['total_bytes'] / (1024 * 1024):.2f} MiB\n\n"
            + "\n".join(lines)
            + "\n\nThao tác sẽ thay toàn bộ archive mùa hiện có trong memory/super_league.db. "
              "Career hiện tại, cầu thủ, hợp đồng và tỷ số đang diễn ra không bị thay đổi. "
              "Không tạo backup hoặc file phụ trong memory/."
        )
        if not messagebox.askyesno("Xem trước và xác nhận nạp mùa", message, parent=self.root):
            return
        try:
            count = self._database().replace_season_archives(records)
            self.status_var.set(f"Đã nạp {count} archive mùa vào SQLite.")
            should_launch = messagebox.askyesno(
                "Nạp dữ liệu thành công",
                f"Đã nạp {count} mùa vào memory/super_league.db.\n\nMở Simulator ngay?",
                parent=self.root,
            )
            if should_launch:
                self.launch_simulator()
        except Exception as exc:
            messagebox.showerror("Nạp dữ liệu thất bại", f"Database chưa được cập nhật nếu thao tác ghi thất bại.\n\n{exc}", parent=self.root)

    def open_project_folder(self):
        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", str(APP_DIR)])
            elif os.name == "nt":
                os.startfile(str(APP_DIR))  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", str(APP_DIR)])
        except Exception as exc:
            messagebox.showerror("Không mở được thư mục", str(exc), parent=self.root)

    def _refresh_status(self):
        checks = []
        for filename, label in (("main.py", "Simulator"), ("recalculate_awards.py", "Award Recalculator"),
                                ("sl_game.py", "Game engine")):
            checks.append(f"{'✓' if (SRC_DIR / filename).is_file() else '✗'} {label}")
        try:
            status = self._database().status()
            checks.append(f"SQLite: {status['integrity']} · schema {status['schema_version']}")
            checks.append(f"Career: {'đã tạo' if status['has_career'] else 'chưa tạo'}")
            checks.append(f"Season archives: {status['tables'].get('season_archives', 0)} mùa")
        except Exception as exc:
            checks.append(f"Database: lỗi ({exc})")
        checks.append("Memory storage: 1 file · super_league.db")
        checks.append(f"Python: {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")
        self.status_var.set("   |   ".join(checks))

    def _poll_children(self):
        finished = []
        for name, process in list(self.children.items()):
            code = process.poll()
            if code is not None:
                finished.append(f"{name} đã đóng (exit {code}).")
                self.children.pop(name, None)
        if finished:
            self.status_var.set("\n".join(finished))
        self.root.after(1200, self._poll_children)

    def _on_close(self):
        # Child applications are intentionally not terminated when the hub closes.
        self.root.destroy()


def main() -> int:
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print(f"Không khởi tạo được giao diện Tkinter: {exc}", file=sys.stderr)
        return 2
    SuperLeagueHub(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
