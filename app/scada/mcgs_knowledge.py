# -*- coding: utf-8 -*-
"""MCGS 组态软件知识库。

封装脚本语法、系统函数、驱动选型、CSV 列格式、版本差异等事实性知识，
供 mcgs_templates 派生素材时引用，也作为 AI 提示词的素材来源。
所有内容基于昆仑通态官方文档与现场工程实践整理。
"""

# ---------------- 版本与协议 ----------------

SUPPORTED_VERSIONS = ["嵌入版", "通用版", "McgsPro"]

# 嵌入版主要面向触摸屏 TPC；通用版面向 PC 上位机；McgsPro 为 TPC Pro 新平台
SUPPORTED_PROTOCOLS = {
    "嵌入版": ["PPI", "Modbus"],
    "通用版": ["PPI", "Modbus", "OPC"],
    "McgsPro": ["PPI", "Modbus", "OPC"],
}


# ---------------- 设备通道 CSV 11 列规范 ----------------

# MCGS 嵌入版/通用版"设备窗口 → 右键 → 设备信息导入"接口的标准 CSV 列结构
# McgsPro 也是 11 列，但列内容格式不同（见下方 McgsPro 专用常量）
CSV_COLUMNS = [
    "通道号",
    "变量名",
    "变量类型",
    "通道名称",
    "读写类型",
    "寄存器名称",
    "数据类型",
    "寄存器地址",
    "地址偏移",
    "采集频次",
    "通道处理",
]

# MCGS 变量类型（数据对象 4 类）
VAR_TYPES = ["开关型", "数值型", "字符型", "组对象"]

# 数据对象类型映射：从 PLC 区/类型推导
PLC_AREA_TO_VAR_TYPE = {
    "I": "开关型",   # 输入映像区，按位读为开关量
    "Q": "开关型",   # 输出映像区
    "M": "开关型",   # 内部存储器按位
    "SM": "开关型",  # 特殊存储器
    "V": "数值型",   # V 区按字节/字/双字读为数值
    "T": "数值型",   # 定时器当前值
    "C": "数值型",   # 计数器当前值
}

# PLC 数据类型 → MCGS 通道数据类型
PLC_DTYPE_MAP = {
    "BOOL": "BOOL",
    "BYTE": "BYTE",
    "INT": "INT",
    "DINT": "DINT",
    "REAL": "REAL",
    "STRING": "STRING",
}

# 读写类型：I 区只读，Q/M/V/T/C 区可读可写
READ_ONLY_AREAS = {"I"}


# ---------------- McgsPro 专用格式 ----------------
# 参考：SMART Exporter + 昆仑通态官方文档
# McgsPro CSV 通道号从 0 开始（嵌入版/通用版从 1 开始）
# McgsPro 变量类型用英文：INTEGER(BOOL) / SINGLE(非 BOOL)
# McgsPro 通道名称固定格式：读写+区域+地址（如 读写M010.0 / 读写VDF3920）
# McgsPro 寄存器名称用中文描述（如 V数据寄存器 / M内部继电器）
# McgsPro 数据类型列用中文描述（如 通道的第00位 / 32位 浮点数）

# PLC 数据类型 → McgsPro 英文变量类型
MCGSPRO_VAR_TYPE_MAP = {
    "BOOL": "INTEGER",   # McgsPro 中 BOOL 用 INTEGER 类型
    "BYTE": "INTEGER",
    "INT": "INTEGER",
    "DINT": "SINGLE",
    "REAL": "SINGLE",
    "STRING": "STRING",
}

# PLC 区 → McgsPro 中文寄存器名称
MCGSPRO_REGISTER_NAMES = {
    "I": "I输入寄存器",
    "Q": "Q输出寄存器",
    "M": "M内部继电器",
    "V": "V数据寄存器",
    "SM": "SM特殊寄存器",
    "T": "T定时器",
    "C": "C计数器",
}

# PLC 数据类型 → McgsPro 中文数据类型描述
MCGSPRO_CHANNEL_DTYPE_MAP = {
    "BOOL": "通道的第00位",  # 位通道
    "BYTE": "8位 无符号整数",
    "INT": "16位 有符号整数",
    "DINT": "32位 有符号整数",
    "REAL": "32位 浮点数",
    "STRING": "字符串",
}


def mcgspro_channel_name(address: str, read_only: bool = False) -> str:
    """生成 McgsPro 通道名称（固定格式：读写/只读 + 区域 + 补零地址）。

    位地址如 M10.0 → 读写M010.0 / 只读M010.0
    字节/字/双字如 VD20 → 读写VDF0020 / 只读VDF0020
    """
    import re
    rw_prefix = "只读" if read_only else "读写"

    # SM 特殊存储器位：必须先于单字母匹配，否则 SM0.1 会被截成 S000.1
    m = re.match(r"^(SM)(\d+)\.(\d+)$", address)
    if m:
        byte, bit = int(m.group(2)), int(m.group(3))
        return f"{rw_prefix}SM{byte:03d}.{bit}"

    # 位地址：Axxx.x
    m = re.match(r"^([IQM])(\d+)\.(\d+)$", address)
    if m:
        area, byte, bit = m.group(1), int(m.group(2)), int(m.group(3))
        return f"{rw_prefix}{area}{byte:03d}.{bit}"

    # VB / VW / VD 前缀
    m = re.match(r"^(VB|VW|VD)(\d+)$", address)
    if m:
        prefix, offset = m.group(1), int(m.group(2))
        # VB → VBUB, VW → VWSB, VD → VDF（参考 SMART Exporter 规范）
        prefix_map = {"VB": "VBUB", "VW": "VWSB", "VD": "VDF"}
        return f"{rw_prefix}{prefix_map[prefix]}{offset:04d}"

    # 单字母区 + 数字（如 T37, C1, M10）
    m = re.match(r"^([IQMTC])(\d+)$", address)
    if m:
        area, offset = m.group(1), int(m.group(2))
        return f"{rw_prefix}{area}{offset:04d}"

    # 兜底
    return f"{rw_prefix}{address}"


def mcgspro_register_address(address: str) -> int:
    """生成 McgsPro 寄存器地址（数值）。

    位地址如 M10.0 → 10 （字节地址）
    字节/字/双字如 VD20 → 20 （偏移量）
    """
    import re
    if re.match(r"^SM\d+\.\d+$", address):  # SM0.1 → 0
        return int(re.match(r"^SM(\d+)", address).group(1))
    m = re.match(r"^([IQM])(\d+)\.(\d+)$", address)
    if m:
        return int(m.group(2))
    m = re.match(r"^([A-Za-z]+)(\d+)$", address)
    if m:
        return int(m.group(2))
    return 0


# ---------------- 协议 → 驱动选型 ----------------

PROTOCOL_DRIVER_MAP = {
    # ---------- 嵌入版 ----------
    ("嵌入版", "PPI"): {
        "driver_name": "西门子_S7-200PPI",
        "parent_device": "通用串口父设备",
        "port": "COM2",
        "baud": 9600,
        "data_bits": 8,
        "stop_bits": 1,
        "parity": "偶校验",
        "station": 2,
        "wiring": (
            "CPU pin3 (B-/TxD-) ↔ 屏 RS485 pin8 (D-)\n"
            "CPU pin8 (A+/TxD+) ↔ 屏 RS485 pin7 (D+)\n"
            "CPU pin5 (GND) ↔ 屏 RS485 pin5 (GND)\n"
            "超过 50m 在两端并接 120Ω 终端电阻"
        ),
    },
    ("嵌入版", "Modbus"): {
        "driver_name": "Modbus_RTU",
        "parent_device": "通用串口父设备",
        "port": "COM2",
        "baud": 9600,
        "data_bits": 8,
        "stop_bits": 1,
        "parity": "偶校验",
        "station": 2,
        "wiring": "同 PPI 接线；PLC 端需在程序中启用 Modbus 从站指令",
    },
    # ---------- 通用版 ----------
    ("通用版", "PPI"): {
        "driver_name": "西门子_S7-200PPI",
        "parent_device": "通用串口父设备",
        "port": "COM2",
        "baud": 9600,
        "data_bits": 8,
        "stop_bits": 1,
        "parity": "偶校验",
        "station": 2,
        "wiring": "PC 串口/USB-485 转换器后接 CPU RS485 口",
    },
    ("通用版", "Modbus"): {
        "driver_name": "Modbus_RTU",
        "parent_device": "通用串口父设备",
        "port": "COM2",
        "baud": 9600,
        "data_bits": 8,
        "stop_bits": 1,
        "parity": "偶校验",
        "station": 2,
        "wiring": "PC 串口/USB-485 转换器后接 PLC RS485 口",
    },
    ("通用版", "OPC"): {
        "driver_name": "OPC_客户端",
        "parent_device": "—",
        "port": "—",
        "baud": "—",
        "data_bits": "—",
        "stop_bits": "—",
        "parity": "—",
        "station": "—",
        "wiring": (
            "需在 PC 上先运行 OPC DA 服务器（如 Kepware / snap7 OPC server），\n"
            "MCGS 通用版设备窗口选 OPC 客户端，连接 OPC 服务器后\n"
            "在 OPC 项列表中粘贴本工具生成的 OPC 项名"
        ),
    },
    # ---------- McgsPro ----------
    # McgsPro 更多走以太网（Smart200 本体口 / 网口模块），也支持串口 PPI
    ("McgsPro", "PPI"): {
        "driver_name": "西门子_S7_Smart200_PPI",
        "parent_device": "通用串口父设备",
        "port": "COM2",
        "baud": 9600,
        "data_bits": 8,
        "stop_bits": 1,
        "parity": "偶校验",
        "station": 2,
        "wiring": (
            "CPU pin3 (B-/TxD-) ↔ 屏 RS485 pin8 (D-)\n"
            "CPU pin8 (A+/TxD+) ↔ 屏 RS485 pin7 (D+)\n"
            "CPU pin5 (GND) ↔ 屏 RS485 pin5 (GND)\n"
            "超过 50m 在两端并接 120Ω 终端电阻\n"
            "注意：Smart200 固件 V2.3+ 开放本体串口 PPI"
        ),
    },
    ("McgsPro", "Modbus"): {
        "driver_name": "Modbus_RTU",
        "parent_device": "通用串口父设备",
        "port": "COM2",
        "baud": 9600,
        "data_bits": 8,
        "stop_bits": 1,
        "parity": "偶校验",
        "station": 2,
        "wiring": "同 PPI 接线；PLC 端需调用 MBUS_INIT / MBUS_SLAVE 指令启用 Modbus 从站",
    },
    ("McgsPro", "OPC"): {
        "driver_name": "OPC_客户端",
        "parent_device": "—",
        "port": "—",
        "baud": "—",
        "data_bits": "—",
        "stop_bits": "—",
        "parity": "—",
        "station": "—",
        "wiring": (
            "需在 PC 上先运行 OPC DA 服务器（如 Kepware / snap7 OPC server），\n"
            "McgsPro 设备窗口选 OPC 客户端，连接 OPC 服务器后\n"
            "在 OPC 项列表中粘贴本工具生成的 OPC 项名"
        ),
    },
}


# ---------------- Modbus 寄存器映射 ----------------

# S7-200 SMART Modbus 从站地址映射规则（参考 SMART Modbus 库默认）
# I 区 → 0x 区（线圈）
# Q 区 → 1x 区（线圈）
# M 区 → 0x 区（线圈）
# V 区 → 4x 区（保持寄存器，按字读取）
def s7_to_modbus_register(address: str) -> tuple:
    """把 S7-200 SMART 地址转为 Modbus 寄存器名+地址+数据类型。
    返回 (寄存器名, 寄存器地址, 数据类型)。
    """
    import re
    m = re.match(r"^([A-Z]+)(\d+)\.(\d+)$", address)
    if m:
        area, byte, bit = m.group(1), int(m.group(2)), int(m.group(3))
        if area in ("I", "Q", "M"):
            mod_addr = byte * 8 + bit + 1  # Modbus 线圈地址从 1 开始
            code = "0x" if area in ("I", "M") else "1x"
            return (code, mod_addr, "BOOL")
    m = re.match(r"^([A-Z]+)(\d+)$", address)
    if m:
        area, byte = m.group(1), int(m.group(2))
        if area in ("I", "Q", "M"):
            return ("0x" if area in ("I", "M") else "1x", byte + 1, "BYTE")
        if area == "V":
            return ("4x", byte + 1, "INT")
    m = re.match(r"^(VB|VW|VD|V)(\d+)$", address)
    if m:
        prefix, offset = m.group(1), int(m.group(2))
        if prefix == "VB":
            return ("4x", offset + 1, "BYTE")
        if prefix == "VW":
            return ("4x", offset + 1, "INT")
        if prefix == "VD":
            return ("4x", offset + 1, "REAL")
        return ("4x", offset + 1, "INT")
    return ("4x", 1, "INT")


# ---------------- OPC 项路径规则 ----------------

def s7_to_opc_item(address: str) -> str:
    """生成 snap7 OPC server 项名（参考示例，实际以 OPC 服务器配置为准）。"""
    return f"S7:[S7-200SMART]{address}"


# ---------------- McgsScript 语法要点 ----------------

MCGS_SCRIPT_GRAMMAR = """\
McgsScript 语法要点（类 VBScript/Basic，McgsPro 支持局部变量与数组）：

1. 变量声明：DIM 变量名 AS integer/float/single/string/byte
   - 局部变量仅脚本内有效
   - McgsPro 支持 DIM 数组：DIM arr(10) AS integer
   - 全局变量即实时数据库数据对象，直接赋值即可
2. 控制流：
   - IF 条件 THEN ... ELSEIF 条件 THEN ... ELSE ... ENDIF
   - WHILE 条件 ... ENDWHILE
   - FOR i = 0 TO N ... NEXT  （McgsPro 支持）
   - BREAK / EXIT
3. 系统函数前加 !：!SetDevice, !SaveData, !TimeStr2I,
   !ExportHistoryData, !WriteFile, !SaveDataToUSB, !Sleep
4. 系统变量前加 $：$MCGS_TIME, $MCGS_DATE, $用户窗口, $走纸
5. 对象树点号引用：MCGS.用户窗口.窗口名.控件名.属性
   - 控件方法：用户窗口.主画面.Open() / .Close()
6. McgsPro 支持自定义子程序/函数（嵌入版/通用版不支持）：
   - SUB 子程序名() ... END SUB
   - FUNCTION 函数名() AS 类型 ... END FUNCTION
7. 数据对象方法：变量名.SaveData() / .AnswerAlm(-1) /
   .SaveDataInitValue() / .SaveDataOnTime(t, 0)
"""


# ---------------- 系统函数清单 ----------------

MCGS_SYSTEM_FUNCTIONS = [
    "!SetDevice(设备名, 命令, 参数)",      # 操作设备
    "!SaveData(变量名)",                    # 条件存盘
    "!SaveDataAll()",                       # 全部存盘
    "!TimeStr2I(时间字符串)",              # 时间转整数
    "!ExportHistoryData()",                 # 导出历史数据
    "!WriteFile(文件名, 内容)",            # 写文件
    "!SaveDataToUSB()",                     # 存盘到 U 盘
    "!Sleep(毫秒)",                         # 延时
    "!Beep()",                              # 蜂鸣
]


# ---------------- 数据对象属性模板 ----------------

DATA_OBJECT_PROPERTIES = {
    "基本": ["名称", "类型", "初值", "注释", "指针化", "变化时自动保存初值"],
    "存盘": ["定时存盘/存内存", "存盘周期(ms, 最小100)", "存盘空间上限(2GB)"],
    "报警": [
        "开关量报警/跳变报警/限值报警/偏差报警/位报警",
        "基准值", "触发误差", "解除误差",
        "报警级别", "报警描述", "报警启用",
    ],
}


# ---------------- MCGS 安装路径检测 ----------------

# 各版本【组态环境】可执行文件名（编辑器）——这是自动化启动与操作的唯一目标
MCGS_CONFIG_EXES = {
    "McgsPro": ["mcgssetpro.exe"],
    "嵌入版": ["mcgssete.exe"],
    "通用版": ["mcgsset.exe"],
}

# 【运行/模拟环境】可执行文件名——直接打开工程运行，无编辑菜单，绝不能作为启动目标
# CEEMU.exe = 嵌入版模拟运行环境；mcgs_app.exe = McgsPro 运行环境；
# McgsCE.exe = 触摸屏运行环境
MCGS_RUNTIME_EXES = [
    "ceemu.exe", "mcgs_app.exe", "mcgsce.exe", "mcgsrun.exe",
]

# 辅助工具 exe——扫描时忽略
MCGS_AUX_EXES = {
    "unwise.exe", "unins000.exe", "setup.exe", "install.exe",
    "mcgslogger.exe", "filetransporttool.exe", "devcon.exe", "datatrans.exe",
}

# 兼容旧引用：各版本组态环境主程序名
MCGS_EXE_NAMES = {ver: list(names) for ver, names in MCGS_CONFIG_EXES.items()}

# 全部已知组态环境 exe（小写集合）
_ALL_CONFIG_EXES = {n for names in MCGS_CONFIG_EXES.values() for n in names}


def _detect_version_from_exe_path(exe_path: str) -> str:
    """根据 exe 文件名和路径关键字判定 MCGS 版本。"""
    import os
    name = os.path.basename(exe_path).lower()
    path_lower = exe_path.lower()
    if name in MCGS_CONFIG_EXES["McgsPro"] or "mcgspro" in path_lower:
        return "McgsPro"
    if name in MCGS_CONFIG_EXES["嵌入版"]:
        return "嵌入版"
    if name in MCGS_CONFIG_EXES["通用版"]:
        return "通用版"
    # 运行环境/未知 exe：按安装目录特征推断
    if "mcgspro" in path_lower:
        return "McgsPro"
    if "mcgse" in path_lower:
        return "嵌入版"
    return "通用版"  # 兜底


def normalize_mcgs_exe_path(exe_path: str) -> str | None:
    """把任何 MCGS 相关 exe 路径规范化为【组态环境】exe 路径。

    - 已是组态环境主程序：原样返回
    - 是运行/模拟环境（CEEMU.exe 等）：在其同目录查找组态环境主程序并返回
      （嵌入版 CEEMU.exe 与 McgsSetE.exe 同处 Program 目录）
    - 找不到组态环境：返回 None
    """
    import os
    if not exe_path:
        return None
    name = os.path.basename(exe_path).lower()
    if name in _ALL_CONFIG_EXES:
        return exe_path
    directory = os.path.dirname(exe_path)
    # 优先按路径推断版本的组态程序，其次遍历全部已知组态程序
    prefer_ver = _detect_version_from_exe_path(exe_path)
    ordered = list(MCGS_CONFIG_EXES.get(prefer_ver, []))
    for ver, names in MCGS_CONFIG_EXES.items():
        if ver != prefer_ver:
            ordered.extend(names)
    for candidate in ordered:
        candidate_path = os.path.join(directory, candidate)
        if os.path.isfile(candidate_path):
            return candidate_path
    return None


def _rank_candidates(candidates: list) -> str | None:
    """从候选 exe 中选出最佳组态环境主程序路径。

    规则：组态环境主程序 > 运行环境（运行环境反推同目录组态程序）；
    组态程序内按 McgsPro > 嵌入版 > 通用版；全部失败返回 None。
    """
    import os
    seen = set()
    uniq = []
    for c in candidates:
        key = c.lower()
        if key not in seen:
            seen.add(key)
            uniq.append(c)

    def _config_score(path: str):
        name = os.path.basename(path).lower()
        for i, ver in enumerate(("McgsPro", "嵌入版", "通用版")):
            if name in MCGS_CONFIG_EXES[ver]:
                return i
        return None

    # 先在候选里直接找组态程序
    best = None
    best_score = 99
    for c in uniq:
        score = _config_score(c)
        if score is not None and score < best_score:
            best, best_score = c, score
    if best:
        return best

    # 候选全是运行环境时，逐个反推同目录组态程序
    for c in uniq:
        normalized = normalize_mcgs_exe_path(c)
        if normalized:
            return normalized
    return None


def _search_registry() -> str | None:
    """第 1 层：查 Windows 卸载注册表，MCGS 安装时必写入。

    扫描 HKLM + HKCU 的 Uninstall 键，找 DisplayName 含 "MCGS"/"McgsPro"/"昆仑通态"，
    从 InstallLocation 或 DisplayIcon 取路径。
    返回第一个找到的 exe 绝对路径，或 None。
    """
    try:
        import winreg
    except ImportError:
        return None

    uninstall_keys = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]

    keywords = ("mcgs", "mcgspro", "昆仑通态", "mcgse", "mcgs e")

    for hkey, sub in uninstall_keys:
        try:
            with winreg.OpenKey(hkey, sub) as root:
                i = 0
                while True:
                    try:
                        subkey_name = winreg.EnumKey(root, i)
                        i += 1
                    except OSError:
                        break  # 遍历完

                    # 跳过"已删除"或明显非 MCGS 的条目
                    if not any(k in subkey_name.lower() for k in keywords):
                        # 也检查 DisplayName（有些安装器写得很随意）
                        try:
                            with winreg.OpenKey(root, subkey_name) as sk:
                                disp_name, _ = winreg.QueryValueEx(sk, "DisplayName")
                            if not any(k in disp_name.lower() for k in keywords):
                                continue
                        except OSError:
                            continue

                    # 找到 MCGS 相关条目，读取路径
                    try:
                        with winreg.OpenKey(root, subkey_name) as sk:
                            for val_name in ("InstallLocation", "DisplayIcon"):
                                try:
                                    val, _ = winreg.QueryValueEx(sk, val_name)
                                    path = str(val).strip().strip('"')
                                    # 去掉 /uninstall 等参数
                                    if " " in path:
                                        path = path.split(" ")[0]
                                    import os as _os
                                    fn_lower = _os.path.basename(path).lower()
                                    if fn_lower in MCGS_AUX_EXES:
                                        continue
                                    if _os.path.isfile(path):
                                        normalized = normalize_mcgs_exe_path(path)
                                        if normalized:
                                            return normalized
                                    # InstallLocation 可能是目录：收集其下全部
                                    # MCGS 相关 exe 后统一排序
                                    if _os.path.isdir(path):
                                        found = []
                                        for dirpath, dirnames, filenames in _os.walk(path):
                                            depth = dirpath.count(_os.sep) - path.count(_os.sep)
                                            if depth > 2:
                                                dirnames.clear()
                                            for fn in filenames:
                                                f_l = fn.lower()
                                                if f_l.endswith(".exe") and f_l not in MCGS_AUX_EXES:
                                                    found.append(_os.path.join(dirpath, fn))
                                        ranked = _rank_candidates(found)
                                        if ranked:
                                            return ranked
                                except OSError:
                                    pass
                    except OSError:
                        continue
        except OSError:
            continue

    return None


def _search_shortcuts() -> str | None:
    """第 2 层：查开始菜单和桌面快捷方式（.lnk 文件）。"""
    import os
    import glob

    lnk_dirs = [
        os.path.join(os.environ.get("ProgramData", ""), "Microsoft", "Windows", "Start Menu", "Programs"),
        os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs"),
        os.path.join(os.path.expanduser("~"), "Desktop"),
        os.path.join(os.environ.get("PUBLIC", ""), "Desktop"),
    ]
    keywords = ("mcgs", "mcgspro", "mcgse", "昆仑")

    for d in lnk_dirs:
        if not os.path.isdir(d):
            continue
        for lnk in glob.glob(os.path.join(d, "**", "*.lnk"), recursive=True):
            lnk_lower = lnk.lower()
            if not any(k in lnk_lower for k in keywords):
                continue
            try:
                # 用 PowerShell 解析 .lnk 的 TargetPath
                import subprocess
                result = subprocess.run(
                    [
                        "powershell", "-NoProfile", "-Command",
                        f"$s = New-Object -ComObject WScript.Shell; $s.CreateShortcut('{lnk}').TargetPath",
                    ],
                    capture_output=True, text=True, timeout=5,
                )
                target = result.stdout.strip()
                fn_lower = os.path.basename(target).lower()
                if fn_lower in MCGS_AUX_EXES:
                    continue
                if target and os.path.isfile(target):
                    # 快捷方式可能指向 CEEMU 等运行环境，统一转为组态程序
                    normalized = normalize_mcgs_exe_path(target)
                    if normalized:
                        return normalized
            except Exception:
                continue

    return None


def _search_drives_fast() -> str | None:
    """第 3 层：在所有本地盘根目录快速扫一层 MCGS 目录，找 Program 子目录下的 exe。

    比全盘 os.walk 快得多——只盘根 + 一级子目录的 Program 目录。
    """
    import os
    import string

    candidates = []
    for letter in string.ascii_uppercase:
        drive = f"{letter}:\\"
        if not os.path.isdir(drive):
            continue
        for root_entry in os.listdir(drive):
            entry = os.path.join(drive, root_entry)
            if not os.path.isdir(entry):
                continue
            name_lower = root_entry.lower()
            if not any(k in name_lower for k in ("mcgs", "mcgspro", "mcgse")):
                continue
            # 收集 MCGS 相关 exe：忽略辅助工具；运行环境（CEEMU 等）也收集，
            # 但排序时组态环境主程序绝对优先
            for dirpath, dirnames, filenames in os.walk(entry):
                depth = dirpath.count(os.sep) - entry.count(os.sep)
                if depth > 3:
                    dirnames.clear()  # 剪枝，只扫前 3 层
                for fn in filenames:
                    fn_lower = fn.lower()
                    if fn_lower.endswith(".exe") and fn_lower not in MCGS_AUX_EXES:
                        candidates.append(os.path.join(dirpath, fn))

    return _rank_candidates(candidates)


def find_mcgs_installed() -> tuple:
    """分层检测本机 MCGS 安装路径（注册表 > 快捷方式 > 全盘快速搜索）。

    返回 (版本标识, exe路径) 或 (None, None)。
    版本标识："McgsPro" / "嵌入版" / "通用版" / None
    """
    exe_path = None

    # 第 1 层：注册表（最可靠）
    exe_path = _search_registry()
    if exe_path:
        version = _detect_version_from_exe_path(exe_path)
        return (version, exe_path)

    # 第 2 层：开始菜单/桌面快捷方式
    exe_path = _search_shortcuts()
    if exe_path:
        version = _detect_version_from_exe_path(exe_path)
        return (version, exe_path)

    # 第 3 层：全盘快速搜索
    exe_path = _search_drives_fast()
    if exe_path:
        version = _detect_version_from_exe_path(exe_path)
        return (version, exe_path)

    return (None, None)


# ---------------- 导入接口说明 ----------------

IMPORT_GUIDE = """\
MCGS 各素材导入方法：

1. 变量字典：MCGS 无文本导入接口，需在"实时数据库"窗口中按字典逐条新建
   数据对象，对照本工具生成的"变量字典.csv"填入类型/初值/注释/报警阈值等。

2. 设备通道 CSV（11 列）：
   - 嵌入版/通用版/McgsPro 均支持
   - 组态环境 → 设备窗口 → 设备组态 → 选中【子设备】构件 → 双击或右键「属性」
     打开设备编辑窗口 → 点「设备信息导入」
   - 选择"设备通道.csv"按提示完成导入
   - 导入后变量自动与数据对象关联
   - 注意：McgsPro 要求驱动名称、驱动库路径、驱动版本与当前工程一致

3. McgsScript 脚本：
   - 直接粘贴到对应脚本编辑器
   - "运行策略" → 选中策略 → 脚本编辑
   - 或"用户窗口" → 窗口属性 → 启动/循环/退出脚本
   - 或控件事件 → 双击 → 事件脚本
   - McgsPro 额外支持自定义 SUB/FUNCTION

4. 画面设计书：
   - 对照设计书在"用户窗口"中新建画面、拖入控件、绑定变量
   - SVG 示意图可作布局参考（不能直接导入）

5. 协议说明：按说明在"设备窗口"中添加父设备与子设备，配置串口参数与从站地址。
"""
