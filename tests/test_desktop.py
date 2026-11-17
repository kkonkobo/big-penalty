import socket
from pathlib import Path

from jobhunt import desktop


def test_default_data_dir_per_platform(tmp_path):
    assert desktop.default_data_dir("darwin", home=tmp_path, environ={}) == (
        tmp_path / "Library" / "Application Support" / "Big Penalty"
    )
    local = tmp_path / "Local"
    assert desktop.default_data_dir("win32", home=tmp_path, environ={"LOCALAPPDATA": str(local)}) == local / "Big Penalty"
    assert desktop.default_data_dir("win32", home=tmp_path, environ={}) == tmp_path / "AppData" / "Local" / "Big Penalty"
    assert desktop.default_data_dir("linux", home=tmp_path, environ={}) == tmp_path / ".local" / "share" / "big-penalty"


def test_parse_env_file_handles_comments_quotes_export_and_blanks():
    text = """
# comment
ANTHROPIC_API_KEY=
export OPENAI_API_KEY="sk-test"
JOBHUNT_HEADLESS = '1'
not a pair
URL=https://x.test/?a=b
"""
    assert desktop.parse_env_file(text) == {
        "OPENAI_API_KEY": "sk-test",
        "JOBHUNT_HEADLESS": "1",
        "URL": "https://x.test/?a=b",
    }


def test_prepare_environment_creates_template_and_loads_without_overriding(tmp_path):
    data = tmp_path / "data"
    environ = {"JOBHUNT_DATA_DIR": str(data), "OPENAI_API_KEY": "from-env"}
    assert desktop.prepare_environment(environ) == data
    env_file = data / ".env"
    assert env_file.read_text(encoding="utf-8") == desktop.ENV_TEMPLATE
    assert "ANTHROPIC_API_KEY" not in environ  # empty template values are not loaded

    env_file.write_text("ANTHROPIC_API_KEY=file-key\nOPENAI_API_KEY=file-other\n", encoding="utf-8")
    desktop.prepare_environment(environ)
    assert environ["ANTHROPIC_API_KEY"] == "file-key"
    assert environ["OPENAI_API_KEY"] == "from-env"


def test_prepare_environment_defaults_to_user_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(desktop, "default_data_dir", lambda environ: tmp_path / "Big Penalty")
    environ: dict[str, str] = {}
    desktop.prepare_environment(environ)
    assert environ["JOBHUNT_DATA_DIR"] == str(tmp_path / "Big Penalty")
    assert (tmp_path / "Big Penalty" / ".env").is_file()


def test_find_port_falls_back_when_preferred_is_busy():
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        taken = busy.getsockname()[1]
        port = desktop.find_port(taken)
    assert port != taken and port > 0


def test_server_serves_ui(tmp_path, monkeypatch):
    import urllib.request

    from jobhunt import db

    monkeypatch.setattr(db, "DB_PATH_OVERRIDE", tmp_path / "t.sqlite3", raising=False)
    port = desktop.find_port(0)
    server, thread = desktop.start_server(port)
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/static/style.css", timeout=10) as resp:
            assert resp.status == 200
    finally:
        desktop.stop_server(server, thread)
    assert not thread.is_alive()


def test_spec_and_launcher_exist():
    root = Path(__file__).resolve().parents[1]
    assert (root / "packaging" / "big_penalty.spec").is_file()
    assert (root / "packaging" / "launch.py").is_file()
