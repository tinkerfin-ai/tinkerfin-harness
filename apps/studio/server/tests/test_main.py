import tinkerfin_studio.__main__ as server_entrypoint
from tinkerfin_studio.__main__ import parse_args


def test_server_arguments_keep_local_safe_defaults() -> None:
    """命令行缺省应仅监听本机并开启开发热重载"""

    options = parse_args([])

    assert options.host == "127.0.0.1"
    assert options.port == 8090
    assert options.reload is True
    assert options.graceful_shutdown_timeout_seconds == 10


def test_server_arguments_allow_container_runtime_values() -> None:
    """部署入口应支持显式监听地址、端口和关闭热重载"""

    options = parse_args(
        [
            "--host",
            "0.0.0.0",
            "--port",
            "9000",
            "--no-reload",
            "--graceful-shutdown-timeout-seconds",
            "30",
        ]
    )

    assert options.host == "0.0.0.0"
    assert options.port == 9000
    assert options.reload is False
    assert options.graceful_shutdown_timeout_seconds == 30


def test_main_keeps_file_logging_in_application_lifespan(tmp_path, monkeypatch) -> None:
    """父进程只配置控制台，文件日志由服务应用持有"""
    from tinkerfin_studio.config.settings import load_settings

    monkeypatch.setenv(
        "BUSINESS_DATABASE_URL", "mysql+asyncmy://studio:secret@db:3306/studio"
    )
    monkeypatch.setenv("COMPONENTS_DATABASE_URL", "mysql+asyncmy://u:p@db/components")
    monkeypatch.setenv("LOG_LEVEL", "INFO")
    monkeypatch.setenv("LOG_FILE_ENABLED", "true")
    monkeypatch.setenv("LOG_FILE_PATH", str(tmp_path / "logs/studio.log"))
    settings = load_settings(env_file=None)
    levels = []
    monkeypatch.setattr(server_entrypoint, "get_settings", lambda: settings)
    monkeypatch.setattr(server_entrypoint, "setup_console_logging", levels.append)
    monkeypatch.setattr(
        server_entrypoint.uvicorn, "run", lambda *_args, **_kwargs: None
    )
    server_entrypoint.main(["--no-reload"])
    assert levels == ["INFO"]
    assert not settings.log_file_path.exists()


def test_main_applies_bounded_graceful_shutdown(monkeypatch) -> None:
    """服务入口必须让长驻 SSE 在单次关闭信号后进入有界取消"""

    from tinkerfin_studio.config.settings import load_settings

    monkeypatch.setenv(
        "BUSINESS_DATABASE_URL", "mysql+asyncmy://studio:secret@db:3306/studio"
    )
    monkeypatch.setenv("COMPONENTS_DATABASE_URL", "mysql+asyncmy://u:p@db/components")
    monkeypatch.setattr(
        server_entrypoint, "get_settings", lambda: load_settings(env_file=None)
    )
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        server_entrypoint,
        "setup_console_logging",
        lambda _level: None,
    )
    monkeypatch.setattr(
        server_entrypoint.uvicorn,
        "run",
        lambda *_args, **kwargs: captured.update(kwargs),
    )

    server_entrypoint.main(["--no-reload", "--graceful-shutdown-timeout-seconds", "7"])

    assert captured["timeout_graceful_shutdown"] == 7
    assert captured["factory"] is True
