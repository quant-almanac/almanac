import os

import llm_adapters
from utils import load_environment_secrets


def test_load_environment_secrets_reads_export_file_without_overriding(tmp_path, monkeypatch):
    secrets = tmp_path / ".nexustrader_secrets"
    secrets.write_text(
        "\n".join(
            [
                "# local secrets",
                "export DEEPSEEK_API_KEY='from-file'",
                'DASHSCOPE_API_KEY="dashscope-file"',
                "EXISTING_KEY=file-value",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.setenv("EXISTING_KEY", "already-set")

    loaded = load_environment_secrets(paths=[secrets])

    assert os.environ["DEEPSEEK_API_KEY"] == "from-file"
    assert os.environ["DASHSCOPE_API_KEY"] == "dashscope-file"
    assert os.environ["EXISTING_KEY"] == "already-set"
    assert loaded == {"DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY"}


def test_deepseek_adapter_loads_secrets_before_reporting_missing_key(tmp_path, monkeypatch):
    secrets = tmp_path / ".almanac_secrets"
    secrets.write_text("export DEEPSEEK_API_KEY=from-file\n", encoding="utf-8")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("NEXUSTRADER_SECRETS_FILE", raising=False)
    monkeypatch.setenv("ALMANAC_SECRETS_FILE", str(secrets))

    seen = {}

    def fake_retry(**kwargs):
        seen.update(kwargs)
        return {"content": "{}", "adapter": "deepseek", "model": kwargs["model"]}

    monkeypatch.setattr(llm_adapters, "_retry_openai_compat", fake_retry)

    result = llm_adapters.call_deepseek("system", "user", model="deepseek-test")

    assert "error" not in result
    assert seen["api_key"] == "from-file"


def test_legacy_secrets_file_override_still_loads(tmp_path, monkeypatch):
    secrets = tmp_path / ".nexustrader_secrets"
    secrets.write_text("export DEEPSEEK_API_KEY=legacy-file\n", encoding="utf-8")
    monkeypatch.delenv("ALMANAC_SECRETS_FILE", raising=False)
    monkeypatch.setenv("NEXUSTRADER_SECRETS_FILE", str(secrets))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    loaded = load_environment_secrets()

    assert loaded == {"DEEPSEEK_API_KEY"}
    assert os.environ["DEEPSEEK_API_KEY"] == "legacy-file"
