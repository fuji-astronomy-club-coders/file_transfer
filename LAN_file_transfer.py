import datetime
import logging
import os
import shutil
from pathlib import Path

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
    'PORT': 5000
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
PORT = config.get('PORT', 5000)

app = Flask(__name__)

@app.before_request
def suppress_werkzeug_logs_after_first_request():
    """初回アクセス時にWerkzeugのログレベルをWARNINGに変更し、その旨をログに残す"""
    logger.info(f"[{request.remote_addr}] 初回接続を検知したため、Werkzeugのログレベルを WARNING に変更します。")
    logging.getLogger('werkzeug').setLevel(logging.WARNING)
    
    # 2回目以降のアクセスで毎回実行されないよう関数自体を取り除く
    app.before_request_funcs[None].remove(suppress_werkzeug_logs_after_first_request)
    
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
    </style>
</head>
<body>
    <h1>ファイルを送信</h1>
    <form id="uploadForm">
        <div class="input-group">
            <label><b>① ファイルを選択 (複数選択可)</b></label><br>
            <input type="file" id="fileInput" accept=".*" multiple>
        </div>
        <div class="input-group">
            <label><b>② フォルダごと選択</b></label><br>
            <input type="file" id="folderInput" webkitdirectory directory multiple>
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

        // キュー表示を更新する関数 (修正)
        function renderQueue() {
            queueBody.innerHTML = '';
            fileQueue.forEach((file, index) => {
                const tr = document.createElement('tr');
                
                const tdName = document.createElement('td');
                tdName.innerText = file.name;
                tdName.title = file.name;
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
                const checkRes = await fetch(`/check_file_exists?filename=${encodeURIComponent(file.name)}`);
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

            const totalChunks = Math.ceil(file.size / CHUNK_SIZE);
            const rawId = `${file.name}_${file.size}_${file.lastModified}`;
            currentUploadId = rawId.replace(/[^a-zA-Z0-9._-]/g, '_');

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

                        await sendChunk(chunk, file.name, currentUploadId, chunkIndex, totalChunks, (loadedInChunk) => {
                            if (isPaused || isCanceled) return;
                            const totalLoadedBytes = (chunkIndex * CHUNK_SIZE) + loadedInChunk;
                            progressBar.value = Math.round((totalLoadedBytes / file.size) * 100);
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

        function sendChunk(chunk, fileName, uploadId, chunkIndex, totalChunks, onProgress) {
            return new Promise((resolve, reject) => {
                const formData = new FormData();
                formData.append('chunk', chunk);
                formData.append('filename', fileName);
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

@app.route('/')
def index():
    logger.info(f"[{request.remote_addr}] トップページにアクセスしました。")
    return render_template_string(HTML_PAGE, retry_lim=RETRY_LIM, chunk_size=CHUNK_SIZE)

@app.route('/check_file_exists', methods=['GET'])
def check_file_exists():
    filename = secure_filename(request.args.get('filename', ''))
    if not filename:
        return jsonify({'exists': False})
    
    filepath = os.path.join(UPLOAD_FOLDER, filename)
    exists = os.path.exists(filepath)
    logger.info(f"[{request.remote_addr}] ファイル重複チェック: '{filename}' -> 存在: {exists}")
    return jsonify({'exists': exists})

@app.route('/upload_status', methods=['GET'])
def upload_status():
    upload_id = secure_filename(request.args.get('upload_id', ''))
    if not upload_id:
        return jsonify({'received_chunks': 0})

    temp_filepath = os.path.join(TEMP_FOLDER, upload_id)
    if os.path.exists(temp_filepath):
        current_size = os.path.getsize(temp_filepath)
        received_chunks = current_size // CHUNK_SIZE
        logger.info(f"[{request.remote_addr}] レジューム確認: ID '{upload_id}', 完了チャンク数: {received_chunks}")
        return jsonify({'received_chunks': received_chunks})
    
    return jsonify({'received_chunks': 0})

@app.route('/upload_chunk', methods=['POST'])
def upload_chunk():
    chunk = request.files.get('chunk')
    filename = secure_filename(request.form.get('filename', ''))
    upload_id = secure_filename(request.form.get('upload_id', ''))
    chunk_index = int(request.form.get('chunk_index', 0))
    total_chunks = int(request.form.get('total_chunks', 1))

    if not chunk or not filename or not upload_id:
        logger.warning(f"[{request.remote_addr}] 無効なチャンクデータが送信されました。")
        return "データが無効です", 400

    temp_filepath = os.path.join(TEMP_FOLDER, upload_id)

    try:
        with open(temp_filepath, 'a+b') as f:
            f.seek(chunk_index * CHUNK_SIZE)
            f.write(chunk.read())
        logger.debug(f"[{request.remote_addr}] チャンク受信完了: '{filename}' ({chunk_index + 1}/{total_chunks})")
    except Exception as e:
        logger.error(f"[{request.remote_addr}] チャンクの書き込みに失敗しました '{filename}': {e}")
        return "書き込みエラー", 500

    if chunk_index == total_chunks - 1:
        final_filepath = os.path.join(UPLOAD_FOLDER, filename)
        try:
            if os.path.exists(final_filepath):
                os.remove(final_filepath)
                logger.info(f"[{request.remote_addr}] 既存ファイル '{filename}' を上書きのために削除しました。")
            os.rename(temp_filepath, final_filepath)
            logger.info(f"[{request.remote_addr}] 転送完了: '{filename}' が '{UPLOAD_FOLDER}' フォルダに保存されました。")
        except Exception as e:
            logger.error(f"[{request.remote_addr}] 一時ファイルから最終ファイルへの結合・移動に失敗しました: {e}")
            return "ファイル処理エラー", 500

    return "OK", 200

@app.route('/upload_cancel', methods=['POST'])
def upload_cancel():
    upload_id = secure_filename(request.form.get('upload_id', ''))
    if not upload_id:
        logger.warning(f"[{request.remote_addr}] 無効なキャンセルリクエスト。")
        return "データが無効です", 400

    temp_filepath = os.path.join(TEMP_FOLDER, upload_id)
    if os.path.exists(temp_filepath):
        try:
            os.remove(temp_filepath)
            logger.info(f"[{request.remote_addr}] [キャンセル] 一時ファイル '{upload_id}' を削除しました。")
        except Exception as e:
            logger.error(f"[{request.remote_addr}] [警告] 一時ファイルの削除に失敗しました: {e}")

    return "OK", 200

if __name__ == '__main__':
    logger.info(f"サーバーを起動しています (Host: {HOST}, Port: {PORT})...")
    app.run(host=HOST, port=PORT)