"""
PCM/WAV 轻量桌面播放器 —— PyQt6 版
====================================
依赖安装：
    pip install PyQt6 pyaudio

Windows 打包 exe：
    pip install pyinstaller
    pyinstaller --onefile --windowed pcm_player_pyqt6.py

功能说明：
    1. 选择根目录后递归扫描所有 .pcm/.wav 文件
    2. 面包屑导航，点击子目录/返回上级 浏览目录树
    3. 双击文件播放，支持播放/暂停/停止/上一首/下一首
    4. 音量滑块（0-100%）实时调节 + 静音开关
    5. 进度条 + 当前时间/总时长 + 播放状态显示
    6. 右侧播放列表面板：显示当前 ▶ / 下一首 → / 所在目录
    7. 单文件/批量 PCM→WAV 转换（固定参数写入 WAV 头）
    8. 批量转换递归扫根目录，输出到各 PCM 同级 wav/ 子目录
"""

import os
import wave
import struct
import threading

import pyaudio
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QObject
from PyQt6.QtGui import QColor, QFont
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget,
    QHBoxLayout, QVBoxLayout, QSplitter,
    QPushButton, QLabel, QSlider, QCheckBox, QProgressBar, QComboBox,
    QTreeWidget, QTreeWidgetItem,
    QFileDialog, QMessageBox, QDialog, QDialogButtonBox,
    QSizePolicy,
)

# ============================================================
# 核心参数（PCM 文件专用固定参数，WAV 文件自动读取头部参数）
# ============================================================
PCM_FORMAT   = pyaudio.paInt16   # 16 位深
PCM_CHANNELS = 1                 # 单声道
PCM_RATE     = 24000             # 采样率 Hz
PCM_CHUNK    = 1024              # 每次读取帧数

# UI 颜色
COLOR_PCM = QColor("#1a6b9e")    # PCM 文件（蓝色）
COLOR_WAV = QColor("#2e7d32")    # WAV 文件（绿色）
COLOR_DIR = QColor("#7b3f00")    # 目录（棕色）
COLOR_PLAYING_BG = QColor("#fff3b0")   # 当前播放行背景（黄）
COLOR_PLAYING_FG = QColor("#7b3f00")   # 当前播放行文字
COLOR_NEXT   = QColor("#2e7d32")       # 下一首文字（绿）

# ============================================================
# 工具函数
# ============================================================

def fmt_time(seconds: float) -> str:
    """将秒数格式化为 mm:ss"""
    if seconds < 0:
        return "00:00"
    m = int(seconds) // 60
    s = int(seconds) % 60
    return f"{m:02d}:{s:02d}"


def adjust_volume(data: bytes, volume: float) -> bytes:
    """
    对 PCM int16 字节数据做音量缩放，volume 范围 0.0-1.0。
    使用 clamp 截断，防止溢出爆音。
    """
    if volume == 1.0:
        return data
    samples = struct.unpack(f"<{len(data)//2}h", data)
    adjusted = [max(-32768, min(32767, int(s * volume))) for s in samples]
    return struct.pack(f"<{len(adjusted)}h", *adjusted)


# ============================================================
# 播放线程信号桥（子线程 → 主线程通信）
# ============================================================

class PlayerSignals(QObject):
    """播放线程向主线程发送的信号"""
    finished   = pyqtSignal()          # 正常播放完毕
    error      = pyqtSignal(str)       # 播放出错，携带错误信息


# ============================================================
# 主窗口
# ============================================================

class PCMPlayerApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PCM/WAV 播放器")
        self.resize(1120, 640)
        self.setMinimumSize(800, 500)

        # PyAudio 实例
        self.pa = pyaudio.PyAudio()

        # 目录状态
        self.root_dir:    str = ""
        self.current_dir: str = ""

        # 当前目录条目：{'type': 'dir'|'pcm'|'wav', 'name': str, 'path': str}
        self.entries:       list[dict] = []
        self.audio_entries: list[dict] = []      # 当前目录音频（普通上一首/下一首）
        self.all_audio_entries: list[dict] = []  # 全局递归音频（跨目录连播）
        self.current_index: int = -1

        # 播放状态
        self.play_thread: threading.Thread | None = None
        self._stop_flag  = threading.Event()
        self._pause_flag = threading.Event()
        self._pause_flag.set()   # set = 不阻塞

        self.is_playing = False
        self.is_paused  = False
        self.current_file: str = ""

        # 播放进度
        self.total_frames  = 0
        self.played_frames = 0

        # 音量
        self._volume     = 80        # 0-100 整数
        self._muted      = False
        self._pre_mute_vol = 80

        # 信号桥
        self._signals = PlayerSignals()
        self._signals.finished.connect(self._on_play_finished)
        self._signals.error.connect(self._on_play_error)

        self._build_ui()

        # 定时器：200ms 刷新进度 / 状态
        self._timer = QTimer(self)
        self._timer.setInterval(200)
        self._timer.timeout.connect(self._update_status)
        self._timer.start()

    # ----------------------------------------------------------
    # UI 构建
    # ----------------------------------------------------------

    def _build_ui(self):
        """构建完整 UI"""
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(10, 6, 10, 6)
        main_layout.setSpacing(4)

        # ---- 顶部：选目录 + 两个转换按钮 ----
        top = QHBoxLayout()
        self.btn_browse = QPushButton("选择根目录")
        self.btn_browse.clicked.connect(self.browse_dir)
        top.addWidget(self.btn_browse)
        top.addStretch()

        self.btn_conv  = QPushButton("转换选中 PCM→WAV")
        self.btn_conva = QPushButton("批量转换全部 PCM→WAV")
        self.btn_conv.clicked.connect(self.convert_selected)
        self.btn_conva.clicked.connect(self.convert_all)
        top.addWidget(self.btn_conv)
        top.addWidget(self.btn_conva)

        main_layout.addLayout(top)

        # ---- 面包屑导航 ----
        bc = QHBoxLayout()
        self.btn_up = QPushButton("⬆ 返回上级")
        self.btn_up.setEnabled(False)
        self.btn_up.clicked.connect(self.go_up)
        bc.addWidget(self.btn_up)
        bc.addWidget(QLabel("位置:"))
        self.lbl_breadcrumb = QLabel("未选择目录")
        self.lbl_breadcrumb.setStyleSheet("color: #333;")
        self.lbl_breadcrumb.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        bc.addWidget(self.lbl_breadcrumb)

        bc_widget = QWidget()
        bc_widget.setLayout(bc)
        bc_widget.setStyleSheet("background:#f0f0f0; padding:2px;")
        main_layout.addWidget(bc_widget)

        # ---- 文件浏览 + 播放列表（左右分割）----
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # 左侧：文件浏览器
        self.tree = QTreeWidget()
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels(["文件名 / 子目录", "格式", "相对路径"])
        self.tree.setColumnWidth(0, 220)
        self.tree.setColumnWidth(1, 55)
        self.tree.setColumnWidth(2, 320)
        self.tree.setSelectionMode(QTreeWidget.SelectionMode.SingleSelection)
        self.tree.itemDoubleClicked.connect(self._on_double_click)
        splitter.addWidget(self.tree)

        # 右侧：播放列表
        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)

        pl_title = QHBoxLayout()
        lbl_pl = QLabel("播放列表")
        lbl_pl.setStyleSheet("font-weight:bold; padding:3px 6px;")
        self.lbl_pl_count = QLabel("")
        self.lbl_pl_count.setStyleSheet("color:#666; padding:3px;")
        pl_title.addWidget(lbl_pl)
        pl_title.addWidget(self.lbl_pl_count)
        pl_title.addStretch()
        pl_title_widget = QWidget()
        pl_title_widget.setLayout(pl_title)
        pl_title_widget.setStyleSheet("background:#e8e8e8;")
        right_layout.addWidget(pl_title_widget)

        self.pl_tree = QTreeWidget()
        self.pl_tree.setColumnCount(3)
        self.pl_tree.setHeaderLabels(["", "文件名", "所在目录"])
        self.pl_tree.setColumnWidth(0, 22)
        self.pl_tree.setColumnWidth(1, 180)
        self.pl_tree.setColumnWidth(2, 220)
        self.pl_tree.itemDoubleClicked.connect(self._on_playlist_double_click)
        right_layout.addWidget(self.pl_tree)

        splitter.addWidget(right_widget)
        splitter.setSizes([620, 460])
        main_layout.addWidget(splitter, stretch=1)

        # ---- 播放状态区 ----
        status = QHBoxLayout()
        status.addWidget(QLabel("状态:"))
        self.lbl_state = QLabel("停止")
        self.lbl_state.setStyleSheet("color:#c0392b; font-weight:bold; min-width:48px;")
        status.addWidget(self.lbl_state)
        status.addWidget(QLabel(" | 文件:"))
        self.lbl_filename = QLabel("—")
        self.lbl_filename.setStyleSheet("color:#555;")
        self.lbl_filename.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        status.addWidget(self.lbl_filename)
        self.lbl_time = QLabel("00:00 / 00:00")
        self.lbl_time.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.lbl_time.setFixedWidth(110)
        status.addWidget(self.lbl_time)
        main_layout.addLayout(status)

        # ---- 进度条 ----
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(8)
        main_layout.addWidget(self.progress)

        # ---- 底部：播放控制居中 + 右侧播放模式/音量 ----
        bottom = QHBoxLayout()
        bottom.setContentsMargins(0, 4, 0, 0)

        # 左侧占位（与右侧等宽，使中间按钮真正居中）
        left_spacer = QHBoxLayout()
        left_spacer.addStretch()
        left_widget = QWidget()
        left_widget.setLayout(left_spacer)

        # 中间：播放控制按钮
        self.btn_prev = QPushButton("⏮")
        self.btn_play = QPushButton("▶ 播放")
        self.btn_next = QPushButton("⏭")
        for btn in (self.btn_prev, self.btn_play, self.btn_next):
            btn.setMinimumWidth(72)
            btn.setFixedHeight(32)
        self.btn_play.setMinimumWidth(96)
        self.btn_prev.clicked.connect(self.prev_track)
        self.btn_play.clicked.connect(self.toggle_play)
        self.btn_next.clicked.connect(self.next_track)

        center = QHBoxLayout()
        center.setSpacing(8)
        center.addWidget(self.btn_prev)
        center.addWidget(self.btn_play)
        center.addWidget(self.btn_next)
        center_widget = QWidget()
        center_widget.setLayout(center)

        # 右侧：播放模式 + 音量
        self.cmb_mode = QComboBox()
        self.cmb_mode.addItems(["不连播", "连播"])
        self.cmb_mode.setCurrentIndex(1)
        self.cmb_mode.setFixedWidth(75)

        self.vol_label = QLabel("80%")
        self.vol_label.setFixedWidth(36)
        self.vol_slider = QSlider(Qt.Orientation.Horizontal)
        self.vol_slider.setRange(0, 100)
        self.vol_slider.setValue(80)
        self.vol_slider.setFixedWidth(110)
        self.vol_slider.valueChanged.connect(self._on_volume_change)
        self.chk_mute = QCheckBox("静音")
        self.chk_mute.toggled.connect(self._on_mute_toggle)

        right = QHBoxLayout()
        right.setSpacing(6)
        right.addStretch()
        right.addWidget(self.cmb_mode)
        right.addWidget(QLabel("音量:"))
        right.addWidget(self.vol_label)
        right.addWidget(self.vol_slider)
        right.addWidget(self.chk_mute)
        right_ctrl_widget = QWidget()
        right_ctrl_widget.setLayout(right)

        # 用 stretch 让中间自然居中，右侧紧靠右边
        bottom.addWidget(left_widget, stretch=1)
        bottom.addWidget(center_widget, stretch=0)
        bottom.addWidget(right_ctrl_widget, stretch=1)

        # stop 按钮保留对象（部分逻辑引用），但不显示
        self.btn_stop = QPushButton("⏹ 停止")
        self.btn_stop.clicked.connect(self.stop)

        main_layout.addLayout(bottom)

    # ----------------------------------------------------------
    # 辅助：活动播放列表
    # ----------------------------------------------------------

    @property
    def _playlist(self) -> list:
        """播放列表：递归当前目录下所有音频（含子目录）"""
        if not self.current_dir or not self.all_audio_entries:
            return self.audio_entries
        prefix = self.current_dir.rstrip(os.sep) + os.sep
        return [e for e in self.all_audio_entries if e['path'].startswith(prefix)]

    # ----------------------------------------------------------
    # 目录扫描
    # ----------------------------------------------------------

    def _scan_all_audio(self):
        """递归扫描根目录所有音频文件"""
        self.all_audio_entries = []
        if not self.root_dir:
            return
        for dirpath, dirnames, filenames in os.walk(self.root_dir):
            dirnames.sort()
            for name in sorted(filenames):
                low = name.lower()
                if low.endswith(".pcm") or low.endswith(".wav"):
                    ftype = "pcm" if low.endswith(".pcm") else "wav"
                    self.all_audio_entries.append({
                        'type': ftype, 'name': name,
                        'path': os.path.join(dirpath, name),
                    })

    def browse_dir(self):
        """选择根目录"""
        d = QFileDialog.getExistingDirectory(self, "选择根目录")
        if not d:
            return
        self.root_dir    = d
        self.current_dir = d
        self._scan_all_audio()
        self._refresh_list()

    def _refresh_list(self):
        """刷新左侧文件浏览器"""
        if not self.current_dir or not os.path.isdir(self.current_dir):
            QMessageBox.critical(self, "错误", f"目录不存在：{self.current_dir}")
            return

        # 面包屑：根目录显示实际文件夹名而非"根目录"
        root_name = os.path.basename(self.root_dir) or self.root_dir
        rel = os.path.relpath(self.current_dir, self.root_dir)
        crumb = root_name if rel == "." else f"{root_name} / {rel.replace(os.sep, ' / ')}"
        self.lbl_breadcrumb.setText(crumb)
        self.btn_up.setEnabled(self.current_dir != self.root_dir)

        # 扫描目录
        self.entries = []
        try:
            items = sorted(os.listdir(self.current_dir))
        except PermissionError:
            QMessageBox.critical(self, "权限错误", f"无法访问：{self.current_dir}")
            return

        for name in items:
            full = os.path.join(self.current_dir, name)
            if os.path.isdir(full):
                self.entries.append({'type': 'dir', 'name': name, 'path': full})
        for name in items:
            full = os.path.join(self.current_dir, name)
            low  = name.lower()
            if os.path.isfile(full):
                if low.endswith(".pcm"):
                    self.entries.append({'type': 'pcm', 'name': name, 'path': full})
                elif low.endswith(".wav"):
                    self.entries.append({'type': 'wav', 'name': name, 'path': full})

        self.audio_entries = [e for e in self.entries if e['type'] in ('pcm', 'wav')]

        if not self.audio_entries and not any(e['type'] == 'dir' for e in self.entries):
            QMessageBox.information(self, "提示", "当前目录下没有 PCM/WAV 音频文件")

        self._fill_tree()
        self._refresh_playlist()

    def _fill_tree(self):
        """填充文件浏览器 Treeview"""
        playing = self.current_file
        self.tree.clear()
        bold = QFont()
        bold.setBold(True)

        for e in self.entries:
            rel_path = os.path.relpath(e['path'], self.root_dir)
            if e['type'] == 'dir':
                in_sub = bool(
                    playing and
                    os.path.commonpath([playing, e['path']]) == e['path']
                )
                label = f"▶ 📁 {e['name']}" if in_sub else f"📁 {e['name']}"
                item = QTreeWidgetItem([label, "目录", rel_path])
                item.setForeground(0, COLOR_DIR)
                item.setForeground(1, COLOR_DIR)
                item.setFont(0, bold)
            elif e['type'] == 'pcm':
                label = f"▶ {e['name']}" if e['path'] == playing else e['name']
                item = QTreeWidgetItem([label, "PCM", rel_path])
                item.setForeground(0, COLOR_PCM)
                item.setForeground(1, COLOR_PCM)
            else:
                label = f"▶ {e['name']}" if e['path'] == playing else e['name']
                item = QTreeWidgetItem([label, "WAV", rel_path])
                item.setForeground(0, COLOR_WAV)
                item.setForeground(1, COLOR_WAV)

            self.tree.addTopLevelItem(item)

    def _update_tree_marks(self):
        """不重扫目录，仅更新 ▶ 标记（播放切换时调用）"""
        playing = self.current_file
        for row, e in enumerate(self.entries):
            item = self.tree.topLevelItem(row)
            if item is None:
                continue
            if e['type'] == 'dir':
                in_sub = bool(
                    playing and
                    os.path.commonpath([playing, e['path']]) == e['path']
                )
                item.setText(0, f"▶ 📁 {e['name']}" if in_sub else f"📁 {e['name']}")
            elif e['type'] in ('pcm', 'wav'):
                item.setText(0, f"▶ {e['name']}" if e['path'] == playing else e['name'])

    def _refresh_playlist(self):
        """刷新右侧播放列表面板"""
        self.pl_tree.clear()
        pl    = self._playlist
        total = len(pl)
        cur   = self.current_index
        nxt   = cur + 1 if 0 <= cur < total - 1 else -1

        self.lbl_pl_count.setText(f"共 {total} 首")
        bold = QFont()
        bold.setBold(True)

        for i, e in enumerate(pl):
            dir_abs = os.path.dirname(e['path'])
            if self.root_dir:
                rel_dir = os.path.relpath(dir_abs, self.root_dir)
                dir_display = "." if rel_dir == "." else rel_dir.replace(os.sep, "/")
            else:
                dir_display = dir_abs

            if i == cur:
                mark = "▶"
                item = QTreeWidgetItem([mark, e['name'], dir_display])
                for col in range(3):
                    item.setBackground(col, COLOR_PLAYING_BG)
                    item.setForeground(col, COLOR_PLAYING_FG)
                item.setFont(1, bold)
            elif i == nxt:
                mark = "→"
                item = QTreeWidgetItem([mark, e['name'], dir_display])
                for col in range(3):
                    item.setForeground(col, COLOR_NEXT)
            else:
                mark = ""
                item = QTreeWidgetItem([mark, e['name'], dir_display])
                color = COLOR_PCM if e['type'] == 'pcm' else COLOR_WAV
                item.setForeground(1, color)

            self.pl_tree.addTopLevelItem(item)

        # 滚动到当前播放项
        if 0 <= cur < total:
            self.pl_tree.scrollToItem(self.pl_tree.topLevelItem(cur))

    # ----------------------------------------------------------
    # 导航
    # ----------------------------------------------------------

    def go_up(self):
        parent = os.path.dirname(self.current_dir)
        if parent and parent != self.current_dir and self.current_dir != self.root_dir:
            self.current_dir = parent
            self._refresh_list()

    def _on_double_click(self, item: QTreeWidgetItem, _col: int):
        row = self.tree.indexOfTopLevelItem(item)
        if row < 0 or row >= len(self.entries):
            return
        entry = self.entries[row]
        if entry['type'] == 'dir':
            self.current_dir = entry['path']
            self._refresh_list()
        else:
            self._load_and_play(entry['path'])

    def _on_playlist_double_click(self, item: QTreeWidgetItem, _col: int):
        row = self.pl_tree.indexOfTopLevelItem(item)
        pl  = self._playlist
        if 0 <= row < len(pl):
            self._load_and_play(pl[row]['path'])

    # ----------------------------------------------------------
    # 播放控制
    # ----------------------------------------------------------

    def _load_and_play(self, filepath: str):
        """停止当前 → 加载新文件 → 开始播放"""
        self._do_stop()
        if not os.path.isfile(filepath):
            QMessageBox.critical(self, "错误", f"文件不存在：{filepath}")
            return

        pl = self._playlist
        self.current_index = next(
            (i for i, e in enumerate(pl) if e['path'] == filepath), -1)
        self.current_file = filepath

        # 底部状态栏：[父目录]  文件名
        parent_dir = os.path.dirname(filepath)
        if self.root_dir:
            rel = os.path.relpath(parent_dir, self.root_dir)
            dir_label = "." if rel == "." else rel.replace(os.sep, "/")
        else:
            dir_label = os.path.basename(parent_dir)
        self.lbl_filename.setText(f"[{dir_label}]  {os.path.basename(filepath)}")

        self._refresh_playlist()
        self._update_tree_marks()

        self._stop_flag.clear()
        self._pause_flag.set()
        self.is_paused = False
        self.play_thread = threading.Thread(
            target=self._play_worker, args=(filepath,), daemon=True)
        self.play_thread.start()

    def _play_worker(self, filepath: str):
        """播放线程：PCM / WAV，含音量调节"""
        ext = os.path.splitext(filepath)[1].lower()
        try:
            if ext == ".pcm":
                self._play_pcm(filepath)
            elif ext == ".wav":
                self._play_wav(filepath)
            else:
                self._signals.error.emit(f"不支持的格式：{ext}")
        except Exception as e:
            self._signals.error.emit(str(e))
        finally:
            self.is_playing = False
            self.is_paused  = False
            if not self._stop_flag.is_set():
                self._signals.finished.emit()

    def _play_pcm(self, filepath: str):
        """播放 PCM 文件（固定参数）"""
        file_size = os.path.getsize(filepath)
        bpf = 2   # paInt16 = 2 bytes/frame
        self.total_frames  = file_size // bpf
        self.played_frames = 0
        self.is_playing    = True

        stream = self.pa.open(
            format=PCM_FORMAT, channels=PCM_CHANNELS,
            rate=PCM_RATE, output=True, frames_per_buffer=PCM_CHUNK)
        try:
            with open(filepath, "rb") as f:
                while not self._stop_flag.is_set():
                    self._pause_flag.wait()
                    if self._stop_flag.is_set():
                        break
                    data = f.read(PCM_CHUNK * bpf)
                    if not data:
                        break
                    data = adjust_volume(data, self._effective_volume())
                    stream.write(data)
                    self.played_frames += len(data) // bpf
        finally:
            stream.stop_stream()
            stream.close()

    def _play_wav(self, filepath: str):
        """播放 WAV 文件（自动读取头部参数）"""
        try:
            wf = wave.open(filepath, "rb")
        except wave.Error as e:
            raise RuntimeError(f"WAV 文件损坏或格式不支持：{e}")

        ch   = wf.getnchannels()
        rate = wf.getframerate()
        sw   = wf.getsampwidth()
        self.total_frames  = wf.getnframes()
        self.played_frames = 0
        self.is_playing    = True

        stream = self.pa.open(
            format=self.pa.get_format_from_width(sw),
            channels=ch, rate=rate, output=True,
            frames_per_buffer=PCM_CHUNK)
        try:
            while not self._stop_flag.is_set():
                self._pause_flag.wait()
                if self._stop_flag.is_set():
                    break
                data = wf.readframes(PCM_CHUNK)
                if not data:
                    break
                if sw == 2:
                    data = adjust_volume(data, self._effective_volume())
                stream.write(data)
                self.played_frames += PCM_CHUNK
        finally:
            wf.close()
            stream.stop_stream()
            stream.close()

    def _effective_volume(self) -> float:
        """返回有效音量（0.0-1.0），静音时返回 0"""
        return 0.0 if self._muted else self._volume / 100.0

    def toggle_play(self):
        """播放 / 暂停 切换"""
        if not self.is_playing and not self.is_paused:
            # 优先播放文件浏览器中选中的条目
            sel = self.tree.currentItem()
            if sel:
                row = self.tree.indexOfTopLevelItem(sel)
                if 0 <= row < len(self.entries):
                    entry = self.entries[row]
                    if entry['type'] in ('pcm', 'wav'):
                        self._load_and_play(entry['path'])
                        return
            # 否则：重播上次播放的文件；从未播过则播第一首
            pl = self._playlist
            if pl:
                idx = self.current_index if 0 <= self.current_index < len(pl) else 0
                self._load_and_play(pl[idx]['path'])

        elif self.is_playing and not self.is_paused:
            # 暂停
            self._pause_flag.clear()
            self.is_paused  = True
            self.is_playing = False
            self.lbl_state.setText("暂停")
            self.btn_play.setText("▶ 继续")

        elif self.is_paused:
            # 恢复
            self._pause_flag.set()
            self.is_playing = True
            self.is_paused  = False
            self.lbl_state.setText("播放中")
            self.btn_play.setText("⏸ 暂停")

    def stop(self):
        """停止播放"""
        self._do_stop()
        self.current_file = ""
        self.lbl_state.setText("停止")
        self.lbl_filename.setText("—")
        self.lbl_time.setText("00:00 / 00:00")
        self.progress.setValue(0)
        self.btn_play.setText("▶ 播放")
        self._update_tree_marks()
        self._refresh_playlist()

    def _do_stop(self):
        """内部停止（不刷新 UI）"""
        self._stop_flag.set()
        self._pause_flag.set()
        if self.play_thread and self.play_thread.is_alive():
            self.play_thread.join(timeout=2)
        self.is_playing = False
        self.is_paused  = False

    def prev_track(self):
        pl = self._playlist
        if not pl:
            return
        self.current_index = (self.current_index - 1) % len(pl)
        self._load_and_play(pl[self.current_index]['path'])

    def next_track(self):
        pl = self._playlist
        if not pl:
            return
        self.current_index = (self.current_index + 1) % len(pl)
        self._load_and_play(pl[self.current_index]['path'])

    # ----------------------------------------------------------
    # 播放完毕 / 错误回调（主线程）
    # ----------------------------------------------------------

    def _on_play_finished(self):
        """正常播放完毕，根据播放模式决定后续行为"""
        pl   = self._playlist
        mode = self.cmb_mode.currentText()   # 不连播 / 连播

        if mode == "连播" and self.current_index < len(pl) - 1:
            self.next_track()
        else:
            # 不连播，或连播已到最后一首 —— 停止，保留 current_index 不动
            self.lbl_state.setText("停止")
            self.progress.setValue(0)
            self.btn_play.setText("▶ 播放")
            self._refresh_playlist()

    def _on_play_error(self, msg: str):
        QMessageBox.critical(self, "播放错误", f"文件无法播放：\n{msg}")

    # ----------------------------------------------------------
    # 音量
    # ----------------------------------------------------------

    def _on_volume_change(self, val: int):
        self._volume = val
        self.vol_label.setText(f"{val}%")

    def _on_mute_toggle(self, checked: bool):
        self._muted = checked
        if checked:
            self._pre_mute_vol = self._volume
            self.vol_label.setText("静音")
        else:
            self._volume = self._pre_mute_vol
            self.vol_slider.setValue(self._pre_mute_vol)
            self.vol_label.setText(f"{self._pre_mute_vol}%")

    # ----------------------------------------------------------
    # 定时刷新（进度 / 时间 / 状态）
    # ----------------------------------------------------------

    def _update_status(self):
        """每 200ms 由 QTimer 调用"""
        if not self.is_playing:
            return
        self.lbl_state.setText("播放中")
        self.btn_play.setText("⏸ 暂停")

        if self.total_frames > 0:
            pct = min(1000, int(self.played_frames / self.total_frames * 1000))
            self.progress.setValue(pct)

            ext = os.path.splitext(self.current_file)[1].lower()
            if ext == ".pcm":
                rate = PCM_RATE
            else:
                try:
                    with wave.open(self.current_file, "rb") as wf:
                        rate = wf.getframerate()
                except Exception:
                    rate = PCM_RATE

            elapsed = self.played_frames / rate
            total   = self.total_frames  / rate
            self.lbl_time.setText(f"{fmt_time(elapsed)} / {fmt_time(total)}")

    # ----------------------------------------------------------
    # 格式转换
    # ----------------------------------------------------------

    def _pcm_to_wav(self, pcm_path: str, out_dir: str | None = None) -> str:
        """
        将单个 PCM 文件转换为 WAV。
        out_dir 指定时保存到该目录（自动创建），否则保存在 PCM 同目录。
        """
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
            wav_path = os.path.join(
                out_dir,
                os.path.splitext(os.path.basename(pcm_path))[0] + ".wav")
        else:
            wav_path = os.path.splitext(pcm_path)[0] + ".wav"

        with open(pcm_path, "rb") as f:
            pcm_data = f.read()
        with wave.open(wav_path, "wb") as wf:
            wf.setnchannels(PCM_CHANNELS)
            wf.setsampwidth(self.pa.get_sample_size(PCM_FORMAT))
            wf.setframerate(PCM_RATE)
            wf.writeframes(pcm_data)
        return wav_path

    def convert_selected(self):
        """转换选中的单个 PCM 文件"""
        sel = self.tree.currentItem()
        if not sel:
            QMessageBox.warning(self, "提示", "请先在列表中选中一个 PCM 文件")
            return
        row = self.tree.indexOfTopLevelItem(sel)
        if row < 0 or row >= len(self.entries):
            return
        entry = self.entries[row]
        if entry['type'] != 'pcm':
            QMessageBox.warning(self, "提示", "请选中一个 PCM 文件进行转换")
            return
        try:
            wav_path = self._pcm_to_wav(entry['path'])
            QMessageBox.information(self, "转换完成", f"已成功转换：\n{wav_path}")
            self._scan_all_audio()
            self._refresh_list()
        except Exception as e:
            QMessageBox.critical(self, "转换失败", f"转换出错：\n{e}")

    def convert_all(self):
        """递归扫描当前目录所有 PCM，批量转换到各自 wav/ 子目录"""
        if not self.current_dir:
            QMessageBox.warning(self, "提示", "请先选择目录")
            return

        pcm_list = []
        for dirpath, dirnames, filenames in os.walk(self.current_dir):
            dirnames.sort()
            for name in sorted(filenames):
                if name.lower().endswith(".pcm"):
                    pcm_list.append(os.path.join(dirpath, name))

        if not pcm_list:
            QMessageBox.warning(self, "提示", "当前目录下没有找到任何 PCM 文件")
            return

        # 进度对话框
        dlg = QDialog(self)
        dlg.setWindowTitle("批量转换中…")
        dlg.setFixedSize(460, 120)
        dlg.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, False)
        dlg_layout = QVBoxLayout(dlg)

        lbl_info = QLabel(f"共发现 {len(pcm_list)} 个 PCM 文件，正在转换…")
        dlg_layout.addWidget(lbl_info)

        pb = QProgressBar()
        pb.setRange(0, len(pcm_list))
        pb.setValue(0)
        dlg_layout.addWidget(pb)

        lbl_cur = QLabel("")
        dlg_layout.addWidget(lbl_cur)

        dlg.show()
        QApplication.processEvents()

        success, fail = 0, 0
        for i, pcm_path in enumerate(pcm_list):
            out_dir = os.path.join(os.path.dirname(pcm_path), "wav")
            rel = os.path.relpath(pcm_path, self.current_dir)
            lbl_cur.setText(f"({i+1}/{len(pcm_list)}) {rel}")
            pb.setValue(i + 1)
            QApplication.processEvents()
            try:
                self._pcm_to_wav(pcm_path, out_dir=out_dir)
                success += 1
            except Exception:
                fail += 1

        dlg.close()
        self._scan_all_audio()
        self._refresh_list()
        QMessageBox.information(
            self, "批量转换完成",
            f"转换结果：\n✅ 成功 {success} 个\n❌ 失败 {fail} 个\n\n"
            f"WAV 文件已保存在各 PCM 所在目录的 wav/ 子文件夹中"
        )

    # ----------------------------------------------------------
    # 关闭清理
    # ----------------------------------------------------------

    def closeEvent(self, event):
        """窗口关闭时释放资源"""
        self._timer.stop()
        self._do_stop()
        self.pa.terminate()
        event.accept()


# ============================================================
# 入口
# ============================================================

if __name__ == "__main__":
    import sys
    app = QApplication(sys.argv)
    win = PCMPlayerApp()
    win.show()
    sys.exit(app.exec())
