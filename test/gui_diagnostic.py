#!/usr/bin/env python3
"""Kiểm tra Tkinter độc lập, không đọc/ghi save của simulator."""
import sys
import tkinter as tk
from tkinter import ttk


def main():
    print(f"Python: {sys.version}")
    print(f"Executable: {sys.executable}")
    print(f"Tkinter: {tk.TkVersion}")
    root = tk.Tk()
    root.title("Super League · Kiểm tra Tkinter")
    root.geometry("680x420")
    root.minsize(520, 320)
    root.configure(bg="#10151d")
    try:
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("TFrame", background="#10151d")
        style.configure("TLabel", background="#10151d", foreground="#e8eef5", font=("Arial", 12))
        style.configure("Title.TLabel", foreground="#31d0aa", font=("Arial", 22, "bold"))
        style.configure("TNotebook", background="#10151d")
        style.configure("TNotebook.Tab", padding=(14, 8))
    except tk.TclError as exc:
        print(f"Không đổi được theme Tk: {exc}")
    wrap = ttk.Frame(root, padding=24)
    wrap.pack(fill="both", expand=True)
    ttk.Label(wrap, text="TKINTER ĐANG HOẠT ĐỘNG", style="Title.TLabel").pack(anchor="w", pady=(0, 10))
    ttk.Label(wrap, text="Nếu bạn nhìn thấy nội dung, Python có thể mở cửa sổ GUI trên máy này.").pack(anchor="w", pady=(0, 18))
    tabs = ttk.Notebook(wrap)
    tabs.pack(fill="both", expand=True)
    for title, message in (("Tổng quan", "Cửa sổ, chữ và màu nền đang được render."),
                           ("Bảng dữ liệu", "Kiểm tra notebook/tab của Tkinter.")):
        page = ttk.Frame(tabs, padding=18)
        tabs.add(page, text=title)
        ttk.Label(page, text=message, wraplength=560).pack(anchor="w", pady=10)
    ttk.Button(wrap, text="Đóng cửa sổ", command=root.destroy).pack(anchor="e", pady=(14, 0))
    print(f"Tcl/Tk patchlevel: {root.tk.call('info', 'patchlevel')}")
    print(f"Screen: {root.winfo_screenwidth()}x{root.winfo_screenheight()}")
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
