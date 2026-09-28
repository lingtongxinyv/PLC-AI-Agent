# -*- coding: utf-8 -*-
"""MCGS 组态素材派生主入口。

从 PLC 程序或独立工艺描述派生 6 类素材：
1. 变量字典（数据对象定义清单，CSV）
2. 设备通道 CSV（11 列，MCGS 可直接导入）
3. 画面设计书（Markdown，含布局建议与控件清单）
4. 画面 SVG 示意图（布局参考，不能直接导入）
5. McgsScript 脚本（可粘贴到脚本编辑器）
6. 协议说明（驱动选型/串口参数/接线）

不生成 .mcg/.mce 工程文件（私有二进制，无开源解析器）。
"""
import os
import re

from .mcgs_knowledge import (
    SUPPORTED_VERSIONS,
    SUPPORTED_PROTOCOLS,
    PLC_AREA_TO_VAR_TYPE,
    READ_ONLY_AREAS,
    PROTOCOL_DRIVER_MAP,
    IMPORT_GUIDE,
)
from .mcgs_csv import build_device_channels_csv
from .mcgs_script import build_script, render_svg_preview


# ---------------- 变量派生工具 ----------------

def _safe_name(text, fallback):
    """从中文说明派生简洁变量名（去标点，保留中文/字母/数字/下划线）。"""
    if not text:
        return fallback
    cleaned = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9_]", "", str(text))
    cleaned = cleaned[:20] if cleaned else fallback
    return cleaned or fallback


# SCL 符号名中表示布尔量的关键词（不区分大小写；含中文）
_SYMBOLIC_BOOL_KEYS = (
    "start", "stop", "motor", "button", "run", "fault", "switch", "sensor", "limit",
    "灯", "按钮", "电机", "限位", "报警", "开关", "运行",
)


def _is_symbolic_bool(address) -> bool:
    """判断 SCL 符号地址（StartButton/Motor 等）是否为布尔量。"""
    s = str(address)
    if re.match(r"^(%|[IQMV]|DB\d|SM)", s, re.I):
        return False  # 显式 PLC 地址，不走符号推断
    low = s.lower()
    return any(k in low or k in s for k in _SYMBOLIC_BOOL_KEYS)


def _var_type_from_addr(address):
    """从 S7 地址派生 MCGS 变量类型。"""
    if _is_symbolic_bool(address):
        return "开关型"
    m = re.match(r"^([A-Z]+)", str(address))
    if m:
        return PLC_AREA_TO_VAR_TYPE.get(m.group(1), "数值型")
    return "数值型"


def _dtype_from_addr(address):
    """从地址派生 PLC 数据类型（CSV 数据类型列）。"""
    s = str(address)
    if "." in s:
        return "BOOL"
    if re.match(r"^VB", s):
        return "BYTE"
    if re.match(r"^VD", s):
        return "REAL"
    if re.match(r"^(VW|T|C)", s):
        return "INT"
    if re.match(r"^[IQM]", s):
        return "BOOL"
    if _is_symbolic_bool(s):
        return "BOOL"
    return "INT"


def _is_read_only(address):
    m = re.match(r"^([A-Z]+)", str(address))
    return bool(m and m.group(1) in READ_ONLY_AREAS)


# ---------------- 联动模式：从 PLC 程序派生变量 ----------------

def _derive_variables_from_program(program):
    """从 program dict 的 io_table 派生变量列表。"""
    variables = []
    seen = set()
    io_table = program.get("io_table", [])
    for row in io_table:
        if isinstance(row, dict):
            addr = row.get("address", "")
            comment = row.get("comment", "")
            dtype = row.get("dtype", "")
        elif isinstance(row, (list, tuple)) and len(row) >= 2:
            addr = str(row[0])
            comment = str(row[1]) if len(row) > 1 else ""
            dtype = str(row[2]) if len(row) > 2 else ""
        else:
            continue
        if not addr or addr in seen:
            continue
        seen.add(addr)
        var_type = _var_type_from_addr(addr)
        if not dtype:
            dtype = _dtype_from_addr(addr)
        variables.append({
            "name": _safe_name(comment, addr.replace(".", "_")),
            "address": addr,
            "var_type": var_type,
            "dtype": dtype,
            "read_only": _is_read_only(addr),
            "comment": comment or addr,
            "init": "0",
        })
    return variables


# ---------------- 独立模式：从工艺描述派生变量 ----------------

def _derive_variables_from_craft(craft_desc):
    """从自然语言工艺描述派生典型 I/O 变量。"""
    text = craft_desc or ""
    variables = []
    seen_addr = set()

    # 1. 先尝试从描述中提取 S7 地址（I0.0/Q0.0/M0.0/VB100/VW100/VD100 等）
    addr_pattern = re.compile(r"\b([IQMV][BDW]?\d+(?:\.\d+)?)\b")
    for m in addr_pattern.finditer(text):
        addr = m.group(1)
        if addr in seen_addr:
            continue
        seen_addr.add(addr)
        # 取地址附近中文作为说明
        start = max(0, m.start() - 12)
        end = min(len(text), m.end() + 12)
        ctx = text[start:end].replace(addr, "").strip("，。、:： ")
        var_type = _var_type_from_addr(addr)
        dtype = _dtype_from_addr(addr)
        variables.append({
            "name": _safe_name(ctx, addr.replace(".", "_")),
            "address": addr,
            "var_type": var_type,
            "dtype": dtype,
            "read_only": _is_read_only(addr),
            "comment": ctx or addr,
            "init": "0",
        })

    # 2. 若描述中无显式地址，按工艺关键词派生典型 I/O
    if not variables:
        typical = [
            ("启动按钮", "I0.0", "BOOL", "开关型", False),
            ("停止按钮", "I0.1", "BOOL", "开关型", False),
            ("运行指示", "Q0.0", "BOOL", "开关型", False),
            ("故障指示", "Q0.1", "BOOL", "开关型", False),
            ("运行状态", "M0.0", "BOOL", "开关型", False),
            ("设定值", "VW100", "INT", "数值型", False),
            ("实际值", "VW102", "INT", "数值型", False),
            ("累计产量", "VD120", "REAL", "数值型", False),
            # 液位/水罐系统
            ("水罐1液位", "VW110", "INT", "数值型", False),
            ("水罐2液位", "VW112", "INT", "数值型", False),
            ("液位上限", "VW114", "INT", "数值型", False),
            ("液位下限", "VW116", "INT", "数值型", False),
            ("水泵启动", "I0.2", "BOOL", "开关型", False),
            ("水泵停止", "I0.3", "BOOL", "开关型", False),
            ("水泵运行", "Q0.2", "BOOL", "开关型", False),
            ("调节阀输出", "VW120", "INT", "数值型", False),
            ("出水阀控制", "Q0.3", "BOOL", "开关型", False),
            ("高液位报警", "Q0.4", "BOOL", "开关型", False),
            ("低液位报警", "Q0.5", "BOOL", "开关型", False),
        ]
        kw_filters = {
            "电机": ["启动按钮", "停止按钮", "运行指示", "故障指示", "运行状态"],
            "传送带": ["启动按钮", "停止按钮", "运行指示", "累计产量"],
            "温度": ["设定值", "实际值", "故障指示"],
            "液位": ["设定值", "实际值", "水罐1液位", "水罐2液位",
                     "液位上限", "液位下限", "高液位报警", "低液位报警"],
            "水罐": ["水罐1液位", "水罐2液位", "液位上限", "液位下限",
                     "高液位报警", "低液位报警", "调节阀输出", "出水阀控制"],
            "罐": ["水罐1液位", "水罐2液位", "液位上限", "液位下限",
                    "高液位报警", "低液位报警"],
            "水泵": ["水泵启动", "水泵停止", "水泵运行", "故障指示",
                     "水罐1液位", "水罐2液位"],
            "泵": ["水泵启动", "水泵停止", "水泵运行", "故障指示"],
            "阀": ["调节阀输出", "出水阀控制"],
            "阀门": ["调节阀输出", "出水阀控制"],
            "风机": ["启动按钮", "停止按钮", "运行指示", "故障指示"],
            "计数": ["累计产量", "运行指示"],
        }
        matched = set()
        for kw, names in kw_filters.items():
            if kw in text:
                matched.update(names)
        if not matched:
            matched = {"启动按钮", "停止按钮", "运行指示", "故障指示"}
        for name, addr, dtype, var_type, ro in typical:
            if name in matched and addr not in seen_addr:
                seen_addr.add(addr)
                variables.append({
                    "name": name,
                    "address": addr,
                    "var_type": var_type,
                    "dtype": dtype,
                    "read_only": ro,
                    "comment": name,
                    "init": "0",
                })

    # 3. 兜底
    if not variables:
        for name, addr, dtype, var_type, ro in [
            ("启动", "I0.0", "BOOL", "开关型", False),
            ("停止", "I0.1", "BOOL", "开关型", False),
            ("运行", "Q0.0", "BOOL", "开关型", False),
        ]:
            variables.append({
                "name": name,
                "address": addr,
                "var_type": var_type,
                "dtype": dtype,
                "read_only": ro,
                "comment": name,
                "init": "0",
            })
    return variables


# ---------------- 主入口：derive_scada ----------------

def derive_scada(program=None, craft_desc=None, mcgs_version="嵌入版",
                 protocol="PPI", name=None):
    """派生 MCGS 组态素材。

    必须提供 program 或 craft_desc 之一。
    program: app.programs.templates.render 返回的 dict（联动模式）
    craft_desc: 工艺描述文本（独立模式）
    mcgs_version: "嵌入版" 或 "通用版"
    protocol: "PPI" / "Modbus" / "OPC"
    name: 组态工程名（可选）
    返回 scada dict。
    """
    if mcgs_version not in SUPPORTED_VERSIONS:
        raise ValueError(f"不支持的 MCGS 版本：{mcgs_version}")
    if protocol not in SUPPORTED_PROTOCOLS.get(mcgs_version, []):
        raise ValueError(f"{mcgs_version} 不支持协议 {protocol}")

    # 派生变量
    if program:
        variables = _derive_variables_from_program(program)
        plc_program_key = program.get("key") or program.get("name", "")
        if not name:
            name = f"{plc_program_key}_MCGS组态" if plc_program_key else "MCGS组态"
    else:
        variables = _derive_variables_from_craft(craft_desc)
        plc_program_key = ""
        if not name:
            name = "MCGS组态"

    # 协议说明
    driver_info = PROTOCOL_DRIVER_MAP.get((mcgs_version, protocol), {})
    protocol_note = _build_protocol_note(mcgs_version, protocol, driver_info)

    # 设备通道 CSV
    csv_text = build_device_channels_csv(variables, mcgs_version, protocol)

    # 画面设计书
    page_name = "主画面"
    svg_preview = render_svg_preview(variables, page_name)
    screen_design = {
        "page_name": page_name,
        "svg_preview": svg_preview,
        "design_text": _build_screen_design_text(variables),
    }

    # 脚本
    scada_ctx = {
        "name": name,
        "mcgs_version": mcgs_version,
        "protocol": protocol,
        "plc_program_key": plc_program_key,
        "variables": variables,
    }
    script = build_script(scada_ctx)

    return {
        "name": name,
        "mcgs_version": mcgs_version,
        "protocol": protocol,
        "plc_program_key": plc_program_key,
        "variables": variables,
        "device_channels_csv": csv_text,
        "screen_design": screen_design,
        "script": script,
        "protocol_note": protocol_note,
        "import_guide": IMPORT_GUIDE,
    }


def _build_protocol_note(mcgs_version, protocol, driver_info):
    """构建协议/驱动/接线说明文本。"""
    lines = []
    lines.append(f"MCGS 版本：{mcgs_version}")
    lines.append(f"通信协议：{protocol}")
    lines.append(f"驱动名称：{driver_info.get('driver_name', '—')}")
    lines.append(f"父设备：{driver_info.get('parent_device', '—')}")
    if protocol != "OPC":
        lines.append(
            f"串口参数：{driver_info.get('port', '—')} "
            f"{driver_info.get('baud', '—')} "
            f"{driver_info.get('data_bits', '—')}"
            f"{driver_info.get('stop_bits', '—')} "
            f"{driver_info.get('parity', '—')}"
        )
        lines.append(f"从站地址：{driver_info.get('station', '—')}")
    lines.append("")
    lines.append("接线说明：")
    lines.append(driver_info.get("wiring", "—"))
    lines.append("")
    if protocol == "Modbus":
        lines.append("Modbus 模式下，PLC 端需在程序中调用 Modbus 从站库指令：")
        lines.append("  - S7-200 SMART：MBUS_INIT / MBUS_SLAVE")
        lines.append("  - 地址映射规则：")
        lines.append("    I/M 区 → 0x 线圈，Q 区 → 1x 线圈，V 区 → 4x 保持寄存器")
        lines.append("    位地址偏移 = byte*8 + bit + 1")
        lines.append("    字地址偏移 = byte + 1")
    elif protocol == "OPC":
        lines.append("OPC 模式下，需先在 PC 上运行 OPC DA 服务器")
        lines.append("（如 Kepware / snap7 OPC server），MCGS 通用版设备窗口选 OPC 客户端，")
        lines.append("连接 OPC 服务器后在项列表中粘贴本工具生成的 OPC 项名。")
    return "\n".join(lines)


def _build_screen_design_text(variables):
    """构建画面设计书文本（Markdown）。"""
    lines = []
    lines.append("# 画面设计书")
    lines.append("")
    lines.append("## 控件布局建议")
    lines.append("")

    switch_vars = [v for v in variables if v.get("var_type") == "开关型"]
    number_vars = [v for v in variables if v.get("var_type") == "数值型"]

    if switch_vars:
        lines.append("### 开关量指示灯（绿色=运行，红色=故障）")
        lines.append("")
        lines.append("| 变量名 | 地址 | 说明 |")
        lines.append("|--------|------|------|")
        for v in switch_vars:
            lines.append(f"| {v['name']} | {v['address']} | {v.get('comment', '')} |")
        lines.append("")

    if number_vars:
        lines.append("### 数值显示控件（数值显示构件，绑定变量）")
        lines.append("")
        lines.append("| 变量名 | 地址 | 数据类型 | 说明 |")
        lines.append("|--------|------|----------|------|")
        for v in number_vars:
            lines.append(
                f"| {v['name']} | {v['address']} | {v.get('dtype', '')} | {v.get('comment', '')} |"
            )
        lines.append("")

    lines.append("### 操作按钮")
    lines.append("")
    lines.append("| 按钮 | 脚本动作 |")
    lines.append("|------|----------|")
    lines.append("| 启动 | 置位运行变量 |")
    lines.append("| 停止 | 复位运行变量 |")
    lines.append("| 报警应答 | 调用 变量名.AnswerAlm(-1) |")
    lines.append("| 存盘 | 调用 !SaveDataAll() |")
    lines.append("")

    lines.append("## 布局示意")
    lines.append("")
    lines.append("详见 SVG 示意图（导出包中的 screen_preview.svg），仅作布局参考。")
    lines.append("")
    return "\n".join(lines)


# ---------------- 渲染为对话文本 ----------------

def render_scada_for_chat(scada):
    """把 scada dict 渲染为 markdown 文本（用于对话中预览）。"""
    lines = []
    lines.append(f"# MCGS 组态方案：{scada['name']}")
    lines.append("")
    lines.append(f"- **MCGS 版本**：{scada['mcgs_version']}")
    lines.append(f"- **通信协议**：{scada['protocol']}")
    if scada.get("plc_program_key"):
        lines.append(f"- **关联 PLC 程序**：{scada['plc_program_key']}")
    lines.append("")

    # 变量字典
    lines.append("## 变量字典（数据对象）")
    lines.append("")
    lines.append("| 变量名 | 类型 | 地址 | 数据类型 | 读写 | 说明 |")
    lines.append("|--------|------|------|----------|------|------|")
    for v in scada["variables"]:
        rw = "只读" if v.get("read_only") else "读写"
        lines.append(
            f"| {v['name']} | {v.get('var_type','')} | {v['address']} | "
            f"{v.get('dtype','')} | {rw} | {v.get('comment','')} |"
        )
    lines.append("")
    lines.append(
        f"> 共 {len(scada['variables'])} 个变量。"
        "MCGS 无文本导入接口，请在「实时数据库」窗口按字典逐条新建数据对象。"
    )
    lines.append("")

    # 设备通道 CSV
    lines.append("## 设备通道 CSV（11 列，可一键导入）")
    lines.append("")
    csv_lines = scada["device_channels_csv"].splitlines()
    preview = csv_lines[:6]
    lines.append("```csv")
    lines.extend(preview)
    if len(csv_lines) > 6:
        lines.append(f"... 共 {len(csv_lines)-1} 行数据")
    lines.append("```")
    lines.append("")
    lines.append("> 导入方法：MCGS → 设备窗口 → 选中设备 → 右键 → 「设备信息导入」→ 选择 CSV")
    lines.append("")

    # 画面设计书
    lines.append("## 画面设计书")
    lines.append("")
    lines.append(scada["screen_design"]["design_text"])

    # 脚本
    lines.append("## McgsScript 脚本")
    lines.append("")
    lines.append("```basic")
    lines.append(scada["script"])
    lines.append("```")
    lines.append("")

    # 协议说明
    lines.append("## 协议与接线说明")
    lines.append("")
    lines.append("```")
    lines.append(scada["protocol_note"])
    lines.append("```")
    lines.append("")

    # 导入指南
    lines.append("## 导入指南")
    lines.append("")
    lines.append(scada["import_guide"])

    return "\n".join(lines)


# ---------------- 导出包 ----------------

def build_export_package(scada, target_dir):
    """把 scada dict 写成 7 个文件到 target_dir。

    文件清单：
    - 变量字典.csv
    - 设备通道.csv
    - 画面设计书.md
    - screen_preview.svg
    - 脚本.mcs
    - 协议说明.txt
    - README.txt
    """
    os.makedirs(target_dir, exist_ok=True)

    # 变量字典 CSV
    var_csv_path = os.path.join(target_dir, "变量字典.csv")
    with open(var_csv_path, "w", encoding="utf-8-sig", newline="") as f:
        f.write("变量名,类型,地址,数据类型,初值,读写,说明\n")
        for v in scada["variables"]:
            rw = "只读" if v.get("read_only") else "读写"
            row = [v["name"], v.get("var_type", ""), v["address"],
                   v.get("dtype", ""), v.get("init", "0"), rw, v.get("comment", "")]
            f.write(",".join('"' + str(c).replace('"', '""') + '"' for c in row) + "\n")

    # 设备通道 CSV（11 列）
    dev_csv_path = os.path.join(target_dir, "设备通道.csv")
    with open(dev_csv_path, "w", encoding="utf-8-sig", newline="") as f:
        f.write(scada["device_channels_csv"])

    # 画面设计书
    md_path = os.path.join(target_dir, "画面设计书.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(scada["screen_design"]["design_text"])

    # SVG 预览
    svg_path = os.path.join(target_dir, "screen_preview.svg")
    with open(svg_path, "w", encoding="utf-8") as f:
        f.write(scada["screen_design"]["svg_preview"])

    # 脚本
    script_path = os.path.join(target_dir, "脚本.mcs")
    with open(script_path, "w", encoding="utf-8") as f:
        f.write(scada["script"])

    # 协议说明
    proto_path = os.path.join(target_dir, "协议说明.txt")
    with open(proto_path, "w", encoding="utf-8") as f:
        f.write(scada["protocol_note"])

    # README
    readme_path = os.path.join(target_dir, "README.txt")
    with open(readme_path, "w", encoding="utf-8") as f:
        f.write(f"MCGS 组态素材包：{scada['name']}\n")
        f.write(f"版本：{scada['mcgs_version']}  协议：{scada['protocol']}\n")
        f.write("=" * 60 + "\n\n")
        f.write("本目录包含以下文件，按 README 顺序导入 MCGS：\n\n")
        f.write("1. 变量字典.csv — 数据对象定义（需在实时数据库手动建）\n")
        f.write("2. 设备通道.csv — 11 列通道表（设备窗口右键「设备信息导入」）\n")
        f.write("3. 画面设计书.md — 画面布局设计说明\n")
        f.write("4. screen_preview.svg — 画面布局示意（参考用，不可导入）\n")
        f.write("5. 脚本.mcs — McgsScript 脚本（粘贴到脚本编辑器）\n")
        f.write("6. 协议说明.txt — 驱动选型/串口参数/接线\n\n")
        f.write("导入详细步骤：\n")
        f.write(scada["import_guide"])

    return {
        "变量字典.csv": var_csv_path,
        "设备通道.csv": dev_csv_path,
        "画面设计书.md": md_path,
        "screen_preview.svg": svg_path,
        "脚本.mcs": script_path,
        "协议说明.txt": proto_path,
        "README.txt": readme_path,
    }
