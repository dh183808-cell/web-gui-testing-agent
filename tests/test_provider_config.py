"""Offline provider configuration contract tests (no external API requests)."""
from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

import pytest

from web_agent import llm_adapter


@pytest.fixture
def fake_openai(monkeypatch):
    created = []

    class FakeCompletion:
        def __init__(self):
            self.calls = []
            self.answer = '{"keyword":"GUI Agent"}'
            self.finish_reason = "stop"

        def create(self, **kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(
                finish_reason=self.finish_reason,
                message=SimpleNamespace(content=self.answer)
            )])

    class FakeSDK:
        def __init__(self, **kwargs):
            self.connection_settings = kwargs
            self.completions = FakeCompletion()
            self.chat = SimpleNamespace(completions=self.completions)
            created.append(self)

    module = ModuleType("openai")
    module.OpenAI = FakeSDK
    monkeypatch.setitem(sys.modules, "openai", module)
    monkeypatch.setattr(llm_adapter, "_load_project_dotenv", lambda: None)
    for name in ("LLM_PROVIDER", "LLM_MODEL", "LLM_JSON_MODE", "DEEPSEEK_API_KEY",
                 "DEEPSEEK_MODEL", "OPENROUTER_API_KEY", "OPENROUTER_MODEL",
                 "OPENAI_API_KEY", "OPENAI_MODEL", "OPENAI_BASE_URL",
                 "OPENROUTER_APP_URL", "OPENROUTER_APP_NAME"):
        monkeypatch.delenv(name, raising=False)
    return created


def test_deepseek_defaults_and_json_mode(monkeypatch, fake_openai):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-deepseek-secret")
    llm = llm_adapter.StructuredLLM(provider="deepseek")
    assert llm.provider == "deepseek"
    assert llm.model == "deepseek-flash"
    assert llm.json_mode is True
    assert fake_openai[0].connection_settings["base_url"] == "https://api.deepseek.com"
    assert fake_openai[0].connection_settings["api_key"] == "fake-deepseek-secret"
    assert llm.complete_json("Return JSON", "test") == {"keyword": "GUI Agent"}
    calls = fake_openai[0].completions.calls
    assert calls[0]["model"] == "deepseek-flash"
    assert calls[0]["response_format"] == {"type": "json_object"}


def test_openrouter_explicit_model_and_prompt_json(monkeypatch, fake_openai):
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake-openrouter-secret")
    monkeypatch.setenv("OPENROUTER_APP_NAME", "Internal Demo")
    llm = llm_adapter.StructuredLLM(provider="openrouter", model="vendor/example-model")
    assert llm.model == "vendor/example-model"
    assert llm.json_mode is False
    sdk = fake_openai[0]
    assert sdk.connection_settings["base_url"] == "https://openrouter.ai/api/v1"
    assert sdk.connection_settings["default_headers"] == {"X-Title": "Internal Demo"}
    sdk.completions.answer = '```json\n{"approved":true}\n```'
    assert llm.complete_json("Use JSON", "test") == {"approved": True}
    call = sdk.completions.calls[0]
    assert "response_format" not in call
    assert "temperature" not in call


def test_openrouter_model_from_environment_and_json_mode_on(monkeypatch, fake_openai):
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake-openrouter-secret")
    monkeypatch.setenv("OPENROUTER_MODEL", "vendor/valid-model")
    monkeypatch.setenv("LLM_JSON_MODE", "on")
    llm = llm_adapter.StructuredLLM(provider="openrouter")
    assert llm.complete_json("hello", "hello") == {"keyword": "GUI Agent"}
    assert fake_openai[0].completions.calls[0]["response_format"] == {"type": "json_object"}


def test_provider_selection_from_environment(monkeypatch, fake_openai):
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake-key")
    monkeypatch.setenv("OPENROUTER_MODEL", "vendor/model")
    llm = llm_adapter.StructuredLLM()
    assert llm.provider == "openrouter"


def test_openai_backwards_compatibility(monkeypatch, fake_openai):
    monkeypatch.setenv("OPENAI_API_KEY", "fake-openai-secret")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://custom.example.test/v1")
    llm = llm_adapter.StructuredLLM(provider="openai")
    assert llm.model == "gpt-4.1-mini"
    assert fake_openai[0].connection_settings["base_url"] == "https://custom.example.test/v1"


def test_missing_secret_and_openrouter_model_fail_clearly(monkeypatch, fake_openai):
    with pytest.raises(ValueError, match="DEEPSEEK_API_KEY"):
        llm_adapter.StructuredLLM(provider="deepseek")
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake-key")
    with pytest.raises(ValueError, match="OPENROUTER_MODEL"):
        llm_adapter.StructuredLLM(provider="openrouter")


def test_invalid_response_does_not_claim_success(monkeypatch, fake_openai):
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake-key")
    llm = llm_adapter.StructuredLLM(provider="openrouter", model="vendor/model")
    fake_openai[0].completions.answer = "This is not JSON"
    with pytest.raises(ValueError, match="JSON"):
        llm.complete_json("JSON", "test")
    fake_openai[0].completions.answer = "[]"
    with pytest.raises(ValueError, match="JSON 对象"):
        llm.complete_json("JSON", "test")


def test_truncated_response_is_rejected(monkeypatch, fake_openai):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key")
    llm = llm_adapter.StructuredLLM(provider="deepseek")
    fake_openai[0].completions.finish_reason = "length"
    with pytest.raises(ValueError, match="截断"):
        llm.complete_json("JSON", "test")


@pytest.mark.parametrize('provider', llm_adapter.PROVIDERS)
def test_explicit_ui_client_ignores_all_developer_defaults(monkeypatch, fake_openai, provider):
    monkeypatch.setattr(llm_adapter, '_load_project_dotenv', lambda: pytest.fail('UI must not load dotenv'))
    monkeypatch.setenv('LLM_MODEL', 'wrong-model')
    monkeypatch.setenv('LLM_JSON_MODE', 'on')
    monkeypatch.setenv('OPENAI_BASE_URL', 'https://wrong.test')
    monkeypatch.setenv('OPENROUTER_APP_NAME', 'private attribution')
    llm = llm_adapter.StructuredLLM(provider=provider, model='chosen/model', api_key='synthetic-user',
                                    use_environment=False)
    sdk = fake_openai[0]
    assert sdk.connection_settings['api_key'] == 'synthetic-user'
    assert sdk.connection_settings['base_url'] == (llm_adapter.PROVIDERS[provider]['base_url'] or 'https://api.openai.com/v1')
    assert 'default_headers' not in sdk.connection_settings
    llm.complete_json('JSON', 'test')
    call = sdk.completions.calls[0]
    assert call['model'] == 'chosen/model'
    assert ('response_format' in call) == (provider in {'deepseek','openai'})
    assert 'synthetic-user' not in str(llm.calls)


def test_ui_empty_secret_never_uses_environment(monkeypatch, fake_openai):
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'synthetic-developer')
    for secret in ('', None):
        with pytest.raises(ValueError, match='DEEPSEEK_API_KEY'):
            llm_adapter.StructuredLLM(api_key=secret, use_environment=False)


@pytest.mark.parametrize('base', ['https://key@example.test/v1', 'https://example.test?key=secret', 'file:///tmp/model'])
def test_credential_urls_are_not_public_config(base):
    with pytest.raises(ValueError, match='Base URL'):
        llm_adapter.public_config('openai','custom',base)


def test_sdk_errors_have_clear_safe_diagnostics():
    error = RuntimeError('synthetic-secret in raw SDK body')
    for code, expected in [(401,'Key'),(404,'模型'),(400,'JSON'),(429,'限流')]:
        error.status_code = code
        text = llm_adapter.safe_model_error(error)
        assert expected in text and 'synthetic-secret' not in text
