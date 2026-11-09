"""Provider-aware JSON LLM adapter for Agents 1/2/3.

Supports DeepSeek, OpenRouter, OpenAI and SiliconFlow via OpenAI-compatible
chat completions endpoints. Template mode never instantiates this class.
CLI credentials come from environment/.env; UI explicitly disables that fallback.
Credentials never belong to artifacts or browser Workers.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


PROVIDERS = {
    "deepseek": {
        "key_env": "DEEPSEEK_API_KEY",
        "model_env": "DEEPSEEK_MODEL",
        "default_model": "deepseek-flash",
        "base_url": "https://api.deepseek.com",
    },
    "openrouter": {
        "key_env": "OPENROUTER_API_KEY",
        "model_env": "OPENROUTER_MODEL",
        "default_model": None,
        "base_url": "https://openrouter.ai/api/v1",
    },
    "openai": {
        "key_env": "OPENAI_API_KEY",
        "model_env": "OPENAI_MODEL",
        "default_model": "gpt-4.1-mini",
        "base_url": None,
    },
    "siliconflow": {
        "key_env": "SILICONFLOW_API_KEY", "model_env": "SILICONFLOW_MODEL",
        "default_model": "Qwen/Qwen2.5-7B-Instruct",
        "base_url": "https://api.siliconflow.cn/v1",
    },
}


def public_config(provider=None, model=None, base_url='', json_mode='auto'):
    """Validate only non-secret UI settings. Never resolve credentials here."""
    if provider not in PROVIDERS:
        raise ValueError('请选择支持的 AI 服务商')
    if not isinstance(model, str) or not model.strip() or len(model) > 160:
        raise ValueError('请填写有效的模型 ID')
    if json_mode not in {'auto', 'on', 'off'}:
        raise ValueError('JSON 模式只能为 auto / on / off')
    base_url = (base_url or '').strip()
    if base_url:
        parts = urlsplit(base_url)
        if parts.scheme not in {'http', 'https'} or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError('API Base URL 须为不含凭据、查询参数的 HTTP(S) 接口地址')
    return dict(provider=provider, model=model.strip(), base_url=base_url, json_mode=json_mode)


def safe_model_error(exc):
    """SDK bodies/URLs can contain secrets; expose only classified diagnostics."""
    code = getattr(exc, 'status_code', None)
    if code in {401, 403}: return '模型服务拒绝认证或授权，请检查 API Key 与账户权限。'
    if code == 404: return '模型或接口地址不存在，请检查模型 ID 和 API Base URL。'
    if code in {400, 422}: return '模型拒绝请求，请检查模型 ID、JSON 模式及接口兼容性。'
    if code == 429: return '模型服务限流或余额不足，请检查账户后再运行。'
    if 'Timeout' in type(exc).__name__: return '模型请求超时，请检查服务连接。'
    if 'Connection' in type(exc).__name__: return '无法连接模型服务，请检查接口地址和网络。'
    return '模型连接或输出校验失败，请检查配置及 JSON 兼容性（' + type(exc).__name__ + '）。'


def _load_project_dotenv() -> None:
    """Make .env work without imposing a new requirement on template mode."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(dotenv_path=Path(__file__).resolve().parent.parent / ".env", override=False)


class StructuredLLM:
    """JSON-only model interface. Fake models can implement complete_json for tests.

    The API and model are selected once for the whole A1/A2/A3 pipeline.
    OpenRouter supports many downstream models, so JSON Object mode is opt-in
    unless LLM_JSON_MODE=on. DeepSeek/OpenAI use JSON Object mode by default.
    """

    def __init__(self, *, provider: str | None = None, model: str | None = None,
                 api_key: str | None = None, base_url: str | None = None,
                 json_mode: str | None = None, use_environment: bool = True) -> None:
        if use_environment:
            _load_project_dotenv()
        env = os.getenv if use_environment else lambda name, default=None: default
        name = (provider or env("LLM_PROVIDER") or "deepseek").strip().lower()
        if name not in PROVIDERS:
            raise ValueError("未知 LLM_PROVIDER：%s；可选 deepseek / openrouter / openai / siliconflow" % name)
        config = PROVIDERS[name]
        api_key = (api_key if api_key is not None else env(config["key_env"], "")).strip()
        if not api_key:
            raise ValueError(
                f"{name} 模式需要 {config['key_env']}；请在 .env 或当前终端设置，"
                "也可以使用 --mode template（无需 API）"
            )

        chosen_model = (
            (model or "").strip()
            or env(config["model_env"], "").strip()
            or env("LLM_MODEL", "").strip()
            or config["default_model"]
        )
        if not chosen_model:
            raise ValueError(
                "OpenRouter 需要指定模型：--model provider/model，或设置 OPENROUTER_MODEL；"
                "可在 https://openrouter.ai/models 查询可用标识"
            )
        self.provider = name
        self.model = chosen_model

        json_mode = (json_mode or env("LLM_JSON_MODE", "auto")).strip().lower()
        if json_mode not in {"auto", "on", "off"}:
            raise ValueError("LLM_JSON_MODE 只能为 auto / on / off")
        self.json_mode = (name not in {"openrouter", "siliconflow"}) if json_mode == "auto" else (json_mode == "on")

        from openai import OpenAI
        kwargs: dict[str, Any] = {"api_key": api_key, "timeout": 60, "max_retries": 1}
        chosen_base = config["base_url"]
        if name == "openai" and use_environment:
            # The original v0.2 supported user-defined OpenAI-compatible gateways.
            chosen_base = env("OPENAI_BASE_URL", "").strip() or None
        chosen_base = base_url or chosen_base
        if not use_environment and not chosen_base:
            chosen_base = 'https://api.openai.com/v1'
        public_config(name, chosen_model, chosen_base, json_mode)
        if chosen_base:
            kwargs["base_url"] = chosen_base
        if name == "openrouter" and use_environment:
            headers = {}
            if os.getenv("OPENROUTER_APP_URL", "").strip():
                headers["HTTP-Referer"] = os.environ["OPENROUTER_APP_URL"].strip()
            if os.getenv("OPENROUTER_APP_NAME", "").strip():
                headers["X-Title"] = os.environ["OPENROUTER_APP_NAME"].strip()
            if headers:
                kwargs["default_headers"] = headers
        self._client = OpenAI(**kwargs)
        self.calls = []

    def close(self):
        close = getattr(self._client, 'close', None)
        if close: close()

    def complete_json(self, system: str, user: str) -> dict[str, Any]:
        # Every existing Agent prompt requires JSON; restating the constraint
        # also satisfies the DeepSeek JSON Object mode prompt requirement.
        messages = [
            {"role": "system", "content": system + "\nReturn exactly one valid JSON object, with no markdown or extra text."},
            {"role": "user", "content": user},
        ]
        kwargs: dict[str, Any] = {"model": self.model, "messages": messages}
        if self.json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        # Some OpenRouter downstream models do not support temperature=0.
        if self.provider != "openrouter":
            kwargs["temperature"] = 0
        import time
        started = time.monotonic()
        result = self._client.chat.completions.create(**kwargs)
        usage = getattr(result, 'usage', None)
        self.calls.append({'provider': self.provider, 'model': self.model,
                           'response_id': getattr(result, 'id', None),
                           'elapsed_seconds': round(time.monotonic()-started,3),
                           'prompt_tokens': getattr(usage, 'prompt_tokens', None),
                           'completion_tokens': getattr(usage, 'completion_tokens', None)})
        if not result.choices:
            raise ValueError("模型没有返回候选结果")
        choice = result.choices[0]
        if getattr(choice, "finish_reason", None) == "length":
            raise ValueError("模型 JSON 输出被截断；请更换模型或调整输出限制")
        raw = (choice.message.content or "").strip()
        if not raw:
            raise ValueError("模型返回空文本，无法获得有效 JSON；可尝试其他模型或关闭 JSON 模式")
        # Models on OpenRouter sometimes wrap a JSON response in code fences.
        # This only strips the complete surrounding fence; no free-form extraction.
        if not self.json_mode and raw.startswith("```") and raw.endswith("```"):
            lines = raw.splitlines()
            if len(lines) >= 3 and lines[0].lower() in {"```", "```json"} and lines[-1] == "```":
                raw = "\n".join(lines[1:-1]).strip()
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as err:
            raise ValueError(
                f"{self.provider} 模型没有返回有效 JSON；可选支持 JSON 的模型，"
                "或将 LLM_JSON_MODE=on（需模型支持）"
            ) from err
        if not isinstance(value, dict):
            raise ValueError("模型输出必须为 JSON 对象")
        return value
