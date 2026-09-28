<div align="center">

# STEP 7 AI Agent

**接入大语言模型的西门子 PLC 编程与通信桌面助手**

用自然语言描述控制需求，AI 自动生成经验证的 PLC 程序、绘制梯形图、读写 PLC 变量——
支持本地模型，数据可以不出本机。

[![Python 3.11+](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Platform Windows](https://img.shields.io/badge/Platform-Windows%2010%2F11-0078D4?logo=windows&logoColor=white)](#)
[![License: MIT](https://img.shields.io/badge/License-MIT-green)](LICENSE)
[![PySide6](https://img.shields.io/badge/GUI-PySide6-41CD52)](#)
[![Ollama](https://img.shields.io/badge/LLM-Ollama%20%7C%20OpenAI%20兼容-000000)](#)

</div>

---

## ✨ 功能特性

- 💬 **对话式编程**：自然语言描述需求即可生成完整程序，气泡式消息渲染、流式输出
- 🧠 **多模型协作**：可添加多个模型（Ollama 本地 + OpenAI 兼容云端混用），程序自动探测各模型的延迟、工具调用与编程能力，按任务智能路由——编程/设计交给「编程主力」，简单问答交给「快速应答」，请求失败自动回退
- 📚 **I/O 地址库预设**：在工具栏中预设每个元件的地址（如 启动按钮 = I0.0），AI 生成程序与组态时直接采用，杜绝地址编造
- 🔌 **真机连接**：S7-200 SMART、S7-200、S7-300/400、S7-1200/1500，读写 I/Q/M/V/T/C/DB 变量，支持 TIA `%` 前缀地址
- 🧪 **内置模拟 PLC**：无需任何硬件即可练习读写、验证程序（snap7 Server，预置产量/炉温/定时器/计数器演示数据）
- 📐 **梯形图自动渲染**：标准 STL 指令自动解析绘制（母线、触点、线圈、TON/CTU 功能框），支持多输出网络，一键导出 PNG
- 📤 **导出 `.awl`**：生成 Micro/WIN SMART 可直接导入的 AWL 文件（GBK 编码）
- 🔍 **模型连通性测试**：分级检测服务可达性 → 鉴权 → 模型存在性 → 极简推理，显示响应耗时
- 🛡️ **安全门禁**：默认只读；写入须勾选授权并逐条弹窗确认，写入/连接/监控全部后台执行，界面不卡顿
- 🎓 **工程 / 学习双模式**：工程模式直接给方案，学习模式引导提问、启发思考
- 🖼️ **组态参考图识别**：上传组态样式图片（如双罐液位系统图），视觉模型自动识别图中工业元件（水泵/水罐/阀门/液位仪表），归纳工艺后一键生成整套 MCGS 组态素材——需配置支持视觉的模型（GPT-4o / qwen-vl / llava 等）
- 🖥️ **MCGS 组态设计助手**：从 PLC 程序、工艺描述或参考图派生组态素材（变量字典 / 设备通道 CSV / 画面设计书 / McgsScript），支持 McgsPro / 嵌入版 / 通用版 × PPI / Modbus / OPC，可一键自动写入 MCGS（纯 ctypes 实现，零第三方 UI 自动化依赖）
- 🎯 **MCGS 智能检测与写入**：自动区分组态程序（McgsSetE/McgsSetPro）与模拟运行环境（CEEMU 等），误定位到运行环境时自动纠正到同目录组态程序；写入时自动新建工程或在已打开工程中操作，跨电脑即装即用
- 🎨 **统一视觉主题**：卡片式面板、主次分明的按钮体系、可滚动 PLC 面板
- 📋 **应用内日志查看器**：统一日志系统（文件 + 控制台 + 实时 UI），带级别过滤与颜色高亮，启动错误一目了然
- 📦 **单文件 exe**：PyInstaller 打包，拷贝到其他 Windows 电脑双击即用，免安装

## 🚀 快速开始

### 方式一：下载可执行文件（最简单）

1. 到 [Releases](https://github.com/lingtongxinyv/PLC-AI-Agent/releases) 下载 `Step7AIAgent.exe`
2. 双击运行（首次启动需解压，等待数秒）
3. 右侧 **PLC 面板 → 启动模拟PLC**，然后直接对话，例如：
   - 「用模板生成一个星三角降压启动程序，切换时间 8 秒」
   - 「读取 VW100 的产量值」
   - 「电机正反转怎么设计？」
   - 「做一套双罐液位控制的 MCGS 组态」
   - 点输入框旁「图片」上传组态样式图（如工艺流程图），让 AI 按图设计组态

> 单文件 exe 已内置全部运行依赖。使用本地模型需另行安装 [Ollama](https://ollama.com)；使用云端 API 需联网并填入 Key。

### 方式二：从源码运行

```powershell
# 需要 Python 3.11+
pip install -r requirements.txt
python main.py
```

核心依赖：[PySide6](https://www.qt.io/)（界面）、[python-snap7](https://pypi.org/project/python-snap7/)（纯 Python PLC 通信，无需 DLL）、[openai](https://pypi.org/project/openai/)（模型接口）。

### 方式三：构建项目内置运行时（免 Python 启动）

仓库不包含体积较大的 `runtime/`，可自行构建：

1. 下载 [python-3.11.9-embed-amd64.zip](https://mirrors.huaweicloud.com/python/3.11.9/python-3.11.9-embed-amd64.zip)，解压到项目 `runtime/` 目录
2. 编辑 `runtime/python311._pth`：加入 `Lib\site-packages` 并取消 `import site` 的注释
3. 下载 [get-pip.py](https://bootstrap.pypa.io/get-pip.py) 并执行：`runtime\python.exe get-pip.py`
4. 安装依赖：
   ```powershell
   runtime\python.exe -m pip install PySide6 python-snap7 openai --ignore-installed
   ```
5. 双击项目根目录的 **`启动.bat`** 即可运行

## 📖 完整使用文档

安装、配置、变量地址格式、故障排查等详细说明请见 **[使用文档.md](使用文档.md)**，内容包括：

| 章节 | 内容 |
|---|---|
| 配置 | Ollama / 云端 API 设置、多模型智能路由、PLC 连接参数、完整配置示例 |
| 功能 | 对话面板、PLC 面板、程序面板、I/O 地址库、多模型协作操作说明 |
| 地址格式 | S7-200 风格（I/Q/M/VB/VW/VD/T/C）与标准 S7 风格（DB/M） |
| 梯形图与 AWL | 支持自动转换的 STL 指令清单、Micro/WIN SMART 导入步骤 |
| 故障排查 | 启动闪退、模型连接、模型路由、PLC 通信、写入与打包常见问题 |

## 🧩 内置程序模板（9 个）

| 模板 key | 名称 | 语言 |
|---|---|---|
| `motor_latch` | 电机启保停控制 | STL |
| `motor_fwd_rev` | 电机正反转互锁控制 | STL |
| `star_delta` | 星三角降压启动 | STL |
| `flasher` | 可调闪烁电路 | STL |
| `traffic_light` | 十字路口交通灯 | STL |
| `conveyor` | 传送带物料计数与延时停机 | STL |
| `dual_belts` | 双皮带连锁启停 | STL |
| `shuttle` | 自动往返小车 | STL |
| `motor_scl` | 电机启保停 | SCL（S7-1200/1500） |

模板代码经验证，AI 会根据需求自动选择并参数化；非标需求则自动编写并支持标准 STL → 梯形图解析。

## 🏗️ 项目结构

```
.
├── main.py                    # 程序入口（软件渲染、全局异常钩子）
├── 启动.bat                   # 内置运行时一键启动
├── 测试启动.bat               # 调试启动（保留控制台输出）
├── requirements.txt
├── build_exe.ps1              # 一键打包脚本
├── app/
│   ├── core/
│   │   ├── config.py          # 配置管理
│   │   └── logger.py          # 统一日志（文件 + 控制台 + Qt 信号）
│   ├── plc/
│   │   ├── profiles.py        # 设备型号配置（4 类）
│   │   ├── address.py         # 变量地址解析
│   │   ├── driver.py          # snap7 真机驱动
│   │   └── mock.py            # 内置模拟 PLC
│   ├── programs/
│   │   ├── templates.py       # 模板库 + 内联代码解析
│   │   ├── ladder.py          # 梯形图模型/绘制 + .awl 导出
│   │   └── stl_parser.py      # STL → 梯形图自动解析
│   ├── scada/                 # MCGS 组态设计助手
│   │   ├── mcgs_knowledge.py  # MCGS 知识库（安装检测/exe 角色识别/语法/驱动/CSV 格式）
│   │   ├── mcgs_csv.py        # 11 列设备通道 CSV 构建器
│   │   ├── mcgs_script.py     # McgsScript 脚本生成
│   │   ├── mcgs_templates.py  # 素材派生主入口（含工艺关键词 I/O 推导）
│   │   └── mcgs_auto_writer.py# MCGS UI 自动化写入（新建/打开工程、设备通道导入）
│   ├── agent/
│   │   ├── prompts.py         # 双模式系统提示词（含 I/O 地址库注入）
│   │   ├── tools.py           # 工具定义与执行器
│   │   ├── connectivity.py    # 模型连通性测试
│   │   ├── probe.py           # 模型能力探测（延迟/工具调用/编程测验）与角色推断
│   │   └── assistant.py       # 对话核心（流式 + 工具循环 + 多模型路由 + 图片多模态）
│   └── ui/
│       ├── main_window.py     # 主窗口（STEP7 | MCGS | 日志 三 Tab）
│       ├── theme.py           # 统一视觉主题（设计令牌 + 全局 QSS）
│       └── workers.py         # 后台线程 Worker
└── 使用文档.md
```

## 🔧 打包 exe

```powershell
python -m PyInstaller --noconfirm --onefile --windowed `
    --name Step7AIAgent --collect-submodules snap7 main.py
# 或直接运行：
powershell -ExecutionPolicy Bypass -File .\build_exe.ps1
```

产物位于 `dist\Step7AIAgent.exe`。

## 🤝 参与贡献

欢迎提交 Issue 与 PR：反馈 Bug、分享 PLC 程序模板、改进 STL 解析器对更多指令的支持。
提交代码前请确认：默认只读门禁等安全逻辑未被绕过。

## 📄 开源协议

基于 [MIT License](LICENSE) 开源，可自由使用与修改。

## ⚠️ 免责声明

本软件用于 PLC 编程辅助与学习。**AI 生成的程序可能存在错误**：任何程序在下载到真实设备前，
必须经过编译、仿真验证并由专业人员人工复核（急停、互锁、过载保护等安全逻辑）。
急停、安全门等安全功能必须采用独立硬件安全回路，不能仅依赖本软件生成的程序。
因不当使用本软件造成的设备损坏或安全事故，作者不承担责任。
