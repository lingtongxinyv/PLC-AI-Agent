# -*- coding: utf-8 -*-
"""
Agent 对话核心。

- Ollama 本地模型与云端 OpenAI 兼容接口共用一套 openai SDK，仅配置不同；
- 支持工具调用（function calling）多轮循环；
- 最终回答以流式 token 返回，减少等待感。

事件回调 event(kind, *args)：
    ("status", text)       状态提示
    ("tool", name, args)   开始调用工具
    ("delta", text)        回答增量
    ("error", text)        错误提示
"""
import base64
import mimetypes
import os
from openai import OpenAI

from .prompts import build_system_prompt
from .probe import infer_roles
from .tools import TOOL_SPECS, ToolExecutor
from app.programs.templates import program_from_answer

_MAX_TOOL_ROUNDS = 8


def _image_to_data_url(path: str) -> str:
    """把本地图片编码为 data URL，供多模态消息使用。"""
    mime, _ = mimetypes.guess_type(path)
    if not mime or not mime.startswith("image/"):
        mime = "image/png"
    with open(path, "rb") as f:
        data = base64.b64encode(f.read()).decode("ascii")
    return f"data:{mime};base64,{data}"

# 任务分类关键词：命中任一 → 编程主力；否则 → 快速应答
_TASK_CODING_HINTS = (
    "程序", "编程", "梯形图", "模板", "生成", "写一", "设计", "STL", "SCL", "代码",
    "启保停", "正反转", "星三角", "交通灯", "传送带", "皮带", "小车", "闪烁", "互锁",
    "工艺", "组态", "MCGS", "导出", "AWL", "联锁", "控制逻辑", "画图", "流程",
)
# 涉及真实 PLC 数据的任务必须经支持工具调用的模型处理
_TASK_TOOL_HINTS = ("读取", "写入", "连接", "状态", "当前值", "扫描", "变量", "监控", "PLC")


class Assistant:
    def __init__(self, config: dict, executor: ToolExecutor):
        self.config = config
        self.executor = executor
        self.transcript = []  # 完整对话记录（含工具消息）

    def update_config(self, config: dict):
        self.config = config

    def reset(self):
        self.transcript = []

    def _build_client(self, entry=None):
        """构建 OpenAI 客户端。

        entry=None：旧单模型逻辑（llm.provider/ollama/cloud）；
        entry=模型条目：按该条目的 provider/base_url/model/api_key 构建。
        """
        llm = self.config["llm"]
        if entry is not None:
            provider = entry.get("provider", "ollama")
            base_url = entry.get("base_url", "").strip()
            api_key = entry.get("api_key", "").strip()
            if not base_url:
                # 未填地址时回退到旧配置中同 provider 的地址
                src = llm.get(provider)
                base_url = src.get("base_url", "") if isinstance(src, dict) else ""
            if provider == "cloud" and not api_key:
                raise RuntimeError(f"模型 {entry.get('model')} 未配置 API Key，请在设置中填写。")
            if not base_url:
                raise RuntimeError(f"模型 {entry.get('model')} 未配置接口地址，请在设置中填写。")
            return OpenAI(base_url=base_url, api_key=api_key or "ollama",
                          timeout=llm.get("timeout", 120)), entry.get("model", "")
        if llm["provider"] == "cloud":
            c = llm["cloud"]
            base_url, model, api_key = c["base_url"], c["model"], c.get("api_key", "")
            if not api_key:
                raise RuntimeError("未配置云端 API Key，请在“设置”中填写。")
        else:
            o = llm["ollama"]
            base_url, model, api_key = o["base_url"], o["model"], "ollama"
        return OpenAI(base_url=base_url, api_key=api_key, timeout=llm.get("timeout", 120)), model

    def _pick_model(self, user_text: str):
        """多模型智能路由：按任务特征选择本轮模型。

        返回 (模型条目|None, 路由原因)。None 表示不路由（单模型/开关关闭/无法区分强弱），
        使用旧单模型逻辑。
        """
        llm = self.config.get("llm", {})
        if not llm.get("routing"):
            return None, ""
        models = [m for m in llm.get("models", []) if m.get("model")]
        if len(models) < 2:
            return None, ""
        coding, fast = infer_roles(models)
        if coding is None or fast is None or coding is fast:
            # 无可区分的强弱模型时，退回延迟最低的可用模型，避免路由形同虚设
            usable = [m for m in models if m.get("ok")]
            if len(usable) < 2:
                return None, ""
            best = min(usable, key=lambda m: m.get("latency_ms", 0))
            return best, "按响应速度自动分配"

        text = user_text or ""
        if any(h in text for h in _TASK_CODING_HINTS) or len(text) > 60:
            return coding, f"编程/设计任务 → {coding.get('model')}"
        if any(h in text for h in _TASK_TOOL_HINTS) and not fast.get("tool_support", False):
            # 快模型不支持工具调用时，涉及真实数据的任务交给主力模型
            return coding, f"需调用工具 → {coding.get('model')}"
        return fast, f"简单问答/查询 → {fast.get('model')}"

    def chat(self, user_text: str, event, image_path: str = None):
        # 构造用户消息：有图片时使用多模态内容（文本 + 图片）
        if image_path:
            try:
                data_url = _image_to_data_url(image_path)
            except Exception as e:
                event("error", f"图片读取失败：{e}")
                return
            user_message = {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_text or "请根据这张组态参考图设计 MCGS 画面。"},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        else:
            user_message = {"role": "user", "content": user_text}
        self.transcript.append(user_message)

        entry, reason = self._pick_model(user_text)
        try:
            client, model = self._build_client(entry)
        except RuntimeError as e:
            event("error", str(e))
            return
        if entry is not None:
            event("status", reason or f"本轮由 {model} 处理")
        if image_path:
            event("status", "正在分析组态参考图……")

        system_message = {
            "role": "system",
            "content": build_system_prompt(
                self.config.get("mode", "engineering"),
                self.executor.device_context(),
                self.executor.io_library_text(),
            ),
        }

        retried = False  # 路由模型请求失败时，允许回退一次
        for _round in range(_MAX_TOOL_ROUNDS):
            try:
                stream = client.chat.completions.create(
                    model=model,
                    messages=[system_message] + self.transcript,
                    tools=TOOL_SPECS,
                    tool_choice="auto",
                    temperature=float(self.config["llm"].get("temperature", 0.3)),
                    stream=True,
                )
            except Exception as e:
                others = [
                    m for m in self.config["llm"].get("models", [])
                    if m.get("model") and m is not entry
                ] if entry is not None and not retried else []
                if others:
                    retried = True
                    entry = others[0]
                    try:
                        client, model = self._build_client(entry)
                    except RuntimeError:
                        event("error", self._friendly_error(e))
                        return
                    event("status", f"请求失败，已回退到 {model} 重试……")
                    continue
                event("error", self._friendly_error(e))
                return

            content_parts = []
            # index -> {id, name, arguments}
            tool_parts = {}

            try:
                for chunk in stream:
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    if delta.content:
                        content_parts.append(delta.content)
                        event("delta", delta.content)
                    if getattr(delta, "tool_calls", None):
                        for piece in delta.tool_calls:
                            slot = tool_parts.setdefault(
                                piece.index,
                                {"id": "", "name": "", "arguments": ""},
                            )
                            if piece.id:
                                slot["id"] = piece.id
                            fn = piece.function
                            if fn and fn.name:
                                slot["name"] += fn.name
                            if fn and fn.arguments:
                                slot["arguments"] += fn.arguments
            except Exception as e:
                event("error", self._friendly_error(e))
                return

            # 无工具调用 → 本轮即最终回答
            if not tool_parts:
                answer = "".join(content_parts)
                self.transcript.append({"role": "assistant", "content": answer})
                # 兜底：模型未调用工具而在回答中直接给出程序时，同步到右侧程序面板
                candidate = program_from_answer(answer)
                if candidate is not None:
                    event("program", candidate)
                return

            # 记录带工具调用的 assistant 消息
            tool_calls = []
            for index in sorted(tool_parts):
                part = tool_parts[index]
                tool_calls.append(
                    {
                        "id": part["id"] or f"call_{_round}_{index}",
                        "type": "function",
                        "function": {
                            "name": part["name"],
                            "arguments": part["arguments"] or "{}",
                        },
                    }
                )
            self.transcript.append(
                {"role": "assistant", "content": "".join(content_parts), "tool_calls": tool_calls}
            )

            # 依次执行工具（作业串行，避免并发访问 PLC）
            for call in tool_calls:
                name = call["function"]["name"]
                args = call["function"]["arguments"]
                event("status", f"调用工具：{name} {args}")
                event("tool", name, args)
                try:
                    result = self.executor.dispatch(name, args)
                except Exception as e:  # 任何工具层漏网异常都不允许中断对话
                    result = f"工具执行出错：{e}"
                self.transcript.append(
                    {"role": "tool", "tool_call_id": call["id"], "content": result}
                )

        event("error", "工具调用轮次过多，已停止。请把需求描述得更具体一些。")

    @staticmethod
    def _friendly_error(e) -> str:
        text = str(e)
        lowered = text.lower()
        if any(k in lowered for k in ("image", "vision", "多模态", "图片",
                                       "does not support", "unsupported")):
            return (
                "当前模型不支持图片识别：请在「设置」中切换到支持视觉的模型"
                "（如 GPT-4o、qwen-vl、llava、deepseek-vl 等），"
                "或改用文字描述画面中的元件（水泵/水罐/阀门等）后再试。"
            )
        if "connection" in lowered or "connect" in lowered:
            return (
                "无法连接模型服务：请确认 Ollama 已启动（命令：ollama serve），"
                "或检查云端 API 地址与网络。"
            )
        if "authentication" in lowered or "api key" in lowered or "401" in lowered:
            return "模型鉴权失败：请检查 API Key 是否正确。"
        if "not found" in lowered and "model" in lowered:
            return "模型不存在：请检查模型名称，或先执行 ollama pull <模型名> 下载。"
        if "timeout" in lowered:
            return "模型响应超时，请稍后重试，或在设置中增大超时时间。"
        return f"模型调用出错：{text}"
