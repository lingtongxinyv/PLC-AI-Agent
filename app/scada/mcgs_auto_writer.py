# -*- coding: utf-8 -*-
"""MCGS UI 自动化写入（零第三方依赖版）。

利用 MCGS 组态环境（McgsSetPro.exe / McgsSetE.exe，均为 MFC 标准 Win32 控件：
SysTabControl32 / SysTreeView32 / SysListView32 / Button / Menu）的特性，
通过 ctypes 直接调用 Win32 API 模拟用户操作，将 AI 生成的组态素材写入 MCGS 工程。

支持的自动化步骤：
0. 启动/激活组态环境主窗口（检测不到时自动启动组态程序，绝不启动 CEEMU 等运行环境）
   无打开工程时自动新建工程（文件>新建工程 → 新建工程设置对话框 → 确定）
1. 自动导入设备通道 CSV（工作台>设备窗口>设备组态>子设备属性>设备信息导入）
2. McgsScript 自动装入剪贴板，引导用户在脚本编辑器中粘贴
3. 数据对象（变量）创建 — MCGS 无批量接口，无法自动化，引导手动完成

设计原则：
- 每步独立 try/except，失败时优雅回退并给出手动操作指引，不阻塞后续步骤
- 跨进程消息一律使用 SendMessageTimeoutW（目标窗口挂起可超时返回，不永久阻塞）
- 切换标签等可能引起状态变化的操作只用物理鼠标/键盘，禁止伪造控件通知
- 通过回调 on_progress(step, message) 报告进度，返回 StepResult 列表
- 仅依赖 Python 标准库 ctypes，任何干净 Windows 电脑均可运行
"""
import ctypes
import os
import subprocess
import time
from ctypes import wintypes
from dataclasses import dataclass
from typing import Callable, Optional

from .mcgs_knowledge import MCGS_CONFIG_EXES


# ================= Win32 绑定 =================

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
shell32 = ctypes.windll.shell32

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

# 消息 / 标志常量
WM_COMMAND = 0x0111
WM_SETTEXT = 0x000C
BM_CLICK = 0x00F5
MN_GETHMENU = 0x01E1
GW_OWNER = 4
SW_RESTORE = 9
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
KEYEVENTF_KEYUP = 0x0002
VK_CONTROL = 0x11
VK_RETURN = 0x0D
VK_MENU = 0x12          # Alt
VK_DOWN = 0x28
VK_ESCAPE = 0x1B

# SendMessageTimeout
SMTO_ABORTIFHUNG = 0x0002

# Tab / ComboBox 控件消息
TCM_GETITEMCOUNT = 0x1304
CB_GETCURSEL = 0x0147
CB_SETCURSEL = 0x014E

# TreeView 消息
TVM_GETNEXTITEM = 0x110A
TVM_GETITEMRECT = 0x1104
TVGN_ROOT = 0x0
TVGN_NEXT = 0x1
TVGN_CHILD = 0x4

# 对话框常用命令 ID
IDOK = 1
IDCANCEL = 2
IDYES = 6
IDNO = 7

# 协议 → 驱动关键词（驱动 DLL 路径 / 驱动构件名称小写匹配）
_PROTOCOL_DRIVER_HINTS = {
    "PPI": ("ppi", "s7-200", "s7200", "s7_200", "s7200ppi"),
    "Modbus": ("modbus",),
    "OPC": ("opc",),
}

# 导入失败提示关键词：含这些词的结果框保留不关（程序化关闭会致 MCGS 崩溃），
# 仅读取文本并报告，由用户用真实鼠标点击确定。
_IMPORT_FAILURE_KEYS = ("失败", "不能", "错误", "不符合", "没有找到", "无法")

# Toolhelp32 进程快照
TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value

MIIM_ID = 0x00000002
MIIM_SUBMENU = 0x00000004
MIIM_STRING = 0x00000040
MIIM_FTYPE = 0x00000100


class MENUITEMINFOW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("fMask", wintypes.UINT),
        ("fType", wintypes.UINT),
        ("fState", wintypes.UINT),
        ("wID", wintypes.UINT),
        ("hSubMenu", wintypes.HMENU),
        ("hbmpChecked", wintypes.HBITMAP),
        ("hbmpUnchecked", wintypes.HBITMAP),
        ("dwItemData", ctypes.c_size_t),
        ("dwTypeData", wintypes.LPWSTR),
        ("cch", wintypes.UINT),
        ("hbmpItem", wintypes.HBITMAP),
    ]


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


def _bind_win32():
    """设置原型，避免 64 位下句柄被截断为 int。"""
    u = user32
    u.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
    u.EnumWindows.restype = wintypes.BOOL
    u.EnumChildWindows.argtypes = [wintypes.HWND, WNDENUMPROC, wintypes.LPARAM]
    u.EnumChildWindows.restype = wintypes.BOOL
    u.IsWindowVisible.argtypes = [wintypes.HWND]
    u.IsWindow.argtypes = [wintypes.HWND]
    u.IsIconic.argtypes = [wintypes.HWND]
    u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    u.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    u.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    u.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    u.GetWindow.argtypes = [wintypes.HWND, ctypes.c_uint]
    u.GetWindow.restype = wintypes.HWND
    u.GetMenu.argtypes = [wintypes.HWND]
    u.GetMenu.restype = wintypes.HMENU
    u.GetSubMenu.argtypes = [wintypes.HMENU, ctypes.c_int]
    u.GetSubMenu.restype = wintypes.HMENU
    u.GetMenuItemCount.argtypes = [wintypes.HMENU]
    u.GetMenuItemInfoW.argtypes = [wintypes.HMENU, wintypes.UINT, wintypes.BOOL,
                                   ctypes.POINTER(MENUITEMINFOW)]
    u.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    u.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    u.SendMessageW.restype = wintypes.LPARAM
    u.SendMessageTimeoutW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
        wintypes.UINT, wintypes.UINT, ctypes.POINTER(wintypes.DWORD),
    ]
    u.SendMessageTimeoutW.restype = wintypes.LPARAM
    u.SetForegroundWindow.argtypes = [wintypes.HWND]
    u.BringWindowToTop.argtypes = [wintypes.HWND]
    u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    u.GetWindowThreadProcessId.restype = wintypes.DWORD
    u.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    u.GetForegroundWindow.restype = wintypes.HWND
    u.OpenClipboard.argtypes = [wintypes.HWND]
    u.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    u.SetClipboardData.restype = wintypes.HANDLE
    u.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.POINTER(wintypes.ULONG)]
    u.mouse_event.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                              wintypes.ULONG, ctypes.POINTER(wintypes.ULONG)]
    u.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]

    kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    # 跨进程内存（枚举/操作树控件需要）
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL,
                                     wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.VirtualAllocEx.argtypes = [wintypes.HANDLE, wintypes.LPVOID,
                                        ctypes.c_size_t, wintypes.DWORD,
                                        wintypes.DWORD]
    kernel32.VirtualAllocEx.restype = wintypes.LPVOID
    kernel32.VirtualFreeEx.argtypes = [wintypes.HANDLE, wintypes.LPVOID,
                                       ctypes.c_size_t, wintypes.DWORD]
    kernel32.WriteProcessMemory.argtypes = [wintypes.HANDLE, wintypes.LPVOID,
                                            wintypes.LPCVOID, ctypes.c_size_t,
                                            ctypes.POINTER(ctypes.c_size_t)]
    kernel32.ReadProcessMemory.argtypes = [wintypes.HANDLE, wintypes.LPCVOID,
                                           wintypes.LPVOID, ctypes.c_size_t,
                                           ctypes.POINTER(ctypes.c_size_t)]
    u.ScreenToClient.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
    u.GetDlgCtrlID.argtypes = [wintypes.HWND]
    u.GetDlgCtrlID.restype = ctypes.c_int

    shell32.ShellExecuteW.argtypes = [
        wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
        wintypes.LPCWSTR, ctypes.c_int,
    ]
    shell32.ShellExecuteW.restype = wintypes.HINSTANCE


_bind_win32()


# ================= 基础 Win32 工具 =================

def _window_text(hwnd) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def _class_name(hwnd) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _enum_windows():
    found = []

    def cb(hwnd, _):
        found.append(hwnd)
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return found


def _enum_children(parent):
    found = []

    def cb(hwnd, _):
        found.append(hwnd)
        return True

    user32.EnumChildWindows(parent, WNDENUMPROC(cb), 0)
    return found


def _menu_items(hmenu) -> list:
    """列出菜单各项的 (位置, wID, hSubMenu, 文本)。"""
    result = []
    count = user32.GetMenuItemCount(hmenu)
    for i in range(max(count, 0)):
        info = MENUITEMINFOW()
        info.cbSize = ctypes.sizeof(MENUITEMINFOW)
        info.fMask = MIIM_ID | MIIM_SUBMENU | MIIM_STRING | MIIM_FTYPE
        buf = ctypes.create_unicode_buffer(256)
        info.dwTypeData = ctypes.cast(buf, wintypes.LPWSTR)
        info.cch = 255
        if user32.GetMenuItemInfoW(hmenu, i, True, ctypes.byref(info)):
            result.append((i, info.wID, info.hSubMenu, buf.value))
    return result


def _find_in_menu(hmenu, text: str):
    """在菜单中递归查找文本包含 text 的项，返回 (wID, hSubMenu)。"""
    for _pos, wid, sub, label in _menu_items(hmenu):
        if text in (label or ""):
            return wid, sub
        if sub:
            found = _find_in_menu(sub, text)
            if found:
                return found
    return None


def _set_clipboard_text(text: str) -> None:
    """把 Unicode 文本写入剪贴板（自行完成全局内存分配）。"""
    buf = (text + "\x00").encode("utf-16-le")
    hglobal = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(buf))
    if not hglobal:
        raise MemoryError("GlobalAlloc 失败")
    ptr = kernel32.GlobalLock(hglobal)
    if not ptr:
        raise MemoryError("GlobalLock 失败")
    ctypes.memmove(ptr, buf, len(buf))
    kernel32.GlobalUnlock(hglobal)
    if not user32.OpenClipboard(None):
        raise OSError("OpenClipboard 失败（剪贴板被占用）")
    try:
        user32.EmptyClipboard()
        if not user32.SetClipboardData(CF_UNICODETEXT, hglobal):
            raise OSError("SetClipboardData 失败")
        hglobal = None  # 所有权已移交，不可再释放
    finally:
        user32.CloseClipboard()


def _key_press(vk: int):
    user32.keybd_event(vk, 0, 0, None)
    time.sleep(0.03)
    user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, None)


def _hotkey(*vkeys):
    for vk in vkeys:
        user32.keybd_event(vk, 0, 0, None)
        time.sleep(0.03)
    time.sleep(0.05)
    for vk in reversed(vkeys):
        user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, None)
        time.sleep(0.03)


def _mouse_click(x: int, y: int, right: bool = False):
    down = 0x0008 if right else 0x0002  # RIGHTDOWN / LEFTDOWN
    up = 0x0010 if right else 0x0004    # RIGHTUP / LEFTUP
    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.05)
    user32.mouse_event(down, 0, 0, 0, None)
    time.sleep(0.05)
    user32.mouse_event(up, 0, 0, 0, None)


# ================= 结果类型 =================

@dataclass
class StepResult:
    step: int            # 步骤序号
    name: str            # 步骤名称
    success: bool        # 是否成功
    manual: bool = False  # 是否需要手动完成
    message: str = ""    # 结果/错误/引导信息


# ================= 进程与跨进程消息 =================

def _process_exe_map() -> dict:
    """通过 Toolhelp32 快照获取 {pid: 进程exe名小写}。"""
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    result = {}
    if not snap or snap == INVALID_HANDLE_VALUE:
        return result
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            result[entry.th32ProcessID] = entry.szExeFile.lower()
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snap)
    return result


def _send_timeout(hwnd, msg: int, wparam=0, lparam=0, timeout_ms: int = 2000):
    """跨进程 SendMessage；目标线程挂起时超时返回 0（不永久阻塞）。

    lparam 传 str 时自动按宽字符串编组（用于 WM_SETTEXT 等）。
    """
    result = wintypes.DWORD()
    if isinstance(lparam, str):
        # 字符串参数使用独立原型（主原型 lparam 为数值 LPARAM）
        _send_timeout_str(hwnd, msg, wparam, ctypes.c_wchar_p(lparam),
                          SMTO_ABORTIFHUNG, timeout_ms,
                          ctypes.byref(result))
    else:
        user32.SendMessageTimeoutW(
            hwnd, msg, wparam, lparam, SMTO_ABORTIFHUNG, timeout_ms,
            ctypes.byref(result))
    return result.value


# SendMessageTimeoutW 的宽字符串 lparam 原型（WM_SETTEXT 跨进程编组）
_send_timeout_str = ctypes.WINFUNCTYPE(
    wintypes.LPARAM, wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
    ctypes.c_wchar_p, wintypes.UINT, wintypes.UINT,
    ctypes.POINTER(wintypes.DWORD),
)(("SendMessageTimeoutW", user32))


# ================= MCGS 窗口识别 =================

_ALL_CONFIG_EXES = {n for names in MCGS_CONFIG_EXES.values() for n in names}


def find_mcgs_setpro_window(target_exe_path: str = None) -> Optional[int]:
    """识别 MCGS【组态环境】主窗口。

    双重判定：
    - 进程名是已知组态环境主程序（McgsSetPro.exe / McgsSetE.exe / McgsSet.exe）
    - 窗口标题含「组态环境」（如「MCGS嵌入版组态环境」「McgsPro组态环境」）
    标题含「运行环境/模拟」的窗口（CEEMU 等）一律排除；#32770 对话框不做主窗口。

    target_exe_path 给出时，其对应进程优先。返回评分最高的窗口 hwnd。
    """
    proc_map = _process_exe_map()
    target_name = (os.path.basename(target_exe_path).lower()
                   if target_exe_path else None)

    best = None
    best_score = 99
    for hwnd in _enum_windows():
        if not user32.IsWindowVisible(hwnd):
            continue
        cls = _class_name(hwnd)
        if cls in ("#32770", "#32768"):
            continue
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        proc_name = proc_map.get(pid.value, "")
        title = _window_text(hwnd)
        if "运行环境" in title or "模拟" in title:
            continue
        by_proc = proc_name in _ALL_CONFIG_EXES
        by_title = "组态环境" in title
        if not (by_proc or by_title):
            continue
        if target_name and proc_name == target_name and by_title:
            score = 0   # 目标程序 + 标题确认
        elif by_proc and by_title:
            score = 1
        elif by_proc:
            score = 2
        else:
            score = 3
        if score < best_score:
            best, best_score = hwnd, score
    return best


def bring_window_to_front(hwnd: int) -> bool:
    """把窗口恢复、置顶并激活；用 AttachThreadInput 绕过前台锁定。"""
    if not hwnd or not user32.IsWindow(hwnd):
        return False
    try:
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
        cur_tid = kernel32.GetCurrentThreadId()
        target_tid = user32.GetWindowThreadProcessId(hwnd, None)
        fg = user32.GetForegroundWindow()
        fg_tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0
        if fg_tid and fg_tid != cur_tid:
            user32.AttachThreadInput(cur_tid, fg_tid, True)
        if target_tid != cur_tid:
            user32.AttachThreadInput(cur_tid, target_tid, True)
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        if target_tid != cur_tid:
            user32.AttachThreadInput(cur_tid, target_tid, False)
        if fg_tid and fg_tid != cur_tid:
            user32.AttachThreadInput(cur_tid, fg_tid, False)
        time.sleep(0.3)
        return True
    except Exception:
        return False


# ================= MCGS 自动化写入器 =================

# 已打开工程的界面标志
_OPEN_PROJECT_MARKS = ("工作台", "设备组态", "动画组态", "主控窗口",
                       "实时数据库", "运行策略", "策略组态")


class MCGSAutoWriter:
    """MCGS 组态自动写入器（纯 ctypes，无 pywin32 依赖）。

    使用：
        writer = MCGSAutoWriter(scada, mcgs_exe_path)
        results = writer.run(on_progress=lambda step, msg: ...)
    """

    def __init__(self, scada: dict, mcgs_exe_path: str = None):
        self.scada = scada
        self.mcgs_exe_path = mcgs_exe_path
        self.results: list = []
        self._csv_path: Optional[str] = None
        self._script_path: Optional[str] = None
        self._progress_cb: Optional[Callable] = None

    def _progress(self, step: int, message: str):
        if self._progress_cb:
            self._progress_cb(step, message)

    def run(self, on_progress: Callable = None) -> list:
        """执行所有自动化步骤，返回 StepResult 列表。"""
        self._progress_cb = on_progress
        self.results = []
        self._add_temp_files()

        # Step 0：启动/激活 MCGS
        self._progress(0, "正在启动/激活 MCGS 组态环境……")
        ok = self._ensure_mcgs_running()
        self.results.append(StepResult(
            0, "启动/激活 MCGS", ok,
            message="ok" if ok else "MCGS 未启动且无法自动启动，请手动打开后重试"))

        if not ok:
            self._progress(0, "MCGS 未就绪，后续步骤跳过（临时文件已生成，可手动导入）。")
            return self.results + [
                StepResult(1, "导入设备通道 CSV", False,
                           message=f"MCGS 未就绪，可手动导入：{self._csv_path}"),
                StepResult(2, "粘贴 McgsScript", False,
                           message=f"MCGS 未就绪，脚本文件：{self._script_path}"),
                StepResult(3, "数据对象创建", False, manual=True, message="需手动创建"),
            ]

        # Step 1：导入设备通道 CSV
        self._progress(1, "正在自动导入设备通道 CSV……")
        ok, msg = self._import_device_channels_csv()
        self.results.append(StepResult(1, "导入设备通道 CSV", ok, message=msg))

        # Step 2：脚本装入剪贴板（脚本编辑器需用户打开，避免盲粘贴破坏工程）
        self._progress(2, "正在将 McgsScript 装入剪贴板……")
        ok, msg = self._prepare_script_clipboard()
        self.results.append(StepResult(2, "准备 McgsScript", ok, message=msg))

        # Step 3：数据对象（必须手动）
        n = len(self.scada.get("variables", []))
        self.results.append(StepResult(
            3, "数据对象创建", False, manual=True,
            message=f"MCGS 无批量创建数据对象的入口，请对照「变量字典」在实时数据库逐条新建（共 {n} 项）。"))

        return self.results

    # ---------- 临时文件 ----------
    def _add_temp_files(self):
        tmp_dir = os.path.join(os.environ.get("TEMP", os.path.expanduser("~")),
                               "STEP7_AI_MCGS")
        os.makedirs(tmp_dir, exist_ok=True)
        self._csv_path = os.path.join(tmp_dir, "device_channels.csv")
        self._script_path = os.path.join(tmp_dir, "mcgs_script.mcs")
        with open(self._csv_path, "w", encoding="utf-8-sig") as f:
            f.write(self.scada["device_channels_csv"])
        with open(self._script_path, "w", encoding="utf-8") as f:
            f.write(self.scada["script"])

    # ---------- 通用控件小工具 ----------
    @staticmethod
    def _control_by_class(parent, classname: str, text: str = None):
        for ch in _enum_children(parent):
            if _class_name(ch) == classname:
                if text is None or text in _window_text(ch):
                    return ch
        return None

    def _has_button(self, parent, text: str) -> bool:
        return self._control_by_class(parent, "Button", text) is not None

    def _click_button(self, parent, text: str) -> bool:
        btn = self._control_by_class(parent, "Button", text)
        if btn:
            user32.SendMessageW(btn, BM_CLICK, 0, 0)
            return True
        return False

    @staticmethod
    def _project_is_open(hwnd) -> bool:
        """判断组态环境中是否已打开工程。"""
        title = _window_text(hwnd)
        if any(m in title for m in _OPEN_PROJECT_MARKS):
            return True
        # 无标题时兜底：MDIClient 下有可见 MDI 子框架
        for ch in _enum_children(hwnd):
            if _class_name(ch) == "MDIClient":
                for c in _enum_children(ch):
                    if user32.IsWindowVisible(c):
                        return True
        return False

    def _wait_dialog(self, title_keys=(), timeout: float = 8):
        """等待出现标题含全部 title_keys 的 #32770 对话框。"""
        t0 = time.time()
        while time.time() - t0 < timeout:
            for h in _enum_windows():
                if user32.IsWindowVisible(h) and _class_name(h) == "#32770":
                    t = _window_text(h)
                    if all(k in t for k in title_keys):
                        return h
            time.sleep(0.3)
        return None

    def _dismiss_question_no(self) -> bool:
        """把「是否使用默认通讯参数……」类 是/否 提问按「否」关闭（不修改父设备）。"""
        for h in _enum_windows():
            if user32.IsWindowVisible(h) and _class_name(h) == "#32770":
                if self._has_button(h, "否(&N)"):
                    return self._click_button(h, "否(&N)")
        return False

    @staticmethod
    def _press_esc():
        _key_press(VK_ESCAPE)
        time.sleep(0.4)

    # ---------- Step 0 ----------
    def _ensure_mcgs_running(self) -> bool:
        hwnd = find_mcgs_setpro_window(self.mcgs_exe_path)
        if hwnd:
            return bring_window_to_front(hwnd)

        if not (self.mcgs_exe_path and os.path.isfile(self.mcgs_exe_path)):
            return False

        # 组态程序以普通权限启动（runas 会弹 UAC，且组态编辑无需提权）
        self._progress(0, f"MCGS 未运行，尝试启动：{self.mcgs_exe_path}")
        try:
            work_dir = os.path.dirname(self.mcgs_exe_path)
            subprocess.Popen([self.mcgs_exe_path], cwd=work_dir)
            for _ in range(40):  # 最多等 20 秒
                time.sleep(0.5)
                hwnd = find_mcgs_setpro_window(self.mcgs_exe_path)
                if hwnd:
                    return bring_window_to_front(hwnd)
        except Exception as e:
            self._progress(0, f"启动异常：{e}")
        return False

    # ---------- Step 1 ----------
    def _import_device_channels_csv(self) -> tuple:
        try:
            hwnd = find_mcgs_setpro_window(self.mcgs_exe_path)
            if not hwnd:
                return False, "MCGS 组态窗口未找到"
            bring_window_to_front(hwnd)

            # 0) 无工程 → 自动新建
            if not self._project_is_open(hwnd):
                self._progress(1, "当前无打开工程，自动新建工程……")
                ok, msg = self._create_new_project(hwnd)
                if not ok:
                    return False, msg
                hwnd = find_mcgs_setpro_window(self.mcgs_exe_path)
                if not hwnd:
                    return False, "新建工程后组态窗口未找到"

            # 1) 工作台 → 设备窗口标签
            ok, msg = self._goto_device_worktab(hwnd)
            if not ok:
                return False, msg

            # 2) 点「设备组态」进入设备编辑器
            hwnd = find_mcgs_setpro_window(self.mcgs_exe_path)
            dev_btn = self._control_by_class(hwnd, "Button", "设备组态")
            if not dev_btn:
                return False, "未找到「设备组态」按钮"
            _send_timeout(dev_btn, BM_CLICK)
            time.sleep(2.5)

            # 3) 设备树遍历 → 子设备属性 → 设备信息导入
            dev_main = self._wait_dialog(("设备组态", "设备窗口"), timeout=8)
            if not dev_main:
                # 主窗口标题即设备编辑器时直接用主窗口
                hwnd = find_mcgs_setpro_window(self.mcgs_exe_path)
                if hwnd and "设备组态" in _window_text(hwnd):
                    dev_main = hwnd
                else:
                    return False, "未进入设备组态窗口"
            return self._import_from_device_tree(dev_main)
        except Exception as e:
            return False, f"导入异常：{e}"

    def _create_new_project(self, hwnd) -> tuple:
        """文件 > 新建工程 → 处理「新建工程设置」对话框 → 等待工作台。"""
        # 从菜单解析「新建工程」wID（嵌入版实测 57600），失败回退固定值
        wid = 57600
        menu = user32.GetMenu(hwnd)
        if menu:
            found = _find_in_menu(menu, "新建工程")
            if found:
                wid = found[0]
        user32.PostMessageW(hwnd, WM_COMMAND, wid, 0)

        dlg = self._wait_dialog(("新建工程",), timeout=8)
        if not dlg:
            return False, "未出现「新建工程设置」对话框"

        # TPC 类型 ComboBox：确保有选中项（默认第 0 项）
        combo = self._control_by_class(dlg, "ComboBox")
        if combo:
            if _send_timeout(combo, CB_GETCURSEL) < 0:
                _send_timeout(combo, CB_SETCURSEL, 0)

        # 确定：取按钮控件 ID 后 Post WM_COMMAND（避免 SendMessage 阻塞）
        ok_btn = self._find_button_deep(dlg, "确定")
        if ok_btn:
            user32.PostMessageW(
                dlg, 0x0111, user32.GetDlgCtrlID(ok_btn), ok_btn)
        else:
            user32.PostMessageW(dlg, 0x0111, IDOK, 0)

        t0 = time.time()
        while time.time() - t0 < 20:
            cur = find_mcgs_setpro_window(self.mcgs_exe_path)
            if cur and "工作台" in _window_text(cur):
                time.sleep(0.5)
                return True, "已新建工程并进入工作台"
            time.sleep(0.4)
        return False, "新建工程后未检测到工作台"

    def _goto_device_worktab(self, hwnd) -> tuple:
        """切换工作台到「设备窗口」标签（向标签控件投递鼠标消息，控件原生处理）。"""
        tab = self._control_by_class(hwnd, "SysTabControl32")
        if not tab:
            return False, "未找到工作台标签控件"
        count = _send_timeout(tab, TCM_GETITEMCOUNT) or 5
        rect = wintypes.RECT()
        user32.GetWindowRect(tab, ctypes.byref(rect))
        width = rect.right - rect.left
        per = width // max(count, 1)

        def post_tab_click(screen_x):
            pt = wintypes.POINT(screen_x, rect.top + 10)
            user32.ScreenToClient(tab, ctypes.byref(pt))
            lp = (pt.y << 16) | (pt.x & 0xFFFF)
            user32.PostMessageW(tab, 0x0201, 1, lp)
            time.sleep(0.07)
            user32.PostMessageW(tab, 0x0202, 0, lp)

        # 先确认当前是否已在设备页
        if self._has_button(hwnd, "设备组态"):
            return True, "ok"
        for i in range(max(count, 5) + 2):
            post_tab_click(rect.left + per * i + per // 2)
            time.sleep(0.7)
            cur = find_mcgs_setpro_window(self.mcgs_exe_path)
            if cur and self._has_button(cur, "设备组态"):
                return True, "ok"
        return False, "未能切换到「设备窗口」标签"

    def _import_from_device_tree(self, dev_main) -> tuple:
        """消息化设备树导入：枚举设备 → 导出元数据 → 协议匹配 → 合并 CSV 导入。

        全部交互使用 PostMessage / SendMessageTimeout，不抢占用户鼠标。
        """
        tree = self._control_by_class(dev_main, "SysTreeView32")
        if not tree:
            return False, "设备组态窗口中未找到设备树"

        # 1) 枚举全部节点及其客户区 rect
        nodes = self._enum_tree_nodes(dev_main, tree)
        if not nodes:
            return False, "设备树中没有任何设备构件"

        # 2) 逐设备打开 → 导出元数据
        tmp_dir = os.path.dirname(self._csv_path)
        devices = []  # [{name, dll, driver, version, hitem, rect}]
        for i, (hitem, rc) in enumerate(nodes):
            export_path = os.path.join(tmp_dir, f"_export_{i}.csv")
            edit_win = self._open_device_edit(tree, rc)
            if not edit_win:
                continue
            if not self._export_device_info(edit_win, export_path):
                self._close_edit(edit_win)
                continue
            meta = self._read_export_meta(export_path)
            self._close_edit(edit_win)
            if meta:
                meta["hitem"] = hitem
                meta["rect"] = rc
                devices.append(meta)

        if not devices:
            return False, ("未能读取任何设备的驱动信息。"
                           f"可手动导入：设备组态>子设备属性>设备信息导入>{self._csv_path}")

        # 3) 按协议关键词选择目标设备
        protocol = self.scada.get("protocol", "")
        hints = _PROTOCOL_DRIVER_HINTS.get(protocol, ())
        target = None
        if hints:
            for d in devices:
                blob = f"{d.get('dll','')} {d.get('driver','')}".lower()
                if any(h in blob for h in hints):
                    target = d
                    break
        if target is None:
            target = devices[0]

        self._progress(1, f"目标设备：{target['name']}（{target.get('driver','')}）")

        # 4) 构造合并导入 CSV（保留设备元数据头，通道换为我方 8 列数据）
        merged = os.path.join(tmp_dir, "_merged_import.csv")
        try:
            self._build_import_csv(target, merged)
        except Exception as e:
            return False, f"构造导入文件失败：{e}"

        # 5) 重新打开目标设备 → 设备信息导入
        edit_win = self._open_device_edit(tree, target["rect"])
        if not edit_win:
            return False, "重新打开目标设备失败"
        # 成功时编辑窗口由「确认」关闭；失败时保留现场，由用户手动关闭
        return self._do_import(edit_win, merged)

    # ---- 树节点枚举 ----
    def _enum_tree_nodes(self, dev_main, tree):
        """枚举树顶层节点（含子节点），返回 [(hItem, (l,t,r,b))]。"""
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(tree, ctypes.byref(pid))
        PROCESS_ALL_ACCESS = 0x1FFFFF
        hproc = kernel32.OpenProcess(PROCESS_ALL_ACCESS, False, pid.value)
        if not hproc:
            return []
        remote = kernel32.VirtualAllocEx(hproc, None, 4096, 0x3000, 0x4)
        try:
            # 收集 handles（深度优先）
            handles = []
            root = _send_timeout(tree, TVM_GETNEXTITEM, TVGN_ROOT, 0)
            stack = [root]
            while stack:
                cur = stack.pop()
                if not cur:
                    continue
                handles.append(cur)
                nxt = _send_timeout(tree, TVM_GETNEXTITEM, TVGN_NEXT, cur)
                child = _send_timeout(tree, TVM_GETNEXTITEM, TVGN_CHILD, cur)
                if nxt:
                    stack.append(nxt)
                if child:
                    stack.append(child)

            result = []
            for hitem in handles:
                rc = wintypes.RECT()
                rc.left = hitem  # TVM_GETITEMRECT 约定：left 放入 hItem
                kernel32.WriteProcessMemory(hproc, remote, ctypes.byref(rc),
                                            ctypes.sizeof(rc), None)
                ok = _send_timeout(tree, TVM_GETITEMRECT, 1, remote)
                rc2 = wintypes.RECT()
                kernel32.ReadProcessMemory(hproc, remote, ctypes.byref(rc2),
                                           ctypes.sizeof(rc2), None)
                if ok:
                    result.append((hitem, (rc2.left, rc2.top,
                                           rc2.right, rc2.bottom)))
            return result
        finally:
            kernel32.VirtualFreeEx(hproc, remote, 0, 0x8000)
            kernel32.CloseHandle(hproc)

    # ---- 对话框结算（统一处理各类弹出框） ----
    @staticmethod
    def _click_dialog_button(dlg, btn):
        # 向按钮本身投递左键按下/抬起，由按钮原生向父框发 WM_COMMAND。
        user32.PostMessageW(btn, 0x0201, 1, 9)
        time.sleep(0.07)
        user32.PostMessageW(btn, 0x0202, 0, 9)
        return user32.GetDlgCtrlID(btn)

    def _dialog_statics(self, h):
        out = []
        for c in _enum_children(h):
            if user32.IsWindowVisible(c) and _class_name(c) == "Static":
                t = _window_text(c)
                if t:
                    out.append(t)
        return out

    def _answer_dialog(self, h):
        """按静态文本内容决定如何回答单个对话框；返回按钮句柄或 'cancel'。"""
        statics = self._dialog_statics(h)
        blob = "".join(statics)
        btn_y = self._find_button_deep(h, "是(&Y)")
        btn_n = self._find_button_deep(h, "否(&N)")
        btn_ok = self._find_button_deep(h, "确定")

        # 默认通讯参数询问 → 否（不改动父设备）
        if "默认通讯参数" in blob:
            return btn_n or "cancel"
        # 覆盖 / 继续导入 / 删除全部通道 类确认 → 是
        if any(k in blob for k in
               ("覆盖", "替换", "另存为", "删除设备中全部通道", "继续导入",
                "继续")):
            return btn_y or "cancel"
        # 成功/普通提示 → 确定
        if "成功" in blob or "完成" in blob:
            return btn_ok or (btn_y if btn_y else "cancel")
        # 无文本确认框（确认另存为，文本尚未渲染）→ 是
        if not statics and (btn_y or btn_n):
            return btn_y or btn_n
        # 有确定按钮 → 确定；否则取消
        return btn_ok or "cancel"

    def _settle_dialogs(self, keep=(), timeout: float = 8,
                        leave_substrings=()):
        """关闭 keep 之外所有可识别的可见 #32770 对话框，返回静态文本。

        结果框可能随处理延迟出现，需连续多轮无可处理框才结束；
        无法识别的框不盲目取消，留给用户处理；
        含 leave_substrings 的框只记录文本、不关闭（实测关闭某些
        失败结果框会导致 MCGS 访问违例崩溃）。
        """
        keep = set(keep)
        left = set()
        texts = []
        t0 = time.time()
        idle = 0
        while time.time() - t0 < timeout:
            pending = [h for h in _enum_windows()
                       if user32.IsWindowVisible(h)
                       and _class_name(h) == "#32770"
                       and h not in keep and h not in left]
            acted = False
            for h in pending:
                statics = self._dialog_statics(h)
                if any(k in "".join(statics) for k in leave_substrings):
                    left.add(h)
                    texts.extend(statics)
                    continue
                answer = self._answer_dialog(h)
                if answer == "cancel" or not answer:
                    continue
                texts.extend(statics)
                self._click_dialog_button(h, answer)
                acted = True
                time.sleep(0.55)
            if acted:
                idle = 0
            else:
                idle += 1
                if idle >= 6:  # 连续约 3 秒无新弹窗
                    break
                time.sleep(0.5)
        return texts

    # ---- 打开/关闭设备编辑窗口 ----
    @staticmethod
    def _find_button_deep(parent, text):
        def walk(h):
            for c in _enum_children(h):
                if _class_name(c) == "Button" and text in _window_text(c):
                    return c
                r = walk(c)
                if r:
                    return r
            return None
        return walk(parent)

    def _open_device_edit(self, tree, rc):
        """双击树节点打开设备编辑窗口；处理「默认通讯参数」询问（点否）。"""
        # 先清掉历史残留对话框（成功提示等）
        self._settle_dialogs(timeout=2)

        x = (rc[0] + rc[2]) // 2
        y = (rc[1] + rc[3]) // 2
        lp = (y << 16) | (x & 0xFFFF)
        for msg, wp in ((0x0201, 1), (0x0202, 0), (0x0203, 1), (0x0202, 0)):
            user32.PostMessageW(tree, msg, wp, lp)
            time.sleep(0.05)

        t0 = time.time()
        while time.time() - t0 < 6:
            # 默认参数询问 → 否（先于其他处理）
            for h in _enum_windows():
                if (user32.IsWindowVisible(h) and _class_name(h) == "#32770"
                        and "默认通讯参数" in "".join(self._dialog_statics(h))):
                    btn = self._find_button_deep(h, "否(&N)")
                    if btn:
                        self._click_dialog_button(h, btn)
                    else:
                        user32.PostMessageW(h, 0x0111, IDNO, 0)
                    time.sleep(0.6)
                    break
            for h in _enum_windows():
                if user32.IsWindowVisible(h) \
                        and _window_text(h) == "设备编辑窗口":
                    return h
            time.sleep(0.25)
        return None

    def _close_edit(self, edit_win):
        """取消关闭设备编辑窗口；等待其彻底消失并清理后续提示框。"""
        try:
            user32.PostMessageW(edit_win, 0x0111, IDCANCEL, 0)
            # 等编辑窗口关闭（最多 6 秒），期间回答「是否丢弃改动」类确认
            t0 = time.time()
            while time.time() - t0 < 6:
                if not user32.IsWindow(edit_win):
                    break
                for h in _enum_windows():
                    if (h != edit_win and user32.IsWindowVisible(h)
                            and _class_name(h) == "#32770"):
                        answer = self._answer_dialog(h)
                        if answer and answer != "cancel":
                            self._click_dialog_button(h, answer)
                        time.sleep(0.5)
                        break
                time.sleep(0.2)
            self._settle_dialogs(timeout=3)
        except Exception:
            pass

    # ---- 设备信息导出 ----
    def _export_device_info(self, edit_win, path) -> bool:
        btn = self._find_button_deep(edit_win, "设备信息导出")
        if not btn:
            return False
        cid = user32.GetDlgCtrlID(btn)
        before = set(_enum_windows())
        user32.PostMessageW(edit_win, 0x0111, cid, btn)

        t0 = time.time()
        sdlg = None
        while time.time() - t0 < 6:
            for h in _enum_windows():
                if (h not in before and user32.IsWindowVisible(h)
                        and _class_name(h) == "#32770"
                        and _window_text(h) == "另存为"):
                    sdlg = h
                    break
            if sdlg:
                break
            time.sleep(0.25)
        if not sdlg:
            return False
        if not self._fill_path_and_confirm(sdlg, path):
            return False
        # 统一处理覆盖确认 + 导出成功提示（保留 edit_win）
        self._settle_dialogs(keep={edit_win}, timeout=8)
        return os.path.isfile(path)

    @staticmethod
    def _read_export_meta(path) -> dict:
        """读导出 CSV（GBK）前 4 行元数据。"""
        try:
            with open(path, encoding="gbk") as f:
                lines = f.read().splitlines()
        except (OSError, UnicodeDecodeError):
            return None
        meta = {}
        keys = {"组态设备名称": "name", "驱动库文件路径": "dll",
                "驱动构件名称": "driver", "驱动构件版本": "version"}
        for line in lines[:4]:
            head = line.split(":", 1)
            if len(head) == 2 and head[0] in keys:
                meta[keys[head[0]]] = head[1].split(",")[0]
        return meta if "name" in meta else None

    def _fill_path_and_confirm(self, dlg, path) -> bool:
        """在标准文件对话框中填路径并点确定（Edit WM_SETTEXT + WM_COMMAND IDOK）。"""
        edit = self._control_by_class(dlg, "Edit")
        if not edit:
            return False
        _send_timeout(edit, WM_SETTEXT, 0, path)
        time.sleep(0.35)
        user32.PostMessageW(dlg, 0x0111, IDOK, 0)
        return True

    # ---- 构造合并导入 CSV ----
    def _build_import_csv(self, device: dict, out_path: str):
        import csv
        import io
        raw_csv = self.scada["device_channels_csv"].lstrip("\ufeff")
        rows = list(csv.reader(io.StringIO(raw_csv)))
        col8 = ["通道号", "变量名", "变量类型", "通道名称", "读写类型",
                "寄存器名称", "数据类型", "寄存器地址"]
        idx = {n: i for i, n in enumerate(rows[0])}
        missing = [n for n in col8 if n not in idx]
        if missing:
            raise ValueError(f"通道 CSV 缺少列：{missing}")

        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\r\n")
        header_lines = [
            f"组态设备名称:{device['name']},,,,,,,",
            f"驱动库文件路径:{device['dll']},,,,,,,",
            f"驱动构件名称:{device['driver']},,,,,,,",
            f"驱动构件版本:{device['version']},,,,,,,",
        ]
        for line in header_lines:
            buf.write(line + "\r\n")
        writer.writerow(col8)
        for r in rows[1:]:
            if not any(cell.strip() for cell in r):
                continue
            writer.writerow([r[idx[n]] for n in col8])

        with open(out_path, "w", encoding="gbk", newline="") as f:
            f.write(buf.getvalue())

    # ---- 执行导入 ----
    def _do_import(self, edit_win, csv_path) -> tuple:
        btn = self._find_button_deep(edit_win, "设备信息导入")
        if not btn:
            return False, "设备编辑窗口中未找到「设备信息导入」按钮"
        cid = user32.GetDlgCtrlID(btn)
        before = set(_enum_windows())
        # 该按钮可能是禁用状态：WM_COMMAND 直达
        user32.PostMessageW(edit_win, 0x0111, cid, btn)

        t0 = time.time()
        fdlg = None
        while time.time() - t0 < 6:
            for h in _enum_windows():
                if (h not in before and user32.IsWindowVisible(h)
                        and _class_name(h) == "#32770"
                        and _window_text(h) == "打开"
                        and self._control_by_class(h, "Edit")):
                    fdlg = h
                    break
            if fdlg:
                break
            time.sleep(0.25)
        if not fdlg:
            return False, "未出现文件选择对话框"

        if not self._fill_path_and_confirm(fdlg, csv_path):
            return False, "文件对话框填写失败"

        # 统一处理后续确认框/结果框（保留 edit_win 与文件对话框），收集全部提示文本；
        # 失败结果框保留不关（关闭会致 MCGS 崩溃），由用户真实鼠标处理。
        texts = self._settle_dialogs(
            keep={edit_win, fdlg}, timeout=12,
            leave_substrings=_IMPORT_FAILURE_KEYS)
        joined = "；".join(t for t in texts if "删除设备中全部通道" not in t)

        if any(k in joined for k in _IMPORT_FAILURE_KEYS):
            return False, (f"MCGS 拒绝导入：{joined}（该提示框已保留，"
                           "请用鼠标点击「确定」后更换设备或检查通道）")

        # 成功：点「确认」保存设备编辑
        ok_btn = self._find_button_deep(edit_win, "确")
        if ok_btn and "认" in _window_text(ok_btn):
            self._click_dialog_button(edit_win, ok_btn)
            time.sleep(0.8)
        success_msg = joined or "MCGS 未返回文本提示"
        return True, f"设备通道已导入（{success_msg}）文件：{csv_path}"

    # ---------- Step 2 ----------
    def _prepare_script_clipboard(self) -> tuple:
        """把脚本装入剪贴板；脚本编辑器由用户打开后 Ctrl+V。

        不做盲粘贴：若把 Ctrl+A/V 发给设备树等界面，可能误粘贴设备构件破坏工程。
        """
        script_text = self.scada.get("script", "")
        if not script_text.strip():
            return False, "脚本内容为空"
        try:
            _set_clipboard_text(script_text)
        except OSError as e:
            return False, f"剪贴板写入失败：{e}。请手动打开脚本文件：{self._script_path}"
        return True, (
            "脚本已装入剪贴板。请在 MCGS 打开脚本编辑器（运行策略→脚本编辑 / "
            "窗口启动·循环·退出脚本 / 控件事件脚本）后直接 Ctrl+V；"
            f"脚本文件：{self._script_path}")
