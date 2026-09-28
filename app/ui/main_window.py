# -*- coding: utf-8 -*-
"""
STEP 7 AI Agent 主窗口。

左侧：对话区（Markdown 渲染，气泡式消息，支持流式输出）
右侧：PLC 面板（模拟/真机连接、变量监控、安全门禁）+ 程序面板（代码/IO表/导出）
"""
import re
import threading

from PySide6.QtCore import (
    QEvent,
    Qt,
    QThread,
    QTimer,
    QObject,
    QMetaObject,
    Signal,
    Slot,
)
from PySide6.QtGui import QGuiApplication, QTextCursor, QTextCharFormat, QColor, QTextDocument
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextBrowser,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtGui import QPixmap

from app.agent.assistant import Assistant
from app.agent.tools import ToolExecutor
from app.core.config import base_dir, cloud_presets, load_config, save_config
from app.programs.ladder import LadderWidget, build_ladder, export_awl_text
from app.plc.address import parse_address
from app.plc.profiles import get_profile, profile_names
from app.scada import SUPPORTED_PROTOCOLS
from app.ui.workers import (
    ChatWorker,
    ConnTestWorker,
    PollWorker,
    ActionWorker,
    McgsDetectWorker,
    ModelProbeWorker,
)


# ---------------- 写操作跨线程确认 ----------------

class ConfirmBridge(QObject):
    """工作线程发起写请求，GUI 线程弹窗确认，工作线程阻塞等待结果。"""

    _request = Signal(str, str)

    def __init__(self):
        super().__init__()
        self._event = threading.Event()
        self._result = False
        # 跨线程自动使用队列连接，保证槽在 GUI 线程执行
        self._request.connect(self._on_request, Qt.ConnectionType.QueuedConnection)

    def ask(self, address: str, value: str) -> bool:
        self._event.clear()
        self._request.emit(address, value)
        self._event.wait(timeout=300)
        return self._result

    @Slot(str, str)
    def _on_request(self, address: str, value: str):
        btn = QMessageBox.warning(
            None,
            "PLC 写入确认",
            f"工具请求向 PLC 写入：\n\n    {address}  ←  {value}\n\n"
            "请核对地址与数值，确认执行？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        self._result = btn == QMessageBox.StandardButton.Yes
        self._event.set()


# ---------------- 对话面板 ----------------

def _md_to_fragment(text: str) -> str:
    """把一段 Markdown 转成可嵌入气泡的 HTML 片段。"""
    doc = QTextDocument()
    doc.setMarkdown(text or "……")
    html = doc.toHtml()
    m = re.search(r"<body[^>]*>(.*)</body>", html, re.S | re.I)
    frag = m.group(1) if m else html
    # QTextDocument 会输出空的 <p class="title">，去掉以免占高
    frag = re.sub(r'<p class="title"></p>', "", frag)
    return frag


class ChatPanel(QWidget):
    def __init__(self):
        super().__init__()
        self._messages = []   # (role, markdown)
        self._stream_buf = None
        self._frag_cache = []  # [i] = (原文, html片段)，避免逐 delta 重渲染全部历史
        # 流式防抖：delta 高频到达时合并为 150ms 一次渲染，
        # 否则长回复会以 O(delta数 × 全文) 频率做 Markdown 转换，卡死 GUI 线程
        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.setInterval(150)
        self._render_timer.timeout.connect(self._rebuild)

        self.view = QTextBrowser()
        self.view.setOpenExternalLinks(False)

        self.input_box = QTextEdit()
        self.input_box.setPlaceholderText("输入你的问题或控制需求……（Enter 发送，Shift+Enter 换行）")
        self.input_box.setFixedHeight(72)
        self.input_box.installEventFilter(self)

        self.btn_image = QPushButton("图片")
        self.btn_image.setToolTip("上传组态样式参考图，助手将根据图中元件自动设计 MCGS 画面")
        self.btn_image.clicked.connect(self._pick_image)
        self.img_thumb = QLabel()
        self.img_thumb.setFixedSize(48, 48)
        self.img_thumb.setStyleSheet(
            "border:1px solid #888; background:#eee;"
        )
        self.img_thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.img_thumb.setCursor(Qt.CursorShape.PointingHandCursor)
        self.img_thumb.mousePressEvent = self._clear_image
        self.img_thumb.hide()

        self.btn_send = QPushButton("发送")

        bottom = QHBoxLayout()
        bottom.addWidget(self.input_box, 1)
        bottom.addWidget(self.btn_image, 0, Qt.AlignmentFlag.AlignBottom)
        bottom.addWidget(self.img_thumb, 0, Qt.AlignmentFlag.AlignBottom)
        bottom.addWidget(self.btn_send, 0, Qt.AlignmentFlag.AlignBottom)

        layout = QVBoxLayout(self)
        layout.addWidget(self.view, 1)
        layout.addLayout(bottom)

        self._attached_image = None

    def eventFilter(self, obj, event):
        if obj is self.input_box and event.type() == QEvent.Type.KeyPress:
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not (
                event.modifiers() & Qt.KeyboardModifier.ShiftModifier
            ):
                self.btn_send.click()
                return True
        return super().eventFilter(obj, event)

    def add_message(self, role: str, text: str):
        self._messages.append((role, text))
        self._rebuild()

    def begin_stream(self):
        self._render_timer.stop()
        self._stream_buf = ""
        self._messages.append(("assistant", ""))
        self._rebuild()

    def append_stream(self, text: str):
        if self._stream_buf is None:
            self.begin_stream()
        self._stream_buf += text
        self._messages[-1] = ("assistant", self._stream_buf)
        # 只累积，渲染交给防抖定时器（150ms 合并一次）
        self._render_timer.start()

    def end_stream(self):
        self._stream_buf = None
        # 结束时取消防抖并做一次最终渲染
        self._render_timer.stop()
        self._rebuild()

    def clear_all(self):
        self._messages = []
        self._stream_buf = None
        self._frag_cache = []
        self._render_timer.stop()
        self._rebuild()

    def set_busy(self, busy: bool):
        self.btn_send.setEnabled(not busy)
        self.input_box.setEnabled(not busy)
        self.btn_image.setEnabled(not busy)

    def current_input(self) -> str:
        return self.input_box.toPlainText().strip()

    def current_image(self):
        """返回当前附加的图片路径，无则返回 None。"""
        return self._attached_image

    def clear_input(self):
        self.input_box.clear()
        self._clear_image()

    def _pick_image(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择组态参考图", "",
            "图片文件 (*.png *.jpg *.jpeg *.bmp *.webp *.gif);;所有文件 (*.*)"
        )
        if not path:
            return
        pix = QPixmap(path)
        if pix.isNull():
            QMessageBox.warning(self, "提示", "无法读取该图片，请换一张。")
            return
        self._attached_image = path
        self.img_thumb.setPixmap(pix.scaled(
            48, 48, Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation))
        self.img_thumb.setToolTip(f"已附加：{path}\n点击缩略图可移除")
        self.img_thumb.show()

    def _clear_image(self, _event=None):
        self._attached_image = None
        self.img_thumb.clear()
        self.img_thumb.hide()

    def _frag(self, idx: int, text: str) -> str:
        """带缓存的 markdown→html：文本未变时直接复用，流式期间只重渲染最后一条。"""
        if idx >= len(self._frag_cache):
            self._frag_cache.extend([None] * (idx + 1 - len(self._frag_cache)))
        cached = self._frag_cache[idx]
        if cached is not None and cached[0] == text:
            return cached[1]
        frag = _md_to_fragment(text)
        self._frag_cache[idx] = (text, frag)
        return frag

    def _rebuild(self):
        bubbles = []
        for i, (role, text) in enumerate(self._messages):
            frag = self._frag(i, text)
            if role == "user":
                caption = (
                    '<div style="color:#6b7280;font-size:11px;margin:6px 4px 1px 0;">我</div>'
                )
                bubble = (
                    '<table width="100%" cellpadding="0" cellspacing="2">'
                    '<tr><td align="right">'
                    + caption +
                    '</td></tr><tr><td align="right">'
                    '<table cellspacing="0" style="background:#2563eb;color:white;">'
                    f'<tr><td style="color:white;padding:8px 12px;">{frag}</td></tr></table>'
                    '</td></tr></table>'
                )
            else:
                caption = (
                    '<div style="color:#6b7280;font-size:11px;margin:6px 0 1px 4px;">'
                    'STEP 7 AI 助手</div>'
                )
                bubble = (
                    '<table width="100%" cellpadding="0" cellspacing="2">'
                    '<tr><td align="left">'
                    + caption +
                    '</td></tr><tr><td align="left">'
                    '<table cellspacing="0" style="background:white;border:1px solid #e3e8f0;">'
                    f'<tr><td style="padding:8px 12px;">{frag}</td></tr></table>'
                    '</td></tr></table>'
                )
            bubbles.append(bubble)
        html = (
            '<html><head><style>'
            'body{background:#f4f6fb;}'
            'p{margin:3px 0;}'
            'code{background:#eef2ff;font-family:Consolas,monospace;padding:0 2px;}'
            'pre{background:#f1f5f9;padding:6px;border-radius:6px;}'
            'table{margin:0;}'
            'li{margin:2px 0;}'
            '</style></head><body>'
            + "".join(bubbles)
            + '</body></html>'
        )
        self.view.setHtml(html)
        self.view.verticalScrollBar().setValue(self.view.verticalScrollBar().maximum())


# ---------------- PLC 面板 ----------------

class PlcPanel(QWidget):
    tags_changed = Signal()

    def __init__(self, executor: ToolExecutor):
        super().__init__()
        self.executor = executor

        # ---- 模拟 PLC ----
        mock_box = QGroupBox("模拟 PLC（无硬件即可练习）")
        self.btn_mock = QPushButton("启动模拟PLC")
        self.lbl_mock = QLabel("模拟PLC：未启动")
        mlay = QVBoxLayout(mock_box)
        mlay.addWidget(self.btn_mock)
        mlay.addWidget(self.lbl_mock)

        # ---- 真机连接 ----
        real_box = QGroupBox("真机连接")
        self.cmb_profile = QComboBox()
        self.cmb_profile.addItems(profile_names())
        self.edt_host = QLineEdit()
        self.spn_rack = QSpinBox()
        self.spn_rack.setRange(0, 10)
        self.spn_slot = QSpinBox()
        self.spn_slot.setRange(0, 10)
        self.btn_connect = QPushButton("连接")
        self.lbl_conn = QLabel("PLC：未连接")

        form = QFormLayout()
        form.addRow("设备型号：", self.cmb_profile)
        form.addRow("IP 地址：", self.edt_host)
        form.addRow("Rack：", self.spn_rack)
        form.addRow("Slot：", self.spn_slot)
        rlay = QVBoxLayout(real_box)
        rlay.addLayout(form)
        rlay.addWidget(self.btn_connect)
        rlay.addWidget(self.lbl_conn)

        # ---- 安全门禁 ----
        safe_box = QGroupBox("安全")
        self.chk_write = QCheckBox("允许写入（默认只读；开启后每次写入仍会弹窗确认）")
        slay = QVBoxLayout(safe_box)
        slay.addWidget(self.chk_write)

        # ---- 变量监控 ----
        watch_box = QGroupBox("变量监控")
        self.tbl_tags = QTableWidget(0, 2)
        self.tbl_tags.setHorizontalHeaderLabels(["地址", "当前值"])
        self.tbl_tags.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.edt_new_tag = QLineEdit()
        self.edt_new_tag.setPlaceholderText("如 VW100 / Q0.0 / VD120")
        self.btn_add = QPushButton("添加")
        self.btn_remove = QPushButton("删除选中")
        self.chk_auto = QCheckBox("自动刷新(1秒)")
        add_row = QHBoxLayout()
        add_row.addWidget(self.edt_new_tag, 1)
        add_row.addWidget(self.btn_add)
        add_row.addWidget(self.btn_remove)

        self.edt_write_value = QLineEdit()
        self.edt_write_value.setPlaceholderText("写入值（布尔写 true/false）")
        self.btn_write = QPushButton("写入选中行")
        write_row = QHBoxLayout()
        write_row.addWidget(self.edt_write_value, 1)
        write_row.addWidget(self.btn_write)

        wlay = QVBoxLayout(watch_box)
        wlay.addWidget(self.tbl_tags, 1)
        wlay.addLayout(add_row)
        wlay.addWidget(self.chk_auto)
        wlay.addLayout(write_row)

        root = QVBoxLayout(self)
        root.addWidget(mock_box)
        root.addWidget(real_box)
        root.addWidget(safe_box)
        root.addWidget(watch_box, 1)

        self._fill_profile_defaults()

    # ---- 默认值 ----
    def _fill_profile_defaults(self):
        name = self.cmb_profile.currentText()
        pf = get_profile(name)
        self.edt_host.setText(pf["host"])
        self.spn_rack.setValue(pf["rack"])
        self.spn_slot.setValue(pf["slot"])

    def sync_from_config(self, cfg: dict):
        plc = cfg["plc"]
        idx = self.cmb_profile.findText(plc.get("profile", "S7-200 SMART"))
        if idx >= 0:
            self.cmb_profile.setCurrentIndex(idx)
        self.edt_host.setText(plc.get("host", self.edt_host.text()))
        self.spn_rack.setValue(int(plc.get("rack", 0)))
        self.spn_slot.setValue(int(plc.get("slot", 1)))
        self.chk_write.setChecked(bool(plc.get("allow_write", False)))

    # ---- 状态刷新 ----
    def refresh_status(self):
        self.lbl_mock.setText(
            f"模拟PLC：{'运行中' if self.executor.mock.running else '未启动'}"
        )
        if self.executor.connected:
            kind = "模拟PLC" if self.executor.is_mock_connection else "真机"
            self.lbl_conn.setText(f"PLC：已连接（{kind}）")
            self.btn_connect.setText("断开")
        else:
            self.lbl_conn.setText("PLC：未连接")
            self.btn_connect.setText("连接")
        self.btn_mock.setText(
            "停止模拟PLC" if self.executor.mock.running else "启动模拟PLC"
        )

    def mark_all(self, text: str = "—"):
        """断开连接时把所有当前值重置为占位符。"""
        for row in range(self.tbl_tags.rowCount()):
            self.tbl_tags.item(row, 1).setText(text)

    @Slot(object)
    def apply_values(self, results):
        """后台轮询结果回到 GUI 线程后刷新表格。"""
        for row, text in results:
            if 0 <= row < self.tbl_tags.rowCount():
                self.tbl_tags.item(row, 1).setText(text)

    def watch_addresses(self):
        """供轮询线程使用：[(行号, 地址), ...]"""
        return [
            (row, self.tbl_tags.item(row, 0).text())
            for row in range(self.tbl_tags.rowCount())
        ]

    # ---- 监控表操作 ----
    def add_tag(self, address: str = ""):
        address = address or self.edt_new_tag.text().strip()
        if not address:
            return
        try:
            parse_address(address)
        except ValueError as e:
            QMessageBox.warning(self, "地址无效", f"无法识别的地址：{address}\n\n{e}")
            return
        row = self.tbl_tags.rowCount()
        self.tbl_tags.insertRow(row)
        self.tbl_tags.setItem(row, 0, QTableWidgetItem(address))
        self.tbl_tags.setItem(row, 1, QTableWidgetItem("—"))
        self.edt_new_tag.clear()
        self.tags_changed.emit()

    def remove_selected(self):
        rows = sorted({i.row() for i in self.tbl_tags.selectedIndexes()}, reverse=True)
        for row in rows:
            self.tbl_tags.removeRow(row)
        if rows:
            self.tags_changed.emit()

    def selected_address(self):
        rows = {i.row() for i in self.tbl_tags.selectedIndexes()}
        if len(rows) == 1:
            row = rows.pop()
            return self.tbl_tags.item(row, 0).text()
        return None


# ---------------- 程序面板 ----------------

class CodePanel(QWidget):
    def __init__(self):
        super().__init__()
        self._program = None

        self.lbl_title = QLabel("尚未生成程序。可在对话中描述需求，或要求“用模板生成”。")

        # 代码页
        self.code_view = QPlainTextEdit()
        self.code_view.setReadOnly(True)
        font = self.code_view.font()
        font.setFamily("Consolas, Courier New")
        self.code_view.setFont(font)

        # 梯形图页
        self.ladder = LadderWidget()
        ladder_scroll = QScrollArea()
        ladder_scroll.setWidgetResizable(True)
        ladder_scroll.setWidget(self.ladder)

        # IO 表页
        self.io_table = QTableWidget(0, 2)
        self.io_table.setHorizontalHeaderLabels(["地址", "说明"])
        self.io_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)

        # 说明页
        self.doc_view = QTextBrowser()

        self.tabs = QTabWidget()
        self.tabs.addTab(self.code_view, "程序代码")
        self.tabs.addTab(ladder_scroll, "梯形图")
        self.tabs.addTab(self.io_table, "I/O 分配表")
        self.tabs.addTab(self.doc_view, "原理与导入")

        self.btn_copy = QPushButton("复制代码")
        self.btn_export = QPushButton("导出文件")
        self.btn_awl = QPushButton("导出AWL")
        self.btn_awl.setToolTip("导出 Micro/WIN SMART 可直接导入的 .awl 程序文件")
        self.btn_png = QPushButton("梯形图存PNG")
        btn_row = QHBoxLayout()
        btn_row.addWidget(self.btn_copy)
        btn_row.addWidget(self.btn_export)
        btn_row.addWidget(self.btn_awl)
        btn_row.addWidget(self.btn_png)
        btn_row.addStretch(1)

        layout = QVBoxLayout(self)
        layout.addWidget(self.lbl_title)
        layout.addWidget(self.tabs, 1)
        layout.addLayout(btn_row)

    def show_program(self, program: dict):
        self._program = program
        self.lbl_title.setText(
            f"{program['name']}（目标：{program['target']} · 语言：{program['language']}）"
        )
        self.code_view.setPlainText(program["code"])
        self.io_table.setRowCount(0)
        for addr, desc in program["io_table"]:
            row = self.io_table.rowCount()
            self.io_table.insertRow(row)
            self.io_table.setItem(row, 0, QTableWidgetItem(addr))
            self.io_table.setItem(row, 1, QTableWidgetItem(desc))
        self.doc_view.setMarkdown(
            f"**工作原理：**\n\n{program['explanation']}\n\n"
            f"**导入步骤：**\n\n{program['import_guide']}"
        )
        rungs = build_ladder(program)
        self.ladder.set_program(program["name"], rungs)
        is_stl = program["language"] == "STL"
        self.btn_awl.setEnabled(is_stl)
        self.btn_png.setEnabled(rungs is not None)

    def copy_code(self):
        if self._program:
            QGuiApplication.clipboard().setText(self._program["code"])

    def export_file(self):
        if not self._program:
            return
        ext = ".scl" if self._program["language"] == "SCL" else ".stl"
        path, _ = QFileDialog.getSaveFileName(
            self, "导出程序", f"{self._program['key']}{ext}",
            f"程序文件 (*{ext});;所有文件 (*.*)",
        )
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self._program["code"])

    def export_awl(self):
        if not self._program or self._program["language"] != "STL":
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "导出 AWL（Micro/WIN SMART 可导入）",
            f"{self._program['key']}.awl",
            "AWL 程序文件 (*.awl);;所有文件 (*.*)",
        )
        if not path:
            return
        try:
            # Micro/WIN SMART 读取 ANSI 编码，中文注释用 GBK 保存
            with open(path, "w", encoding="gbk", errors="replace") as f:
                f.write(export_awl_text(self._program))
            QMessageBox.information(
                self, "导出成功",
                f"已导出：{path}\n\n导入方法：Micro/WIN SMART 菜单"
                "“文件 > 导入”，选择该 .awl 文件即可。",
            )
        except Exception as e:
            QMessageBox.warning(self, "导出失败", str(e))

    def export_ladder_png(self):
        if not self._program or not self.ladder.has_content():
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "导出梯形图", f"{self._program['key']}_梯形图.png",
            "PNG 图片 (*.png);;所有文件 (*.*)",
        )
        if not path:
            return
        try:
            if self.ladder.grab().save(path, "PNG"):
                QMessageBox.information(self, "导出成功", f"已导出：{path}")
            else:
                QMessageBox.warning(self, "导出失败", "图片保存失败，请检查路径权限。")
        except Exception as e:
            QMessageBox.warning(self, "导出失败", str(e))


# ---------------- MCGS 组态面板（独立同级面板） ----------------

class ScadaPanel(QWidget):
    """MCGS 组态设计助手独立面板。

    功能：
    1. MCGS 连接按钮：检测本机是否安装 MCGS（McgsPro/嵌入版/通用版），
       已安装则一键启动；未安装显示提示。
    2. 版本/协议下拉：McgsPro / 嵌入版 / 通用版 × PPI / Modbus / OPC
    3. 4 个素材 Tab（占满面板）：变量字典 / 设备通道 / 画面设计书 / 脚本
    4. 导出包：一键导出 7 个文件到目录
    """

    def __init__(self, tool_executor: ToolExecutor, config: dict):
        super().__init__()
        self._scada = None
        self.executor = tool_executor
        self.config = config

        # ========== 顶部：MCGS 连接区 ==========
        self.btn_connect_mcgs = QPushButton("🔌 重新检测")
        self.btn_connect_mcgs.setToolTip("自动检测本机 MCGS 安装位置（注册表→快捷方式→全盘搜索）")
        self.lbl_mcgs_status = QLabel("未检测到 MCGS")
        self.lbl_mcgs_status.setStyleSheet("color: #999;")
        self.btn_start_mcgs = QPushButton("▶ 启动 MCGS")
        self.btn_start_mcgs.setEnabled(False)
        self.btn_start_mcgs.setToolTip("启动已安装的 MCGS 组态软件")
        self.btn_locate_mcgs = QPushButton("📂 手动定位")
        self.btn_locate_mcgs.setToolTip("自动检测不到时，手动选择 MCGS 组态软件可执行文件")
        self.lbl_mcgs_path = QLabel("")
        self.lbl_mcgs_path.setStyleSheet("color: #6b7280;")
        self.mcgs_exe_path = None

        conn_row = QHBoxLayout()
        conn_row.addWidget(self.btn_connect_mcgs)
        conn_row.addWidget(self.lbl_mcgs_status)
        conn_row.addWidget(self.btn_start_mcgs)
        conn_row.addWidget(self.btn_locate_mcgs)
        conn_row.addWidget(self.lbl_mcgs_path, 1)

        # ========== 中部：生成控制条 ==========
        self.cmb_version = QComboBox()
        self.cmb_version.addItems(["McgsPro", "嵌入版", "通用版"])
        self.cmb_protocol = QComboBox()
        self.cmb_protocol.addItems(["PPI", "Modbus", "OPC"])
        self.btn_generate = QPushButton("⚡ 生成组态素材")
        self.btn_generate.setToolTip("按当前版本/协议，用默认工艺描述生成组态素材")
        self.btn_generate.setProperty("variant", "success")
        self.btn_export = QPushButton("📦 导出包")
        self.btn_export.setToolTip("将当前素材导出为 7 个文件到指定目录")
        self.btn_auto_write = QPushButton("🔄 写入 MCGS")
        self.btn_auto_write.setToolTip("自动写入 MCGS（需先启动 MCGS，UI 自动化）")
        self.btn_auto_write.setProperty("variant", "primary")

        ctrl_row = QHBoxLayout()
        ctrl_row.addWidget(QLabel("版本"))
        ctrl_row.addWidget(self.cmb_version)
        ctrl_row.addWidget(QLabel("协议"))
        ctrl_row.addWidget(self.cmb_protocol)
        ctrl_row.addStretch(1)
        ctrl_row.addWidget(self.btn_generate)
        ctrl_row.addWidget(self.btn_export)
        ctrl_row.addWidget(self.btn_auto_write)

        # ========== 自动化进度条（默认隐藏） ==========
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 3)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setFormat("%v/3  %p%")
        self.progress_bar.setVisible(False)
        self.lbl_progress = QLabel("")
        self.lbl_progress.setStyleSheet("color: #0969da; font-weight: bold;")

        progress_row = QHBoxLayout()
        progress_row.addWidget(QLabel("自动化进度："))
        progress_row.addWidget(self.progress_bar, 1)
        progress_row.addWidget(self.lbl_progress)
        progress_container = QWidget()
        progress_container.setLayout(progress_row)
        progress_container.setVisible(False)
        self._progress_container = progress_container

        # ========== 4 个素材 Tab（占满剩余空间） ==========
        self.material_tabs = QTabWidget()

        # --- Tab 1：变量字典 ---
        var_tab = QWidget()
        self.tbl_vars = QTableWidget(0, 6)
        self.tbl_vars.setHorizontalHeaderLabels(
            ["变量名", "类型", "地址", "数据类型", "读写", "说明"]
        )
        self.tbl_vars.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self.tbl_vars.verticalHeader().setDefaultSectionSize(32)
        btn_copy_vars = QPushButton("📋 复制 CSV")
        btn_copy_vars.clicked.connect(self._copy_vars_csv)
        var_lay = QVBoxLayout(var_tab)
        var_lay.addWidget(self.tbl_vars, 1)
        var_btn_row = QHBoxLayout()
        var_btn_row.addStretch(1)
        var_btn_row.addWidget(btn_copy_vars)
        var_lay.addLayout(var_btn_row)
        self.material_tabs.addTab(var_tab, "📋 变量字典")

        # --- Tab 2：设备通道 CSV ---
        csv_tab = QWidget()
        self.txt_csv = QPlainTextEdit()
        self.txt_csv.setReadOnly(True)
        font = self.txt_csv.font()
        font.setFamily("Consolas, Courier New")
        font.setPointSize(font.pointSize() + 1)
        self.txt_csv.setFont(font)
        btn_copy_csv = QPushButton("📋 复制 CSV")
        btn_copy_csv.clicked.connect(
            lambda: QGuiApplication.clipboard().setText(self.txt_csv.toPlainText())
        )
        csv_lay = QVBoxLayout(csv_tab)
        csv_lay.addWidget(self.txt_csv, 1)
        csv_btn_row = QHBoxLayout()
        csv_btn_row.addStretch(1)
        csv_btn_row.addWidget(btn_copy_csv)
        csv_lay.addLayout(csv_btn_row)
        self.material_tabs.addTab(csv_tab, "🔌 设备通道 CSV")

        # --- Tab 3：画面设计书 ---
        screen_tab = QWidget()
        self.txt_screen = QTextBrowser()
        self.txt_screen.setOpenExternalLinks(False)
        btn_copy_screen = QPushButton("📋 复制设计书")
        btn_copy_screen.clicked.connect(
            lambda: QGuiApplication.clipboard().setText(self.txt_screen.toPlainText())
        )
        screen_lay = QVBoxLayout(screen_tab)
        screen_lay.addWidget(self.txt_screen, 1)
        screen_btn_row = QHBoxLayout()
        screen_btn_row.addStretch(1)
        screen_btn_row.addWidget(btn_copy_screen)
        screen_lay.addLayout(screen_btn_row)
        self.material_tabs.addTab(screen_tab, "🖼 画面设计书")

        # --- Tab 4：McgsScript 脚本 ---
        script_tab = QWidget()
        self.txt_script = QPlainTextEdit()
        self.txt_script.setReadOnly(True)
        sc_font = self.txt_script.font()
        sc_font.setFamily("Consolas, Courier New")
        sc_font.setPointSize(sc_font.pointSize() + 1)
        self.txt_script.setFont(sc_font)
        btn_copy_script = QPushButton("📋 复制脚本")
        btn_copy_script.clicked.connect(
            lambda: QGuiApplication.clipboard().setText(self.txt_script.toPlainText())
        )
        script_lay = QVBoxLayout(script_tab)
        script_lay.addWidget(self.txt_script, 1)
        script_btn_row = QHBoxLayout()
        script_btn_row.addStretch(1)
        script_btn_row.addWidget(btn_copy_script)
        script_lay.addLayout(script_btn_row)
        self.material_tabs.addTab(script_tab, "📝 McgsScript 脚本")

        # ========== 全局布局 ==========
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)
        layout.addLayout(conn_row)
        layout.addLayout(ctrl_row)
        layout.addWidget(self._progress_container)
        layout.addWidget(self.material_tabs, 1)

        # ========== 信号 ==========
        self.btn_connect_mcgs.clicked.connect(self._detect_mcgs)
        self.btn_start_mcgs.clicked.connect(self._start_mcgs)
        self.btn_locate_mcgs.clicked.connect(self._manual_locate_mcgs)
        self.cmb_version.currentIndexChanged.connect(self._on_version_changed)
        self.btn_generate.clicked.connect(self._generate_now)
        self.btn_export.clicked.connect(self.export_package)
        self.btn_auto_write.clicked.connect(self._auto_write_to_mcgs)

        # 检测线程占位
        self._detect_thread = None
        self._detect_worker = None

        # ========== 初始化版本/协议下拉（先按配置过滤协议，再恢复选择） ==========
        mcgs_cfg = config.get("mcgs", {})
        self._on_version_changed()
        vi = self.cmb_version.findText(mcgs_cfg.get("version", "McgsPro"))
        if vi >= 0:
            self.cmb_version.setCurrentIndex(vi)
        pi = self.cmb_protocol.findText(mcgs_cfg.get("protocol", "PPI"))
        if pi >= 0:
            self.cmb_protocol.setCurrentIndex(pi)

        # ========== 首次自动检测 MCGS（后台，避免全盘搜索卡住启动） ==========
        self._detect_mcgs()

    # ---------- MCGS 后台检测 ----------
    def _detect_mcgs(self):
        """后台分层检测本机 MCGS 安装路径（注册表 > 快捷方式 > 全盘搜索）。"""
        if self._detect_thread is not None:
            return
        self.lbl_mcgs_status.setText("检测中……")
        self.lbl_mcgs_status.setStyleSheet("color: #999;")
        self.lbl_mcgs_path.setText("")

        thread = QThread()
        worker = McgsDetectWorker()
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.detected.connect(self._apply_detection)
        thread.start()
        self._detect_thread = thread
        self._detect_worker = worker

    def _apply_detection(self, ver: str, path: str):
        """检测线程完成后回到 GUI 线程渲染结果。"""
        thread, worker = self._detect_thread, self._detect_worker
        self._detect_thread = self._detect_worker = None
        if thread is not None:
            thread.quit()
            thread.wait()
            worker.deleteLater()

        if ver and path:
            self.mcgs_exe_path = path
            self.lbl_mcgs_status.setText(f"✓ {ver}")
            self.lbl_mcgs_status.setStyleSheet("color: #16a34a; font-weight: bold;")
            self.lbl_mcgs_path.setText(f"📁 {path}")
            self.btn_start_mcgs.setEnabled(True)
            idx = self.cmb_version.findText(ver)
            if idx >= 0:
                self.cmb_version.setCurrentIndex(idx)
        else:
            self.mcgs_exe_path = None
            self.lbl_mcgs_status.setText("✗ 未检测到")
            self.lbl_mcgs_status.setStyleSheet("color: #dc2626;")
            self.lbl_mcgs_path.setText(
                "已扫描注册表/快捷方式/全盘，未找到 MCGS。"
                "可点「📂 手动定位」，或直接选择版本后点生成。"
            )
            self.btn_start_mcgs.setEnabled(False)

    def _on_version_changed(self):
        """版本变化时按支持矩阵过滤协议；嵌入版不支持 OPC。"""
        version = self.cmb_version.currentText()
        allowed = SUPPORTED_PROTOCOLS.get(version, ["PPI"])
        cur = self.cmb_protocol.currentText()
        self.cmb_protocol.blockSignals(True)
        self.cmb_protocol.clear()
        self.cmb_protocol.addItems(allowed)
        if cur in allowed:
            self.cmb_protocol.setCurrentText(cur)
        self.cmb_protocol.blockSignals(False)

    def _manual_locate_mcgs(self):
        """用户通过文件对话框手动选择 MCGS exe 路径。

        若误选运行/模拟环境（CEEMU.exe 等），自动替换为同目录组态环境主程序。
        """
        import os
        from app.scada.mcgs_knowledge import (
            _detect_version_from_exe_path,
            normalize_mcgs_exe_path,
            MCGS_RUNTIME_EXES,
        )
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 MCGS 组态环境主程序（McgsSetE / McgsSetPro）", "",
            "可执行文件 (*.exe);;所有文件 (*.*)",
        )
        if not path:
            return
        if not os.path.isfile(path):
            QMessageBox.warning(self, "路径无效", f"文件不存在：\n{path}")
            return

        normalized = normalize_mcgs_exe_path(path)
        if not normalized:
            QMessageBox.warning(
                self, "无法识别组态环境",
                f"所选文件不是 MCGS 组态环境主程序，且同目录未找到组态程序：\n{path}\n\n"
                "请选择 McgsSetE.exe（嵌入版）或 McgsSetPro.exe（McgsPro）。")
            return

        note = ""
        if os.path.basename(path).lower() in MCGS_RUNTIME_EXES:
            note = f"（检测到运行环境 {os.path.basename(path)}，已自动定位组态程序）"
        path = normalized
        ver = _detect_version_from_exe_path(path)
        self.mcgs_exe_path = path
        self.lbl_mcgs_status.setText(f"✓ {ver}")
        self.lbl_mcgs_status.setStyleSheet("color: #1a7f37; font-weight: bold;")
        self.lbl_mcgs_path.setText(f"📁 {path}（手动定位）{note}")
        self.btn_start_mcgs.setEnabled(True)
        idx = self.cmb_version.findText(ver)
        if idx >= 0:
            self.cmb_version.setCurrentIndex(idx)

    def _start_mcgs(self):
        """启动 MCGS 组态环境（独立进程）。"""
        if not self.mcgs_exe_path:
            return
        import os
        import subprocess
        from app.scada.mcgs_knowledge import normalize_mcgs_exe_path
        exe_path = normalize_mcgs_exe_path(self.mcgs_exe_path) or self.mcgs_exe_path
        self.mcgs_exe_path = exe_path
        try:
            work_dir = os.path.dirname(exe_path)
            subprocess.Popen(
                [exe_path],
                cwd=work_dir or None,
            )
            self.lbl_mcgs_path.setText(
                "▶ 已启动 MCGS 组态环境。写入时若无打开工程将自动新建工程。"
            )
        except Exception as e:
            QMessageBox.warning(self, "启动失败", str(e))

    # ---------- 生成组态 ----------
    def _generate_now(self):
        """按当前版本/协议生成组态素材，自动刷新 4 个 Tab。"""
        version = self.cmb_version.currentText()
        protocol = self.cmb_protocol.currentText()
        self.btn_generate.setEnabled(False)
        self.btn_generate.setText("生成中……")
        QGuiApplication.processEvents()
        try:
            self.executor.generate_scada(
                craft_desc="电机启停控制，带运行指示与故障报警",
                mcgs_version=version,
                protocol=protocol,
            )
        except Exception as e:
            QMessageBox.warning(self, "生成失败", str(e))
        finally:
            self.btn_generate.setEnabled(True)
            self.btn_generate.setText("⚡ 生成组态素材")

    # ---------- 渲染 ----------
    def show_scada(self, scada: dict):
        """渲染 scada dict 到 4 个素材 Tab。"""
        self._scada = scada
        name = scada.get("name", "MCGS组态")
        version = scada.get("mcgs_version", "McgsPro")
        protocol = scada.get("protocol", "PPI")
        vi = self.cmb_version.findText(version)
        if vi >= 0:
            self.cmb_version.setCurrentIndex(vi)
        pi = self.cmb_protocol.findText(protocol)
        if pi >= 0:
            self.cmb_protocol.setCurrentIndex(pi)

        # 变量字典表
        self.tbl_vars.setRowCount(0)
        for v in scada["variables"]:
            row = self.tbl_vars.rowCount()
            self.tbl_vars.insertRow(row)
            rw = "只读" if v.get("read_only") else "读写"
            cells = [v["name"], v.get("var_type", ""), v["address"],
                     v.get("dtype", ""), rw, v.get("comment", "")]
            for col, text in enumerate(cells):
                self.tbl_vars.setItem(row, col, QTableWidgetItem(str(text)))

        # 通道 CSV
        self.txt_csv.setPlainText(scada["device_channels_csv"])

        # 画面设计书 + SVG 源码（QTextBrowser 不直接渲染 SVG，降级为代码块）
        design_md = scada["screen_design"]["design_text"]
        svg = scada["screen_design"]["svg_preview"]
        self.txt_screen.setMarkdown(design_md)
        self.txt_screen.append(
            "\n\n---\n\n**SVG 布局示意（源码，可另存为 .svg 浏览）：**\n\n```svg\n"
            + svg + "\n```"
        )

        # 脚本
        self.txt_script.setPlainText(scada["script"])

        # 自动切到变量字典 Tab
        self.material_tabs.setCurrentIndex(0)

        # 弹窗提示（ScadaPanel 没有状态栏，用消息框通知）
        QMessageBox.information(
            self, "✓ 组态素材已生成",
            f"{name}\n版本：{version} · 协议：{protocol}\n"
            f"变量数：{len(scada['variables'])}\n\n"
            f"请按顺序操作：\n"
            f"  ① 切到「设备通道 CSV」Tab，复制后到 MCGS 设备窗口右键「设备信息导入」\n"
            f"  ② 切到「变量字典」Tab，对照在 MCGS 实时数据库逐条新建数据对象\n"
            f"  ③ 切到「McgsScript 脚本」Tab，复制粘贴到 MCGS 脚本编辑器\n"
            f"  ④ 或点右上角「📦 导出包」一键导出 7 个文件到目录",
        )

    # ---------- 导出 ----------
    def export_package(self):
        if not self._scada:
            QMessageBox.information(self, "提示", "尚未生成 MCGS 组态，无内容可导出。")
            return
        target = QFileDialog.getExistingDirectory(
            self, "选择导出目录", f"{self._scada['name']}_导出包"
        )
        if not target:
            return
        try:
            from app.scada import build_export_package
            files = build_export_package(self._scada, target)
            names = "\n".join(f"- {n}" for n in files)
            QMessageBox.information(
                self, "导出成功",
                f"已导出 {len(files)} 个文件到：\n{target}\n\n{names}\n\n"
                "导入顺序请查阅目录中的 README.txt。",
            )
        except Exception as e:
            QMessageBox.warning(self, "导出失败", str(e))

    def _copy_vars_csv(self):
        if not self._scada:
            return
        import csv, io
        out = io.StringIO()
        out.write("\ufeff")
        writer = csv.writer(out)
        writer.writerow(["变量名", "类型", "地址", "数据类型", "初值", "读写", "说明"])
        for v in self._scada["variables"]:
            rw = "只读" if v.get("read_only") else "读写"
            writer.writerow([v["name"], v.get("var_type", ""), v["address"],
                             v.get("dtype", ""), v.get("init", "0"), rw, v.get("comment", "")])
        QGuiApplication.clipboard().setText(out.getvalue())

    # ---------- 自动写入 MCGS ----------
    def _auto_write_to_mcgs(self):
        """用 UI 自动化把当前 scada 写入 MCGS 工程。

        在后台线程执行，通过信号更新进度。
        """
        if not self._scada:
            QMessageBox.information(self, "提示", "尚未生成 MCGS 组态素材，先点「⚡ 生成组态素材」。")
            return

        from PySide6.QtCore import QThread, Signal

        # 确认对话框
        btn = QMessageBox.question(
            self, "🔄 自动写入 MCGS",
            "将通过 UI 自动化把组态素材写入 MCGS 工程：\n\n"
            "  ① 启动/激活 MCGS 组态环境（不启动 CEEMU 等运行环境）\n"
            "  ② 无打开工程时自动新建工程\n"
            "  ③ 打开设备组态，在子设备属性中自动导入设备通道 CSV\n"
            "  ④ 脚本装入剪贴板，打开脚本编辑器后 Ctrl+V\n\n"
            "⚠ 数据对象（变量）创建无法自动化，需手动在实时数据库新建。\n"
            "⚠ 自动化期间请不要移动鼠标或操作键盘。\n\n"
            "点「是」继续。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if btn != QMessageBox.StandardButton.Yes:
            return

        # 创建后台线程执行
        class WriteWorker(QThread):
            progress = Signal(int, str)
            finished_all = Signal(list)  # List[StepResult]

            def __init__(self, scada, mcgs_path):
                super().__init__()
                self.scada = scada
                self.mcgs_path = mcgs_path

            def run(self):
                try:
                    from app.scada import MCGSAutoWriter
                    writer = MCGSAutoWriter(self.scada, self.mcgs_path)
                    results = writer.run(
                        on_progress=lambda s, m: self.progress.emit(s, m)
                    )
                    self.finished_all.emit(results)
                except Exception as e:
                    # 构造一个失败的结果
                    from app.scada.mcgs_auto_writer import StepResult
                    self.finished_all.emit([StepResult(0, "执行异常", False, message=str(e))])

        self._progress_container.setVisible(True)
        self.progress_bar.setValue(0)
        self.lbl_progress.setText("准备中……")
        self.btn_auto_write.setEnabled(False)

        self._write_worker = WriteWorker(self._scada, self.mcgs_exe_path)
        self._write_worker.progress.connect(self._on_write_progress)
        self._write_worker.finished_all.connect(self._on_write_finished)
        self._write_worker.start()

    def _on_write_progress(self, step: int, message: str):
        self.progress_bar.setValue(step)
        self.lbl_progress.setText(f"步骤 {step}/3：{message}")

    def _on_write_finished(self, results: list):
        self.btn_auto_write.setEnabled(True)
        self.progress_bar.setValue(3)
        self.lbl_progress.setText("自动化完成")

        # 汇总结果
        success_count = sum(1 for r in results if r.success)
        manual_count = sum(1 for r in results if r.manual)
        fail_count = sum(1 for r in results if not r.success and not r.manual)

        lines = ["📋 自动化执行结果：\n"]
        for r in results:
            icon = "✅" if r.success else ("✍️" if r.manual else "❌")
            status = "成功" if r.success else ("需手动" if r.manual else "失败")
            lines.append(f"  {icon} Step {r.step}: {r.name} — {status}")
            if r.message:
                lines.append(f"     {r.message}")

        summary = f"\n成功 {success_count} / 需手动 {manual_count} / 失败 {fail_count}"
        lines.append(summary)

        if fail_count > 0:
            QMessageBox.warning(self, "🔄 自动写入完成（部分失败）", "\n".join(lines))
        elif manual_count > 0:
            QMessageBox.information(self, "🔄 自动写入完成", "\n".join(lines))
        else:
            QMessageBox.information(self, "🔄 自动写入成功", "\n".join(lines))

        # 3 秒后隐藏进度条
        from PySide6.QtCore import QTimer
        QTimer.singleShot(3000, lambda: self._progress_container.setVisible(False))

    def shutdown(self):
        """主窗口关闭时回收检测线程与写入线程（尽力等待，不阻塞过久）。"""
        if self._detect_thread is not None:
            self._detect_thread.quit()
            self._detect_thread.wait(2000)
            if self._detect_worker is not None:
                self._detect_worker.deleteLater()
            self._detect_thread = None
            self._detect_worker = None
        write_worker = getattr(self, "_write_worker", None)
        if write_worker is not None and write_worker.isRunning():
            write_worker.wait(3000)


# ---------------- 设置对话框 ----------------

class ModelEditDialog(QDialog):
    """单个模型条目的添加/编辑对话框。"""

    def __init__(self, parent=None, entry: dict = None):
        super().__init__(parent)
        self.setWindowTitle("编辑模型" if entry else "添加模型")
        entry = entry or {}

        self.cmb_provider = QComboBox()
        self.cmb_provider.addItems(["ollama", "cloud"])
        self.cmb_provider.setCurrentText(entry.get("provider", "ollama"))

        self.cmb_preset = QComboBox()
        self.cmb_preset.addItem("快捷预设", None)
        for name, (url, model) in cloud_presets().items():
            self.cmb_preset.addItem(name, (url, model))

        self.edt_url = QLineEdit(entry.get("base_url", ""))
        self.edt_model = QLineEdit(entry.get("model", ""))
        self.edt_key = QLineEdit(entry.get("api_key", ""))
        self.edt_key.setEchoMode(QLineEdit.EchoMode.Password)

        form = QFormLayout()
        form.addRow("类型：", self.cmb_provider)
        form.addRow("快捷预设：", self.cmb_preset)
        form.addRow("接口地址：", self.edt_url)
        form.addRow("模型名称：", self.edt_model)
        form.addRow("API Key：", self.edt_key)
        hint = QLabel("cloud 类型需填写 API Key；ollama 类型 Key 留空即可。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #6b7280;")

        self.btn_ok = QPushButton("确定")
        self.btn_ok.setProperty("variant", "primary")
        self.btn_cancel = QPushButton("取消")
        btns = QHBoxLayout()
        btns.addStretch(1)
        btns.addWidget(self.btn_ok)
        btns.addWidget(self.btn_cancel)

        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(hint)
        lay.addLayout(btns)
        self.resize(430, 260)

        self.cmb_provider.currentTextChanged.connect(self._on_provider)
        self.cmb_preset.currentIndexChanged.connect(self._apply_preset)
        self.btn_ok.clicked.connect(self._ok)
        self.btn_cancel.clicked.connect(self.reject)
        self._on_provider(self.cmb_provider.currentText())

    def _on_provider(self, provider: str):
        self.cmb_preset.setEnabled(provider == "cloud")

    def _apply_preset(self):
        data = self.cmb_preset.currentData()
        if data:
            self.edt_url.setText(data[0])
            self.edt_model.setText(data[1])

    def _ok(self):
        if not self.edt_model.text().strip():
            QMessageBox.warning(self, "缺少信息", "模型名称不能为空。")
            return
        self.accept()

    def entry(self) -> dict:
        provider = self.cmb_provider.currentText()
        return {
            "provider": provider,
            "base_url": self.edt_url.text().strip(),
            "model": self.edt_model.text().strip(),
            "api_key": self.edt_key.text().strip() if provider == "cloud" else "",
        }


class SettingsDialog(QWidget):
    def __init__(self, config: dict):
        super().__init__()
        self.setWindowTitle("模型设置")
        self.config = config
        llm = config["llm"]
        self._models = [dict(m) for m in llm.get("models", [])]  # 工作副本

        # ---- 多模型智能路由 ----
        self.chk_routing = QCheckBox("启用多模型智能路由（≥2 个模型时按任务自动分配）")
        self.chk_routing.setToolTip(
            "开启后：编程/设计任务自动交给编程能力最强的模型（编程主力），\n"
            "简单问答/查询交给响应最快的模型（快速应答）。\n"
            "请先添加至少两个模型并点击「检测模型能力」。"
        )
        self.chk_routing.setChecked(bool(llm.get("routing")))

        models_box = QGroupBox("模型列表")
        self.tbl_models = QTableWidget(0, 4)
        self.tbl_models.setHorizontalHeaderLabels(["类型", "模型名称", "接口地址", "探测状态"])
        self.tbl_models.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.tbl_models.verticalHeader().setDefaultSectionSize(28)
        self.tbl_models.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.tbl_models.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.tbl_models.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.tbl_models.doubleClicked.connect(self._edit_model)

        self.btn_add = QPushButton("添加")
        self.btn_edit = QPushButton("编辑")
        self.btn_del = QPushButton("删除")
        self.btn_probe = QPushButton("检测模型能力")
        self.btn_probe.setProperty("variant", "primary")
        self.btn_probe.setToolTip(
            "逐个测试连通性、首字延迟、工具调用支持与编程能力；\n"
            "探测结果作为智能路由的角色分配依据。"
        )

        mcol = QVBoxLayout()
        mcol.addWidget(self.btn_add)
        mcol.addWidget(self.btn_edit)
        mcol.addWidget(self.btn_del)
        mcol.addSpacing(8)
        mcol.addWidget(self.btn_probe)
        mcol.addStretch(1)
        mrow = QHBoxLayout()
        mrow.addWidget(self.tbl_models, 1)
        mrow.addLayout(mcol)
        mform = QVBoxLayout(models_box)
        mform.addLayout(mrow)
        self.lbl_probe_hint = QLabel(
            "双击行可编辑；保存前请点「检测模型能力」，否则路由无法区分模型强弱。"
        )
        self.lbl_probe_hint.setWordWrap(True)
        self.lbl_probe_hint.setStyleSheet("color: #6b7280;")
        mform.addWidget(self.lbl_probe_hint)

        # ---- 生成参数 ----
        other_box = QGroupBox("生成参数")
        self.spn_temp = QDoubleSpinBox()
        self.spn_temp.setRange(0.0, 1.0)
        self.spn_temp.setSingleStep(0.1)
        self.spn_temp.setValue(float(llm.get("temperature", 0.3)))
        self.spn_timeout = QSpinBox()
        self.spn_timeout.setRange(10, 600)
        self.spn_timeout.setValue(int(llm.get("timeout", 120)))
        eform = QFormLayout(other_box)
        eform.addRow("温度：", self.spn_temp)
        eform.addRow("超时(秒)：", self.spn_timeout)

        # ---- 连通性测试 ----
        self.btn_test = QPushButton("测试连通性")
        self.btn_test.setToolTip("测试选中的模型；未选中时测试默认配置")
        self.lbl_test_result = QLabel("")
        self.lbl_test_result.setWordWrap(True)
        self.lbl_test_result.setStyleSheet("color: #555;")
        test_row = QHBoxLayout()
        test_row.addWidget(self.btn_test)
        test_row.addWidget(self.lbl_test_result, 1)

        btns = QHBoxLayout()
        btns.addStretch(1)
        self.btn_save = QPushButton("保存")
        self.btn_save.setProperty("variant", "primary")
        self.btn_cancel = QPushButton("取消")
        btns.addWidget(self.btn_save)
        btns.addWidget(self.btn_cancel)

        layout = QVBoxLayout(self)
        layout.addWidget(self.chk_routing)
        layout.addWidget(models_box, 1)
        layout.addWidget(other_box)
        layout.addLayout(test_row)
        layout.addLayout(btns)
        self.resize(660, 640)

        self._test_thread = None
        self._test_worker = None
        self._probe_thread = None
        self._probe_worker = None

        self._fill_models()
        self.btn_add.clicked.connect(self._add_model)
        self.btn_edit.clicked.connect(self._edit_model)
        self.btn_del.clicked.connect(self._del_model)
        self.btn_probe.clicked.connect(self._probe_models)
        self.btn_test.clicked.connect(self.test_now)

    # ---------- 模型列表 ----------
    def _fill_models(self):
        self.tbl_models.setRowCount(0)
        for m in self._models:
            row = self.tbl_models.rowCount()
            self.tbl_models.insertRow(row)
            self.tbl_models.setItem(row, 0, QTableWidgetItem(m.get("provider", "")))
            self.tbl_models.setItem(row, 1, QTableWidgetItem(m.get("model", "")))
            self.tbl_models.setItem(row, 2, QTableWidgetItem(m.get("base_url", "")))
            self.tbl_models.setItem(row, 3, QTableWidgetItem(self._status_text(m)))

    @staticmethod
    def _status_text(m: dict) -> str:
        if not m.get("ok"):
            msg = m.get("message", "")
            return f"✗ {msg}" if msg else "未探测"
        tools = "支持工具" if m.get("tool_support") else "无工具"
        return f"✓ {m.get('latency_ms', 0)}ms · {tools} · 编程{m.get('coding_score', 0)}分"

    def _add_model(self):
        dlg = ModelEditDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._models.append(dlg.entry())
            self._fill_models()

    def _edit_model(self, *_):
        row = self.tbl_models.currentRow()
        if row < 0:
            QMessageBox.information(self, "提示", "请先选中一个模型（双击行也可编辑）。")
            return
        dlg = ModelEditDialog(self, self._models[row])
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._models[row] = dlg.entry()
            self._fill_models()

    def _del_model(self):
        row = self.tbl_models.currentRow()
        if row < 0:
            return
        self._models.pop(row)
        self._fill_models()

    # ---------- 能力探测 ----------
    def _probe_models(self):
        if self._probe_thread is not None:
            return
        if not self._models:
            QMessageBox.information(self, "提示", "请先添加模型。")
            return
        self.btn_probe.setEnabled(False)
        self.lbl_probe_hint.setText(
            f"正在探测 {len(self._models)} 个模型（每个需数秒到一分钟），请稍候……"
        )
        thread = QThread()
        worker = ModelProbeWorker(
            list(enumerate(self._models)), int(self.spn_timeout.value())
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._on_probe_progress)
        worker.probed.connect(self._on_probe_result)
        worker.finished_all.connect(self._on_probe_done)
        thread.start()
        self._probe_thread = thread
        self._probe_worker = worker

    def _on_probe_progress(self, _idx: int, text: str):
        self.lbl_probe_hint.setText(text)

    def _on_probe_result(self, idx: int, result: dict):
        if 0 <= idx < len(self._models) and 0 <= idx < self.tbl_models.rowCount():
            m = self._models[idx]
            for key in ("ok", "latency_ms", "tool_support", "coding_score"):
                m[key] = result.get(key)
            m["message"] = "" if result.get("ok") else result.get("message", "探测失败")
            item = self.tbl_models.item(idx, 3)
            if item is not None:
                item.setText(self._status_text(m))

    def _on_probe_done(self):
        self.lbl_probe_hint.setText(
            "探测完成：编程得分最高者担任「编程主力」，首字延迟最低者担任「快速应答」。"
        )
        self.btn_probe.setEnabled(True)
        if self._probe_thread is not None:
            self._probe_thread.quit()
            self._probe_thread.wait()
            self._probe_worker.deleteLater()
            self._probe_thread = None
            self._probe_worker = None

    # ---------- 连通性测试 ----------
    def _selected_llm_config(self) -> dict:
        """构造 test_connection 所需的旧格式 llm 配置。"""
        row = self.tbl_models.currentRow()
        if 0 <= row < len(self._models):
            m = self._models[row]
            return {
                "provider": m.get("provider", "ollama"),
                "ollama": {"base_url": m.get("base_url", ""), "model": m.get("model", "")},
                "cloud": {
                    "base_url": m.get("base_url", ""),
                    "model": m.get("model", ""),
                    "api_key": m.get("api_key", ""),
                },
            }
        llm = self.config["llm"]
        return {"provider": llm["provider"], "ollama": dict(llm["ollama"]), "cloud": dict(llm["cloud"])}

    def test_now(self):
        if self._test_thread is not None:
            return
        self.btn_test.setEnabled(False)
        self.lbl_test_result.setStyleSheet("color: #555;")
        self.lbl_test_result.setText("正在测试（连通 → 模型 → 推理），请稍候……")

        thread = QThread()
        worker = ConnTestWorker(self._selected_llm_config())
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.done.connect(self._show_test_result)
        thread.start()
        self._test_thread = thread
        self._test_worker = worker

    def _show_test_result(self, result: dict):
        self.lbl_test_result.setStyleSheet(
            "color: #1a7f37;" if result["ok"] else "color: #c62828;"
        )
        self.lbl_test_result.setText(result["message"])
        self.btn_test.setEnabled(True)
        if self._test_thread is not None:
            self._test_thread.quit()
            self._test_thread.wait(5000)
            self._test_worker.deleteLater()
            self._test_thread = None
            self._test_worker = None

    def closeEvent(self, event):
        # 关闭对话框时请求探测停止并回收线程，避免悬挂
        if self._probe_worker is not None:
            self._probe_worker.stop()
        for t_attr, w_attr in (("_test_thread", "_test_worker"),
                               ("_probe_thread", "_probe_worker")):
            thread = getattr(self, t_attr)
            if thread is not None:
                thread.quit()
                thread.wait(3000)
                worker = getattr(self, w_attr)
                if worker is not None:
                    worker.deleteLater()
                setattr(self, t_attr, None)
                setattr(self, w_attr, None)
        super().closeEvent(event)

    def result_config(self) -> dict:
        cfg = self.config
        llm = cfg["llm"]
        llm["routing"] = self.chk_routing.isChecked()
        llm["models"] = [dict(m) for m in self._models]
        llm["temperature"] = self.spn_temp.value()
        llm["timeout"] = self.spn_timeout.value()
        # 同步首个模型到旧单模型字段，保持默认配置与回退链路有效
        if llm["models"]:
            first = llm["models"][0]
            provider = first.get("provider", "ollama")
            llm["provider"] = provider
            patch = {"base_url": first.get("base_url", ""), "model": first.get("model", "")}
            if provider == "cloud":
                patch["api_key"] = first.get("api_key", "")
            llm[provider].update(patch)
        return cfg


# ---------------- I/O 地址库对话框 ----------------

class IOLibraryDialog(QWidget):
    """I/O 地址库编辑器：用户预设元件地址，AI 生成程序/组态时直接采用。"""

    def __init__(self, config: dict):
        super().__init__()
        self.setWindowTitle("I/O 地址库")
        self.config = config

        self.tbl = QTableWidget(0, 3)
        self.tbl.setHorizontalHeaderLabels(["元件/符号", "地址", "备注"])
        self.tbl.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.tbl.verticalHeader().setDefaultSectionSize(30)

        hint = QLabel(
            "预设元件地址后，AI 生成非标程序、脚本与组态时将直接采用这些地址。\n"
            "地址格式：I0.0 / Q0.1 / M2.3 / VW100 / VD200 / T37 / C1 等。"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #6b7280;")

        self.btn_add = QPushButton("添加")
        self.btn_remove = QPushButton("删除选中")
        self.btn_clear = QPushButton("清空")
        self.btn_save = QPushButton("保存")
        self.btn_save.setProperty("variant", "primary")
        self.btn_close = QPushButton("关闭")

        btn_col = QVBoxLayout()
        btn_col.addWidget(self.btn_add)
        btn_col.addWidget(self.btn_remove)
        btn_col.addWidget(self.btn_clear)
        btn_col.addStretch(1)

        left = QHBoxLayout()
        left.addWidget(self.tbl, 1)
        left.addLayout(btn_col)

        btns = QHBoxLayout()
        btns.addStretch(1)
        btns.addWidget(self.btn_save)
        btns.addWidget(self.btn_close)

        lay = QVBoxLayout(self)
        lay.addWidget(hint)
        lay.addLayout(left, 1)
        lay.addLayout(btns)
        self.resize(560, 480)

        self._load()
        self.btn_add.clicked.connect(self._add_row)
        self.btn_remove.clicked.connect(self._remove_selected)
        self.btn_clear.clicked.connect(lambda: self.tbl.setRowCount(0))
        self.btn_save.clicked.connect(self._save)
        self.btn_close.clicked.connect(self.close)

    def _load(self):
        for e in self.config.get("io_library", []):
            self._add_row(e.get("symbol", ""), e.get("address", ""), e.get("comment", ""))

    def _add_row(self, symbol: str = "", address: str = "", comment: str = ""):
        row = self.tbl.rowCount()
        self.tbl.insertRow(row)
        self.tbl.setItem(row, 0, QTableWidgetItem(symbol))
        self.tbl.setItem(row, 1, QTableWidgetItem(address))
        self.tbl.setItem(row, 2, QTableWidgetItem(comment))
        self.tbl.setCurrentCell(row, 0)

    def _remove_selected(self):
        rows = sorted({i.row() for i in self.tbl.selectedIndexes()}, reverse=True)
        for r in rows:
            self.tbl.removeRow(r)

    def _collect(self):
        rows = []
        for row in range(self.tbl.rowCount()):
            def cell(c):
                it = self.tbl.item(row, c)
                return it.text().strip() if it is not None else ""
            symbol, address, comment = cell(0), cell(1), cell(2)
            if symbol or address or comment:
                rows.append((row + 1, symbol, address.upper(), comment))
        return rows

    def _save(self):
        rows = self._collect()
        for lineno, symbol, address, _c in rows:
            if not symbol or not address:
                QMessageBox.warning(
                    self, "保存失败", f"第 {lineno} 行：元件名称与地址都必须填写。"
                )
                return
            try:
                parse_address(address)
            except ValueError as e:
                QMessageBox.warning(
                    self, "地址无效",
                    f"第 {lineno} 行：元件「{symbol}」的地址 {address} 无法识别。\n\n{e}",
                )
                return
        self.config["io_library"] = [
            {"symbol": s, "address": a, "comment": c}
            for _l, s, a, c in rows
        ]
        if save_config(self.config):
            QMessageBox.information(
                self, "已保存",
                f"I/O 地址库已保存（{len(rows)} 项）。\n下一次对话开始即生效。",
            )
        else:
            QMessageBox.warning(self, "保存失败", "配置文件写入失败，请检查磁盘权限。")


# ---------------- 主窗口 ----------------

class MainWindow(QMainWindow):
    # 程序就绪信号：工具在工作线程产出程序后，经队列连接回到 GUI 线程刷新
    program_ready = Signal(dict)
    # MCGS 组态就绪信号
    scada_ready = Signal(dict)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("STEP 7 AI Agent  |  MCGS 组态助手")
        self.resize(1280, 820)

        self.config = load_config()
        self.executor = ToolExecutor()
        self.executor.config = self.config
        self.assistant = Assistant(self.config, self.executor)

        self.chat = ChatPanel()
        self.plc_panel = PlcPanel(self.executor)
        self.code_panel = CodePanel()
        self.scada_panel = ScadaPanel(self.executor, self.config)
        self.plc_panel.sync_from_config(self.config)
        self.executor.allow_write = self.config["plc"].get("allow_write", False)

        # ---- 顶层 Tab：STEP 7 AI Agent 与 MCGS 组态助手同级 ----
        self.top_tabs = QTabWidget()

        # Tab 0：STEP 7 AI Agent（对话 + PLC/程序面板）
        step7_tabs = QTabWidget()
        plc_scroll = QScrollArea()
        plc_scroll.setWidgetResizable(True)
        plc_scroll.setWidget(self.plc_panel)
        step7_tabs.addTab(plc_scroll, "PLC")
        step7_tabs.addTab(self.code_panel, "程序")
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self.chat)
        splitter.addWidget(step7_tabs)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        self.top_tabs.addTab(splitter, "STEP 7 AI Agent")

        # Tab 1：MCGS 组态助手（独立面板）
        self.top_tabs.addTab(self.scada_panel, "MCGS 组态助手")

        # Tab 2：运行日志（应用内实时查看）
        self._build_log_tab()
        self.top_tabs.addTab(self._log_tab_widget, "📋 运行日志")

        self.setCentralWidget(self.top_tabs)

        # ---- 注册日志信号桥（让所有 logging 记录实时流入 UI） ----
        try:
            from app.core.logger import QtLogEmitter, set_log_emitter, tail_log
            self._log_emitter = QtLogEmitter()
            set_log_emitter(self._log_emitter)
            self._log_emitter.log_record.connect(self._on_log_record)
            # 先加载历史
            for line in tail_log(200):
                self.txt_log.append(line.rstrip())
        except Exception:
            pass

        # ---- 跨线程写确认 ----
        self._confirm_bridge = ConfirmBridge()
        self.executor.confirm_write = self._confirm_write
        self.executor.on_connection_changed = self._on_connection_changed
        # 程序刷新统一走信号
        self.program_ready.connect(self.code_panel.show_program)
        self.executor.on_program_generated = self.program_ready.emit
        # MCGS 组态刷新走信号 → ScadaPanel
        self.scada_ready.connect(self.scada_panel.show_scada)
        self.executor.on_scada_generated = self.scada_ready.emit

        # ---- 信号连接 ----
        self.chat.btn_send.clicked.connect(self._send_message)
        self.plc_panel.btn_mock.clicked.connect(self._toggle_mock)
        self.plc_panel.btn_connect.clicked.connect(self._toggle_connect)
        self.plc_panel.cmb_profile.currentIndexChanged.connect(
            self.plc_panel._fill_profile_defaults
        )
        self.plc_panel.btn_add.clicked.connect(lambda: self.plc_panel.add_tag())
        self.plc_panel.btn_remove.clicked.connect(self.plc_panel.remove_selected)
        self.plc_panel.tags_changed.connect(self._sync_poll_addresses)
        self.plc_panel.btn_write.clicked.connect(self._write_selected)
        self.plc_panel.chk_auto.toggled.connect(self._toggle_auto_poll)
        self.plc_panel.chk_write.toggled.connect(self._toggle_allow_write)
        self.code_panel.btn_copy.clicked.connect(self.code_panel.copy_code)
        self.code_panel.btn_export.clicked.connect(self.code_panel.export_file)
        self.code_panel.btn_awl.clicked.connect(self.code_panel.export_awl)
        self.code_panel.btn_png.clicked.connect(self.code_panel.export_ladder_png)

        # 工具栏：模式切换
        toolbar = self.addToolBar("主工具栏")
        toolbar.addWidget(QLabel("模式： "))
        self.cmb_mode = QComboBox()
        self.cmb_mode.addItem("工程模式", "engineering")
        self.cmb_mode.addItem("学习模式", "learning")
        if self.config.get("mode") == "learning":
            self.cmb_mode.setCurrentIndex(1)
        self.cmb_mode.currentIndexChanged.connect(self._toggle_mode)
        toolbar.addWidget(self.cmb_mode)
        toolbar.addSeparator()
        act_test = toolbar.addAction("测试连接")
        act_test.triggered.connect(self._test_current_connection)
        act_settings = toolbar.addAction("模型设置")
        act_settings.triggered.connect(self._open_settings)
        act_io = toolbar.addAction("I/O 地址库")
        act_io.setToolTip("预设元件地址，AI 生成程序/组态时直接采用")
        act_io.triggered.connect(self._open_io_library)
        act_clear = toolbar.addAction("清空对话")
        act_clear.triggered.connect(self._clear_chat)
        act_scada = toolbar.addAction("MCGS 组态")
        act_scada.triggered.connect(self._open_scada_dialog)
        act_about = toolbar.addAction("关于")
        act_about.triggered.connect(self._show_about)

        self._thread = None
        self._worker = None
        self._conn_thread = None
        self._conn_worker = None
        self._act_thread = None
        self._act_worker = None
        self._poll_thread = None
        self._poll_worker = None
        self._settings_dialog = None

        self.plc_panel.refresh_status()
        self._welcome()

    # ---------- 欢迎语 ----------
    def _welcome(self):
        self.chat.add_message(
            "assistant",
            "你好！我是 **STEP 7 AI 助手**，可以帮你：\n\n"
            "1. 设计与编写西门子 PLC 程序（S7-200 SMART / S7-1200/1500 等）；\n"
            "2. 连接 PLC 读写变量、分析状态；\n"
            "3. 对不明确的需求向你提问、给出提示与建议。\n\n"
            "**快速开始**：你现在没有硬件也没关系，在右侧 **PLC 面板点击“启动模拟PLC”**，"
            "然后就可以对我说：\n\n"
            "- “用模板生成一个星三角降压启动程序”\n"
            "- “读取 VW100 的产量值”\n"
            "- “电机正反转怎么设计？”（可切换顶部的**学习模式**让我引导你思考）\n\n"
            "💡 也可以在工具栏 **「I/O 地址库」** 中预设元件地址"
            "（如 启动按钮 = I0.0），我生成程序与组态时会直接采用这些地址。",
        )

    # ---------- 模型连通性测试（工具栏，测试已生效配置） ----------
    def _test_current_connection(self):
        if self._conn_thread is not None:
            return
        self.statusBar().showMessage("正在测试模型连接……")

        thread = QThread()
        worker = ConnTestWorker(self.config["llm"])
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.done.connect(self._on_conn_test_done)
        thread.start()
        self._conn_thread = thread
        self._conn_worker = worker

    def _on_conn_test_done(self, result: dict):
        self.statusBar().showMessage(result["message"], 8000)
        if result["ok"]:
            QMessageBox.information(self, "模型连接正常", result["message"])
        else:
            QMessageBox.warning(self, "模型连接失败", result["message"])
        if self._conn_thread is not None:
            self._conn_thread.quit()
            self._conn_thread.wait(5000)
            self._conn_worker.deleteLater()
            self._conn_thread = None
            self._conn_worker = None

    # ---------- 发送消息 ----------
    def _send_message(self):
        text = self.chat.current_input()
        image_path = self.chat.current_image()
        if not text and not image_path or self._thread is not None:
            return
        self.chat.clear_input()
        # 用户消息：文本 + 可能的图片缩略图
        user_msg = text
        if image_path:
            user_msg = f"{text}\n\n[已附加组态参考图]" if text else "[已附加组态参考图]"
        self.chat.add_message("user", user_msg)
        self.chat.begin_stream()
        self.chat.set_busy(True)
        self.statusBar().showMessage("正在思考……")

        self._thread = QThread()
        self._worker = ChatWorker(self.assistant, text, image_path)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.delta.connect(self.chat.append_stream)
        self._worker.status.connect(lambda s: self.statusBar().showMessage(s))
        self._worker.tool.connect(self._on_tool_event)
        self._worker.error.connect(self._on_error)
        self._worker.program.connect(self.code_panel.show_program)
        self._worker.scada.connect(self.scada_panel.show_scada)
        self._worker.finished.connect(self._on_finished)
        self._thread.start()

    def _on_finished(self):
        self.chat.end_stream()
        self.chat.set_busy(False)
        self.statusBar().clearMessage()
        self.plc_panel.refresh_status()
        if self._thread:
            self._thread.quit()
            self._thread.wait(5000)
        self._thread = None
        self._worker = None

    def _on_error(self, text: str):
        self.chat.append_stream(f"\n\n> ⚠️ **错误：** {text}")

    def _on_tool_event(self, name: str, args: str):
        self.statusBar().showMessage(f"调用工具 {name} {args}")

    # ---------- 连接回调 ----------
    def _on_connection_changed(self):
        self.plc_panel.refresh_status()
        if not self.executor.connected:
            self.plc_panel.mark_all("—")

    # ---------- 通用后台动作 ----------
    def _run_action(self, fn, on_done=None) -> bool:
        if self._act_thread is not None:
            QMessageBox.information(self, "提示", "上一个后台操作尚未完成，请稍候。")
            return False
        thread = QThread()
        worker = ActionWorker(fn)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.done.connect(lambda r: self._on_action_done(r, on_done))
        worker.failed.connect(lambda r: self.statusBar().showMessage(f"操作失败：{r}", 5000))
        worker.finished.connect(self._cleanup_action)
        thread.start()
        self._act_thread = thread
        self._act_worker = worker
        return True

    def _on_action_done(self, result: str, on_done):
        if on_done is not None:
            on_done(result)

    def _cleanup_action(self):
        thread, worker = self._act_thread, self._act_worker
        self._act_thread = None
        self._act_worker = None
        if thread is not None:
            thread.quit()
            thread.wait(5000)
            worker.deleteLater()

    # ---------- PLC 面板动作 ----------
    def _toggle_mock(self):
        if self.executor.mock.running:
            # 若正连着模拟PLC先断开
            if self.executor.connected and self.executor.is_mock_connection:
                self.executor.driver.disconnect()
            self.executor.mock.stop()
            self.plc_panel.mark_all("—")
        else:
            try:
                self.executor.mock.start()
            except Exception as e:
                QMessageBox.critical(self, "错误", str(e))
        self.plc_panel.refresh_status()

    def _toggle_connect(self):
        if self.executor.connected:
            # 断开为本地调用，直接执行
            self.executor.driver.disconnect()
            self.plc_panel.refresh_status()
            self.plc_panel.mark_all("—")
            return
        panel = self.plc_panel
        panel.btn_connect.setEnabled(False)
        use_mock = self.executor.mock.running

        def job():
            if use_mock:
                return self.executor.connect_plc(use_mock=True)
            return self.executor.connect_plc(
                use_mock=False,
                profile=panel.cmb_profile.currentText(),
                host=panel.edt_host.text().strip(),
                rack=panel.spn_rack.value(),
                slot=panel.spn_slot.value(),
            )

        def done(msg):
            panel.btn_connect.setEnabled(True)
            self.statusBar().showMessage(msg, 5000)
            self.plc_panel.refresh_status()
            self._persist_plc_config()

        if not self._run_action(job, done):
            panel.btn_connect.setEnabled(True)

    def _write_selected(self):
        address = self.plc_panel.selected_address()
        value = self.plc_panel.edt_write_value.text().strip()
        if not address or value == "":
            QMessageBox.information(self, "提示", "请选中一行并填写写入值。")
            return
        panel = self.plc_panel
        panel.btn_write.setEnabled(False)

        def done(msg):
            panel.btn_write.setEnabled(True)
            self.statusBar().showMessage(msg, 5000)

        if not self._run_action(lambda: self.executor.write_tag(address, value), done):
            panel.btn_write.setEnabled(True)

    # ---------- 变量监控后台轮询 ----------
    def _toggle_auto_poll(self, checked: bool):
        if checked:
            self._start_poll()
        else:
            self._stop_poll()

    def _start_poll(self):
        if self._poll_thread is not None:
            return

        def reader(addr):
            if not self.executor.connected:
                return "—"
            return self.executor.driver.read(addr)

        thread = QThread()
        worker = PollWorker(reader, self.plc_panel.watch_addresses(), 1000)
        worker.moveToThread(thread)
        thread.started.connect(worker.start)
        worker.values.connect(self.plc_panel.apply_values)
        thread.start()
        self._poll_thread = thread
        self._poll_worker = worker

    def _stop_poll(self):
        thread, worker = self._poll_thread, self._poll_worker
        self._poll_thread = None
        self._poll_worker = None
        if worker is not None:
            QMetaObject.invokeMethod(worker, "stop", Qt.ConnectionType.QueuedConnection)
        if thread is not None:
            thread.quit()
            thread.wait(3000)
            worker.deleteLater()

    def _sync_poll_addresses(self):
        if self._poll_worker is not None:
            self._poll_worker.update_addresses(self.plc_panel.watch_addresses())

    def _toggle_allow_write(self, checked: bool):
        self.executor.allow_write = checked
        self.config["plc"]["allow_write"] = checked
        save_config(self.config)

    # ---------- 模式 / 设置 ----------
    def _toggle_mode(self, _index: int):
        mode = self.cmb_mode.currentData()
        self.config["mode"] = mode
        save_config(self.config)

    def _open_settings(self):
        dlg = SettingsDialog(self.config)
        dlg.btn_cancel.clicked.connect(dlg.close)

        def _save():
            self.config = dlg.result_config()
            save_config(self.config)
            self.assistant.update_config(self.config)
            self._persist_plc_config()
            dlg.close()

        dlg.btn_save.clicked.connect(_save)
        dlg.setWindowModality(Qt.WindowModality.ApplicationModal)
        dlg.show()
        # 持有引用，防止对话框被垃圾回收
        self._settings_dialog = dlg

    def _open_io_library(self):
        dlg = IOLibraryDialog(self.config)
        dlg.setWindowModality(Qt.WindowModality.ApplicationModal)
        dlg.show()
        # 持有引用，防止对话框被垃圾回收
        self._io_library_dialog = dlg

    def _clear_chat(self):
        self.chat.clear_all()
        self.assistant.reset()
        self._welcome()

    def _show_about(self):
        QMessageBox.information(
            self,
            "关于",
            "STEP 7 AI Agent\n\n"
            "西门子 S7 系列 PLC 的 AI 编程与通信助手\n"
            "支持 S7-200 SMART / S7-1200/1500 / S7-300/400\n"
            "模型后端：Ollama 本地 / OpenAI 兼容云端\n\n"
            "MCGS 组态设计助手：派生变量字典/通道CSV/画面设计书/McgsScript脚本\n"
            "支持 嵌入版 + 通用版，协议 PPI / Modbus / OPC\n\n"
            "安全提示：真机写入须人工确认，下载前请先编译与仿真。",
        )

    # ---------- MCGS 组态独立入口 ----------
    def _open_scada_dialog(self):
        """弹工艺描述输入对话框 + 版本/协议下拉，确认后直接调工具生成组态。"""
        from PySide6.QtWidgets import QDialog, QDialogButtonBox
        dlg = QDialog(self)
        dlg.setWindowTitle("MCGS 组态设计助手")
        dlg.setMinimumWidth(480)
        form = QFormLayout()
        edt_craft = QPlainTextEdit()
        edt_craft.setPlaceholderText(
            "请描述工艺需求，如：\n"
            "电机启停控制，带故障指示与运行状态\n"
            "传送带计数，累计产量保存\n"
            "也可直接写明地址（如 I0.0 启动 / Q0.0 运行 / VW100 设定值）"
        )
        edt_craft.setFixedHeight(120)
        cmb_v = QComboBox()
        cmb_v.addItems(["McgsPro", "嵌入版", "通用版"])
        cmb_v.setCurrentText(self.scada_panel.cmb_version.currentText())
        cmb_p = QComboBox()
        cmb_p.addItems(["PPI", "Modbus", "OPC"])
        cmb_p.setCurrentText(self.scada_panel.cmb_protocol.currentText())
        form.addRow("工艺描述：", edt_craft)
        form.addRow("MCGS 版本：", cmb_v)
        form.addRow("协议：", cmb_p)
        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        lay = QVBoxLayout(dlg)
        lay.addLayout(form)
        lay.addWidget(btns)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        craft = edt_craft.toPlainText().strip()
        if not craft:
            QMessageBox.information(self, "提示", "请填写工艺描述。")
            return
        version = cmb_v.currentText()
        protocol = cmb_p.currentText()
        # 同步到 scada_panel 下拉
        vi = self.scada_panel.cmb_version.findText(version)
        if vi >= 0:
            self.scada_panel.cmb_version.setCurrentIndex(vi)
        pi = self.scada_panel.cmb_protocol.findText(protocol)
        if pi >= 0:
            self.scada_panel.cmb_protocol.setCurrentIndex(pi)
        # 持久化到 config
        self.config.setdefault("mcgs", {})
        self.config["mcgs"]["version"] = version
        self.config["mcgs"]["protocol"] = protocol
        save_config(self.config)
        # 直接调工具（同步执行，完成后通过回调走 scada_ready 信号刷新 GUI）
        self.statusBar().showMessage(f"正在生成 MCGS 组态（{version}·{protocol}）……", 3000)
        try:
            text = self.executor.generate_scada(
                craft_desc=craft,
                mcgs_version=version,
                protocol=protocol,
            )
        except Exception as e:
            QMessageBox.warning(self, "生成失败", str(e))
            return
        # 切到 MCGS 组态助手 tab
        self.top_tabs.setCurrentIndex(1)
        # 把工具返回的 markdown 文本塞进对话区
        self.chat.add_message("user", f"（MCGS 组态）{craft}（版本：{version}，协议：{protocol}）")
        self.chat.add_message("assistant", text)
        self.statusBar().showMessage("MCGS 组态已生成，请在「MCGS 组态助手」面板查看。", 5000)

    # ---------- 配置持久化 ----------
    def _persist_plc_config(self):
        plc = self.config["plc"]
        plc["profile"] = self.plc_panel.cmb_profile.currentText()
        plc["host"] = self.plc_panel.edt_host.text().strip()
        plc["rack"] = self.plc_panel.spn_rack.value()
        plc["slot"] = self.plc_panel.spn_slot.value()
        save_config(self.config)

    # ---------- 写确认（工作线程调用，阻塞到GUI回复） ----------
    def _confirm_write(self, address: str, value) -> bool:
        return self._confirm_bridge.ask(address, str(value))

    # ---------- 关闭清理 ----------
    # ---------- 运行日志 Tab ----------
    def _build_log_tab(self):
        """构建应用内日志查看器 Tab。"""
        self._log_tab_widget = QWidget()

        # 日志文本框（带颜色高亮）
        self.txt_log = QPlainTextEdit()
        self.txt_log.setReadOnly(True)
        log_font = self.txt_log.font()
        log_font.setFamily("Consolas, Courier New")
        self.txt_log.setFont(log_font)

        # 过滤控件
        self.chk_show_debug = QCheckBox("DEBUG")
        self.chk_show_debug.setChecked(False)
        self.chk_show_info = QCheckBox("INFO")
        self.chk_show_info.setChecked(True)
        self.chk_show_warn = QCheckBox("WARNING")
        self.chk_show_warn.setChecked(True)
        self.chk_show_error = QCheckBox("ERROR")
        self.chk_show_error.setChecked(True)
        self.chk_auto_scroll = QCheckBox("自动滚动")
        self.chk_auto_scroll.setChecked(True)
        self.chk_show_debug.toggled.connect(lambda: self._apply_log_filter())
        self.chk_show_info.toggled.connect(lambda: self._apply_log_filter())
        self.chk_show_warn.toggled.connect(lambda: self._apply_log_filter())
        self.chk_show_error.toggled.connect(lambda: self._apply_log_filter())

        btn_clear = QPushButton("清空")
        btn_clear.clicked.connect(lambda: self.txt_log.clear())
        btn_copy = QPushButton("复制全部")
        btn_copy.clicked.connect(
            lambda: QGuiApplication.clipboard().setText(self.txt_log.toPlainText())
        )
        btn_open_file = QPushButton("打开日志文件")
        btn_open_file.clicked.connect(self._open_log_file)

        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("显示级别："))
        filter_row.addWidget(self.chk_show_debug)
        filter_row.addWidget(self.chk_show_info)
        filter_row.addWidget(self.chk_show_warn)
        filter_row.addWidget(self.chk_show_error)
        filter_row.addStretch(1)
        filter_row.addWidget(self.chk_auto_scroll)
        filter_row.addWidget(btn_clear)
        filter_row.addWidget(btn_copy)
        filter_row.addWidget(btn_open_file)

        lay = QVBoxLayout(self._log_tab_widget)
        lay.addLayout(filter_row)
        lay.addWidget(self.txt_log, 1)

        # 缓存所有日志记录（用于过滤）
        self._all_log_records: list = []

    def _on_log_record(self, level: int, time_str: str, logger: str, message: str):
        """Qt 信号槽：接收一条 logging 记录并追加到 UI。"""
        import logging
        self._all_log_records.append((level, time_str, logger, message))
        self._append_log_line(level, time_str, logger, message)

    def _append_log_line(self, level: int, time_str: str, logger: str, message: str):
        """追加一行带颜色的日志。"""
        # 级别过滤
        import logging
        if level == logging.DEBUG and not self.chk_show_debug.isChecked():
            return
        if level == logging.INFO and not self.chk_show_info.isChecked():
            return
        if level == logging.WARNING and not self.chk_show_warn.isChecked():
            return
        if level >= logging.ERROR and not self.chk_show_error.isChecked():
            return

        # 颜色
        color_map = {
            logging.DEBUG: QColor(128, 128, 128),   # 灰色
            logging.INFO: QColor(30, 30, 30),       # 黑色
            logging.WARNING: QColor(200, 120, 0),   # 橙色
            logging.ERROR: QColor(200, 0, 0),       # 红色
            logging.CRITICAL: QColor(150, 0, 150),  # 紫色
        }
        color = color_map.get(level, QColor(0, 0, 0))

        # 级别名
        name_map = {
            logging.DEBUG: "DEBUG",
            logging.INFO: "INFO",
            logging.WARNING: "WARN",
            logging.ERROR: "ERROR",
            logging.CRITICAL: "CRIT",
        }
        level_name = name_map.get(level, str(level))

        line = f"[{time_str}] [{level_name}] [{logger}] {message}"

        # 带 color 的追加
        fmt = QTextCharFormat()
        fmt.setForeground(color)
        self.txt_log.moveCursor(QTextCursor.MoveOperation.End)
        self.txt_log.setCurrentCharFormat(fmt)
        self.txt_log.insertPlainText(line + "\n")
        self.txt_log.setCurrentCharFormat(QTextCharFormat())  # 恢复默认

        # 自动滚动
        if self.chk_auto_scroll.isChecked():
            self.txt_log.moveCursor(QTextCursor.MoveOperation.End)

    def _apply_log_filter(self):
        """重新应用过滤：清空后按过滤条件重绘所有缓存记录。"""
        self.txt_log.clear()
        for rec in self._all_log_records:
            self._append_log_line(*rec)

    def _open_log_file(self):
        """用系统默认程序打开 app.log。"""
        try:
            from app.core.logger import _log_file
            import os, subprocess
            path = _log_file()
            if os.path.isfile(path):
                os.startfile(path)
            else:
                QMessageBox.information(self, "日志文件不存在",
                                        f"日志文件尚未生成：{path}")
        except Exception as e:
            QMessageBox.warning(self, "打开失败", str(e))

    def closeEvent(self, event):
        # 先隐藏窗口，避免回收期间用户再触发动作
        self.hide()

        # 1) 停止变量监控轮询
        self._stop_poll()

        # 2) 回收 ScadaPanel 检测/写入线程
        try:
            self.scada_panel.shutdown()
        except Exception:
            pass

        # 3) 回收通用动作线程
        if self._act_thread is not None:
            self._act_thread.quit()
            self._act_thread.wait(3000)
            if self._act_worker is not None:
                self._act_worker.deleteLater()
            self._act_thread = None
            self._act_worker = None

        # 4) 回收模型连通性测试线程
        if self._conn_thread is not None:
            self._conn_thread.quit()
            self._conn_thread.wait(3000)
            if self._conn_worker is not None:
                self._conn_worker.deleteLater()
            self._conn_thread = None
            self._conn_worker = None

        # 5) 回收对话线程（可能正处在 LLM 调用中，尽力等待；仍未结束则交由进程退出回收）
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(3000)
            if self._worker is not None:
                self._worker.deleteLater()
            self._thread = None
            self._worker = None

        # 6) 断开 PLC、停止模拟服务
        try:
            self.executor.driver.disconnect()
            self.executor.mock.stop()
        finally:
            event.accept()
