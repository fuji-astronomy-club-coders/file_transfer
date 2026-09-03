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
            print(f"[起動時クリーンアップ] 一時フォルダ '{TEMP_FOLDER}' を削除・削除初期化しました。")
        except Exception as e:
            print(f"[警告] 一時フォルダの削除に失敗しました: {e}")
    os.makedirs(TEMP_FOLDER, exist_ok=True)

# フォルダ生成とクリーンアップの実行
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
    </style>
</head>
<body>
    <h1>動画・.serファイルを送信</h1>
    <form id="uploadForm">
        <input type="file" id="fileInput" name="video_file" accept="video/*,.ser" required>
        <br><br>
        <button type="submit" id="uploadBtn">アップロード</button>
    </form>

    <progress id="progressBar" value="0" max="100"></progress>
    <div id="status"></div>

    <script>
        // Python側から動的に挿入される設定値
        const retry_lim = {{ retry_lim }};
        const CHUNK_SIZE = {{ chunk_size }};

        document.getElementById('uploadForm').addEventListener('submit', async function(e) {
            e.preventDefault();

            const fileInput = document.getElementById('fileInput');
            const file = fileInput.files[0];
            if (!file) return;

            const progressBar = document.getElementById('progressBar');
            const status = document.getElementById('status');
            const uploadBtn = document.getElementById('uploadBtn');

            uploadBtn.disabled = true;
            status.style.color = "black";
            progressBar.style.display = 'block';
            progressBar.value = 0;

            const totalChunks = Math.ceil(file.size / CHUNK_SIZE);

            // ファイル名・サイズ・更新日時から一意のアップロードIDを作成
            const rawId = `${file.name}_${file.size}_${file.lastModified}`;
            const uploadId = rawId.replace(/[^a-zA-Z0-9._-]/g, '_');

            // --- 1. サーバーから前回のアップロード進捗を取得（レジューム機能） ---
            let startChunk = 0;
            try {
                const res = await fetch(`/upload_status?upload_id=${uploadId}`);
                if (res.ok) {
                    const data = await res.json();
                    startChunk = data.received_chunks || 0;
                    if (startChunk > totalChunks) startChunk = totalChunks;
                }
            } catch (err) {
                console.warn("進捗ステータスの取得に失敗しました。最初から送信します。", err);
            }

            // 進捗に応じた通知表示
            if (startChunk > 0 && startChunk < totalChunks) {
                status.innerText = `前回の続き（${startChunk + 1}/${totalChunks} チャンク目）から再開します...`;
            } else if (startChunk >= totalChunks) {
                progressBar.value = 100;
                status.innerText = `「${file.name}」のアップロードはすでに完了しています！`;
                status.style.color = "green";
                uploadBtn.disabled = false;
                fileInput.value = '';
                return;
            }

            // --- 2. 未送信のチャンクからアップロードを開始 ---
            for (let chunkIndex = startChunk; chunkIndex < totalChunks; chunkIndex++) {
                const start = chunkIndex * CHUNK_SIZE;
                const end = Math.min(file.size, start + CHUNK_SIZE);
                const chunk = file.slice(start, end);

                let success = false;
                let attempts = 0;

                // 通信失敗時に retry_lim の上限までリトライを実行
                while (attempts <= retry_lim && !success) {
                    try {
                        if (attempts > 0) {
                            status.innerText = `通信エラーが発生したため再試行中... (${chunkIndex + 1}/${totalChunks} チャンク目, リトライ ${attempts}/${retry_lim})`;
                            status.style.color = "orange";
                            await new Promise(res => setTimeout(res, 1000 * attempts)); // 試行回数に応じて待機
                        } else {
                            const percent = Math.round((chunkIndex / totalChunks) * 100);
                            status.innerText = `${percent}% アップロード中... (${chunkIndex + 1}/${totalChunks} チャンク)`;
                            status.style.color = "black";
                        }

                        await sendChunk(chunk, file.name, uploadId, chunkIndex, totalChunks);
                        success = true;
                    } catch (err) {
                        attempts++;
                        if (attempts > retry_lim) {
                            status.innerText = `エラー: チャンク ${chunkIndex + 1} の送信に失敗しました（${retry_lim}回試行後失敗）。`;
                            status.style.color = "red";
                            uploadBtn.disabled = false;
                            return;
                        }
                    }
                }

                // プログレスバーの更新
                const percentComplete = Math.round(((chunkIndex + 1) / totalChunks) * 100);
                progressBar.value = percentComplete;
            }

            // 全チャンク完了時
            status.innerText = `「${file.name}」のアップロードが完了しました！`;
            status.style.color = "green";
            progressBar.style.display = 'none';
            uploadBtn.disabled = false;
            fileInput.value = '';
        });

        // チャンク単位での送信関数
        function sendChunk(chunk, fileName, uploadId, chunkIndex, totalChunks) {
            return new Promise((resolve, reject) => {
                const formData = new FormData();
                formData.append('chunk', chunk);
                formData.append('filename', fileName);
                formData.append('upload_id', uploadId);
                formData.append('chunk_index', chunkIndex);
                formData.append('total_chunks', totalChunks);

                const xhr = new XMLHttpRequest();
                xhr.open('POST', '/upload_chunk', true);

                xhr.onload = function() {
                    if (xhr.status === 200) {
                        resolve();
                    } else {
                        reject(new Error('サーバーエラー'));
                    }
                };

                xhr.onerror = function() {
                    reject(new Error('通信エラー'));
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

# 現在の一時ファイルの保存状況（何チャンク目まで受け取ったか）を返すAPI
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

# チャンクの書き込みエンドポイント
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

    # 特定の位置へ精度高く追記 (seekを利用)
    with open(temp_filepath, 'a+b') as f:
        f.seek(chunk_index * CHUNK_SIZE)
        f.write(chunk.read())

    # 最後のチャンクが完了したら、一時ファイルから本保存フォルダへ移動
    if chunk_index == total_chunks - 1:
        final_filepath = os.path.join(UPLOAD_FOLDER, filename)
        if os.path.exists(final_filepath):
            os.remove(final_filepath)
        os.rename(temp_filepath, final_filepath)

    return "OK", 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
