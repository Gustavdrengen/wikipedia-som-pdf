#!/usr/bin/env python3
"""Tkinter entrypoint for the article PDF generator."""

import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from src.app import generate
from src.config import DEFAULT_REQUEST_DELAY


def main() -> None:
    root = tk.Tk()
    root.title("Artikler som PDF")
    root.geometry("720x560")
    messages: queue.Queue[str] = queue.Queue()

    input_path = tk.StringVar()
    output_path = tk.StringVar(value="Noter")
    epub_path = tk.StringVar()
    workers = tk.IntVar(value=2)
    delay = tk.DoubleVar(value=DEFAULT_REQUEST_DELAY)
    refresh = tk.BooleanVar(value=False)

    frame = ttk.Frame(root, padding=12)
    frame.pack(fill="both", expand=True)
    frame.columnconfigure(1, weight=1)
    ttk.Label(frame, text="Inputfil:").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=4)
    ttk.Entry(frame, textvariable=input_path).grid(row=0, column=1, sticky="ew", pady=4)
    ttk.Button(frame, text="Vælg", command=lambda: input_path.set(filedialog.askopenfilename())).grid(row=0, column=2, padx=(8, 0), pady=4)
    ttk.Label(frame, text="Outputmappe:").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=4)
    ttk.Entry(frame, textvariable=output_path).grid(row=1, column=1, sticky="ew", pady=4)
    ttk.Button(frame, text="Vælg", command=lambda: output_path.set(filedialog.askdirectory())).grid(row=1, column=2, padx=(8, 0), pady=4)
    ttk.Label(frame, text="PDF-workers:").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=4)
    ttk.Spinbox(frame, from_=1, to=32, textvariable=workers, width=8).grid(row=2, column=1, sticky="w", pady=4)
    ttk.Label(frame, text="Request delay (sek.):").grid(row=3, column=0, sticky="w", padx=(0, 8), pady=4)
    ttk.Spinbox(frame, from_=0, to=60, increment=0.1, textvariable=delay, width=8).grid(row=3, column=1, sticky="w", pady=4)
    ttk.Label(frame, text="EPUB (valgfri):").grid(row=4, column=0, sticky="w", padx=(0, 8), pady=4)
    ttk.Entry(frame, textvariable=epub_path).grid(row=4, column=1, sticky="ew", pady=4)
    ttk.Button(
        frame,
        text="Vælg",
        command=lambda: epub_path.set(
            filedialog.asksaveasfilename(
                defaultextension=".epub",
                filetypes=[("EPUB", "*.epub")],
            )
        ),
    ).grid(row=4, column=2, padx=(8, 0), pady=4)
    ttk.Checkbutton(frame, text="Hent artikler igen (ignorér cache)", variable=refresh).grid(
        row=5, column=1, sticky="w", pady=4
    )
    log_widget = tk.Text(frame, height=18, state="disabled", wrap="word")
    log_widget.grid(row=6, column=0, columnspan=3, sticky="nsew", pady=(12, 8))
    frame.rowconfigure(6, weight=1)
    start_button = ttk.Button(frame, text="Start")
    start_button.grid(row=7, column=0, columnspan=3, pady=4)

    def write_log(message: str, **_: object) -> None:
        messages.put(message)

    def run() -> None:
        try:
            generate(
                Path(input_path.get()),
                Path(output_path.get()),
                workers.get(),
                delay.get(),
                write_log,
                refresh=refresh.get(),
                epub=Path(epub_path.get()) if epub_path.get().strip() else None,
            )
        except Exception as exc:
            messages.put(f"ERROR: {exc}")
        finally:
            messages.put("__DONE__")

    def start() -> None:
        if not input_path.get():
            messagebox.showerror("Mangler input", "Vælg en inputfil først.")
            return
        start_button.configure(state="disabled")
        threading.Thread(target=run, daemon=True).start()

    def poll() -> None:
        try:
            while True:
                message = messages.get_nowait()
                if message == "__DONE__":
                    start_button.configure(state="normal")
                else:
                    log_widget.configure(state="normal")
                    log_widget.insert("end", message + "\n")
                    log_widget.see("end")
                    log_widget.configure(state="disabled")
        except queue.Empty:
            pass
        root.after(100, poll)

    start_button.configure(command=start)
    poll()
    root.mainloop()


if __name__ == "__main__":
    main()
