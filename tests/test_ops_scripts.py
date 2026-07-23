from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def test_status_script_reports_v5_services_not_streamlit_dashboard():
    status = _read("status.sh")

    assert "ALMANAC v5.0 状態確認" in status
    assert "uvicorn api.main:app" in status
    assert "next-server" in status
    assert "telegram_bot.py" in status
    assert "alert.py" in status
    assert "http://localhost:3000" in status
    assert "http://localhost:8000" in status
    assert "http://localhost:8501" not in status
    assert "bot_commands.py" not in status
    assert "log_streamlit.txt" not in status


def test_status_script_tails_live_fastapi_launchagent_log():
    status = _read("status.sh")

    assert "fastapi_log.txt" in status
    assert "logs/api.log" not in status


def test_start_script_does_not_advertise_start_v5_as_streamlit_launcher():
    start = _read("start.sh")

    assert "ダッシュボード: http://localhost:3000 (Next.js)" in start
    assert "API:          http://localhost:8000 (FastAPI)" in start
    assert "FastAPI手動起動: ./start_v5.sh" in start
    assert "Streamlit:    http://localhost:8501 (手動起動: ./start_v5.sh)" not in start


def test_readme_describes_current_fastapi_nextjs_stack():
    readme = _read("README.md")

    assert "Python, FastAPI, Next.js" in readme
    assert "./start_v5.sh" in readme
    assert "http://localhost:3000" in readme
    assert "Python, FastAPI, Streamlit" not in readme


def test_proposed_cron_and_launchagents_reference_existing_python_files():
    checked = []
    paths = [
        path
        for path in [ROOT / "crontab.proposed", *sorted((ROOT / "launchagents").glob("*.plist"))]
        if path.exists()
    ]
    for path in paths:
        text = path.read_text(encoding="utf-8")
        refs = sorted(set(re.findall(r"(?:venv/bin/python|python|python3)\s+([A-Za-z0-9_./-]+\.py)", text)))
        for ref in refs:
            checked.append((path.name, ref))
            assert (ROOT / ref).exists(), f"{path.name} references missing {ref}"

    assert checked
