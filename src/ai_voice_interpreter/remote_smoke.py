from __future__ import annotations

import argparse
import shutil
import subprocess
import tempfile
import wave
from pathlib import Path

from .config import AppConfig
from .exceptions import GatewayError
from .remote import GatewayClient


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="真实远程 Gateway 按句闭环测试（不使用 Mock）")
    parser.add_argument("--base-url")
    parser.add_argument("--token")
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--keep-files", action="store_true")
    parser.add_argument("--play", action="store_true")
    parser.add_argument("--verify-output", action="store_true")
    return parser


def run(args: argparse.Namespace) -> int:
    config = AppConfig.load()
    base_url = args.base_url or config.ai_gateway_base_url
    token = args.token or config.ai_gateway_token
    if not base_url or not token:
        print("FAIL: 缺少 Gateway 地址或 Token。")
        return 2
    if config.app_mode == "mock":
        print("FAIL: remote-smoke 不允许在 APP_MODE=mock 下运行。")
        return 2

    output_dir = (
        Path("remote-smoke-output")
        if args.keep_files
        else Path(tempfile.mkdtemp(prefix="aivi-remote-smoke-"))
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / "remote-tts.wav"
    try:
        client = GatewayClient(base_url, token, config.network_timeout_seconds)
        response = client.interpret(args.audio)
        downloaded = client.download_audio(response, destination)
        with wave.open(str(downloaded.path), "rb") as wav:
            audio_summary = (
                f"wav {wav.getframerate()}Hz {wav.getnchannels()}ch "
                f"{wav.getsampwidth() * 8}bit"
            )
        if args.verify_output:
            _verify_semantics(response.translated_text)
        print(f"PASS Gateway request_id: {response.request_id}")
        print(f"PASS ASR text: {response.recognized_text}")
        print(f"PASS Translation: {response.translated_text}")
        print(
            "PASS Latency: "
            f"upload+server={response.upload_and_processing_ms:.0f}ms "
            f"asr={response.latency.get('asr_ms', 0):.0f}ms "
            f"translation={response.latency.get('translation_ms', 0):.0f}ms "
            f"tts={response.latency.get('tts_ms', 0):.0f}ms "
            f"server_total={response.latency.get('total_ms', 0):.0f}ms "
            f"download={downloaded.download_ms:.0f}ms"
        )
        print(f"PASS Audio: {audio_summary}, {downloaded.size_bytes} bytes")
        print(f"PASS Provider request_ids: {response.provider_request_ids}")
        if args.play:
            completed = subprocess.run(
                ["/usr/bin/afplay", str(downloaded.path)],
                check=False,
                capture_output=True,
            )
            if completed.returncode != 0:
                raise GatewayError(f"afplay 播放失败，退出码 {completed.returncode}。")
            print("PASS afplay: 播放完成")
        if args.keep_files:
            print(f"PASS Audio kept: {downloaded.path.resolve()}")
        return 0
    except (GatewayError, OSError, wave.Error, ValueError) as exc:
        print(f"FAIL: {exc}")
        return 1
    finally:
        if not args.keep_files:
            shutil.rmtree(output_dir, ignore_errors=True)


def _verify_semantics(translated_text: str) -> None:
    normalized = translated_text.lower()
    groups = (
        ("today",),
        ("project progress", "progress of the project"),
        ("next delivery plan", "next delivery", "delivery plan"),
        ("discuss", "discussion"),
    )
    missing = [group[0] for group in groups if not any(term in normalized for term in group)]
    if missing:
        raise ValueError(f"英文译文语义校验未通过，缺少：{', '.join(missing)}")


def main() -> int:
    return run(build_parser().parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
