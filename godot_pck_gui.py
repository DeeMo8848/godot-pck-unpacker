#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Godot PCK 解包器 —— 图形界面版
================================

基于 godot_pck_unpacker.py 的解包内核, 提供:
  - 选择/拖拽 .pck 文件 (Windows 拖拽)
  - 选择输出文件夹
  - 按类型导出: 全部 / 图像 / 音效 / 字体 / 脚本 / 场景 / 着色器 / 资源
  - 音频/字体自动还原为可用格式 (.sample→.wav, .fontdata→.ttf)
  - 进度条 + 日志 + 取消

零额外基础依赖: 仅用 Python 标准库 (tkinter)。纹理转 PNG 可选依赖 Pillow。
"""

import os
import sys
import threading
import queue
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from godot_pck_unpacker import unpack, parse_header, parse_directory, RES_PREFIX

# 资源类型 -> 源扩展名
# 注意: PCK 里**打包后**的音频是 .sample(Godot4 WAV) / .oggvorbisstr(Godot4 OGG),
# 字体是 .fontdata; 而不是 .wav/.ogg/.ttf 等源格式。分类必须列打包后的扩展名, 否则会漏。
CATEGORIES = {
    "图像":   ["ctex", "stex", "png", "webp", "jpg", "jpeg", "bmp", "svg", "tga", "ico", "dds", "ktx", "exr", "hdr"],
    "音效":   ["sample", "oggvorbisstr", "mp3str", "wav", "ogg", "mp3", "opus", "flac", "m4a"],
    "字体":   ["fontdata", "ttf", "otf", "font", "woff", "woff2", "fnt"],
    "脚本":   ["gd", "gdc", "cs"],
    "场景":   ["tscn", "scn"],
    "着色器": ["gdshader", "shader"],
    "资源":   ["res", "tres"],
}

# ---------- Windows 拖拽支持 (ctypes 子类化 WNDPROC) ----------
if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes
    _user32 = ctypes.windll.user32
    _shell32 = ctypes.windll.shell32
    _WM_DROPFILES = 0x0233
    _GWL_WNDPROC = -4
    _GWL_EXSTYLE = -20
    _WS_EX_ACCEPTFILES = 0x10
    # 关键: 64 位下必须设置正确的 restype/argtypes, 否则指针被截断会崩溃
    _user32.SetWindowLongPtrW.restype = ctypes.c_void_p
    _user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
    _user32.GetWindowLongW.restype = ctypes.c_int
    _user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.CallWindowProcW.restype = wintypes.LPARAM
    _user32.CallWindowProcW.argtypes = [
        ctypes.c_void_p, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    _shell32.DragQueryFileW.restype = ctypes.c_uint
    _shell32.DragQueryFileW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_wchar_p, ctypes.c_uint]
    _shell32.DragFinish.argtypes = [ctypes.c_void_p]


class App:
    def __init__(self, root):
        self.root = root
        root.title("Godot PCK 解包器")
        root.geometry("720x620")
        try:
            root.iconbitmap()  # 无图标则忽略
        except Exception:
            pass

        self.pck_path = tk.StringVar()
        self.out_path = tk.StringVar()
        self.mode = tk.StringVar(value="all")  # all / custom
        self.cat_vars = {name: tk.BooleanVar(value=False) for name in CATEGORIES}
        self.organize_var = tk.BooleanVar(value=True)
        self.convert_var = tk.BooleanVar(value=True)
        self.media_var = tk.BooleanVar(value=True)   # 音频/字体还原为 .wav/.ttf
        self.keepraw_var = tk.BooleanVar(value=False)
        self.cat_btns = {}
        self.skip_meta_var = tk.BooleanVar(value=True)  # 默认跳过 .import/.remap 元数据
        self._dropped = []  # 拖拽: 由 WNDPROC 填充, 主线程 _poll 消费
        self.status_var = tk.StringVar(value="请选择或拖入 .pck 文件")
        self.info_var = tk.StringVar(value="")

        self.cancelled = False
        self.running = False
        self.log_q = queue.Queue()
        self.prog_q = queue.Queue()

        self._build_ui()
        if sys.platform == "win32":
            self._setup_dragdrop()
        self._poll()

    # ---------------- UI ----------------
    def _build_ui(self):
        # 源文件
        f = ttk.LabelFrame(self.root, text="源文件 (PCK)")
        f.pack(fill="x", padx=10, pady=6)
        ttk.Entry(f, textvariable=self.pck_path, state="readonly").pack(
            side="left", fill="x", expand=True, padx=(6, 4), pady=6)
        ttk.Button(f, text="浏览...", command=self._choose_pck).pack(side="left", padx=4, pady=6)
        self.drop_label = ttk.Label(f, text="可将 .pck 拖拽到此处", foreground="gray")
        self.drop_label.pack(side="left", padx=4, pady=6)

        # 输出文件夹
        f2 = ttk.LabelFrame(self.root, text="输出文件夹")
        f2.pack(fill="x", padx=10, pady=6)
        ttk.Entry(f2, textvariable=self.out_path, state="readonly").pack(
            side="left", fill="x", expand=True, padx=(6, 4), pady=6)
        ttk.Button(f2, text="选择...", command=self._choose_out).pack(side="left", padx=4, pady=6)

        # 导出类型
        f3 = ttk.LabelFrame(self.root, text="导出类型")
        f3.pack(fill="x", padx=10, pady=6)
        ttk.Radiobutton(f3, text="全部", variable=self.mode, value="all").pack(
            side="left", padx=8, pady=4)
        ttk.Radiobutton(f3, text="自定义分类", variable=self.mode, value="custom").pack(
            side="left", padx=8, pady=4)
        cf = ttk.Frame(f3)
        cf.pack(fill="x", padx=8, pady=2)
        for name in CATEGORIES:
            b = ttk.Checkbutton(cf, text=name, variable=self.cat_vars[name])
            b.pack(side="left", padx=6)
            self.cat_btns[name] = b
        # 选「全部」时分类复选框全选置灰, 选「自定义」时清空恢复可选
        self.mode.trace_add("write", lambda *a: self._on_mode_change())

        # 选项
        f4 = ttk.LabelFrame(self.root, text="选项")
        f4.pack(fill="x", padx=10, pady=6)
        row1 = ttk.Frame(f4)
        row1.pack(fill="x", padx=4, pady=2)
        ttk.Checkbutton(row1, text="纹理归位到原始路径", variable=self.organize_var).pack(side="left", padx=8)
        ttk.Checkbutton(row1, text="纹理转为 PNG", variable=self.convert_var).pack(side="left", padx=8)
        ttk.Checkbutton(row1, text="额外保留原始纹理(工程重建)", variable=self.keepraw_var).pack(side="left", padx=8)
        row2 = ttk.Frame(f4)
        row2.pack(fill="x", padx=4, pady=2)
        ttk.Checkbutton(row2, text="音频/字体还原为可用格式 (.sample→.wav, .fontdata→.ttf)", variable=self.media_var).pack(side="left", padx=8)
        row3 = ttk.Frame(f4)
        row3.pack(fill="x", padx=4, pady=2)
        ttk.Checkbutton(row3, text="跳过 .import/.remap 元数据 (勾选后只保留真实资源, 输出更干净)", variable=self.skip_meta_var).pack(side="left", padx=8)

        # 分析信息
        ttk.Label(self.root, textvariable=self.info_var, foreground="darkgreen").pack(
            anchor="w", padx=12, pady=(2, 0))

        # 进度
        self.progress = ttk.Progressbar(self.root, maximum=100, mode="determinate")
        self.progress.pack(fill="x", padx=12, pady=6)
        ttk.Label(self.root, textvariable=self.status_var, foreground="gray").pack(anchor="w", padx=12)

        # 按钮
        bf = ttk.Frame(self.root)
        bf.pack(fill="x", padx=10, pady=4)
        self.start_btn = ttk.Button(bf, text="开始解包", command=self._start)
        self.start_btn.pack(side="left", padx=6)
        self.cancel_btn = ttk.Button(bf, text="取消", command=self._cancel, state="disabled")
        self.cancel_btn.pack(side="left", padx=6)
        ttk.Button(bf, text="打开输出文件夹", command=self._open_out).pack(side="left", padx=6)

        # 日志
        lf = ttk.LabelFrame(self.root, text="日志")
        lf.pack(fill="both", expand=True, padx=10, pady=6)
        self.log_text = tk.Text(lf, height=12, wrap="word", state="disabled")
        self.log_text.pack(side="left", fill="both", expand=True, padx=6, pady=6)
        sb = ttk.Scrollbar(lf, command=self.log_text.yview)
        sb.pack(side="right", fill="y")
        self.log_text.config(yscrollcommand=sb.set)
        self._on_mode_change()

    def _on_mode_change(self):
        """选「全部」-> 分类复选框全选并置灰; 选「自定义」-> 清空并恢复可选。"""
        all_mode = (self.mode.get() == "all")
        for name, btn in getattr(self, "cat_btns", {}).items():
            if all_mode:
                self.cat_vars[name].set(True)
                btn.config(state="disabled")
            else:
                self.cat_vars[name].set(False)
                btn.config(state="normal")

    # ---------------- 选择 ----------------
    def _choose_pck(self):
        p = filedialog.askopenfilename(
            title="选择 PCK 文件",
            filetypes=[("Godot PCK", "*.pck"), ("所有文件", "*.*")])
        if p:
            self._set_pck(p)

    def _set_pck(self, p):
        self.pck_path.set(p)
        self.drop_label.config(text=os.path.basename(p), foreground="black")
        if not self.out_path.get():
            self.out_path.set(os.path.splitext(p)[0] + "_unpacked")
        self._analyze()

    def _choose_out(self):
        d = filedialog.askdirectory(title="选择输出文件夹")
        if d:
            self.out_path.set(d)

    def _analyze(self):
        p = self.pck_path.get()
        if not p or not os.path.isfile(p):
            return

        def worker():
            try:
                with open(p, "rb") as f:
                    hdr = parse_header(f)
                    entries = parse_directory(f, hdr, os.path.getsize(p))
                cats = {n: 0 for n in CATEGORIES}
                cats["其他"] = 0
                for e in entries:
                    rel = e["path"].replace(RES_PREFIX, "").replace("\x00", "")
                    ext = os.path.splitext(rel)[1].lower().lstrip(".")
                    placed = False
                    for n, exts in CATEGORIES.items():
                        if ext in exts:
                            cats[n] += 1
                            placed = True
                            break
                    if not placed:
                        cats["其他"] += 1
                godot = "%d.%d.%d" % (hdr["major"], hdr["minor"], hdr["patch"])
                info = "Godot %s (PCK v%d) | 共 %d 文件   " % (godot, hdr["version"], len(entries))
                info += "  ".join("%s:%d" % (n, cats[n]) for n in CATEGORIES if cats[n])
                if cats["其他"]:
                    info += "  其他:%d" % cats["其他"]
                self.info_var.set(info)
                self.log_q.put(("[PCK] 已分析: " + info, False))
            except Exception as ex:
                self.log_q.put(("[!] 分析失败: %s" % ex, True))

        threading.Thread(target=worker, daemon=True).start()

    # ---------------- 解包 ----------------
    def _start(self):
        p = self.pck_path.get()
        out = self.out_path.get()
        if not p or not os.path.isfile(p):
            messagebox.showerror("错误", "请先选择有效的 .pck 文件")
            return
        if not out:
            messagebox.showerror("错误", "请选择输出文件夹")
            return

        if self.mode.get() == "all":
            exts = None
        else:
            exts = set()
            for n, v in self.cat_vars.items():
                if v.get():
                    exts.update(CATEGORIES[n])
            if not exts:
                messagebox.showerror("错误", "请至少选择一个分类，或选“全部”")
                return
            exts = sorted(exts)

        self.cancelled = False
        self.running = True
        self.start_btn.config(state="disabled")
        self.cancel_btn.config(state="normal")
        self.log_text.config(state="normal")
        self.log_text.delete("1.0", tk.END)
        self.log_text.config(state="disabled")
        self.progress["value"] = 0

        kwargs = dict(
            pck_path=p, output_dir=out,
            convert=self.convert_var.get(), organize=self.organize_var.get(),
            convert_media=self.media_var.get(),
            keep_webp=False, keep_raw=self.keepraw_var.get(),
            list_only=False, quiet=False, skip_meta=self.skip_meta_var.get(),
            include=None, exclude=None, exts=exts,
            log_cb=self._log_cb, progress_cb=self._prog_cb,
            cancel_cb=lambda: self.cancelled,
        )
        threading.Thread(target=self._worker, args=(kwargs,), daemon=True).start()

    def _worker(self, kwargs):
        try:
            unpack(**kwargs)
        except Exception as ex:
            self.log_q.put(("[错误] %s" % ex, True))
        finally:
            self.root.after(0, self._finish)

    def _finish(self):
        self.running = False
        self.start_btn.config(state="normal")
        self.cancel_btn.config(state="disabled")
        self.status_var.set("完成")
        self.progress["value"] = 100

    def _cancel(self):
        self.cancelled = True
        self.status_var.set("正在取消...")

    # ---------------- 回调 ----------------
    def _log_cb(self, msg, is_err=False):
        self.log_q.put((msg, is_err))

    def _prog_cb(self, cur, total):
        self.prog_q.put((cur, total))

    def _open_out(self):
        out = self.out_path.get()
        if out and os.path.isdir(out):
            if sys.platform == "win32":
                os.startfile(out)
            else:
                os.system('xdg-open "%s"' % out)

    def _poll(self):
        try:
            while not self.log_q.empty():
                msg, is_err = self.log_q.get_nowait()
                self.log_text.config(state="normal")
                self.log_text.insert(tk.END, msg + "\n")
                self.log_text.see(tk.END)
                self.log_text.config(state="disabled")
            while not self.prog_q.empty():
                cur, total = self.prog_q.get_nowait()
                if total:
                    self.progress["value"] = cur * 100.0 / total
                    self.status_var.set("%d / %d" % (cur, total))
            # 处理拖入的文件: 由 WNDPROC 填充 self._dropped, 此处才触碰 Tk (主线程安全)
            if self._dropped:
                files = self._dropped
                self._dropped = []
                self._on_drop(files)
        except Exception:
            pass
        self.root.after(120, self._poll)

    # ---------------- 拖拽 ----------------
    def _setup_dragdrop(self):
        hwnd = self.root.winfo_id()
        if not hwnd:
            return
        WNDPROC = ctypes.WINFUNCTYPE(
            wintypes.LPARAM, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
        proc_ref = {}

        def proc(hwnd, msg, wp, lp):
            # 关键(崩溃根因): 回调由操作系统在 Tk 的消息泵内部直接调用,
            # 此刻若调用任何 Tk/Tcl API (如 root.after) 会造成 Tcl 重入 -> 栈损坏
            # -> "Fatal Python error: PyEval_RestoreThread ... " 进程崩溃。
            # 因此这里只做纯 Python/shell32 操作: 把路径存进普通列表, 绝不触碰 Tk。
            # 由主线程 _poll 定时消费 self._dropped 再回填界面。
            try:
                if msg == _WM_DROPFILES:
                    try:
                        count = _shell32.DragQueryFileW(wp, 0xFFFFFFFF, None, 0)
                    except Exception:
                        count = 0
                    if count and count < 1000:
                        for i in range(count):
                            try:
                                n = _shell32.DragQueryFileW(wp, i, None, 0)
                                if not n or n > 8192:
                                    continue
                                buf = ctypes.create_unicode_buffer(int(n) + 1)
                                _shell32.DragQueryFileW(wp, i, buf, int(n) + 1)
                                self._dropped.append(buf.value)
                            except Exception:
                                continue
                    try:
                        _shell32.DragFinish(wp)
                    except Exception:
                        pass
                    return 0
                if proc_ref.get("old"):
                    return _user32.CallWindowProcW(proc_ref["old"], hwnd, msg, wp, lp)
                return 0
            except Exception:
                return 0

        new_proc = WNDPROC(proc)
        old_val = _user32.SetWindowLongPtrW(hwnd, _GWL_WNDPROC, new_proc)
        if not old_val:
            return  # 子类化失败则放弃拖拽(不影响其余功能), 避免崩溃
        proc_ref["old"] = ctypes.c_void_p(old_val)
        self._drop_proc = new_proc      # 保持引用, 防止被 GC
        self.root._drop_proc = new_proc
        try:
            ex = _user32.GetWindowLongW(hwnd, _GWL_EXSTYLE)
            _user32.SetWindowLongW(hwnd, _GWL_EXSTYLE, ex | _WS_EX_ACCEPTFILES)
        except Exception:
            pass

    def _on_drop(self, files):
        if files:
            p = files[0]
            if p.lower().endswith(".pck"):
                self._set_pck(p)
            else:
                messagebox.showinfo("提示", "请拖入 .pck 文件")


def main():
    root = tk.Tk()
    app = App(root)
    if len(sys.argv) > 1 and os.path.isfile(sys.argv[1]):
        root.after(0, app._set_pck, sys.argv[1])
    root.mainloop()


if __name__ == "__main__":
    main()
