from pathlib import Path

from ai_voice_interpreter.config import AppConfig
from ai_voice_interpreter.doctor import CheckLevel, collect_checks, format_report


def executable_afplay(tmp_path: Path) -> Path:
    path = tmp_path / "afplay"
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def test_doctor_reports_ready_without_calling_any_api(tmp_path: Path) -> None:
    config = AppConfig(app_mode="real", dashscope_api_key="fake-unit-test-key")
    imported: list[str] = []

    def fake_importer(name: str) -> object:
        imported.append(name)
        return object()

    report = collect_checks(
        config_loader=lambda: config,
        python_version=(3, 11, 9),
        platform_name="darwin",
        package_importer=fake_importer,
        microphone_probe=lambda: (True, "测试麦克风可用"),
        afplay_path=executable_afplay(tmp_path),
        temp_directory=tmp_path,
    )
    assert report.ready_for_real_mode
    assert imported
    assert all(check.level != CheckLevel.FAIL for check in report.checks)


def test_doctor_hides_key_and_warns_when_missing(tmp_path: Path) -> None:
    config = AppConfig(app_mode="real", dashscope_api_key="")
    report = collect_checks(
        config_loader=lambda: config,
        python_version=(3, 14, 2),
        platform_name="darwin",
        package_importer=lambda _name: object(),
        microphone_probe=lambda: (True, "测试麦克风可用"),
        afplay_path=executable_afplay(tmp_path),
        temp_directory=tmp_path,
    )
    output = format_report(report)
    assert not report.ready_for_real_mode
    assert "DASHSCOPE_API_KEY: 未配置" in output
    assert "CLONED_VOICE_ID: 未配置，使用系统音色" in output


def test_doctor_reports_missing_dependency_as_failure(tmp_path: Path) -> None:
    def importer(name: str) -> object:
        if name == "dashscope":
            raise ImportError(name)
        return object()

    report = collect_checks(
        config_loader=lambda: AppConfig(app_mode="real", dashscope_api_key="configured"),
        python_version=(3, 11, 0),
        platform_name="darwin",
        package_importer=importer,
        microphone_probe=lambda: (True, "测试麦克风可用"),
        afplay_path=executable_afplay(tmp_path),
        temp_directory=tmp_path,
    )
    package_check = next(check for check in report.checks if check.name == "Python packages")
    assert package_check.level == CheckLevel.FAIL
    assert not report.ready_for_real_mode
