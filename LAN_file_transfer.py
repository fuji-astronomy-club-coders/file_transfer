import datetime
import logging
import os
import platform
import re
import shutil
import socket
import subprocess
from pathlib import Path, PurePosixPath

import yaml
from flask import Flask, jsonify, render_template_string, request
from werkzeug.utils import secure_filename

# --- 1. 詳細なロギングの設定 ---
LOG_FILE = Path('logs') / f'transfer_server{ datetime.datetime.now().strftime("%Y%m%d_%H%M%S")}.log'  # noqa: DTZ005
LOG_FILE.parent.mkdir(exist_ok=True)

file_handler = logging.FileHandler(LOG_FILE, encoding='utf-8')
file_handler.setLevel(logging.DEBUG)

stream_handler = logging.StreamHandler()
stream_handler.setLevel(logging.INFO)

logging.basicConfig(
    format='%(asctime)s [%(levelname)s] %(message)s',
    level=logging.DEBUG,  # ルートロガーのレベル
    handlers=[
        file_handler,
        stream_handler
    ]
)
logger = logging.getLogger(__name__)

# logging.getLogger('werkzeug').setLevel(logging.WARNING)

# --- 2. YAML設定の読み込みと生成 ---
CONFIG_FILE = 'file_transfar_config.yaml'
DEFAULT_CONFIG = {
    'RETRY_LIM': 3,
    'CHUNK_SIZE': 5242880,  # 5MB (5 * 1024 * 1024)
    'UPLOAD_FOLDER': 'uploads',
    'TEMP_FOLDER': 'temp_uploads',
    'HOST': '0.0.0.0',
    'PORT': 5000,
    # 直結時は受信側PCのEthernetをこの固定IPに設定する
    'DIRECT_ETHERNET_IP': '192.168.50.1',
    'DIRECT_ETHERNET_SUBNET_MASK': '255.255.255.0'
}

if not os.path.exists(CONFIG_FILE):
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            yaml.dump(DEFAULT_CONFIG, f, default_flow_style=False)
        logger.info(f"設定ファイルが見つからないため、デフォルト設定 '{CONFIG_FILE}' を生成しました。")
    except Exception as e:
        logger.error(f"設定ファイルの生成に失敗しました: {e}")

try:
    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f) or DEFAULT_CONFIG
    logger.info(f"設定ファイル '{CONFIG_FILE}' を読み込みました。")
except Exception as e:
    logger.error(f"設定ファイルの読み込みに失敗しました。デフォルト設定を使用します: {e}")
    config = DEFAULT_CONFIG

# 設定値の適用
RETRY_LIM = config.get('RETRY_LIM', 3)
CHUNK_SIZE = config.get('CHUNK_SIZE', 5242880)
UPLOAD_FOLDER = config.get('UPLOAD_FOLDER', 'uploads')
TEMP_FOLDER = config.get('TEMP_FOLDER', 'temp_uploads')
HOST = config.get('HOST', '0.0.0.0')
PORT = int(config.get('PORT', 5000))
DIRECT_ETHERNET_IP = str(config.get('DIRECT_ETHERNET_IP', '192.168.50.1'))
DIRECT_ETHERNET_SUBNET_MASK = str(config.get('DIRECT_ETHERNET_SUBNET_MASK', '255.255.255.0'))

app = Flask(__name__)

@app.before_request
def suppress_werkzeug_logs_after_first_request():
    """初回アクセス時にWerkzeugのログレベルをWARNINGに変更し、その旨をログに残す"""
    logger.info(f"[{request.remote_addr}] 初回接続を検知したため、Werkzeugのログレベルを WARNING に変更します。")
    logging.getLogger('werkzeug').setLevel(logging.WARNING)
    
    # 2回目以降のアクセスで毎回実行されないよう関数自体を取り除く
    app.before_request_funcs[None].remove(suppress_werkzeug_logs_after_first_request)
    
def sanitize_relative_path(raw_path):
    """アップロード相対パスを検証し、OS依存の危険な文字を除去する。

    Parameters
    ----------
    raw_path : str
        ブラウザーから受け取ったファイル名または相対パス。

    Returns
    -------
    pathlib.Path
        アップロード先ルートからの相対パス。

    Raises
    ------
    ValueError
        空のパス、絶対パス、または親ディレクトリへの参照が含まれる場合。
    """
    value = str(raw_path or '').replace('\\', '/')
    if not value or value.startswith('/') or re.match(r'^[A-Za-z]:', value):
        raise ValueError('相対パスが無効です')

    raw_parts = PurePosixPath(value).parts
    if not raw_parts or any(part in ('', '.', '..', '/') for part in raw_parts):
        raise ValueError('相対パスに無効な要素が含まれています')

    safe_parts = []
    reserved_names = {
        'CON', 'PRN', 'AUX', 'NUL',
        *(f'COM{i}' for i in range(1, 10)),
        *(f'LPT{i}' for i in range(1, 10)),
    }
    for part in raw_parts:
        # Windowsでファイル名に使えない文字と制御文字を置換する。
        safe = re.sub(r'[<>:"|?*\x00-\x1f]', '_', part).rstrip(' .')
        if not safe or safe in ('.', '..'):
            raise ValueError('ファイル名が無効です')
        if safe.split('.')[0].upper() in reserved_names:
            safe = f'_{safe}'
        safe_parts.append(safe)

    return Path(*safe_parts)


def resolve_upload_path(relative_path):
    """アップロード相対パスを保存先ルート内の絶対パスへ解決する。"""
    root = Path(UPLOAD_FOLDER).resolve()
    destination = (root / relative_path).resolve()
    try:
        destination.relative_to(root)
    except ValueError as exc:
        raise ValueError('保存先がアップロードフォルダーの外側です') from exc
    return destination


def normalize_upload_id(raw_upload_id):
    """一時ファイル名に使えるアップロードIDを返す。"""
    upload_id = secure_filename(str(raw_upload_id or ''))
    if not upload_id or upload_id in {'.', '..'}:
        raise ValueError('アップロードIDが無効です')
    return upload_id


def cleanup_temp_folder():
    """起動時に未完了の過去の一時ファイルを全削除する関数"""
    if os.path.exists(TEMP_FOLDER):
        try:
            shutil.rmtree(TEMP_FOLDER)
            logger.info(f"[クリーンアップ] 一時フォルダ '{TEMP_FOLDER}' を初期化しました。")
        except Exception as e:
            logger.warning(f"[警告] 一時フォルダの削除に失敗しました: {e}")
    os.makedirs(TEMP_FOLDER, exist_ok=True)

# フォルダ生成とクリーンアップ実行
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
cleanup_temp_folder()

# --- HTMLテンプレート ---
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
        
        // 複数ファイル処理用の変数
        let fileQueue = [];
        let currentFileIndex = 0;

        const uploadForm = document.getElementById('uploadForm');
        const fileInput = document.getElementById('fileInput');
        const folderInput = document.getElementById('folderInput');
        const uploadBtn = document.getElementById('uploadBtn');
        const pauseBtn = document.getElementById('pauseBtn');
        const cancelBtn = document.getElementById('cancelBtn');
        
        const overallProgressContainer = document.getElementById('overallProgressContainer');
        const overallProgressBar = document.getElementById('overallProgressBar');
        const overallStatus = document.getElementById('overallStatus');
        
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
                fileQueue.splice(index, 1);
                overallProgressBar.value = Math.round((currentFileIndex / fileQueue.length) * 100) || 0;
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
                    await fetch('/upload_cancel', { method: 'POST', body: formData });
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
            
            // ファイル入力とフォルダ入力のファイルを結合
            const files = Array.from(fileInput.files).concat(Array.from(folderInput.files));
            
            // すべてのファイルをキューに追加（フィルタリング処理を削除）
            fileQueue = files;

            if (fileQueue.length === 0) {
                alert('アップロード対象のファイルが選択されていません。');
                return;
            }

            // 初期化
            currentFileIndex = 0;
            isCanceled = false;
            isPaused = false;

            uploadBtn.disabled = true;
            fileInput.disabled = true;
            folderInput.disabled = true;
            pauseBtn.style.display = 'inline-block';
            cancelBtn.style.display = 'inline-block';
            
            overallProgressContainer.style.display = 'block';
            queueContainer.style.display = 'block';
            fileProgressContainer.style.display = 'block';

            renderQueue();
            processNextFile();
        });

        // キューに入ったファイルを順番に処理する関数
        async function processNextFile() {
            if (isCanceled) return;

            if (currentFileIndex >= fileQueue.length) {
                overallProgressBar.value = 100;
                overallStatus.innerText = `完了 (${fileQueue.length} / ${fileQueue.length})`;
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
            overallProgressBar.value = Math.round((currentFileIndex / fileQueue.length) * 100);
            overallStatus.innerText = `${currentFileIndex + 1} / ${fileQueue.length} 個目を処理中...`;
            
            // 個別UIの更新
            currentFileName.innerText = file.name;
            progressBar.value = 0;
            status.innerText = '準備中...';
            status.style.color = 'black';
            speedInfo.innerText = '';

            await uploadSingleFile(file);
            
            if (!isCanceled) {
                currentFileIndex++;
                processNextFile();
            }
        }

        // 1つのファイルをアップロードするロジック
        async function uploadSingleFile(file) {
            try {
                const relativePath = file.webkitRelativePath || file.name;
            const checkRes = await fetch(`/check_file_exists?path=${encodeURIComponent(relativePath)}`);
                if (checkRes.ok) {
                    const checkData = await checkRes.json();
                    if (checkData.exists) {
                        const confirmOverwrite = confirm(`同名のファイル「${file.name}」が既に存在します。\\n上書きして続行しますか？\\n(キャンセルを押すとこのファイルをスキップします)`);
                        if (!confirmOverwrite) {
                            status.innerText = 'スキップしました。';
                            status.style.color = '#777';
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
                const res = await fetch(`/upload_status?upload_id=${currentUploadId}`);
                if (res.ok) {
                    const data = await res.json();
                    startChunk = data.received_chunks || 0;
                    if (startChunk > totalChunks) startChunk = totalChunks;
                }
            } catch (err) {}

            if (startChunk > 0 && startChunk < totalChunks) {
                status.innerText = `続き（${startChunk + 1}/${totalChunks} チャンク目）から再開します...`;
            } else if (startChunk >= totalChunks) {
                progressBar.value = 100;
                status.innerText = `既に完了しています`;
                status.style.color = 'green';
                return true;
            }

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

                        await sendChunk(chunk, file, relativePath, currentUploadId, chunkIndex, totalChunks, (loadedInChunk) => {
                            if (isPaused || isCanceled) return;
                            const totalLoadedBytes = (chunkIndex * CHUNK_SIZE) + loadedInChunk;
                            progressBar.value = file.size === 0 ? 100 : Math.round((totalLoadedBytes / file.size) * 100);
                            status.innerText = `${progressBar.value}% アップロード中... (${chunkIndex + 1}/${totalChunks} チャンク)`;
                            status.style.color = 'black';

                            const elapsedSec = (Date.now() - sessionStartTime) / 1000;
                            if (elapsedSec > 0.3) {
                                const bytesPerSec = (totalLoadedBytes - sessionStartBytes) / elapsedSec;
                                speedInfo.innerText = `速度: ${(bytesPerSec / (1024 * 1024)).toFixed(2)} MB/s | 残り時間: 約 ${formatTime((file.size - totalLoadedBytes) / bytesPerSec)}`;
                            }
                        });
                        success = true;
                    } catch (err) {
                        if (isCanceled) return false;
                        if (err.message === 'paused') continue;
                        attempts++;
                        if (attempts > retry_lim) {
                            status.innerText = `エラー: 送信に失敗しました。`;
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
                formData.append('upload_id', uploadId);
                formData.append('chunk_index', chunkIndex);
                formData.append('total_chunks', totalChunks);

                const xhr = new XMLHttpRequest();
                currentXhr = xhr;
                xhr.open('POST', '/upload_chunk', true);

                xhr.upload.onprogress = function(e) {
                    if (e.lengthComputable && onProgress) onProgress(e.loaded);
                };
                xhr.onload = function() {
                    currentXhr = null;
                    if (xhr.status === 200) resolve();
                    else reject(new Error('サーバーエラー'));
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

def get_local_ipv4_addresses():
    """OS標準コマンド等から各ネットワークインターフェースのIPv4を取得する。"""
    addresses = []
    seen = set()

    def add(adapter, ip):
        if not ip or ip.startswith('127.') or ip in seen:
            return
        seen.add(ip)
        addresses.append({'adapter': adapter or 'ネットワーク', 'ip': ip})

    if platform.system() == 'Windows':
        try:
            result = subprocess.run(
                ['ipconfig'], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=3, check=False
            )
            adapter = 'ネットワークアダプター'
            for line in result.stdout.splitlines():
                stripped = line.strip()
                if stripped and not line[:1].isspace() and stripped.endswith(':'):
                    adapter = stripped[:-1]
                match = re.search(r'IPv4[^:]*:\s*(\d{1,3}(?:\.\d{1,3}){3})', line, re.IGNORECASE)
                if match:
                    add(adapter, match.group(1))
        except (OSError, subprocess.SubprocessError) as exc:
            logger.debug('ipconfig によるIPアドレス検出に失敗しました: %s', exc)
    else:
        try:
            result = subprocess.run(
                ['ip', '-o', '-4', 'addr', 'show'], capture_output=True, text=True, timeout=3, check=False
            )
            for line in result.stdout.splitlines():
                match = re.match(r'\d+:\s+([^\s:@]+).*?\sinet\s+(\d{1,3}(?:\.\d{1,3}){3})/', line)
                if match:
                    add(match.group(1), match.group(2))
        except (OSError, subprocess.SubprocessError) as exc:
            logger.debug('ip コマンドによるIPアドレス検出に失敗しました: %s', exc)

    # OSコマンドで得られない環境向けのフォールバック（既定経路側のアドレス）
    if not addresses:
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                add(socket.gethostname(), info[4][0])
        except OSError as exc:
            logger.debug('ホスト名からのIPアドレス検出に失敗しました: %s', exc)

    return addresses


@app.route('/')
def index():
    logger.info(f"[{request.remote_addr}] トップページにアクセスしました。")
    return render_template_string(HTML_PAGE, retry_lim=RETRY_LIM, chunk_size=CHUNK_SIZE)


@app.route('/connection_info', methods=['GET'])
def connection_info():
    """直結モードの設定案内に使うIPアドレス情報を返す。"""
    return jsonify({
        'addresses': get_local_ipv4_addresses(),
        'direct_ethernet_ip': DIRECT_ETHERNET_IP,
        'direct_ethernet_subnet_mask': DIRECT_ETHERNET_SUBNET_MASK,
        'direct_ethernet_url': f'http://{DIRECT_ETHERNET_IP}:{PORT}'
    })

@app.route('/check_file_exists', methods=['GET'])
def check_file_exists():
    """保存先の相対パスが既に存在するか確認する。"""
    raw_path = request.args.get('path') or request.args.get('filename', '')
    try:
        relative_path = sanitize_relative_path(raw_path)
        filepath = resolve_upload_path(relative_path)
    except ValueError as exc:
        logger.warning("[%s] 無効な重複チェックパス: %s", request.remote_addr, exc)
        return jsonify({'exists': False, 'error': '無効なパスです'}), 400

    exists = filepath.is_file()
    logger.info("[%s] ファイル重複チェック: '%s' -> 存在: %s", request.remote_addr, relative_path, exists)
    return jsonify({'exists': exists})

@app.route('/upload_status', methods=['GET'])
def upload_status():
    """中断したアップロードの受信済みチャンク数を返す。"""
    try:
        upload_id = normalize_upload_id(request.args.get('upload_id', ''))
    except ValueError:
        return jsonify({'received_chunks': 0}), 400

    temp_filepath = Path(TEMP_FOLDER) / upload_id
    if temp_filepath.is_file():
        current_size = temp_filepath.stat().st_size
        received_chunks = current_size // CHUNK_SIZE
        logger.info("[%s] レジューム確認: ID '%s', 完了チャンク数: %s", request.remote_addr, upload_id, received_chunks)
        return jsonify({'received_chunks': received_chunks})

    return jsonify({'received_chunks': 0})

@app.route('/upload_chunk', methods=['POST'])
def upload_chunk():
    """チャンクを受信し、最終チャンク受信時にファイルを確定する。

    ファイルのバイト列は変更せず、ブラウザーが公開する最終更新日時を復元する。
    フォルダー選択時は相対パスも維持する。
    """
    chunk = request.files.get('chunk')
    raw_path = request.form.get('relative_path') or request.form.get('filename', '')
    try:
        relative_path = sanitize_relative_path(raw_path)
        final_filepath = resolve_upload_path(relative_path)
        upload_id = normalize_upload_id(request.form.get('upload_id', ''))
        chunk_index = int(request.form.get('chunk_index', '0'))
        total_chunks = int(request.form.get('total_chunks', '1'))
        file_size = int(request.form.get('file_size', '-1'))
    except (ValueError, TypeError):
        logger.warning("[%s] 無効なチャンクメタデータが送信されました。", request.remote_addr)
        return "データが無効です", 400

    if (
        not chunk
        or chunk_index < 0
        or total_chunks < 1
        or chunk_index >= total_chunks
        or file_size < 0
    ):
        logger.warning("[%s] 無効なチャンクデータが送信されました。", request.remote_addr)
        return "データが無効です", 400

    try:
        last_modified_ms = int(request.form.get('last_modified_ms', ''))
    except (ValueError, TypeError):
        last_modified_ms = None

    temp_filepath = Path(TEMP_FOLDER) / upload_id

    try:
        temp_filepath.parent.mkdir(parents=True, exist_ok=True)
        # a+b では seek() 後も常に末尾へ書き込まれるため、r+b / w+b を使う。
        mode = 'r+b' if temp_filepath.exists() else 'w+b'
        with temp_filepath.open(mode) as file_handle:
            file_handle.seek(chunk_index * CHUNK_SIZE)
            shutil.copyfileobj(chunk.stream, file_handle)
        logger.debug(
            "[%s] チャンク受信完了: '%s' (%s/%s)",
            request.remote_addr, relative_path, chunk_index + 1, total_chunks,
        )
    except OSError as exc:
        logger.error("[%s] チャンクの書き込みに失敗しました '%s': %s", request.remote_addr, relative_path, exc)
        return "書き込みエラー", 500

    if chunk_index == total_chunks - 1:
        try:
            actual_size = temp_filepath.stat().st_size
            if actual_size != file_size:
                logger.warning(
                    "[%s] ファイルサイズ不一致: '%s' (expected=%s, actual=%s)",
                    request.remote_addr, relative_path, file_size, actual_size,
                )
                return "受信サイズが一致しません", 400

            final_filepath.parent.mkdir(parents=True, exist_ok=True)
            if last_modified_ms is not None:
                # File.lastModified は Unix epoch からのミリ秒。
                # atime は送信元から取得できないため、mtime のみを復元する。
                os.utime(temp_filepath, (datetime.datetime.now().timestamp(), last_modified_ms / 1000))
            # 既存ファイル削除を先行させず、置換を原子的に行う。
            os.replace(temp_filepath, final_filepath)
            logger.info(
                "[%s] 転送完了: '%s' を保存しました (last_modified_preserved=%s)",
                request.remote_addr, relative_path, last_modified_ms is not None,
            )
        except (OSError, ValueError, OverflowError) as exc:
            logger.error("[%s] ファイル確定処理に失敗しました '%s': %s", request.remote_addr, relative_path, exc)
            return "ファイル処理エラー", 500

    return "OK", 200

@app.route('/upload_cancel', methods=['POST'])
def upload_cancel():
    """中断したアップロードの一時ファイルを削除する。"""
    try:
        upload_id = normalize_upload_id(request.form.get('upload_id', ''))
    except ValueError:
        logger.warning("[%s] 無効なキャンセルリクエスト。", request.remote_addr)
        return "データが無効です", 400

    temp_filepath = Path(TEMP_FOLDER) / upload_id
    if temp_filepath.is_file():
        try:
            temp_filepath.unlink()
            logger.info("[%s] [キャンセル] 一時ファイル '%s' を削除しました。", request.remote_addr, upload_id)
        except OSError as exc:
            logger.error("[%s] [警告] 一時ファイルの削除に失敗しました: %s", request.remote_addr, exc)
            return "削除エラー", 500

    return "OK", 200

if __name__ == '__main__':
    logger.info(f"サーバーを起動しています (Host: {HOST}, Port: {PORT})...")
    app.run(host=HOST, port=PORT)