# -*- coding: utf-8 -*-
"""后台线程对象：避免 LLM 调用、工具执行、轮询、检测等阻塞 Qt 界面。"""
from PySide6.QtCore import QObject, QTimer, Slot, Signal

from app.agent.connectivity import test_connection


class ChatWorker(QObject):
    delta = Signal(str)
    status = Signal(str)
    tool = Signal(str, str)
    error = Signal(str)
    program = Signal(dict)
    scada = Signal(dict)
    finished = Signal()

    def __init__(self, assistant, text: str, image_path: str = None):
        super().__init__()
        self._assistant = assistant
        self._text = text
        self._image_path = image_path

    @Slot()
    def run(self):
        def event(kind, *args):
            if kind == "delta":
                self.delta.emit(args[0])
            elif kind == "status":
                self.status.emit(args[0])
            elif kind == "tool":
                self.tool.emit(args[0], args[1])
            elif kind == "error":
                self.error.emit(args[0])
            elif kind == "program":
                self.program.emit(args[0])
            elif kind == "scada":
                self.scada.emit(args[0])

        try:
            self._assistant.chat(self._text, event, image_path=self._image_path)
        except Exception as e:  # 兜底，避免线程静默崩溃
            self.error.emit(f"对话处理异常：{e}")
        finally:
            self.finished.emit()


class ConnTestWorker(QObject):
    """模型连通性测试 worker：done 携带结构化结果 dict。"""

    done = Signal(dict)

    def __init__(self, llm_config: dict):
        super().__init__()
        self._llm = llm_config

    @Slot()
    def run(self):
        try:
            result = test_connection(self._llm)
        except Exception as e:
            result = {"ok": False, "stage": "service", "latency_ms": 0,
                      "message": f"测试异常：{e}"}
        self.done.emit(result)


class PollWorker(QObject):
    """周期性后台读取（定时器运行在 worker 线程），避免 GUI 线程被网络往返卡住。

    reader: callable(addr) -> 值；addresses: [(行号, 地址), ...]
    """

    values = Signal(object)  # [(行号, 显示文本), ...]

    def __init__(self, reader, addresses, interval_ms: int):
        super().__init__()
        self._read = reader
        self._addresses = list(addresses)
        self._interval = max(int(interval_ms), 200)
        self._timer = None
        self._running = False

    @Slot()
    def start(self):
        if self._timer is None:
            self._timer = QTimer()
            self._timer.timeout.connect(self._poll)
        self._running = True
        self._timer.start(self._interval)
        self._poll()

    @Slot()
    def stop(self):
        self._running = False
        if self._timer is not None:
            self._timer.stop()

    @Slot(object)
    def update_addresses(self, addresses):
        self._addresses = list(addresses)

    def _poll(self):
        if not self._running:
            return
        results = []
        for row, addr in self._addresses:
            try:
                results.append((row, str(self._read(addr))))
            except Exception as e:
                results.append((row, f"读取失败：{e}"))
        self.values.emit(results)


class ActionWorker(QObject):
    """一次性后台动作：执行无参 callable，结果经信号回 GUI 线程。"""

    done = Signal(str)
    failed = Signal(str)
    finished = Signal()

    def __init__(self, fn):
        super().__init__()
        self._fn = fn

    @Slot()
    def run(self):
        try:
            result = self._fn()
            self.done.emit("" if result is None else str(result))
        except Exception as e:
            self.failed.emit(str(e))
        finally:
            self.finished.emit()


class McgsDetectWorker(QObject):
    """后台检测 MCGS 安装：detected(版本, 路径)，未找到时均为空串。"""

    detected = Signal(str, str)

    @Slot()
    def run(self):
        ver, path = "", ""
        try:
            from app.scada.mcgs_knowledge import find_mcgs_installed
            v, p = find_mcgs_installed()
            ver, path = v or "", p or ""
        except Exception:
            pass
        self.detected.emit(ver, path)


class ModelProbeWorker(QObject):
    """逐个探测模型能力（连通/延迟/工具调用/编程测验），结果经信号回 GUI 线程。

    entries: 探测条目列表 [(序号, 模型条目dict), ...]
    """

    progress = Signal(int, str)      # (序号, 状态文本)
    probed = Signal(int, dict)       # (序号, 探测结果)
    finished_all = Signal()

    def __init__(self, entries, timeout: int = 60):
        super().__init__()
        self._entries = list(entries)
        self._timeout = timeout
        self._stop = False

    def stop(self):
        """请求停止探测（对话框关闭/重探时调用），当前条目完成后即退出。"""
        self._stop = True

    @Slot()
    def run(self):
        from app.agent.probe import probe_model
        for idx, entry in self._entries:
            if self._stop:
                break
            self.progress.emit(idx, f"正在探测 {entry.get('model', '')}……")
            try:
                result = probe_model(entry, self._timeout)
            except Exception as e:
                result = {"ok": False, "latency_ms": 0, "tool_support": False,
                          "coding_score": 0, "message": f"探测异常：{e}"}
            if self._stop:
                break
            self.probed.emit(idx, result)
        self.finished_all.emit()
