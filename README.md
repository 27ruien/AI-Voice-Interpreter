# AI Voice Interpreter

AI Voice Interpreter 是面向 macOS 的单向中文到英文实时语音翻译 MVP。默认链路由服务器连接 Qwen3.5 LiveTranslate：Mac 将 16 kHz 单声道 PCM 按 100 ms 分块发送到 Gateway，模型自动检测语音起止，Gateway 把实时中文字幕、英文翻译和解码后的 Binary PCM 返回 Mac 边收边播。

> 当前版本仍是单向中文到英文的实时语音翻译 MVP。LiveTranslate 会自动检测语音起止并输出翻译文本和音频，但当前产品不是双向、全双工会议同传；建议使用耳机，尚未实现声学回声消除。

## 当前架构

默认 Streaming Pipeline：

```text
Mac PCM 16 kHz/mono/S16LE
→ HTTPS/WSS Gateway
→ qwen3.5-livetranslate-flash-realtime
→ 源语言字幕 + 英文翻译 + Base64 PCM
→ Gateway 解码为 Binary PCM
→ Mac 24 kHz/mono/S16LE 边收边播
```

模块化回退 Pipeline 保留：

```text
Mac PCM
→ Gateway WebRTC VAD
→ paraformer-realtime-v2
→ qwen-mt-flash
→ cosyvoice-v3-flash
→ Binary PCM
→ Mac 播放
```

HTTP One-shot Fallback 保留：

```text
Mac 完整 WAV → POST /v1/interpret → Paraformer → Qwen-MT → CosyVoice
→ 下载完整 WAV → /usr/bin/afplay
```

LiveTranslate 在连接和 `session.update` 尚未成功、也尚未输出音频时，Gateway 最多自动切换一次 modular。已播放任何 LiveTranslate 音频后不会自动切换并重播。其他流式失败仍遵循客户端现有规则：只有尚未播放流式音频时才可使用 HTTP Fallback；已播放部分音频后不会重放完整句子。

DashScope API Key 和 Workspace ID 仅位于服务器 `/srv/ai-voice-interpreter/server/.env`。Mac Remote 模式只保存 Gateway Token，不需要也不应配置 DashScope API Key。

## Mac 安装

要求 macOS、Python 3.11–3.14、麦克风和扬声器。

```bash
git clone https://github.com/27ruien/AI-Voice-Interpreter.git
cd AI-Voice-Interpreter
cp .env.example .env
make setup
```

Mac `.env` 的产品配置：

```dotenv
APP_MODE=real
INTERPRETER_MODE=remote_stream
AI_GATEWAY_BASE_URL=https://gridworks.cn/tool/ai-interpreter-api
AI_GATEWAY_TOKEN=
DASHSCOPE_API_KEY=

ASR_MODEL=paraformer-realtime-v2
TRANSLATION_MODEL=qwen-mt-flash
TTS_MODEL=cosyvoice-v3-flash
TTS_VOICE=
CLONED_VOICE_ID=

STREAM_AUDIO_CHUNK_MS=100
STREAM_SEND_QUEUE_MAX_CHUNKS=50
STREAM_RING_BUFFER_SECONDS=30
STREAM_PLAYBACK_PREBUFFER_MS=150
STREAM_PLAYBACK_QUEUE_MAX_SECONDS=10
STREAM_PLAYBACK_SAVE_LAST_TURN=true
STREAM_CAPTURE_MODE=safe
STREAM_HTTP_FALLBACK=true
STREAM_VOICE_MODE=standard
```

`STREAM_VOICE_MODE` 允许 `standard` 或 `clone_once`。GUI 对应“标准音色”和“模仿我的音色（实验）”。声音复刻只能用于本人或已获授权的声音；`clone_once` 会在会话开始阶段提取一次音色，完成前可能使用默认音色过渡。用户音频不会因复刻而永久保存。

启动前诊断：

```bash
make doctor
```

Doctor 不调用收费模型。它检查 Python、macOS、依赖、配置、麦克风、`afplay`、临时目录、Gateway `/readyz`、默认 Pipeline、Streaming 可用性和 WSS TLS；它只显示 Key/Token 是否配置，不显示具体值。真实 Provider 权限必须使用单独的受控权限探测。

启动 GUI：

```bash
make run
```

Mock GUI：

```bash
make mock
```

Mock 不调用任何外部 API，也不能作为真实服务验收结果。

首次使用麦克风时，请在“系统设置 → 隐私与安全性 → 麦克风”允许 Terminal、iTerm 或 Python，随后完全退出并重新启动应用。

## GUI 操作

- “流式模式”默认使用“实时翻译（推荐）”；后端回退时显示“模块化回退”。
- “标准音色”使用服务器配置的 LiveTranslate 系统音色。
- “模仿我的音色（实验）”使用 `enable_voice_clone=true`、`voice=default`、`frequency=once`。
- Source transcription 不可用时显示“源语言字幕暂不可用”，翻译和音频可继续。
- Safe Mode 默认在播放时暂停麦克风以降低扬声器回灌；Headphones Mode 持续采集并强烈建议佩戴耳机。
- “停止同传”只在结束整个 Session 时发送 `session.finish`，自然停顿不会结束上游 Session。
- “按句模式”继续使用 HTTP One-shot。

## Gateway WebSocket 协议

公网地址：

```text
https://gridworks.cn/tool/ai-interpreter-api
→ wss://gridworks.cn/tool/ai-interpreter-api/v1/stream
```

Token 只放在 `Authorization: Bearer ...` Header，不放在 URL、日志或控制消息中。首条客户端消息是协议 `1.0` 的 `session.start`，随后 Binary Frame 为 16 kHz、单声道、16-bit little-endian PCM。客户端发送 `session.stop` 后，Gateway 在 LiveTranslate 模式发送上游 `session.finish` 并等待 `session.finished`。

兼容事件：

- `session.started` / `session.completed`
- `vad.speech_start` / `vad.speech_end`
- `asr.partial` / `asr.final`
- `translation.partial` / `translation.final`
- `tts.audio.start` / Binary PCM / `tts.audio.end`
- `turn.completed` / `warning` / `error` / `pong`

新增但向后兼容的事件：

- `provider.started` / `provider.changed`
- `source_transcription.unavailable`
- `voice_clone.status`
- `usage.updated`

`session.started` 返回 `pipeline_provider`、不敏感的上游模型名、上游 Session ID、Voice Mode 和由 `session.updated.output_audio_format` 得出的播放参数。Provider Request ID 不可获得时不会伪造；LiveTranslate 使用真实的 upstream session/response/item/event ID。

LiveTranslate 的 `text` 是已确认内容，`stash` 是可修订预测。Gateway 会覆盖预测、去除重复事件，并只发送一次 Final。`response.audio.delta` 的 Base64 音频在服务器校验并解码后才以 Binary PCM 下发；Base64 不会进入 Mac 协议或日志。

## 服务器配置

模板为 `server/.env.example`，实际文件为 `/srv/ai-voice-interpreter/server/.env`，权限必须为 `600`。Phase 2.1 配置：

```dotenv
STREAM_PIPELINE_PROVIDER=livetranslate
STREAM_PIPELINE_FALLBACK_PROVIDER=modular
ALLOW_STREAM_PIPELINE_OVERRIDE=false

LIVETRANSLATE_MODEL=qwen3.5-livetranslate-flash-realtime
LIVETRANSLATE_SOURCE_LANGUAGE=zh
LIVETRANSLATE_TARGET_LANGUAGE=en
LIVETRANSLATE_OUTPUT_MODALITIES=text,audio
LIVETRANSLATE_VOICE=Tina
LIVETRANSLATE_ENABLE_SOURCE_TRANSCRIPTION=true
LIVETRANSLATE_SOURCE_ASR_MODEL=qwen3-asr-flash-realtime
LIVETRANSLATE_SOURCE_TRANSCRIPTION_FALLBACK=none
LIVETRANSLATE_ENABLE_VOICE_CLONE=false
LIVETRANSLATE_VOICE_CLONE_FREQUENCY=once
LIVETRANSLATE_CONNECT_TIMEOUT_SECONDS=15
LIVETRANSLATE_SESSION_FINISH_TIMEOUT_SECONDS=20
LIVETRANSLATE_AUDIO_QUEUE_MAX_CHUNKS=100
LIVETRANSLATE_HOTWORDS_JSON={"项目进度":"project progress","交付计划":"delivery plan"}
```

上游地址由 `DASHSCOPE_WORKSPACE_ID` 构造：

```text
wss://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/api-ws/v1/realtime
?model=qwen3.5-livetranslate-flash-realtime
```

不要使用 `/api/v1` 或 `/compatible-mode/v1` 作为 LiveTranslate WebSocket 地址。TLS 验证始终开启。生产默认不允许客户端覆盖 Provider；非法 Provider 会使 `/readyz` 返回配置错误，不会静默选择其他链路。

`LIVETRANSLATE_HOTWORDS_JSON` 为空时不发送 `corpus`。当前默认只加入“项目进度 → project progress”和“交付计划 → delivery plan”。

现有 modular 和 HTTP 配置继续保留：`ASR_MODEL=paraformer-v2` 用于 HTTP 文件识别，`STREAM_ASR_MODEL=paraformer-realtime-v2` 用于 modular Streaming，翻译为 `qwen-mt-flash`，TTS 为 `cosyvoice-v3-flash`。不要用实时 ASR 模型覆盖 HTTP ASR 模型。

## Provider 权限探测

权限探测会使用服务器现有 Key/Workspace，实时发送一段约 1–2 秒中文 PCM，但不播放输出、不自动重试：

```bash
make provider-permission-smoke
```

该命令会产生一次真实 Provider Session，可能计费。它只输出成功状态、模型、错误 code/message 和上游 Session/Response/Event ID，不输出 API Key 或 Gateway Token。重复的账户权限错误出现后应停止真实调用。

## 真实 LiveTranslate Smoke

准备中等句测试音频：

```bash
say -v Tingting -o /tmp/ai-interpreter-livetranslate.aiff \
  "你好，我们今天主要讨论项目进度和下一步的交付计划。"
afconvert -f WAVE -d LEI16@16000 -c 1 \
  /tmp/ai-interpreter-livetranslate.aiff \
  /tmp/ai-interpreter-livetranslate.wav
```

系统音色真实测试：

```bash
make livetranslate-smoke \
  LIVETRANSLATE_SMOKE_FLAGS="--audio /tmp/ai-interpreter-livetranslate.wav --play --voice-mode standard"
```

只有系统音色成功后，才执行一次声音复刻测试：

```bash
make livetranslate-smoke \
  LIVETRANSLATE_SMOKE_FLAGS="--audio /tmp/ai-interpreter-livetranslate.wav --play --voice-mode clone-once"
```

入口还支持 `--microphone`、`--duration`、`--keep-files`、`--json-report`、`--max-turns` 和 `--no-source-transcription`。WAV 会按真实时间、100 ms 分块发送，Mac 不直连阿里云。默认输出报告位于被 Git 忽略的 `livetranslate-smoke-output/`。技术播放成功不等于主观音质或声音相似度已确认，最终听感需要用户本人确认。

## 自动化测试、Benchmark 与 Soak

自动化测试不调用收费 API：

```bash
make test
make lint
make server-test
make server-lint
make stream-test
make livetranslate-test
make doctor
git diff --check
```

现有无收费 Mock Benchmark：

```bash
make stream-benchmark
```

真实 Pipeline 对比报告只读取已存在的成功真实 Smoke，不发模型请求：

```bash
make pipeline-benchmark
```

输出位于被忽略的 `pipeline-benchmark-output/`。缺少真实样本时显示 `unavailable`；单样本不计算 P95；Mock 延迟不会混入真实对比。

30 分钟 Mock Soak：

```bash
make stream-soak
```

短时开发回归可用 `make stream-soak SOAK_MINUTES=0.1`，但不能代替正式 30 分钟记录。Mock、Benchmark 和 Soak 报告分别位于被忽略的输出目录。

## Health、Ready 与隐私

- `GET /healthz`：公开存活检查，不调用模型。
- `GET /readyz`：检查配置、临时目录、Streaming、默认/回退 Provider、Workspace、Source transcription 和 Voice clone，不调用模型。
- `WSS /v1/stream`：Bearer 鉴权的流式入口。
- `POST /v1/interpret`：Bearer 鉴权的 HTTP One-shot。
- `GET /v1/audio/{uuid}`：下载 HTTP TTS 临时 WAV。

`/readyz` 不返回 API Key、完整 Gateway Token、内部 Endpoint 或敏感 Header。INFO 日志只记录 ID、状态、长度、字节数、队列深度、使用量和延迟，不记录 API Key、Token、完整音频、Base64 音频或完整用户语音文本。

Mac Ring Buffer 默认只在内存保留最近 30 秒。HTTP Fallback WAV 用完即删；最后一轮重播 WAV 在退出时清理。服务器 Streaming 音频不落盘。`--keep-files` 只写入被忽略目录，使用者负责清理。

所有输入、输出和播放队列均有上限；达到 80% 记录 Warning，队列满会结束 Session。客户端断开会立即取消上游连接、Sender/Receiver Task 和 Queue，避免后台继续调用收费模型。

## Nginx 与部署

仓库中的 `deploy/nginx-websocket-map.conf` 定义 `$connection_upgrade`，`deploy/nginx-ai-interpreter.conf` 提供 WSS 和 HTTP prefix location。当前服务器实际配置位于 `/etc/nginx/snippets/ai-interpreter.conf`，由现有 HTTPS server block 引入。

只在现有配置不能工作时修改 Nginx。修改前先 `sudo nginx -T`，备份到 `/etc/nginx/backups/`，通过 `sudo nginx -t` 后只 reload；不得修改 ProjectAI location、证书或其他域名。

服务器部署：

```bash
cd /srv/ai-voice-interpreter
git pull --ff-only origin main
sudo docker compose -f server/compose.yaml build
sudo docker compose -f server/compose.yaml up -d
sudo docker compose -f server/compose.yaml ps
curl http://127.0.0.1:8100/healthz
curl http://127.0.0.1:8100/readyz
```

Compose 项目为 `ai-voice-interpreter`，容器为 `ai-voice-interpreter-gateway`，端口只绑定 `127.0.0.1:8100`。不要停止、重启或修改 `/srv/projectai`、`project-ai-os` 或 `127.0.0.1:3100`。

如果 LiveTranslate 部署失败而 modular/HTTP 正常，可只把服务器 `STREAM_PIPELINE_PROVIDER` 改为 `modular` 并重建 Gateway。不要 force push、不要删除 Git 历史、不要执行全局 Docker prune、不要重启 Docker daemon或服务器。

## Local Direct 与旧音色登记

`INTERPRETER_MODE=local` 仅供开发诊断，会让 Mac 直接调用 DashScope，不是产品默认，也不用于本轮 Remote 验收。现有 CosyVoice 预登记 CLI 只服务旧的 local/modular 开发链路：

```bash
.venv/bin/python -m ai_voice_interpreter.voice_enrollment \
  --audio-url "https://example.com/my-voice.wav" \
  --prefix myvoice \
  --language zh
```

LiveTranslate 的本轮声音复刻使用服务端实时 `clone_once`，不使用该 CLI，也不开发 Voice Profile、样本管理或数据库。
