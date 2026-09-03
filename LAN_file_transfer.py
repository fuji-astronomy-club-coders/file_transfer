import os
import shutil
from flask import Flask, request, jsonify, render_template_string
from werkzeug.utils import secure_filename

app = Flask(__name__)

# 設定値
RETRY_LIM = 3                       # リトライ上限回数 (Python側で変更可能)
CHUNK_SIZE = 5 * 1024 * 1024        # 1チャンクあたりのサイズ（5MB）
UPLOAD_FOLDER = 'uploads'
TEMP_FOLDER = 'temp_uploads'

def cleanup_temp_folder():
    """起動時に未完了の過去の一時ファイルを全削除する関数"""
    if os.path.exists(TEMP_FOLDER):
        try:
            shutil.rmtree(TEMP_FOLDER)
            print(f"[起動時クリーンアップ] 一時フォルダ '{TEMP_FOLDER}' を削除初期化しました。")
        except Exception as e:
            print(f"[警告] 一時フォルダの削除に失敗しました: {e}")
    os.makedirs(TEMP_FOLDER, exist_ok=True)

# フォルダ生成と起動時クリーンアップ
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
cleanup_temp_folder()

HTML_PAGE = """
<!DOCTYPE html>
<html lang="ja">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>ファイルアップロード</title>
    <style>
        body { font-family: sans-serif; padding: 20px; }
        #progressBar { display: none; width: 100%; max-width: 400px; height: 20px; margin-top: 10px; }
        #status { margin-top: 10px; font-weight: bold; }
        .btn-group { margin-top: 10px; }
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

    <script>
        // Python側から動的に挿入される設定値
        const retry_lim = {{ retry_lim }};
        const CHUNK_SIZE = {{ chunk_size }};

        // 状態管理変数
        let currentXhr = null;
        let isPaused = false;
        let isCanceled = false;
        let currentUploadId = '';

        const uploadForm = document.getElementById('uploadForm');
        const fileInput = document.getElementById('fileInput');
        const uploadBtn = document.getElementById('uploadBtn');
        const pauseBtn = document.getElementById('pauseBtn');
        const cancelBtn = document.getElementById('cancelBtn');
        const progressBar = document.getElementById('progressBar');
        const status = document.getElementById('status');

        // UI表示リセット関数
        function resetUI() {
            uploadBtn.disabled = false;
            fileInput.disabled = false;
            pauseBtn.style.display = 'none';
            cancelBtn.style.display = 'none';
            pauseBtn.innerText = '一時停止';
            isPaused = false;
            isCanceled = false;
            currentXhr = null;
            currentUploadId = '';
        }

        // 一時停止 / 再開 ボタンの処理
        pauseBtn.addEventListener('click', function() {
            if (isCanceled) return;

            if (!isPaused) {
                isPaused = true;
                pauseBtn.innerText = '再開';
                status.innerText = '一時停止中...';
                status.style.color = 'orange';
                if (currentXhr) {
                    currentXhr.abort(); // 現在通信中のチャンクを中断
                }
            } else {
                isPaused = false;
                pauseBtn.innerText = '一時停止';
                status.innerText = '送信を再開中...';
                status.style.color = 'black';
            }
        });

        // キャンセル ボタンの処理
        cancelBtn.addEventListener('click', async function() {
            if (isCanceled) return;

            isCanceled = true;
            isPaused = false;

            if (currentXhr) {
                currentXhr.abort(); // 通信を中断
            }

            status.innerText = 'アップロードを取り消し中...';
            status.style.color = 'red';

            // サーバー側の一時ファイルを削除
            if (currentUploadId) {
                try {
                    const formData = new FormData();
                    formData.append('upload_id', currentUploadId);
                    await fetch('/upload_cancel', { method: 'POST', body: formData });
                } catch (err) {
                    console.warn('キャンセルリクエスト送信に失敗しました:', err);
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

            // --- 1. サーバーから前回のアップロード進捗を取得 ---
            let startChunk = 0;
            try {
                const res = await fetch(`/upload_status?upload_id=${currentUploadId}`);
                if (res.ok) {
                    const data = await res.json();
                    startChunk = data.received_chunks || 0;
                    if (startChunk > totalChunks) startChunk = totalChunks;
                }
            } catch (err) {
                console.warn("進捗ステータスの取得に失敗しました。最初から送信します。", err);
            }

            if (startChunk > 0 && startChunk < totalChunks) {
                status.innerText = `前回の続き（${startChunk + 1}/${totalChunks} チャンク目）から再開します...`;
            } else if (startChunk >= totalChunks) {
                progressBar.value = 100;
                status.innerText = `「${file.name}」のアップロードはすでに完了しています！`;
                status.style.color = 'green';
                resetUI();
                fileInput.value = '';
                return;
            }

            // --- 2. 未送信のチャンクからアップロードを開始 ---
            for (let chunkIndex = startChunk; chunkIndex < totalChunks; chunkIndex++) {
                if (isCanceled) break;

                // 一時停止状態の待機ループ
                while (isPaused) {
                    if (isCanceled) break;
                    await new Promise(res => setTimeout(res, 200));
                }
                if (isCanceled) break;

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
                            status.innerText = `通信エラーが発生したため再試行中... (${chunkIndex + 1}/${totalChunks} チャンク目, リトライ ${attempts}/${retry_lim})`;
                            status.style.color = 'orange';
                            await new Promise(res => setTimeout(res, 1000 * attempts));
                            if (isCanceled) break;
                        } else {
                            const percent = Math.round((chunkIndex / totalChunks) * 100);
                            status.innerText = `${percent}% アップロード中... (${chunkIndex + 1}/${totalChunks} チャンク)`;
                            status.style.color = 'black';
                        }

                        await sendChunk(chunk, file.name, currentUploadId, chunkIndex, totalChunks);
                        success = true;
                    } catch (err) {
                        if (isCanceled) break;

                        // 一時停止で中断された場合はリトライ回数としてカウントしない
                        if (err.message === 'paused') {
                            continue;
                        }

                        attempts++;
                        if (attempts > retry_lim) {
                            status.innerText = `エラー: チャンク ${chunkIndex + 1} の送信に失敗しました（${retry_lim}回試行後失敗）。`;
                            status.style.color = 'red';
                            resetUI();
                            return;
                        }
                    }
                }

                if (isCanceled) break;

                const percentComplete = Math.round(((chunkIndex + 1) / totalChunks) * 100);
                progressBar.value = percentComplete;
            }

            if (isCanceled) return;

            // アップロード成功時
            status.innerText = `「${file.name}」のアップロードが完了しました！`;
            status.style.color = 'green';
            progressBar.style.display = 'none';
            resetUI();
            fileInput.value = '';
        });

        // チャンク送信関数
        function sendChunk(chunk, fileName, uploadId, chunkIndex, totalChunks) {
            return new Promise((resolve, reject) => {
                const formData = new FormData();
                formData.append('chunk', chunk);
                formData.append('filename', fileName);
                formData.append('upload_id', uploadId);
                formData.append('chunk_index', chunkIndex);
                formData.append('total_chunks', totalChunks);

                const xhr = new XMLHttpRequest();
                currentXhr = xhr; // アボート（中断）できるように参照を保持
                xhr.open('POST', '/upload_chunk', true);

                xhr.onload = function() {
                    currentXhr = null;
                    if (xhr.status === 200) {
                        resolve();
                    } else {
                        reject(new Error('サーバーエラー'));
                    }
                };

                xhr.onerror = function() {
                    currentXhr = null;
                    reject(new Error('通信エラー'));
                };

                xhr.onabort = function() {
                    currentXhr = null;
                    if (isCanceled) {
                        reject(new Error('canceled'));
                    } else if (isPaused) {
                        reject(new Error('paused'));
                    } else {
                        reject(new Error('aborted'));
                    }
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
    return render_template_string(
        HTML_PAGE, 
        retry_lim=RETRY_LIM, 
        chunk_size=CHUNK_SIZE
    )

# 進捗確認API
@app.route('/upload_status', methods=['GET'])
def upload_status():
    upload_id = secure_filename(request.args.get('upload_id', ''))
    if not upload_id:
        return jsonify({'received_chunks': 0})

    temp_filepath = os.path.join(TEMP_FOLDER, upload_id)
    if os.path.exists(temp_filepath):
        current_size = os.path.getsize(temp_filepath)
        received_chunks = current_size // CHUNK_SIZE
        return jsonify({'received_chunks': received_chunks})
    else:
        return jsonify({'received_chunks': 0})

# チャンク書き込みAPI
@app.route('/upload_chunk', methods=['POST'])
def upload_chunk():
    chunk = request.files.get('chunk')
    filename = secure_filename(request.form.get('filename', ''))
    upload_id = secure_filename(request.form.get('upload_id', ''))
    chunk_index = int(request.form.get('chunk_index', 0))
    total_chunks = int(request.form.get('total_chunks', 1))

    if not chunk or not filename or not upload_id:
        return "データが無効です", 400

    temp_filepath = os.path.join(TEMP_FOLDER, upload_id)

    with open(temp_filepath, 'a+b') as f:
        f.seek(chunk_index * CHUNK_SIZE)
        f.write(chunk.read())

    if chunk_index == total_chunks - 1:
        final_filepath = os.path.join(UPLOAD_FOLDER, filename)
        if os.path.exists(final_filepath):
            os.remove(final_filepath)
        os.rename(temp_filepath, final_filepath)

    return "OK", 200

# キャンセルAPI (一時ファイルの破棄)
@app.route('/upload_cancel', methods=['POST'])
def upload_cancel():
    upload_id = secure_filename(request.form.get('upload_id', ''))
    if not upload_id:
        return "データが無効です", 400

    temp_filepath = os.path.join(TEMP_FOLDER, upload_id)
    if os.path.exists(temp_filepath):
        try:
            os.remove(temp_filepath)
            print(f"[キャンセル] 一時ファイル '{upload_id}' を削除しました。")
        except Exception as e:
            print(f"[警告] 一時ファイルの削除に失敗しました: {e}")

    return "OK", 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
