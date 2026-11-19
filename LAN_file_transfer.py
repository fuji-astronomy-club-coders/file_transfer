"""LAN/Wi-Fiおよびイーサネット直結でファイルを転送する Flask アプリケーション。

受信側PCで起動し、送信側PCのブラウザーからアクセスする。ファイルのバイト列、
ブラウザーから取得できる最終更新日時、フォルダー相対パスを維持する。

設定ファイル file_transfar_config.yaml は初回起動時に自動生成される。
"""
from __future__ import annotations

import datetime
import hmac
import ipaddress
import logging
import math
import os
import platform
import re
import secrets
import shutil
import socket
import subprocess
import threading
import time
from functools import wraps
from pathlib import Path
from typing import Any, Callable, TypeVar

import yaml
from flask import Flask, jsonify, render_template_string, request
from werkzeug.exceptions import RequestEntityTooLarge

# --- Logging ---
LOG_DIRECTORY = Path("logs")
LOG_DIRECTORY.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIRECTORY / f"transfer_server_{datetime.datetime.now():%Y%m%d_%H%M%S}.log"
file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
file_handler.setLevel(logging.DEBUG)
stream_handler = logging.StreamHandler()
stream_handler.setLevel(logging.INFO)
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.DEBUG,
    handlers=[file_handler, stream_handler],
)
logger = logging.getLogger(__name__)

# Keep this historical filename for compatibility with existing installations.
CONFIG_FILE = Path("file_transfar_config.yaml")
DEFAULT_CONFIG: dict[str, Any] = {
    "RETRY_LIM": 3,
    "CHUNK_SIZE": 5 * 1024 * 1024,
    "UPLOAD_FOLDER": "uploads",
    "TEMP_FOLDER": "temp_uploads",
    "HOST": "0.0.0.0",
    "PORT": 5000,
    "DIRECT_ETHERNET_IP": "192.168.50.1",
    "DIRECT_ETHERNET_SUBNET_MASK": "255.255.255.0",
    "ACCESS_TOKEN": "",
}


def _write_config(config: dict[str, Any]) -> None:
    """Persist the config mapping as UTF-8 YAML."""
    with CONFIG_FILE.open("w", encoding="utf-8") as stream:
        yaml.safe_dump(config, stream, allow_unicode=True, sort_keys=False)


def _read_config() -> dict[str, Any]:
    """Load settings, creating a config file on first run."""
    if not CONFIG_FILE.exists():
        initial = DEFAULT_CONFIG.copy()
        initial["ACCESS_TOKEN"] = secrets.token_urlsafe(32)
        try:
            _write_config(initial)
            logger.info("設定ファイル %s を生成しました。", CONFIG_FILE)
        except OSError as exc:
            logger.error("設定ファイルを生成できません: %s", exc)
        return initial
    try:
        with CONFIG_FILE.open("r", encoding="utf-8") as stream:
            loaded = yaml.safe_load(stream)
        if loaded is None:
            loaded = {}
        if not isinstance(loaded, dict):
            raise ValueError("設定ファイルの最上位はキーと値のマッピングである必要があります")
        merged = DEFAULT_CONFIG.copy()
        merged.update(loaded)
        logger.info("設定ファイル %s を読み込みました。", CONFIG_FILE)
        return merged
    except (OSError, yaml.YAMLError, ValueError) as exc:
        logger.error("設定ファイルを読み込めないため、既定値を使用します: %s", exc)
        return DEFAULT_CONFIG.copy()


def _int_setting(config: dict[str, Any], key: str, default: int, minimum: int, maximum: int) -> int:
    """Return an integer config value only when it is in range."""
    try:
        value = int(config.get(key, default))
    except (TypeError, ValueError):
        logger.warning("設定 %s が整数ではないため、既定値 %s を使用します。", key, default)
        return default
    if not minimum <= value <= maximum:
        logger.warning("設定 %s が許容範囲外のため、既定値 %s を使用します。", key, default)
        return default
    return value


def _ipv4_setting(config: dict[str, Any], key: str, default: str) -> str:
    """Return a validated IPv4 address from settings."""
    try:
        return str(ipaddress.IPv4Address(str(config.get(key, default))))
    except ipaddress.AddressValueError:
        logger.warning("設定 %s がIPv4アドレスではないため、%s を使用します。", key, default)
        return default


_config = _read_config()
RETRY_LIM = _int_setting(_config, "RETRY_LIM", 3, 0, 10)
CHUNK_SIZE = _int_setting(_config, "CHUNK_SIZE", 5 * 1024 * 1024, 1, 100 * 1024 * 1024)
PORT = _int_setting(_config, "PORT", 5000, 1, 65535)
HOST = str(_config.get("HOST", "0.0.0.0")).strip() or "0.0.0.0"
DIRECT_ETHERNET_IP = _ipv4_setting(_config, "DIRECT_ETHERNET_IP", "192.168.50.1")
DIRECT_ETHERNET_SUBNET_MASK = _ipv4_setting(
    _config, "DIRECT_ETHERNET_SUBNET_MASK", "255.255.255.0"
)

ACCESS_TOKEN = _config.get("ACCESS_TOKEN", "")
if not isinstance(ACCESS_TOKEN, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", ACCESS_TOKEN):
    ACCESS_TOKEN = secrets.token_urlsafe(32)
    _config["ACCESS_TOKEN"] = ACCESS_TOKEN
    try:
        _write_config(_config)
        logger.info("転送認証トークンを生成し、設定ファイルへ保存しました。")
    except OSError as exc:
        logger.error("認証トークンを設定ファイルへ保存できませんでした: %s", exc)


def _is_within(path: Path, parent: Path) -> bool:
    """Return True when path is parent or is below parent."""
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


_CURRENT_DIR = Path.cwd().resolve()
_upload_setting = Path(str(_config.get("UPLOAD_FOLDER") or "uploads")).expanduser()
_temp_setting = Path(str(_config.get("TEMP_FOLDER") or "temp_uploads")).expanduser()
if _upload_setting.is_symlink() or _temp_setting.is_symlink():
    logger.error("UPLOAD_FOLDER または TEMP_FOLDER がシンボリックリンクのため既定の保存先を使用します。")
    _upload_setting, _temp_setting = Path("uploads"), Path("temp_uploads")
UPLOAD_ROOT = _upload_setting.resolve()
TEMP_ROOT = _temp_setting.resolve()
_unsafe_roots = (
    UPLOAD_ROOT == TEMP_ROOT
    or _is_within(TEMP_ROOT, UPLOAD_ROOT)
    or _is_within(UPLOAD_ROOT, TEMP_ROOT)
    or UPLOAD_ROOT in {Path(UPLOAD_ROOT.anchor), _CURRENT_DIR}
    or TEMP_ROOT in {Path(TEMP_ROOT.anchor), _CURRENT_DIR}
    or _is_within(_CURRENT_DIR, UPLOAD_ROOT)
    or _is_within(_CURRENT_DIR, TEMP_ROOT)
)
if _unsafe_roots:
    logger.error("UPLOAD_FOLDER と TEMP_FOLDER の設定が危険または重複しているため既定値を使用します。")
    UPLOAD_ROOT = (_CURRENT_DIR / "uploads").resolve()
    TEMP_ROOT = (_CURRENT_DIR / "temp_uploads").resolve()
UPLOAD_FOLDER = str(UPLOAD_ROOT)
TEMP_FOLDER = str(TEMP_ROOT)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = CHUNK_SIZE + 2 * 1024 * 1024

# A lock protects each upload's temp-file operations and in-memory metadata.
_UPLOAD_LOCK = threading.RLock()
_UPLOAD_METADATA: dict[str, tuple[tuple[str, ...], int, int, int | None]] = {}
_COMPLETED_UPLOADS: dict[str, tuple[str, int, int | None]] = {}
_FIRST_REQUEST_LOGGED = False


def safe_relative_parts(relative_path: str) -> tuple[str, ...]:
    """Validate a browser-supplied relative path and return its path components.

    Raises
    ------
    ValueError
        If the path is absolute, traverses a parent directory, or contains names
        that are invalid on common Windows filesystems.
    """
    if not isinstance(relative_path, str) or not relative_path or "\x00" in relative_path:
        raise ValueError("空または無効なパスです")
    normalized = relative_path.replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        raise ValueError("絶対パスは指定できません")
    parts = normalized.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("相対パスに無効な階層が含まれています")
    reserved_names = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    reserved_names.update(f"COM{i}" for i in range(1, 10))
    reserved_names.update(f"LPT{i}" for i in range(1, 10))
    for part in parts:
        if len(part) > 255 or part.endswith((".", " ")):
            raise ValueError("ファイル名の長さまたは末尾文字が無効です")
        if any(ord(char) < 32 or char in '<>:"|?*' for char in part):
            raise ValueError("ファイル名に使用できない文字が含まれています")
        if part.split(".", 1)[0].upper() in reserved_names:
            raise ValueError("OS予約名は使用できません")
    return tuple(parts)


def safe_destination_path(parts: tuple[str, ...]) -> Path:
    """Resolve a destination and reject symlinks or paths outside UPLOAD_ROOT."""
    candidate = UPLOAD_ROOT.joinpath(*parts)
    current = UPLOAD_ROOT
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("保存先にシンボリックリンクは使用できません")
    destination = candidate.resolve(strict=False)
    if not _is_within(destination, UPLOAD_ROOT) or destination == UPLOAD_ROOT:
        raise ValueError("保存先がアップロードフォルダーの外側です")
    return destination


def _valid_upload_id(value: str) -> str:
    """Validate a browser-generated upload identifier for use as a temp filename."""
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise ValueError("アップロードIDが無効です")
    return value


def _remove_path(path: Path) -> None:
    """Delete a temporary file, directory, or symlink without following symlinks."""
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path)


def cleanup_temp_folder() -> None:
    """Clear stale temporary uploads at startup while retaining the temp root."""
    TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    try:
        for entry in TEMP_ROOT.iterdir():
            _remove_path(entry)
        logger.info("一時フォルダー %s を初期化しました。", TEMP_ROOT)
    except OSError as exc:
        logger.warning("一時フォルダーを完全に初期化できませんでした: %s", exc)


UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
cleanup_temp_folder()

F = TypeVar("F", bound=Callable[..., Any])


def require_access_token(view: F) -> F:
    """Require the configured transfer token on an API endpoint."""
    @wraps(view)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        supplied_token = request.headers.get("X-Transfer-Token", "")
        if not supplied_token or not supplied_token.isascii() or not hmac.compare_digest(supplied_token, ACCESS_TOKEN):
            return "認証トークンが無効です", 401
        return view(*args, **kwargs)
    return wrapped  # type: ignore[return-value]


def get_local_ipv4_addresses() -> list[dict[str, str]]:
    """Detect local non-loopback IPv4 addresses using OS commands."""
    addresses: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(adapter: str, address: str) -> None:
        try:
            ip = str(ipaddress.IPv4Address(address))
        except ipaddress.AddressValueError:
            return
        if ip.startswith("127.") or ip in seen:
            return
        seen.add(ip)
        addresses.append({"adapter": adapter or "ネットワーク", "ip": ip})

    if platform.system() == "Windows":
        try:
            result = subprocess.run(
                ["ipconfig"], capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=3, check=False,
            )
            adapter = "ネットワークアダプター"
            for line in result.stdout.splitlines():
                stripped = line.strip()
                if stripped and not line[:1].isspace() and stripped.endswith(":"):
                    adapter = stripped[:-1]
                match = re.search(r"IPv4[^:]*:\s*(\d{1,3}(?:\.\d{1,3}){3})", line, re.IGNORECASE)
                if match:
                    add(adapter, match.group(1))
        except (OSError, subprocess.SubprocessError) as exc:
            logger.debug("ipconfig によるIPアドレス検出に失敗しました: %s", exc)
    else:
        try:
            result = subprocess.run(
                ["ip", "-o", "-4", "addr", "show"], capture_output=True,
                text=True, timeout=3, check=False,
            )
            for line in result.stdout.splitlines():
                match = re.match(r"\d+:\s+([^\s:@]+).*?\sinet\s+(\d{1,3}(?:\.\d{1,3}){3})/", line)
                if match:
                    add(match.group(1), match.group(2))
        except (OSError, subprocess.SubprocessError) as exc:
            logger.debug("ip コマンドによるIPアドレス検出に失敗しました: %s", exc)
    if not addresses:
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                add(socket.gethostname(), info[4][0])
        except OSError as exc:
            logger.debug("ホスト名からのIPアドレス検出に失敗しました: %s", exc)
    return addresses


HTML_PAGE = """
<!DOCTYPE html>
<html lang="ja">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>ファイル一括アップロード</title>
    <style>
        body { font-family: sans-serif; padding: 20px; line-height: 1.6; }
        .input-group { margin-bottom: 15px; padding: 10px; border: 1px solid #ddd; background: #fdfdfd; max-width: 500px;}
        progress { width: 100%; max-width: 500px; height: 20px; margin-top: 5px; }
        .progress-container { display: none; margin-top: 20px; padding: 15px; border: 1px solid #ccc; max-width: 500px; background-color: #f9f9f9; }
        .progress-title { font-weight: bold; margin-bottom: 5px; }
        #status { margin-top: 10px; font-weight: bold; }
        #speedInfo { margin-top: 5px; color: #555; font-size: 0.9em; }
        button { padding: 8px 16px; margin-right: 5px; cursor: pointer; font-size: 14px; }
        button:disabled { cursor: not-allowed; opacity: 0.6; }
        
        /* キュー用のスタイル */
        #queueContainer { max-height: 250px; overflow-y: auto; }
        #queueTable { width: 100%; border-collapse: collapse; font-size: 13px; margin-top: 10px; }
        #queueTable th, #queueTable td { padding: 6px; border-bottom: 1px solid #ddd; text-align: left; }
        .btn-remove { padding: 3px 8px; font-size: 11px; color: white; background: #dc3545; border: none; border-radius: 3px; cursor: pointer; }
        .btn-remove:hover { background: #c82333; }
        .file-name-cell { max-width: 200px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .connection-panel { max-width: 760px; margin: 0 0 20px; padding: 14px 16px; border: 1px solid #cbd5e1; border-radius: 8px; background: #f8fafc; }
        .connection-panel h2 { margin: 0 0 8px; font-size: 18px; }
        .connection-panel select { padding: 7px; font-size: 14px; }
        .connection-details { margin-top: 12px; padding: 12px; border-left: 4px solid #2563eb; background: #fff; }
        .connection-details ol { margin-top: 8px; padding-left: 22px; }
        .connection-details code { overflow-wrap: anywhere; }
        .muted { color: #555; font-size: 0.92em; }
    </style>
</head>
<body>
    <h1>ファイルを送信</h1>

    <section class="connection-panel" aria-labelledby="connectionModeTitle">
        <h2 id="connectionModeTitle">接続方法</h2>
        <label for="connectionMode"><b>転送ネットワーク：</b></label>
        <select id="connectionMode">
            <option value="lan">通常のLAN / Wi-Fi</option>
            <option value="direct">PC同士をイーサネット直結</option>
        </select>
        <p class="muted">受信側（保存先PC）でこのアプリを起動し、送信側PCのブラウザーから受信側のURLを開きます。</p>
        <div id="directConnectionDetails" class="connection-details" hidden>
            <b>イーサネット直結の設定</b>
            <ol>
                <li>2台のPCをLANケーブルで直接接続します（ルーターは不要です）。</li>
                <li>受信側PCの Ethernet IPv4 を <code id="directServerIp">192.168.50.1</code>、サブネットマスクを <code id="directSubnetMask">255.255.255.0</code> に設定します。デフォルトゲートウェイとDNSは空欄で構いません。</li>
                <li>送信側PCの Ethernet IPv4 を <code>192.168.50.2</code>、サブネットマスクを <code id="directSubnetMaskSender">255.255.255.0</code> に設定します。IPアドレスは2台で重複させないでください。</li>
                <li>送信側PCで次の受信側URLを開きます：<strong><code id="directTransferUrl">http://192.168.50.1:5000</code></strong></li>
                <li>Windows ファイアウォールが通信を尋ねたら、信頼できるプライベートネットワーク上でのみ許可してください。</li>
            </ol>
            <div><b>このPCで検出したIPv4アドレス：</b> <span id="localAddresses">取得中...</span></div>
            <p class="muted">推奨IPが既存ネットワークと重複する場合は、設定ファイルの <code>DIRECT_ETHERNET_IP</code> と案内するIPを別の未使用サブネットに変更してください。直結モードはネットワーク設定を自動変更せず、既存のアップロード機能で転送します。</p>
        </div>
        <div class="connection-details">
            <label for="accessToken"><b>転送認証トークン：</b></label><br>
            <input id="accessToken" type="password" autocomplete="off" spellcheck="false" style="width:min(100%, 480px); padding:8px; box-sizing:border-box;" aria-describedby="accessTokenHelp">
            <p id="accessTokenHelp" class="muted">受信側PCのコンソール、または file_transfar_config.yaml の ACCESS_TOKEN を入力してください。トークンを知っている端末のみアップロード操作ができます。</p>
        </div>
    </section>

    <form id="uploadForm">
        <div class="input-group">
            <label><b>① ファイルを選択 (複数選択可)</b></label><br>
            <input type="file" id="fileInput" accept=".*" multiple>
        </div>
        <div class="input-group">
            <label><b>② フォルダごと選択</b></label><br>
            <input type="file" id="folderInput" webkitdirectory directory multiple>
            <p class="muted">フォルダー構造とファイルの最終更新日時を保持して転送します。</p>
        </div>
        
        <button type="submit" id="uploadBtn">アップロード開始</button>
        <button type="button" id="pauseBtn" style="display:none;">一時停止</button>
        <button type="button" id="cancelBtn" style="display:none;">キャンセル</button>
    </form>

    <!-- 全体の進捗 -->
    <div id="overallProgressContainer" class="progress-container">
        <div class="progress-title">全体の進捗: <span id="overallStatus" style="font-weight:normal;">0 / 0</span></div>
        <progress id="overallProgressBar" value="0" max="100"></progress>
        <div id="overallTimeInfo">合計予想所要時間: 計算中...</div>
    </div>

    <!-- アップロードキュー -->
    <div id="queueContainer" class="progress-container">
        <div class="progress-title">アップロードキュー</div>
        <table id="queueTable">
            <thead>
                <tr>
                    <th>ファイル名</th>
                    <th>サイズ</th>
                    <th>状態</th>
                    <th>操作</th>
                </tr>
            </thead>
            <tbody id="queueBody"></tbody>
        </table>
    </div>

    <!-- 現在のファイルの進捗 -->
    <div id="fileProgressContainer" class="progress-container">
        <div class="progress-title">現在のファイル: <span id="currentFileName" style="font-weight:normal; color:#0056b3;"></span></div>
        <progress id="progressBar" value="0" max="100"></progress>
        <div id="status">待機中...</div>
        <div id="speedInfo"></div>
    </div>

    <script>
        const retry_lim = {{ retry_lim }};
        const CHUNK_SIZE = {{ chunk_size }};

        const connectionMode = document.getElementById('connectionMode');
        const directConnectionDetails = document.getElementById('directConnectionDetails');
        const directServerIp = document.getElementById('directServerIp');
        const directSubnetMask = document.getElementById('directSubnetMask');
        const directSubnetMaskSender = document.getElementById('directSubnetMaskSender');
        const directTransferUrl = document.getElementById('directTransferUrl');
        const localAddresses = document.getElementById('localAddresses');
        const accessTokenInput = document.getElementById('accessToken');

        function getAuthHeaders() {
            return { 'X-Transfer-Token': accessTokenInput.value.trim() };
        }

        function updateConnectionMode() {
            directConnectionDetails.hidden = connectionMode.value !== 'direct';
        }
        connectionMode.addEventListener('change', updateConnectionMode);
        updateConnectionMode();

        fetch('/connection_info').then(response => response.ok ? response.json() : Promise.reject())
            .then(info => {
                directServerIp.textContent = info.direct_ethernet_ip;
                directSubnetMask.textContent = info.direct_ethernet_subnet_mask;
                directSubnetMaskSender.textContent = info.direct_ethernet_subnet_mask;
                directTransferUrl.textContent = info.direct_ethernet_url;
                if (info.addresses && info.addresses.length) {
                    localAddresses.textContent = info.addresses.map(item => `${item.adapter}: ${item.ip}`).join(' / ');
                } else {
                    localAddresses.textContent = '検出できませんでした。OSのネットワーク設定をご確認ください。';
                }
            })
            .catch(() => { localAddresses.textContent = '取得できませんでした。'; });

        let currentXhr = null;
        let isPaused = false;
        let isCanceled = false;
        let currentUploadId = '';
        let sessionStartTime = 0;
        let sessionStartBytes = 0;
        let totalQueueBytes = 0;
        let uploadedTotalBytesSoFar = 0;
        let overallStartTime = 0;
        
        // 複数ファイル処理用の変数
        let fileQueue = [];
        let currentFileIndex = 0;
        let failedFileIndex = -1;

        const uploadForm = document.getElementById('uploadForm');
        const fileInput = document.getElementById('fileInput');
        const folderInput = document.getElementById('folderInput');
        const uploadBtn = document.getElementById('uploadBtn');
        const pauseBtn = document.getElementById('pauseBtn');
        const cancelBtn = document.getElementById('cancelBtn');
        
        const overallProgressContainer = document.getElementById('overallProgressContainer');
        const overallProgressBar = document.getElementById('overallProgressBar');
        const overallStatus = document.getElementById('overallStatus');
        const overallTimeInfo = document.getElementById('overallTimeInfo');
        
        const queueContainer = document.getElementById('queueContainer');
        const queueBody = document.getElementById('queueBody');

        const fileProgressContainer = document.getElementById('fileProgressContainer');
        const progressBar = document.getElementById('progressBar');
        const status = document.getElementById('status');
        const speedInfo = document.getElementById('speedInfo');
        const currentFileName = document.getElementById('currentFileName');

        function resetUI() {
            uploadBtn.disabled = false;
            fileInput.disabled = false;
            folderInput.disabled = false;
            accessTokenInput.disabled = false;
            pauseBtn.style.display = 'none';
            cancelBtn.style.display = 'none';
            pauseBtn.innerText = '一時停止';
            speedInfo.innerText = '';
            isPaused = false;
            isCanceled = false;
            currentXhr = null;
            currentUploadId = '';
            fileInput.value = '';
            folderInput.value = '';
        }

        function formatTime(seconds) {
            if (!isFinite(seconds) || seconds < 0) return '計算中...';
            if (seconds < 60) return `${Math.round(seconds)}秒`;
            const mins = Math.floor(seconds / 60);
            const secs = Math.round(seconds % 60);
            if (mins < 60) return `${mins}分${secs}秒`;
            const hours = Math.floor(mins / 60);
            const remMins = mins % 60;
            return `${hours}時間${remMins}分`;
        }
        // ファイルサイズを適切な単位にフォーマットする関数 (追加)
        function formatFileSize(bytes) {
            if (bytes === 0) return '0 B';
            const k = 1024;
            const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
            const i = Math.floor(Math.log(bytes) / Math.log(k));
            return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i];
        }

        // 相対パスが長い場合でも一時ファイル名がOSの長さ制限を超えないようにする。
        // 同じファイルを選び直した場合には同じIDとなり、レジュームに利用できる。
        function createUploadId(file, relativePath) {
            const source = `${relativePath}\u0000${file.size}\u0000${file.lastModified}`;
            let hashA = 2166136261;
            let hashB = 0x9e3779b9;
            for (let i = 0; i < source.length; i++) {
                const code = source.charCodeAt(i);
                hashA = Math.imul(hashA ^ code, 16777619);
                hashB = Math.imul(hashB ^ (code + i), 2246822519);
            }
            const suffix = `${file.size}_${file.lastModified}`;
            return `${(hashA >>> 0).toString(16).padStart(8, '0')}${(hashB >>> 0).toString(16).padStart(8, '0')}_${suffix}`;
        }

        // キュー表示を更新する関数 (修正)
        function renderQueue() {
            queueBody.innerHTML = '';
            fileQueue.forEach((file, index) => {
                const tr = document.createElement('tr');
                
                const tdName = document.createElement('td');
                const relativePath = file.webkitRelativePath || file.name;
                tdName.innerText = relativePath;
                tdName.title = relativePath;
                tdName.className = 'file-name-cell';
                
                const tdSize = document.createElement('td');
                // ↓ 固定表示から formatFileSize() 関数に呼び出しを変更
                tdSize.innerText = formatFileSize(file.size);
                
                const tdStatus = document.createElement('td');
                const tdAction = document.createElement('td');

                if (index < currentFileIndex) {
                    tdStatus.innerText = '完了';
                    tdStatus.style.color = 'green';
                } else if (index === failedFileIndex) {
                    tdStatus.innerText = '失敗';
                    tdStatus.style.color = 'red';
                } else if (index === currentFileIndex) {
                    tdStatus.innerText = isCanceled ? 'キャンセル' : '処理中...';
                    tdStatus.style.color = 'blue';
                } else {
                    tdStatus.innerText = '待機中';
                    tdStatus.style.color = '#555';
                    
                    const removeBtn = document.createElement('button');
                    removeBtn.innerText = '削除';
                    removeBtn.className = 'btn-remove';
                    removeBtn.onclick = () => removeQueueItem(index);
                    tdAction.appendChild(removeBtn);
                }
                
                tr.appendChild(tdName);
                tr.appendChild(tdSize);
                tr.appendChild(tdStatus);
                tr.appendChild(tdAction);
                queueBody.appendChild(tr);
            });
        }

        // 待機中のファイルをキューから削除する関数
        function removeQueueItem(index) {
            if (index > currentFileIndex) {
                const removedFile = fileQueue[index];
                totalQueueBytes = Math.max(0, totalQueueBytes - removedFile.size);
                fileQueue.splice(index, 1);
                overallProgressBar.value = totalQueueBytes > 0
                    ? Math.min(100, Math.round((uploadedTotalBytesSoFar / totalQueueBytes) * 100))
                    : 0;
                overallStatus.innerText = `${currentFileIndex + 1} / ${fileQueue.length} 個目を処理中...`;
                renderQueue();
            }
        }

        pauseBtn.addEventListener('click', function() {
            if (isCanceled || fileQueue.length === 0) return;
            if (!isPaused) {
                isPaused = true;
                pauseBtn.innerText = '再開';
                status.innerText = '一時停止中...';
                status.style.color = 'orange';
                speedInfo.innerText = '';
                if (currentXhr) currentXhr.abort();
            } else {
                isPaused = false;
                pauseBtn.innerText = '一時停止';
                status.innerText = '送信を再開中...';
                status.style.color = 'black';
                sessionStartTime = 0;
            }
        });

        cancelBtn.addEventListener('click', async function() {
            if (isCanceled || fileQueue.length === 0) return;
            isCanceled = true;
            isPaused = false;
            if (currentXhr) currentXhr.abort();

            status.innerText = 'アップロードを取り消し中...';
            status.style.color = 'red';
            speedInfo.innerText = '';

            if (currentUploadId) {
                try {
                    const formData = new FormData();
                    formData.append('upload_id', currentUploadId);
                    await fetch('/upload_cancel', { method: 'POST', headers: getAuthHeaders(), body: formData });
                } catch (err) {
                    console.warn('キャンセルエラー:', err);
                }
            }
            status.innerText = 'すべてのアップロードをキャンセルしました。';
            overallStatus.innerText = 'キャンセル済み';
            fileQueue = []; // キューをクリア
            renderQueue();
            resetUI();
        });

        uploadForm.addEventListener('submit', async function(e) {
            e.preventDefault();

            if (!accessTokenInput.value.trim()) {
                alert('受信側PCの認証トークンを入力してください。');
                accessTokenInput.focus();
                return;
            }
            try {
                const authResponse = await fetch('/auth_check', { headers: getAuthHeaders() });
                if (!authResponse.ok) {
                    alert('認証トークンが正しくありません。受信側PCの設定を確認してください。');
                    accessTokenInput.focus();
                    return;
                }
            } catch (err) {
                alert('受信側PCに接続できません。ネットワーク設定を確認してください。');
                return;
            }

            // ファイル入力とフォルダ入力のファイルを結合
            const files = Array.from(fileInput.files).concat(Array.from(folderInput.files));
            fileQueue = files;

            if (fileQueue.length === 0) {
                alert('アップロード対象のファイルが選択されていません。');
                return;
            }

            failedFileIndex = -1;
            currentFileIndex = 0;
            isCanceled = false;
            isPaused = false;
            totalQueueBytes = fileQueue.reduce((sum, file) => sum + file.size, 0);
            uploadedTotalBytesSoFar = 0;
            overallStartTime = Date.now();
            overallProgressBar.value = 0;
            overallTimeInfo.innerText = `合計予想所要時間: 計算中... (全 ${formatFileSize(totalQueueBytes)})`;

            uploadBtn.disabled = true;
            fileInput.disabled = true;
            folderInput.disabled = true;
            accessTokenInput.disabled = true;
            pauseBtn.style.display = 'inline-block';
            cancelBtn.style.display = 'inline-block';
            
            overallProgressContainer.style.display = 'block';
            queueContainer.style.display = 'block';
            fileProgressContainer.style.display = 'block';

            renderQueue();
            const logData = new FormData();
            logData.append('file_count', String(fileQueue.length));
            try {
                await fetch('/log_start_session', { method: 'POST', headers: getAuthHeaders(), body: logData });
            } catch (err) {
                console.warn('開始ログの送信に失敗しました:', err);
            }
            processNextFile();
        });

        function getOverallBytesPerSec() {
            const elapsedSec = (Date.now() - overallStartTime) / 1000;
            if (elapsedSec < 0.5 || uploadedTotalBytesSoFar <= 0) return 0;
            return uploadedTotalBytesSoFar / elapsedSec;
        }

        // キューに入ったファイルを順番に処理する関数
        async function processNextFile() {
            if (isCanceled) return;

            if (currentFileIndex >= fileQueue.length) {
                overallProgressBar.value = 100;
                overallStatus.innerText = `完了 (${fileQueue.length} / ${fileQueue.length})`;
                overallTimeInfo.innerText = `合計所要時間: 完了 (${formatFileSize(totalQueueBytes)})`;
                status.innerText = 'すべてのアップロードが完了しました！';
                status.style.color = 'green';
                speedInfo.innerText = '';
                renderQueue(); // 最後の状態を更新
                resetUI();
                return;
            }

            renderQueue(); // 現在のファイルのステータスを「処理中」に更新

            const file = fileQueue[currentFileIndex];
            
            // 全体UIの更新
            overallProgressBar.value = totalQueueBytes > 0
                ? Math.min(100, Math.round((uploadedTotalBytesSoFar / totalQueueBytes) * 100))
                : 0;
            overallStatus.innerText = `${currentFileIndex + 1} / ${fileQueue.length} 個目を処理中...`;
            
            // 個別UIの更新
            currentFileName.innerText = file.webkitRelativePath || file.name;
            progressBar.value = 0;
            status.innerText = '準備中...';
            status.style.color = 'black';
            speedInfo.innerText = '';

            const succeeded = await uploadSingleFile(file);
            if (isCanceled) return;
            if (!succeeded) {
                failedFileIndex = currentFileIndex;
                status.innerText = '転送に失敗しました。接続・保存先・ログを確認してください。';
                status.style.color = 'red';
                renderQueue();
                resetUI();
                return;
            }
            currentFileIndex++;
            processNextFile();
        }

        // 1つのファイルをアップロードするロジック
        async function uploadSingleFile(file) {
            try {
                const relativePath = file.webkitRelativePath || file.name;
            const checkRes = await fetch(`/check_file_exists?path=${encodeURIComponent(relativePath)}`, { headers: getAuthHeaders() });
                if (checkRes.ok) {
                    const checkData = await checkRes.json();
                    if (checkData.exists) {
                        const confirmOverwrite = confirm(`同名のファイル「${file.name}」が既に存在します。\\n上書きして続行しますか？\\n(キャンセルを押すとこのファイルをスキップします)`);
                        if (!confirmOverwrite) {
                            status.innerText = 'スキップしました。';
                            status.style.color = '#777';
                            uploadedTotalBytesSoFar += file.size;
                            overallProgressBar.value = totalQueueBytes > 0
                                ? Math.min(100, Math.round((uploadedTotalBytesSoFar / totalQueueBytes) * 100))
                                : 0;
                            return true; // 処理自体は正常終了として次へ
                        }
                    }
                }
            } catch (err) {
                console.warn("重複チェックエラー:", err);
            }

            const totalChunks = Math.max(1, Math.ceil(file.size / CHUNK_SIZE));
            const relativePath = file.webkitRelativePath || file.name;
            currentUploadId = createUploadId(file, relativePath);

            let startChunk = 0;
            try {
                const res = await fetch(`/upload_status?upload_id=${encodeURIComponent(currentUploadId)}`, { headers: getAuthHeaders() });
                if (res.ok) {
                    const data = await res.json();
                    startChunk = data.received_chunks || 0;
                    if (startChunk >= totalChunks) startChunk = Math.max(0, totalChunks - 1);
                }
            } catch (err) {}

            if (startChunk > 0) {
                status.innerText = `続き（${startChunk + 1}/${totalChunks} チャンク目）から再開します...`;
            }

            const alreadyUploadedBytesInFile = Math.min(file.size, startChunk * CHUNK_SIZE);
            uploadedTotalBytesSoFar += alreadyUploadedBytesInFile;
            let filePrevLoaded = alreadyUploadedBytesInFile;
            sessionStartTime = 0;

            for (let chunkIndex = startChunk; chunkIndex < totalChunks; chunkIndex++) {
                if (isCanceled) return false;
                while (isPaused) {
                    if (isCanceled) return false;
                    await new Promise(res => setTimeout(res, 200));
                }
                if (isCanceled) return false;

                if (sessionStartTime === 0) {
                    sessionStartTime = Date.now();
                    sessionStartBytes = chunkIndex * CHUNK_SIZE;
                }

                const start = chunkIndex * CHUNK_SIZE;
                const end = Math.min(file.size, start + CHUNK_SIZE);
                const chunk = file.slice(start, end);

                let success = false;
                let attempts = 0;

                while (attempts <= retry_lim && !success) {
                    if (isCanceled) return false;
                    while (isPaused) {
                        if (isCanceled) return false;
                        await new Promise(res => setTimeout(res, 200));
                    }
                    if (isCanceled) return false;

                    try {
                        if (attempts > 0) {
                            status.innerText = `通信エラー再試行中... (${chunkIndex + 1}/${totalChunks} チャンク, リトライ ${attempts}/${retry_lim})`;
                            status.style.color = 'orange';
                            speedInfo.innerText = '';
                            await new Promise(res => setTimeout(res, 1000 * attempts));
                            if (isCanceled) return false;
                            sessionStartTime = Date.now();
                            sessionStartBytes = chunkIndex * CHUNK_SIZE;
                        }

                        await sendChunk(chunk, file, relativePath, currentUploadId, chunkIndex, totalChunks, (loadedInChunk, requestBytes) => {
                            if (isPaused || isCanceled) return;
                            const actualChunkBytes = requestBytes > 0
                                ? Math.min(chunk.size, Math.round(loadedInChunk * chunk.size / requestBytes))
                                : 0;
                            const totalLoadedInFile = (chunkIndex * CHUNK_SIZE) + actualChunkBytes;
                            const delta = totalLoadedInFile - filePrevLoaded;
                            if (delta > 0) {
                                uploadedTotalBytesSoFar += delta;
                                filePrevLoaded = totalLoadedInFile;
                            }
                            progressBar.value = file.size === 0 ? 100 : Math.min(100, Math.round((totalLoadedInFile / file.size) * 100));
                            status.innerText = `${progressBar.value}% アップロード中... (${chunkIndex + 1}/${totalChunks} チャンク | ${formatFileSize(totalLoadedInFile)} / ${formatFileSize(file.size)})`;
                            status.style.color = 'black';

                            overallProgressBar.value = totalQueueBytes > 0
                                ? Math.min(100, Math.round((uploadedTotalBytesSoFar / totalQueueBytes) * 100))
                                : 0;
                            const overallSpeed = getOverallBytesPerSec();
                            if (overallSpeed > 0) {
                                const remainingBytes = Math.max(0, totalQueueBytes - uploadedTotalBytesSoFar);
                                overallTimeInfo.innerText = `合計予想所要時間: 約 ${formatTime(remainingBytes / overallSpeed)} (残り ${formatFileSize(remainingBytes)} / 全 ${formatFileSize(totalQueueBytes)})`;
                            } else {
                                overallTimeInfo.innerText = `合計予想所要時間: 計算中... (全 ${formatFileSize(totalQueueBytes)})`;
                            }

                            const elapsedSec = (Date.now() - sessionStartTime) / 1000;
                            if (elapsedSec > 0.3) {
                                const bytesPerSec = (totalLoadedInFile - sessionStartBytes) / elapsedSec;
                                if (bytesPerSec > 0) {
                                    speedInfo.innerText = `ファイル速度: ${(bytesPerSec / (1024 * 1024)).toFixed(2)} MB/s | 残り時間: 約 ${formatTime((file.size - totalLoadedInFile) / bytesPerSec)}`;
                                }
                            }
                        });
                        const completedBytes = Math.min(file.size, (chunkIndex + 1) * CHUNK_SIZE);
                        const finalDelta = completedBytes - filePrevLoaded;
                        if (finalDelta > 0) {
                            uploadedTotalBytesSoFar += finalDelta;
                            filePrevLoaded = completedBytes;
                        }
                        success = true;
                    } catch (err) {
                        if (isCanceled) return false;
                        if (err.message === 'paused') continue;
                        attempts++;
                        if (attempts > retry_lim) {
                            status.innerText = `エラー: ${err.message || '送信に失敗しました。'}`;
                            status.style.color = 'red';
                            speedInfo.innerText = '';
                            return false;
                        }
                    }
                }
            }

            progressBar.value = 100;
            status.innerText = `完了！`;
            status.style.color = 'green';
            return true;
        }

        function sendChunk(chunk, file, relativePath, uploadId, chunkIndex, totalChunks, onProgress) {
            return new Promise((resolve, reject) => {
                const formData = new FormData();
                formData.append('chunk', chunk);
                formData.append('filename', file.name);
                formData.append('relative_path', relativePath);
                formData.append('last_modified_ms', String(file.lastModified));
                formData.append('file_size', String(file.size));
                formData.append('total_size', String(file.size));
                formData.append('upload_id', uploadId);
                formData.append('chunk_index', chunkIndex);
                formData.append('total_chunks', totalChunks);

                const xhr = new XMLHttpRequest();
                currentXhr = xhr;
                xhr.open('POST', '/upload_chunk', true);
                xhr.setRequestHeader('X-Transfer-Token', accessTokenInput.value.trim());

                xhr.upload.onprogress = function(e) {
                    if (e.lengthComputable && onProgress) onProgress(e.loaded, e.total);
                };
                xhr.onload = function() {
                    currentXhr = null;
                    if (xhr.status === 200) resolve();
                    else reject(new Error(xhr.responseText || `サーバーエラー (${xhr.status})`));
                };
                xhr.onerror = function() {
                    currentXhr = null;
                    reject(new Error('通信エラー'));
                };
                xhr.onabort = function() {
                    currentXhr = null;
                    if (isCanceled) reject(new Error('canceled'));
                    else if (isPaused) reject(new Error('paused'));
                    else reject(new Error('aborted'));
                };
                xhr.send(formData);
            });
        }
    </script>
</body>
</html>
"""

@app.before_request
def suppress_werkzeug_logs_after_first_request() -> None:
    """Log the first connection and suppress routine Werkzeug access logs afterward."""
    global _FIRST_REQUEST_LOGGED
    if not _FIRST_REQUEST_LOGGED:
        logger.info("[%s] 初回接続を検知しました。", request.remote_addr)
        logging.getLogger("werkzeug").setLevel(logging.WARNING)
        _FIRST_REQUEST_LOGGED = True


@app.route("/", methods=["GET"])
def index() -> str:
    """Render the browser-based upload page."""
    logger.info("[%s] トップページへアクセスがありました。", request.remote_addr)
    return render_template_string(HTML_PAGE, retry_lim=RETRY_LIM, chunk_size=CHUNK_SIZE)


@app.route("/connection_info", methods=["GET"])
def connection_info() -> Any:
    """Return network addresses and the configured direct-Ethernet URL."""
    return jsonify(
        {
            "addresses": get_local_ipv4_addresses(),
            "direct_ethernet_ip": DIRECT_ETHERNET_IP,
            "direct_ethernet_subnet_mask": DIRECT_ETHERNET_SUBNET_MASK,
            "direct_ethernet_url": f"http://{DIRECT_ETHERNET_IP}:{PORT}",
        }
    )


@app.route("/auth_check", methods=["GET"])
@require_access_token
def auth_check() -> Any:
    """Confirm that the supplied transfer token is valid."""
    return jsonify({"authenticated": True})


@app.route("/log_start_session", methods=["POST"])
@require_access_token
def log_start_session() -> tuple[str, int]:
    """Log the start of a batch upload."""
    try:
        file_count = max(0, min(int(request.form.get("file_count", "0")), 1_000_000))
    except (TypeError, ValueError):
        return "ファイル数が無効です", 400
    logger.info("[%s] 一括アップロード開始（%sファイル）", request.remote_addr, file_count)
    return "OK", 200


@app.route("/check_file_exists", methods=["GET"])
@require_access_token
def check_file_exists() -> Any:
    """Check for an existing file after validating the requested relative path."""
    raw_path = request.args.get("path") or request.args.get("filepath") or request.args.get("relative_path", "")
    try:
        parts = safe_relative_parts(raw_path)
        filepath = safe_destination_path(parts)
    except ValueError as exc:
        logger.warning("[%s] 無効な重複チェックパス: %s", request.remote_addr, exc)
        return jsonify({"exists": False, "error": str(exc)}), 400
    exists = filepath.is_file()
    logger.info("[%s] 重複確認: %s -> %s", request.remote_addr, "/".join(parts), exists)
    return jsonify({"exists": exists})


@app.route("/upload_status", methods=["GET"])
@require_access_token
def upload_status() -> Any:
    """Return how many full chunks have been received for an upload."""
    try:
        upload_id = _valid_upload_id(request.args.get("upload_id", ""))
    except ValueError as exc:
        return jsonify({"error": str(exc), "received_chunks": 0}), 400
    with _UPLOAD_LOCK:
        # A new browser upload session deliberately starts over for a completed ID.
        _COMPLETED_UPLOADS.pop(upload_id, None)
        temp_filepath = TEMP_ROOT / upload_id
        if temp_filepath.is_symlink():
            return jsonify({"error": "一時ファイルが無効です", "received_chunks": 0}), 409
        if not temp_filepath.is_file():
            return jsonify({"received_chunks": 0})
        current_size = temp_filepath.stat().st_size
        metadata = _UPLOAD_METADATA.get(upload_id)
        if metadata is not None:
            total_size, total_chunks = metadata[1], metadata[2]
            if current_size > total_size:
                return jsonify({"error": "一時ファイルのサイズが不正です", "received_chunks": 0}), 409
            received_chunks = min(total_chunks, current_size // CHUNK_SIZE)
        else:
            received_chunks = current_size // CHUNK_SIZE
        logger.info(
            "[%s] レジューム状態確認: ID %s、受信済みチャンク %s",
            request.remote_addr, upload_id, received_chunks,
        )
        return jsonify({"received_chunks": received_chunks})


@app.route("/upload_chunk", methods=["POST"])
@require_access_token
def upload_chunk() -> tuple[str, int]:
    """Receive a chunk, verify its size, and atomically commit the completed file."""
    chunk = request.files.get("chunk")
    raw_path = request.form.get("relative_path") or request.form.get("filepath") or request.form.get("filename", "")
    try:
        upload_id = _valid_upload_id(request.form.get("upload_id", ""))
        parts = safe_relative_parts(raw_path)
        destination = safe_destination_path(parts)
        chunk_index = int(request.form.get("chunk_index", "-1"))
        total_chunks = int(request.form.get("total_chunks", "0"))
        total_size = int(request.form.get("total_size", request.form.get("file_size", "-1")))
        raw_mtime = request.form.get("last_modified_ms", "")
        last_modified_ms = int(raw_mtime) if raw_mtime != "" else None
    except (ValueError, TypeError) as exc:
        logger.warning("[%s] 無効なチャンクメタデータ: %s", request.remote_addr, exc)
        return "データが無効です", 400

    if chunk is None:
        return "チャンクデータがありません", 400
    if total_size < 0 or total_chunks < 1:
        return "ファイルサイズまたはチャンク数が無効です", 400
    expected_chunks = max(1, math.ceil(total_size / CHUNK_SIZE))
    if total_chunks != expected_chunks or not 0 <= chunk_index < total_chunks:
        return "チャンク番号またはチャンク数が無効です", 400
    # Browser File.lastModified uses epoch milliseconds. Bound it to a practical
    # datetime range before passing it to the filesystem.
    if last_modified_ms is not None and not 0 <= last_modified_ms <= 253402300799999:
        return "最終更新日時が無効です", 400

    expected_chunk_size = min(CHUNK_SIZE, max(0, total_size - chunk_index * CHUNK_SIZE))
    chunk_data = chunk.stream.read(expected_chunk_size + 1)
    if len(chunk_data) != expected_chunk_size:
        return "チャンクサイズが想定値と一致しません", 400

    metadata = (parts, total_size, total_chunks, last_modified_ms)
    temp_filepath = TEMP_ROOT / upload_id
    relative_display = "/".join(parts)

    with _UPLOAD_LOCK:
        if temp_filepath.is_symlink():
            return "一時ファイルにシンボリックリンクは使用できません", 409
        completed = _COMPLETED_UPLOADS.get(upload_id)
        if completed == (str(destination), total_size, last_modified_ms) and destination.is_file():
            return "OK", 200
        if completed is not None:
            _COMPLETED_UPLOADS.pop(upload_id, None)

        existing_metadata = _UPLOAD_METADATA.get(upload_id)
        if existing_metadata is not None and existing_metadata != metadata:
            return "同一のアップロードIDに異なるファイル情報が指定されました", 409
        if existing_metadata is None:
            if chunk_index != 0 or temp_filepath.exists():
                return "最初のチャンクから送信してください", 409
            _UPLOAD_METADATA[upload_id] = metadata

        try:
            if temp_filepath.exists():
                current_size = temp_filepath.stat().st_size
                if current_size > total_size:
                    return "一時ファイルのサイズが想定値を超えています", 409
                received_chunks = current_size // CHUNK_SIZE
                if chunk_index > received_chunks:
                    return "チャンクが順序どおりに送信されていません", 409
                with temp_filepath.open("r+b") as stream:
                    stream.seek(chunk_index * CHUNK_SIZE)
                    stream.write(chunk_data)
                    stream.truncate(max(current_size, chunk_index * CHUNK_SIZE + len(chunk_data)))
            else:
                if chunk_index != 0:
                    return "最初のチャンクから送信してください", 409
                with temp_filepath.open("w+b") as stream:
                    stream.write(chunk_data)
        except OSError as exc:
            logger.exception("チャンクの保存に失敗しました: %s", exc)
            return "チャンクの書き込みに失敗しました", 500

        logger.debug(
            "[%s] チャンク保存: %s (%s/%s)",
            request.remote_addr, relative_display, chunk_index + 1, total_chunks,
        )
        if chunk_index == 0:
            logger.info(
                "[%s] 転送開始: %s（%s bytes、%sチャンク）",
                request.remote_addr, relative_display, total_size, total_chunks,
            )

        if chunk_index == total_chunks - 1:
            try:
                if temp_filepath.stat().st_size != total_size:
                    return "受信したファイルサイズが一致しません", 409
                destination = safe_destination_path(parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination = safe_destination_path(parts)
                if last_modified_ms is not None:
                    # The browser does not expose the source access time; preserve mtime only.
                    os.utime(temp_filepath, (time.time(), last_modified_ms / 1000))
                os.replace(temp_filepath, destination)
            except (OSError, ValueError, OverflowError) as exc:
                logger.exception("最終ファイルの保存に失敗しました: %s", exc)
                return "ファイルの保存に失敗しました", 500

            _UPLOAD_METADATA.pop(upload_id, None)
            _COMPLETED_UPLOADS[upload_id] = (str(destination), total_size, last_modified_ms)
            while len(_COMPLETED_UPLOADS) > 256:
                _COMPLETED_UPLOADS.pop(next(iter(_COMPLETED_UPLOADS)))
            logger.info(
                "[%s] 転送完了: %s (mtime_preserved=%s)",
                request.remote_addr, relative_display, last_modified_ms is not None,
            )
    return "OK", 200


@app.route("/upload_cancel", methods=["POST"])
@require_access_token
def upload_cancel() -> tuple[str, int]:
    """Discard a partial upload and its in-memory state."""
    try:
        upload_id = _valid_upload_id(request.form.get("upload_id", ""))
    except ValueError as exc:
        return f"リクエストが無効です: {exc}", 400
    with _UPLOAD_LOCK:
        temp_filepath = TEMP_ROOT / upload_id
        try:
            if temp_filepath.is_file() or temp_filepath.is_symlink():
                temp_filepath.unlink(missing_ok=True)
            _UPLOAD_METADATA.pop(upload_id, None)
            _COMPLETED_UPLOADS.pop(upload_id, None)
        except OSError as exc:
            logger.exception("一時ファイルの削除に失敗しました: %s", exc)
            return "キャンセル処理に失敗しました", 500
    logger.info("[%s] 転送をキャンセルしました（ID %s）", request.remote_addr, upload_id)
    return "OK", 200


@app.errorhandler(RequestEntityTooLarge)
def handle_request_too_large(_error: RequestEntityTooLarge) -> tuple[str, int]:
    """Return a Japanese error when a chunk exceeds the configured request limit."""
    return "チャンクが許容サイズを超えています。CHUNK_SIZE を確認してください。", 413


if __name__ == "__main__":
    logger.info("サーバーを開始します (Host: %s, Port: %s)...", HOST, PORT)
    print(f"ファイル転送の認証トークン: {ACCESS_TOKEN}", flush=True)
    logger.warning(
        "通信はHTTPで暗号化されません。信頼できるプライベートLAN内でのみ使用し、"
        "ルーターのポート転送やインターネットへの公開を行わないでください。"
    )
    app.run(host=HOST, port=PORT, debug=False, threaded=True)
