"""Offline, batch face redaction desktop interface.

The UI performs no network requests. All decoding, detection and exports run in
workers; Tk widgets are touched only from the main thread.
"""
from __future__ import annotations

import argparse
import os
import queue
import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from engine import (
    CancelledError,
    EngineError,
    FaceEngine,
    MediaSelection,
    Settings,
    process_file,
    read_preview,
    validate_local_path,
)
import cv2
import numpy as np
from PIL import Image, ImageTk

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".wmv", ".mpeg", ".mpg", ".mts", ".m2ts"}
SUPPORTED_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS
MODE_NAMES = {"自动保留最大人脸": "largest", "手动保留指定人脸": "selected", "遮挡全部人脸": "all"}
EFFECT_NAMES = {"马赛克": "mosaic", "高斯模糊": "blur", "纯色遮挡": "solid"}
QUALITY_NAMES = {"标准 · 640": 640, "精细 · 960": 960, "高精度 · 1280": 1280, "快速 · 320": 320}


def _require_local_path(path: str) -> str:
    """Reject URLs, UNC shares and mapped remote drives before file I/O."""
    try:
        return str(validate_local_path(str(path).strip()))
    except EngineError as exc:
        raise ValueError(str(exc)) from exc


def _selection_snapshot(selection: MediaSelection) -> MediaSelection:
    """Copy mutable selections while sharing an immutable reference image."""
    return MediaSelection(
        keep_faces=[np.asarray(face).copy() for face in selection.keep_faces],
        manual_boxes=[list(box) for box in selection.manual_boxes],
        reference_frame=selection.reference_frame,
        anchor_frame=selection.anchor_frame,
    )


@dataclass
class QueueItem:
    path: str
    selection: MediaSelection = field(default_factory=MediaSelection)
    mode_override: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    status: str = "待处理"


class FaceMaskApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("人脸马赛克工作室 · 离线版")
        screen_width, screen_height = root.winfo_screenwidth(), root.winfo_screenheight()
        width = min(1340, max(860, screen_width - 60))
        height = min(870, max(540, screen_height - 80))
        root.geometry(f"{width}x{height}+{max(0, (screen_width - width) // 2)}+{max(0, (screen_height - height) // 2)}")
        root.minsize(min(1000, width), min(640, height))
        icon = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1])) / "assets" / "app.ico"
        if icon.is_file():
            try:
                root.iconbitmap(str(icon))
            except tk.TclError:
                pass
        root.configure(bg="#edf1f7")
        self.events: queue.Queue = queue.Queue()
        self.preview_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="preview")
        self.preview_engine: FaceEngine | None = None
        self.preview_generation = 0
        self.settings_after: str | None = None
        self.seek_after: str | None = None
        self.items: dict[str, QueueItem] = {}
        self.current_id: str | None = None
        self.current_frame: np.ndarray | None = None
        self.current_faces = np.empty((0, 15), dtype=np.float32)
        self.current_keep: set[int] = set()
        self.current_masked: np.ndarray | None = None
        self.current_index = 0
        self.current_metadata: dict[str, Any] = {}
        self.photo: ImageTk.PhotoImage | None = None
        self.canvas_transform = (1.0, 0.0, 0.0)
        self.drag_origin: tuple[float, float] | None = None
        self.drag_item: int | None = None
        self.busy = False
        self.cancel_event = threading.Event()
        self.closed = False
        self.preview_loading = False
        self.ui_lock_widgets: list[Any] = []
        self.default_states: dict[Any, str] = {}
        self.mode_var = tk.StringVar(value="自动保留最大人脸")
        self.effect_var = tk.StringVar(value="马赛克")
        self.quality_var = tk.StringVar(value="标准 · 640")
        self.strength_var = tk.IntVar(value=18)
        self.padding_var = tk.IntVar(value=25)
        self.threshold_var = tk.IntVar(value=65)
        self.audio_var = tk.BooleanVar(value=True)
        self.view_var = tk.StringVar(value="source")
        self.tool_var = tk.StringVar(value="select")
        self.recursive_var = tk.BooleanVar(value=True)
        app_dir = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
        self.output_var = tk.StringVar(value=str(app_dir / "处理结果"))
        self.status_var = tk.StringVar(value="添加图片或视频，选择保留的人脸，然后批量导出。")
        self.preview_info_var = tk.StringVar(value="尚未选择文件")
        self.selection_info_var = tk.StringVar(value="绿色保留主角，橙色遮挡背景人物。")
        self.progress_var = tk.DoubleVar(value=0)
        self.progress_text_var = tk.StringVar(value="等待开始")
        self.seek_var = tk.DoubleVar(value=0)
        self.frame_time_var = tk.StringVar(value="00:00 / 00:00")
        self._style()
        self._build()
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.bind("<Delete>", lambda _e: self._remove_selected())
        root.after(80, self._poll)

    def _style(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(".", font=("Microsoft YaHei UI", 10), foreground="#25324a", background="#edf1f7")
        style.configure("TFrame", background="#edf1f7")
        style.configure("Card.TFrame", background="#ffffff")
        style.configure("TLabel", background="#ffffff", foreground="#25324a")
        style.configure("Muted.TLabel", foreground="#6b7890", background="#ffffff", font=("Microsoft YaHei UI", 9))
        style.configure("Section.TLabel", foreground="#17263f", background="#ffffff", font=("Microsoft YaHei UI", 11, "bold"))
        style.configure("TButton", padding=(12, 7), background="#ffffff", bordercolor="#d4dce8", lightcolor="#ffffff", darkcolor="#ffffff")
        style.map("TButton", background=[("active", "#e9eff8")], foreground=[("disabled", "#a4acb9")])
        style.configure("Accent.TButton", foreground="#ffffff", background="#2864e8", bordercolor="#2864e8", padding=(16, 10), font=("Microsoft YaHei UI", 11, "bold"))
        style.map("Accent.TButton", background=[("disabled", "#a4b9e7"), ("active", "#174ecc")], foreground=[("disabled", "#eef3ff")])
        style.configure("TRadiobutton", background="#ffffff", padding=(0, 4))
        style.configure("TCheckbutton", background="#ffffff", padding=(0, 4))
        style.configure("TCombobox", padding=5, fieldbackground="#ffffff", background="#ffffff")
        style.map("TCombobox", fieldbackground=[("readonly", "#ffffff"), ("disabled", "#f1f3f7")])
        style.configure("TEntry", padding=6, fieldbackground="#ffffff")
        style.configure("Treeview", background="#ffffff", fieldbackground="#ffffff", rowheight=34, borderwidth=0, font=("Microsoft YaHei UI", 9))
        style.configure("Treeview.Heading", background="#f1f4f9", foreground="#65738a", relief="flat", padding=7, font=("Microsoft YaHei UI", 9))
        style.map("Treeview", background=[("selected", "#e5eeff")], foreground=[("selected", "#194cad")])
        style.configure("Horizontal.TProgressbar", troughcolor="#e3e9f2", background="#2864e8", borderwidth=0)
        style.configure("TLabelframe", background="#ffffff", bordercolor="#e2e7f0")
        style.configure("TLabelframe.Label", background="#ffffff", foreground="#63728b")
        style.configure("TPanedwindow", background="#edf1f7")

    def _lockable(self, widget, state="normal"):
        self.ui_lock_widgets.append(widget)
        self.default_states[widget] = state
        return widget

    def _build(self):
        header = tk.Frame(self.root, background="#17243c", height=80)
        header.pack(fill="x")
        header.pack_propagate(False)
        tk.Label(header, text="人脸马赛克工作室", font=("Microsoft YaHei UI", 21, "bold"), fg="#ffffff", bg="#17243c").pack(side="left", padx=24, pady=18)
        tk.Label(header, text="批量图片 / 视频 · 运动跟踪", font=("Microsoft YaHei UI", 10), fg="#bdc9de", bg="#17243c").pack(side="left", padx=10)
        tk.Label(header, text="●  本地处理 · 无需联网", font=("Microsoft YaHei UI", 10), fg="#9de1c8", bg="#17243c").pack(side="right", padx=25)
        content = ttk.Frame(self.root, padding=(16, 14, 16, 10))
        content.pack(fill="both", expand=True)
        content.columnconfigure(0, weight=1)
        content.rowconfigure(0, weight=1)
        panes = ttk.Panedwindow(content, orient="horizontal")
        panes.grid(row=0, column=0, sticky="nsew")
        queue_card = ttk.Frame(panes, style="Card.TFrame", padding=14)
        preview_card = ttk.Frame(panes, style="Card.TFrame", padding=14)
        settings_card = ttk.Frame(panes, style="Card.TFrame", padding=16)
        panes.add(queue_card, weight=2)
        panes.add(preview_card, weight=5)
        panes.add(settings_card, weight=2)
        self.root.after(150, lambda: self._set_sashes(panes))
        self._build_queue(queue_card)
        self._build_preview(preview_card)
        self._build_settings(settings_card)
        self._build_footer(content)

    def _set_sashes(self, panes):
        try:
            width = panes.winfo_width()
            panes.sashpos(0, max(220, int(width * .235)))
            panes.sashpos(1, max(730, width - 285))
        except tk.TclError:
            pass

    def _build_queue(self, parent):
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(3, weight=1)
        title = ttk.Frame(parent, style="Card.TFrame")
        title.grid(row=0, column=0, sticky="ew")
        ttk.Label(title, text="01  批量文件", style="Section.TLabel").pack(side="left")
        self.count_label = ttk.Label(title, text="0 个", style="Muted.TLabel")
        self.count_label.pack(side="right")
        buttons = ttk.Frame(parent, style="Card.TFrame")
        buttons.grid(row=1, column=0, sticky="ew", pady=(12, 4))
        self._lockable(ttk.Button(buttons, text="＋ 添加文件", command=self._add_files)).pack(side="left", fill="x", expand=True, padx=(0, 6))
        self._lockable(ttk.Button(buttons, text="添加文件夹", command=self._add_folder)).pack(side="left", fill="x", expand=True)
        self._lockable(ttk.Checkbutton(parent, text="文件夹包含子文件夹", variable=self.recursive_var)).grid(row=2, column=0, sticky="w", pady=(0, 8))
        treebox = ttk.Frame(parent, style="Card.TFrame")
        treebox.grid(row=3, column=0, sticky="nsew")
        self.tree = ttk.Treeview(treebox, columns=("name", "status"), show="headings", selectmode="extended")
        self.tree.heading("name", text="文件名")
        self.tree.heading("status", text="状态")
        self.tree.column("name", width=160, minwidth=110, stretch=True)
        self.tree.column("status", width=88, minwidth=75, stretch=False)
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(treebox, orient="vertical", command=self.tree.yview)
        scrollbar.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.bind("<<TreeviewSelect>>", self._queue_selected)
        self.tree.tag_configure("error", foreground="#c43e4a")
        self.tree.tag_configure("done", foreground="#138563")
        bottom = ttk.Frame(parent, style="Card.TFrame")
        bottom.grid(row=4, column=0, sticky="ew", pady=(10, 8))
        self._lockable(ttk.Button(bottom, text="移除所选", command=self._remove_selected)).pack(side="left", padx=(0, 6))
        self._lockable(ttk.Button(bottom, text="清空", command=self._clear)).pack(side="left")
        ttk.Label(parent, text="支持常见图片与视频格式。\n单击文件预览；每个文件可分别调整。", style="Muted.TLabel", wraplength=250).grid(row=5, column=0, sticky="w", pady=(3, 0))

    def _build_preview(self, parent):
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(1, weight=1)
        top = ttk.Frame(parent, style="Card.TFrame")
        top.grid(row=0, column=0, sticky="ew")
        ttk.Label(top, text="02  预览与调整", style="Section.TLabel").pack(side="left")
        self._lockable(ttk.Radiobutton(top, text="原图与选框", variable=self.view_var, value="source", command=self._render)).pack(side="right", padx=(8, 0))
        self._lockable(ttk.Radiobutton(top, text="遮挡效果", variable=self.view_var, value="masked", command=self._render)).pack(side="right")
        self.canvas = tk.Canvas(parent, bg="#111b2b", highlightthickness=0, cursor="arrow", height=370)
        self.canvas.grid(row=1, column=0, sticky="nsew", pady=(8, 6))
        self.canvas.bind("<Configure>", lambda _e: self._render())
        self.canvas.bind("<ButtonPress-1>", self._canvas_press)
        self.canvas.bind("<B1-Motion>", self._canvas_drag)
        self.canvas.bind("<ButtonRelease-1>", self._canvas_release)
        timeline = ttk.Frame(parent, style="Card.TFrame")
        timeline.grid(row=2, column=0, sticky="ew")
        self.seek_scale = self._lockable(ttk.Scale(timeline, from_=0, to=1, variable=self.seek_var, command=self._seek_changed))
        self.seek_scale.pack(side="left", fill="x", expand=True)
        ttk.Label(timeline, textvariable=self.frame_time_var, style="Muted.TLabel").pack(side="right", padx=(12, 0))
        self.seek_scale.configure(state="disabled")
        ttk.Label(parent, textvariable=self.preview_info_var, style="Muted.TLabel", wraplength=600).grid(row=3, column=0, sticky="w", pady=(4, 4))
        toolrow = ttk.Frame(parent, style="Card.TFrame")
        toolrow.grid(row=4, column=0, sticky="ew")
        self._lockable(ttk.Radiobutton(toolrow, text="点击人脸：保留 / 遮挡", variable=self.tool_var, value="select", command=self._tool_changed)).pack(side="left")
        self._lockable(ttk.Radiobutton(toolrow, text="拖框补充遮挡", variable=self.tool_var, value="draw", command=self._tool_changed)).pack(side="left", padx=(10, 0))
        actions = ttk.Frame(parent, style="Card.TFrame")
        actions.grid(row=5, column=0, sticky="ew", pady=(3, 3))
        self._lockable(ttk.Button(actions, text="撤销最后一个手工框", command=self._undo_box)).pack(side="left", padx=(0, 6))
        self._lockable(ttk.Button(actions, text="重置本文件调整", command=self._reset_selection)).pack(side="left")
        ttk.Label(parent, textvariable=self.selection_info_var, style="Muted.TLabel", wraplength=580).grid(row=6, column=0, sticky="w", pady=(3, 0))

    def _build_settings(self, parent):
        canvas = tk.Canvas(parent, bg="#ffffff", highlightthickness=0, width=246)
        scrollbar = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        canvas.configure(yscrollcommand=scrollbar.set)
        inner = ttk.Frame(canvas, style="Card.TFrame")
        window = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window, width=e.width))
        def wheel(event):
            widget = event.widget
            while widget is not None:
                if widget == inner or widget == canvas:
                    canvas.yview_scroll(int(-event.delta / 120), "units")
                    return "break"
                widget = getattr(widget, "master", None)
        self.root.bind("<MouseWheel>", wheel, add="+")
        parent = inner
        ttk.Label(parent, text="03  遮挡设置", style="Section.TLabel").pack(anchor="w", pady=(0, 14))
        self._combo(parent, "保留方式", self.mode_var, list(MODE_NAMES))
        ttk.Label(parent, text="自动模式保留当前帧最大的人脸。\n预览点击可转为本文件手动选择。", style="Muted.TLabel", wraplength=230).pack(anchor="w", pady=(0, 12))
        self._combo(parent, "遮挡效果", self.effect_var, list(EFFECT_NAMES))
        self._scale(parent, "遮挡强度", self.strength_var, 6, 64, "")
        self._scale(parent, "脸部外扩", self.padding_var, 10, 70, "%")
        self._scale(parent, "检测置信度", self.threshold_var, 40, 95, "%")
        self._combo(parent, "检测精度", self.quality_var, list(QUALITY_NAMES))
        self._lockable(ttk.Checkbutton(parent, text="保留视频原声", variable=self.audio_var, command=self._settings_changed)).pack(anchor="w", pady=(5, 12))
        ttk.Separator(parent).pack(fill="x", pady=(0, 12))
        ttk.Label(parent, text="导出文件夹", font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w", pady=(0, 6))
        self._lockable(ttk.Entry(parent, textvariable=self.output_var)).pack(fill="x")
        outputrow = ttk.Frame(parent, style="Card.TFrame")
        outputrow.pack(fill="x", pady=(7, 12))
        self._lockable(ttk.Button(outputrow, text="选择文件夹", command=self._choose_output)).pack(side="left", fill="x", expand=True, padx=(0, 5))
        ttk.Button(outputrow, text="打开", command=self._open_output).pack(side="left")
        info = tk.Frame(parent, bg="#f0f5ff", padx=10, pady=10)
        info.pack(fill="x", pady=(4, 0))
        tk.Label(info, text="使用提示", bg="#f0f5ff", fg="#28518c", font=("Microsoft YaHei UI", 10, "bold"), anchor="w").pack(fill="x")
        tk.Label(info, text="• 视频导出时会跟踪人脸与手工框\n• 手动保留依据选定帧的人脸\n• 请选清晰正面的帧再调整\n• 小脸、侧脸、遮挡可能漏检\n• 导出后请复查，必要时补框", bg="#f0f5ff", fg="#536f99", font=("Microsoft YaHei UI", 9), justify="left", wraplength=230).pack(fill="x", pady=(5, 0))

    def _combo(self, parent, text, variable, values):
        ttk.Label(parent, text=text).pack(anchor="w", pady=(0, 6))
        box = self._lockable(ttk.Combobox(parent, textvariable=variable, values=values, state="readonly"), "readonly")
        box.pack(fill="x", pady=(0, 13))
        box.bind("<<ComboboxSelected>>", self._settings_changed)
        return box

    def _scale(self, parent, text, variable, low, high, suffix):
        row = ttk.Frame(parent, style="Card.TFrame")
        row.pack(fill="x")
        ttk.Label(row, text=text).pack(side="left")
        value_label = ttk.Label(row, text=f"{variable.get()}{suffix}", style="Muted.TLabel")
        value_label.pack(side="right")
        def changed(value):
            rounded = int(round(float(value)))
            variable.set(rounded)
            value_label.configure(text=f"{rounded}{suffix}")
            self._settings_changed()
        self._lockable(ttk.Scale(parent, from_=low, to=high, variable=variable, command=changed)).pack(fill="x", pady=(3, 14))

    def _build_footer(self, parent):
        footer = ttk.Frame(parent, style="Card.TFrame", padding=14)
        footer.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        actions = ttk.Frame(footer, style="Card.TFrame")
        actions.pack(side="right", padx=(15, 0))
        self.start_button = self._lockable(ttk.Button(actions, text="开始批量处理", style="Accent.TButton", command=self._start))
        self.start_button.pack(side="left")
        self.cancel_button = ttk.Button(actions, text="取消", command=self._cancel, state="disabled")
        self.cancel_button.pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="日志", command=self._show_log).pack(side="left", padx=(8, 0))
        progress = ttk.Frame(footer, style="Card.TFrame")
        progress.pack(side="left", fill="x", expand=True)
        ttk.Label(progress, textvariable=self.progress_text_var).pack(anchor="w")
        ttk.Progressbar(progress, variable=self.progress_var, maximum=100).pack(fill="x", pady=(6, 0))
        ttk.Label(parent, textvariable=self.status_var, background="#edf1f7", foreground="#65738a", font=("Microsoft YaHei UI", 9), wraplength=1260).grid(row=2, column=0, sticky="w", pady=(8, 0))
        self.log_lines: list[str] = []
        self.log_window: tk.Toplevel | None = None
        self.log_text: tk.Text | None = None
        self._render()

    def _settings(self, item: QueueItem | None = None) -> Settings:
        return Settings(
            effect=EFFECT_NAMES[self.effect_var.get()],
            mode=(item.mode_override if item and item.mode_override else MODE_NAMES[self.mode_var.get()]),
            strength=self.strength_var.get(),
            padding=self.padding_var.get() / 100,
            threshold=self.threshold_var.get() / 100,
            detection_size=QUALITY_NAMES[self.quality_var.get()],
            preserve_audio=self.audio_var.get(),
        )

    def _add_files(self):
        paths = filedialog.askopenfilenames(parent=self.root, title="添加需要处理的图片或视频", filetypes=[("图片与视频", " ".join("*" + ext for ext in sorted(SUPPORTED_EXTENSIONS))), ("所有文件", "*.*")])
        self._add_paths(paths)

    def _add_folder(self):
        directory = filedialog.askdirectory(parent=self.root, title="选择图片或视频文件夹")
        if not directory:
            return
        base = Path(directory)
        try:
            _require_local_path(directory)
            paths = base.rglob("*") if self.recursive_var.get() else base.iterdir()
            output = Path(_require_local_path(self.output_var.get())).resolve()
            self._add_paths(str(p) for p in paths if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS and output not in p.resolve().parents)
        except (OSError, ValueError) as exc:
            messagebox.showerror("无法读取文件夹", str(exc), parent=self.root)

    def _add_paths(self, paths):
        existing = {os.path.normcase(os.path.abspath(item.path)) for item in self.items.values()}
        first = None
        count = 0
        for path in paths:
            try:
                path = os.path.abspath(_require_local_path(path))
            except ValueError as exc:
                self._log(str(exc))
                continue
            key = os.path.normcase(path)
            if key in existing or Path(path).suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue
            existing.add(key)
            iid = self.tree.insert("", "end", values=(Path(path).name, "待处理"))
            self.items[iid] = QueueItem(path)
            first = first or iid
            count += 1
        self.count_label.configure(text=f"{len(self.items)} 个")
        if count:
            self.status_var.set(f"已添加 {count} 个文件。每个文件的手动选择会单独保存。")
            self._log(f"添加 {count} 个文件，共 {len(self.items)} 个。")
        if first and not self.current_id:
            self.tree.selection_set(first)
            self.tree.focus(first)

    def _remove_selected(self):
        if self.busy:
            return
        ids = self.tree.selection()
        for iid in ids:
            self.items.pop(iid, None)
            self.tree.delete(iid)
        self.count_label.configure(text=f"{len(self.items)} 个")
        if self.current_id in ids:
            self._clear_preview()
            if self.items:
                self.tree.selection_set(next(iter(self.items)))

    def _clear(self):
        if self.busy:
            return
        for iid in list(self.items):
            self.tree.delete(iid)
        self.items.clear()
        self.count_label.configure(text="0 个")
        self._clear_preview()

    def _clear_preview(self):
        self.preview_generation += 1
        self.current_id = None
        self.current_frame = self.current_masked = None
        self.current_faces = np.empty((0, 15), dtype=np.float32)
        self.current_keep = set()
        self.current_metadata = {}
        self.preview_loading = False
        self.preview_info_var.set("尚未选择文件")
        self.selection_info_var.set("绿色保留主角，橙色遮挡背景人物。")
        self.frame_time_var.set("00:00 / 00:00")
        self.seek_scale.configure(state="disabled")
        self._render()

    def _queue_selected(self, _event=None):
        if self.busy:
            return
        selected = self.tree.selection()
        if not selected:
            return
        iid = self.tree.focus() if self.tree.focus() in selected else selected[0]
        if iid == self.current_id:
            return
        self.current_id = iid
        self.current_frame = self.current_masked = None
        self.current_index = 0
        self.seek_var.set(0)
        self.current_metadata = {}
        self._request_preview(0)

    def _settings_changed(self, _event=None):
        if self.busy or self.closed:
            return
        if self.settings_after:
            self.root.after_cancel(self.settings_after)
        self.settings_after = self.root.after(220, self._refresh_settings)

    def _refresh_settings(self):
        self.settings_after = None
        if self.current_id:
            self._request_preview(self.current_index, reuse=True)

    def _seek_changed(self, value):
        if self.busy or not self.current_id or self.current_metadata.get("type") != "video":
            return
        index = int(round(float(value)))
        fps = float(self.current_metadata.get("fps") or 25)
        self.frame_time_var.set(f"{self._time(index / fps)} / {self._time(float(self.current_metadata.get('duration') or 0))}")
        if index == self.current_index:
            return
        if self.seek_after:
            self.root.after_cancel(self.seek_after)
        self.seek_after = self.root.after(160, lambda: self._seek_to(index))

    def _seek_to(self, index):
        self.seek_after = None
        if self.current_id and not self.busy:
            self._request_preview(index)

    @staticmethod
    def _time(seconds):
        seconds = max(0, int(seconds))
        if seconds >= 3600:
            return f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"
        return f"{seconds // 60:02d}:{seconds % 60:02d}"

    def _request_preview(self, index, reuse=False):
        if self.closed or self.busy or not self.current_id:
            return
        iid = self.current_id
        item = self.items.get(iid)
        if item is None:
            return
        self.preview_generation += 1
        token = self.preview_generation
        settings = self._settings(item)
        selection = _selection_snapshot(item.selection)
        source = self.current_frame.copy() if reuse and self.current_frame is not None and index == self.current_index else None
        metadata = dict(self.current_metadata)
        self.preview_loading = True
        self.status_var.set("正在读取媒体并检测人脸……")
        self._render()
        def worker():
            try:
                if self.preview_engine is None:
                    self.preview_engine = FaceEngine()
                if token != self.preview_generation:
                    return
                frame, meta = (source, metadata) if source is not None else read_preview(item.path, index)
                faces = self.preview_engine.detect(frame, settings)
                keep = self.preview_engine.keep_indices(frame, settings, selection, faces=faces)
                # Random seeks cannot establish the exported motion tracks. Show
                # manual boxes only at their true anchor instead of implying
                # that a fixed rectangle follows the person in this preview.
                if meta.get("type") == "video" and index != selection.anchor_frame:
                    selection.manual_boxes = []
                masked = self.preview_engine.preview(frame, settings, selection)
                self.events.put(("preview", token, iid, index, frame, meta, faces, keep, masked))
            except Exception as exc:
                self.events.put(("preview_error", token, iid, str(exc)))
        self.preview_pool.submit(worker)

    def _accept_preview(self, token, iid, index, frame, metadata, faces, keep, masked):
        if token != self.preview_generation or iid != self.current_id or self.busy:
            return
        self.preview_loading = False
        self.current_frame = frame
        self.current_masked = masked
        self.current_faces = np.asarray(faces if faces is not None else np.empty((0, 15)), dtype=np.float32)
        self.current_keep = set(keep)
        self.current_index = index
        self.current_metadata = metadata
        self.items[iid].metadata = metadata
        width = metadata.get("width", frame.shape[1])
        height = metadata.get("height", frame.shape[0])
        is_video = metadata.get("type") == "video"
        frame_count = max(1, int(metadata.get("frame_count") or 1))
        self.seek_scale.configure(to=max(1, frame_count - 1), state="normal" if is_video and frame_count > 1 else "disabled")
        self.seek_var.set(index)
        fps = float(metadata.get("fps") or 25)
        self.frame_time_var.set(f"{self._time(index / fps)} / {self._time(float(metadata.get('duration') or 0))}" if is_video else "图片")
        filename = Path(self.items[iid].path).name
        extra = f" · {fps:.2f} fps · 帧 {index + 1}/{frame_count}" if is_video else ""
        self.preview_info_var.set(f"{filename}\n{width} × {height}{extra} · 检出 {len(self.current_faces)} 张人脸")
        self._selection_summary()
        self.status_var.set("预览已更新。点击人脸切换保留；拖框可补充漏检区域。")
        self._render()

    def _selection_summary(self):
        if not self.current_id:
            return
        item = self.items[self.current_id]
        settings = self._settings(item)
        mode = {"largest": "自动保留最大人脸", "all": "遮挡全部人脸", "selected": "手动保留指定人脸"}[settings.mode]
        manual = len(item.selection.manual_boxes)
        info = f"{mode} · 本帧保留 {len(self.current_keep)} 张，遮挡 {max(0, len(self.current_faces) - len(self.current_keep))} 张"
        if settings.mode == "selected":
            info += f" · 编辑帧 {item.selection.anchor_frame + 1}"
        if manual:
            info += f"\n手工框 {manual} 个，从编辑帧开始跟随后续画面；预览只在编辑帧显示手工框。"
        else:
            info += "\n绿色保留，橙色遮挡。视频预览逐帧检测，导出时跟踪。"
        self.selection_info_var.set(info)

    def _render(self):
        if self.closed:
            return
        self.canvas.delete("all")
        width = max(1, self.canvas.winfo_width())
        height = max(1, self.canvas.winfo_height())
        if self.current_frame is None:
            self.canvas.create_text(width / 2, height / 2 - 18, text="正在读取媒体……" if self.preview_loading else "添加文件，开始保护画面中的人脸", fill="#c3cee0", font=("Microsoft YaHei UI", 14), width=max(100, width - 45))
            self.canvas.create_text(width / 2, height / 2 + 22, text="图片 / 视频 · 手动选择 · 自动跟踪", fill="#71849f", font=("Microsoft YaHei UI", 10))
            return
        frame = self.current_masked if self.view_var.get() == "masked" and self.current_masked is not None else self.current_frame
        fh, fw = frame.shape[:2]
        scale = min(width / fw, height / fh)
        dw, dh = max(1, int(fw * scale)), max(1, int(fh * scale))
        ox, oy = (width - dw) / 2, (height - dh) / 2
        self.canvas_transform = (scale, ox, oy)
        resized = cv2.resize(frame, (dw, dh), interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR)
        self.photo = ImageTk.PhotoImage(Image.fromarray(cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)))
        self.canvas.create_image(ox, oy, anchor="nw", image=self.photo)
        if self.view_var.get() == "source":
            for index, row in enumerate(self.current_faces):
                x, y, w, h = [float(v) for v in row[:4]]
                keep = index in self.current_keep
                color = "#63e6b3" if keep else "#ffb15c"
                x1, y1, x2, y2 = ox + x * scale, oy + y * scale, ox + (x + w) * scale, oy + (y + h) * scale
                self.canvas.create_rectangle(x1, y1, x2, y2, outline=color, width=2)
                self.canvas.create_text(x1 + 4, max(oy + 4, y1 - 17), anchor="nw", text=f"{index + 1}  {'保留' if keep else '遮挡'}", fill=color, font=("Microsoft YaHei UI", 9, "bold"))
            item = self.items.get(self.current_id)
            if item and self.current_index == item.selection.anchor_frame:
                for index, box in enumerate(item.selection.manual_boxes):
                    x, y, w, h = [float(v) for v in box[:4]]
                    self.canvas.create_rectangle(ox + x * scale, oy + y * scale, ox + (x + w) * scale, oy + (y + h) * scale, outline="#f5a3ff", width=2, dash=(6, 3))
                    self.canvas.create_text(ox + x * scale + 4, oy + y * scale + 4, anchor="nw", text=f"补框 {index + 1}", fill="#f5a3ff", font=("Microsoft YaHei UI", 9, "bold"))
        if self.preview_loading:
            self.canvas.create_rectangle(0, 0, width, 28, fill="#17243c", outline="")
            self.canvas.create_text(12, 14, text="正在更新预览……", anchor="w", fill="#d9e4f8", font=("Microsoft YaHei UI", 9))

    def _tool_changed(self):
        self.view_var.set("source")
        self.canvas.configure(cursor="crosshair" if self.tool_var.get() == "draw" else "hand2")
        self._render()

    def _image_point(self, x, y):
        if self.current_frame is None:
            return None
        scale, ox, oy = self.canvas_transform
        px, py = (x - ox) / scale, (y - oy) / scale
        h, w = self.current_frame.shape[:2]
        return min(max(0, px), w - 1), min(max(0, py), h - 1)

    def _edit_ready(self):
        if self.busy or self.preview_loading or self.current_frame is None or not self.current_id:
            return False
        if self.view_var.get() != "source":
            self.view_var.set("source")
            self._render()
            self.status_var.set("已切换到原图选框视图，再次点击人脸即可调整。")
            return False
        item = self.items[self.current_id]
        if item.selection.manual_boxes and item.selection.anchor_frame != self.current_index:
            self.status_var.set(f"已有手工框固定在编辑帧 {item.selection.anchor_frame + 1}。请回到该帧继续编辑，或重置本文件后在新帧调整。")
            return False
        return True

    def _canvas_press(self, event):
        if not self._edit_ready():
            return
        point = self._image_point(event.x, event.y)
        if point is None:
            return
        scale, ox, oy = self.canvas_transform
        h, w = self.current_frame.shape[:2]
        if not (ox <= event.x <= ox + w * scale and oy <= event.y <= oy + h * scale):
            return
        if self.tool_var.get() == "draw":
            self.drag_origin = point
            self.drag_item = self.canvas.create_rectangle(event.x, event.y, event.x, event.y, outline="#f5a3ff", width=2, dash=(6, 3))
            return
        hits = []
        for index, row in enumerate(self.current_faces):
            x, y, fw, fh = row[:4]
            if x <= point[0] <= x + fw and y <= point[1] <= y + fh:
                hits.append((float(fw * fh), index))
        if not hits:
            self.status_var.set("这里没有检测框。需要遮挡漏检人物时，选择“拖框补充遮挡”。")
            return
        index = min(hits)[1]
        keeps = set(self.current_keep)
        keeps.remove(index) if index in keeps else keeps.add(index)
        self._save_anchor(keeps)
        self._request_preview(self.current_index, reuse=True)

    def _save_anchor(self, keeps):
        item = self.items[self.current_id]
        item.mode_override = "selected"
        item.selection.keep_faces = [self.current_faces[index].copy() for index in sorted(keeps)]
        item.selection.reference_frame = self.current_frame.copy()
        item.selection.anchor_frame = self.current_index
        self._update_item(self.current_id, "待处理 · 手调")

    def _canvas_drag(self, event):
        if self.drag_origin is None or self.drag_item is None:
            return
        point = self._image_point(event.x, event.y)
        if point is None:
            return
        scale, ox, oy = self.canvas_transform
        x, y = self.drag_origin
        self.canvas.coords(self.drag_item, ox + x * scale, oy + y * scale, ox + point[0] * scale, oy + point[1] * scale)

    def _canvas_release(self, event):
        if self.drag_origin is None:
            return
        origin = self.drag_origin
        self.drag_origin = None
        if self.drag_item:
            self.canvas.delete(self.drag_item)
        self.drag_item = None
        point = self._image_point(event.x, event.y)
        if point is None or self.busy:
            return
        x, y = min(origin[0], point[0]), min(origin[1], point[1])
        width, height = abs(point[0] - origin[0]), abs(point[1] - origin[1])
        if width < 8 or height < 8:
            self.status_var.set("手工框太小，请拖出至少 8 × 8 像素的遮挡区域。")
            return
        self._save_anchor(self.current_keep)
        self.items[self.current_id].selection.manual_boxes.append([x, y, width, height])
        self._request_preview(self.current_index, reuse=True)

    def _undo_box(self):
        if self.busy or not self.current_id:
            return
        item = self.items[self.current_id]
        if item.selection.manual_boxes:
            item.selection.manual_boxes.pop()
            self._request_preview(self.current_index, reuse=True)
        else:
            self.status_var.set("当前文件还没有手工遮挡框。")

    def _reset_selection(self):
        if self.busy or not self.current_id:
            return
        item = self.items[self.current_id]
        item.selection = MediaSelection()
        item.mode_override = None
        self._update_item(self.current_id, "待处理")
        self._request_preview(self.current_index, reuse=True)

    def _choose_output(self):
        try:
            current = _require_local_path(self.output_var.get())
            initial = current if Path(current).is_dir() else str(Path(sys.executable).parent)
        except ValueError:
            initial = str(Path(sys.executable).parent)
        path = filedialog.askdirectory(parent=self.root, title="选择导出文件夹", initialdir=initial)
        if path:
            try:
                self.output_var.set(_require_local_path(path))
            except ValueError as exc:
                messagebox.showerror("请选择本地文件夹", str(exc), parent=self.root)

    def _open_output(self):
        try:
            path = Path(_require_local_path(self.output_var.get())).expanduser()
            path.mkdir(parents=True, exist_ok=True)
            os.startfile(str(path.resolve()))
        except (OSError, ValueError) as exc:
            messagebox.showerror("无法打开文件夹", str(exc), parent=self.root)

    def _start(self):
        if self.busy:
            return
        if not self.items:
            messagebox.showinfo("尚未添加文件", "请先添加需要处理的图片或视频。", parent=self.root)
            return
        output = self.output_var.get().strip()
        if not output:
            messagebox.showinfo("选择导出文件夹", "请设置导出文件夹。", parent=self.root)
            return
        try:
            _require_local_path(output)
            output_dir = Path(output).expanduser().resolve()
            output_dir.mkdir(parents=True, exist_ok=True)
            probe = output_dir / f".facemask_write_test_{os.getpid()}"
            probe.write_bytes(b"")
            probe.unlink()
        except (OSError, ValueError) as exc:
            messagebox.showerror("导出文件夹不可写", str(exc), parent=self.root)
            return
        jobs = [(iid, item.path, self._settings(item), _selection_snapshot(item.selection)) for iid, item in self.items.items()]
        if any(settings.mode == "selected" and not len(selection.keep_faces) for _, _, settings, selection in jobs):
            self._log("部分文件采用手动模式但未选定保留人脸，这些文件将遮挡全部检测到的人脸。")
        self.preview_generation += 1
        self.preview_loading = False
        self.cancel_event.clear()
        self.busy = True
        self._set_busy(True)
        self.progress_var.set(0)
        self.progress_text_var.set(f"准备处理 {len(jobs)} 个文件")
        self.status_var.set("正在批量处理。原文件会保留，结果保存至导出文件夹。")
        for iid, _, _, _ in jobs:
            self._update_item(iid, "等待中")
        self._log(f"开始批量处理 {len(jobs)} 个文件；导出：{output_dir}")
        def worker():
            complete = 0
            failed = 0
            canceled = False
            batch_engine = None
            for job_index, (iid, path, settings, selection) in enumerate(jobs):
                if self.cancel_event.is_set():
                    canceled = True
                    break
                self.events.put(("item_status", iid, "处理中"))
                self.events.put(("log", f"处理：{path}"))
                def progress(fraction, message, index=job_index, file_path=path):
                    self.events.put(("progress", 100 * (index + max(0, min(1, fraction))) / len(jobs), f"{index + 1}/{len(jobs)} · {Path(file_path).name} · {message}"))
                try:
                    if batch_engine is None:
                        batch_engine = FaceEngine()
                    result = batch_engine.process_file(path, str(output_dir), settings, selection, self.cancel_event, progress)
                    complete += 1
                    self.events.put(("item_status", iid, "已完成"))
                    self.events.put(("log", f"完成：{result.get('output') or result.get('path') or result.get('output_path') or path}"))
                    for warning in result.get("warnings", []):
                        self.events.put(("log", f"提示：{warning}"))
                except CancelledError:
                    canceled = True
                    self.events.put(("item_status", iid, "已取消"))
                    break
                except Exception as exc:
                    failed += 1
                    self.events.put(("item_status", iid, "失败"))
                    self.events.put(("log", f"失败：{Path(path).name} — {exc}"))
                    self.events.put(("log", traceback.format_exc()))
            self.events.put(("batch_done", complete, failed, canceled))
        threading.Thread(target=worker, name="batch-redaction", daemon=True).start()

    def _set_busy(self, busy):
        for widget in self.ui_lock_widgets:
            try:
                widget.configure(state="disabled" if busy else self.default_states[widget])
            except tk.TclError:
                pass
        self.cancel_button.configure(state="normal" if busy else "disabled")
        self.canvas.configure(cursor="watch" if busy else ("crosshair" if self.tool_var.get() == "draw" else "hand2"))
        if not busy and self.current_metadata.get("type") != "video":
            self.seek_scale.configure(state="disabled")

    def _cancel(self):
        if self.busy:
            self.cancel_event.set()
            self.cancel_button.configure(state="disabled")
            self.status_var.set("正在取消，等待当前帧或封装步骤完成……")
            self._log("用户请求取消。")

    def _update_item(self, iid, status):
        if iid not in self.items:
            return
        self.items[iid].status = status
        tag = "error" if status == "失败" else "done" if status == "已完成" else ""
        self.tree.item(iid, values=(Path(self.items[iid].path).name, status), tags=(tag,) if tag else ())

    def _poll(self):
        if self.closed:
            return
        try:
            for _ in range(150):
                event = self.events.get_nowait()
                name, *args = event
                if name == "preview":
                    self._accept_preview(*args)
                elif name == "preview_error":
                    token, iid, message = args
                    if token == self.preview_generation and iid == self.current_id and not self.busy:
                        self.preview_loading = False
                        self.current_frame = self.current_masked = None
                        self.status_var.set(f"预览失败：{message}")
                        self.preview_info_var.set(f"无法预览：{Path(self.items[iid].path).name}")
                        self._log(f"预览失败：{message}")
                        self._render()
                elif name == "item_status":
                    self._update_item(*args)
                elif name == "progress":
                    self.progress_var.set(args[0])
                    self.progress_text_var.set(args[1])
                elif name == "log":
                    self._log(args[0])
                elif name == "batch_done":
                    completed, failed, canceled = args
                    self.busy = False
                    self._set_busy(False)
                    if canceled:
                        for iid, item in self.items.items():
                            if item.status == "等待中":
                                self._update_item(iid, "未处理")
                    else:
                        self.progress_var.set(100)
                    text = f"{'已取消' if canceled else '处理结束'} · 完成 {completed} 个 · 失败 {failed} 个"
                    self.progress_text_var.set(text)
                    self.status_var.set(text + "。结果位于导出文件夹，请复查人物移动、侧脸和漏检区域。")
                    self._log(text)
                    self._render()
        except queue.Empty:
            pass
        self.root.after(80, self._poll)

    def _log(self, line):
        from datetime import datetime
        text = f"[{datetime.now():%H:%M:%S}] {line}"
        self.log_lines.append(text)
        if len(self.log_lines) > 2000:
            self.log_lines = self.log_lines[-1500:]
        if self.log_text and self.log_window and self.log_window.winfo_exists():
            self.log_text.configure(state="normal")
            self.log_text.insert("end", text + "\n")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")

    def _show_log(self):
        if self.log_window and self.log_window.winfo_exists():
            self.log_window.lift()
            return
        window = tk.Toplevel(self.root)
        self.log_window = window
        window.title("处理日志 · 人脸马赛克工作室")
        window.geometry("860x480")
        text = tk.Text(window, bg="#111b2b", fg="#c9d7ea", font=("Microsoft YaHei UI", 10), wrap="word", padx=12, pady=12)
        text.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(window, orient="vertical", command=text.yview)
        scrollbar.pack(side="right", fill="y")
        text.configure(yscrollcommand=scrollbar.set)
        text.insert("end", "\n".join(self.log_lines))
        text.configure(state="disabled")
        text.see("end")
        self.log_text = text

    def _on_close(self):
        if self.busy:
            if not messagebox.askyesno("正在处理", "退出会取消尚未完成的文件。确定退出吗？", parent=self.root):
                return
            self.cancel_event.set()
        self.closed = True
        self.preview_generation += 1
        self.preview_pool.shutdown(wait=False, cancel_futures=True)
        self.root.destroy()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Offline batch face redaction")
    parser.add_argument("--ui-smoke", nargs="?", const="", default=None, metavar="FILE", help="Open the UI briefly; optionally preview a local media file")
    args, _unknown = parser.parse_known_args(argv)
    root = tk.Tk()
    app = FaceMaskApp(root)
    if args.ui_smoke is not None:
        if args.ui_smoke:
            app._add_paths([args.ui_smoke])
        root.after(2400, app._on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
