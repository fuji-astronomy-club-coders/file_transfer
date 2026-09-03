import os
import shutil
import yaml
import logging
from flask import Flask, request, jsonify, render_template_string
from werkzeug.utils import secure_filename

# --- 1. 詳細なロギングの設定 ---
LOG_FILE = 'transfer_server.log'
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# --- 2. YAML設定の読み込みと生成 ---
CONFIG_FILE = 'config.yaml'
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

# --- HTMLテンプレート (前回から変更なし) ---
HTML_PAGE = """
<!DOCTYPE html>
<html lang="ja">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>ファイルアップロード</title>
    <style>
        body { font-family: sans-serif; padding: 20px; }
        #progressBar { display: none; width: 100%; max-width: 500px; height: 20px; margin-top: 10px; }
        #status { margin-top: 10px; font-weight: bold; }
        #speedInfo { margin-top: 5px; color: #555; font-size: 0.9em; }
        button { padding: 6px 14px; margin-right: 5px; cursor: pointer; }
        button:disabled { cursor: not-allowed; opacity: 0.6; }
    </style>
</head>
<body>
    <h1>動画・.serファイルを送信</h1>
    <form id="uploadForm">
        <input type="file" id="fileInput" name="video_file" accept="video/*,.ser" required>
        <br><br>
        <button type="submit" id="uploadBtn">アップロード</button>
        <button type="button" id="pauseBtn" style="display:none;">一時停止</button>
        <button type="button" id="cancelBtn" style="display:none;">キャンセル</button>
    </form>

    <progress id="progressBar" value="0" max="100"></progress>
    <div id="status"></div>
    <div id="speedInfo"></div>

    <script>
        const retry_lim = {{ retry_lim }};
        const CHUNK_SIZE = {{ chunk_size }};

        let currentXhr = null;
        let isPaused = false;
        let isCanceled = false;
        let currentUploadId = '';
        let sessionStartTime = 0;
        let sessionStartBytes = 0;

        const uploadForm = document.getElementById('uploadForm');
        const fileInput = document.getElementById('fileInput');
        const uploadBtn = document.getElementById('uploadBtn');
        const pauseBtn = document.getElementById('pauseBtn');
        const cancelBtn = document.getElementById('cancelBtn');
        const progressBar = document.getElementById('progressBar');
        const status = document.getElementById('status');
        const speedInfo = document.getElementById('speedInfo');

        function resetUI() {
            uploadBtn.disabled = false;
            fileInput.disabled = false;
            pauseBtn.style.display = 'none';
            cancelBtn.style.display = 'none';
            pauseBtn.innerText = '一時停止';
            speedInfo.innerText = '';
            isPaused = false;
            isCanceled = false;
            currentXhr = null;
            currentUploadId = '';
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

        pauseBtn.addEventListener('click', function() {
            if (isCanceled) return;
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
            if (isCanceled) return;
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
            status.innerText = 'アップロードをキャンセルしました。';
            progressBar.style.display = 'none';
            progressBar.value = 0;
            resetUI();
        });

        uploadForm.addEventListener('submit', async function(e) {
            e.preventDefault();
            const file = fileInput.files[0];
            if (!file) return;

            try {
                const checkRes = await fetch(`/check_file_exists?filename=${encodeURIComponent(file.name)}`);
                if (checkRes.ok) {
                    const checkData = await checkRes.json();
                    if (checkData.exists) {
                        const confirmOverwrite = confirm(`同名のファイル「${file.name}」が既に存在します。\\n上書きして続行しますか？`);
                        if (!confirmOverwrite) {
                            status.innerText = '送信を中止しました。';
                            status.style.color = 'black';
                            return;
                        }
                    }
                }
            } catch (err) {
                console.warn("重複チェックエラー:", err);
            }

            resetUI();
            uploadBtn.disabled = true;
            fileInput.disabled = true;
            pauseBtn.style.display = 'inline-block';
            cancelBtn.style.display = 'inline-block';
            status.style.color = 'black';
            progressBar.style.display = 'block';
            progressBar.value = 0;

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
                status.innerText = `完了しています！`;
                status.style.color = 'green';
                resetUI();
                fileInput.value = '';
                return;
            }

            for (let chunkIndex = startChunk; chunkIndex < totalChunks; chunkIndex++) {
                if (isCanceled) break;
                while (isPaused) {
                    if (isCanceled) break;
                    await new Promise(res => setTimeout(res, 200));
                }
                if (isCanceled) break;

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
                    if (isCanceled) break;
                    while (isPaused) {
                        if (isCanceled) break;
                        await new Promise(res => setTimeout(res, 200));
                    }
                    if (isCanceled) break;

                    try {
                        if (attempts > 0) {
                            status.innerText = `通信エラー再試行中... (${chunkIndex + 1}/${totalChunks} チャンク, リトライ ${attempts}/${retry_lim})`;
                            status.style.color = 'orange';
                            speedInfo.innerText = '';
                            await new Promise(res => setTimeout(res, 1000 * attempts));
                            if (isCanceled) break;
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
                        if (isCanceled) break;
                        if (err.message === 'paused') continue;
                        attempts++;
                        if (attempts > retry_lim) {
                            status.innerText = `エラー: 送信に失敗しました。`;
                            status.style.color = 'red';
                            speedInfo.innerText = '';
                            resetUI();
                            return;
                        }
                    }
                }
            }

            if (isCanceled) return;
            progressBar.value = 100;
            status.innerText = `「${file.name}」のアップロードが完了しました！`;
            status.style.color = 'green';
            speedInfo.innerText = '';
            progressBar.style.display = 'none';
            resetUI();
            fileInput.value = '';
        });

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
        logger.info(f"[{request.remote_addr}] チャンク受信完了: '{filename}' ({chunk_index + 1}/{total_chunks})")
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
