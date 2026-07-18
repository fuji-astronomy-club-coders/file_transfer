import os
from flask import Flask, request, render_template_string
from werkzeug.utils import secure_filename

app = Flask(__name__)

# 保存するフォルダ名
UPLOAD_FOLDER = 'uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# 進捗バー付きのHTML（JavaScriptを追加）
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
        document.getElementById('uploadForm').addEventListener('submit', function(e) {
            e.preventDefault(); // 通常の画面遷移をストップ

            var fileInput = document.getElementById('fileInput');
            var file = fileInput.files[0];
            if (!file) return;

            var formData = new FormData();
            formData.append('video_file', file);

            var xhr = new XMLHttpRequest();
            var progressBar = document.getElementById('progressBar');
            var status = document.getElementById('status');
            var uploadBtn = document.getElementById('uploadBtn');

            // 送信ボタンを一時的に無効化
            uploadBtn.disabled = true;
            status.style.color = "black";

            // 進捗状況を監視してプログレスバーを更新
            xhr.upload.addEventListener('progress', function(e) {
                if (e.lengthComputable) {
                    var percentComplete = Math.round((e.loaded / e.total) * 100);
                    progressBar.style.display = 'block';
                    progressBar.value = percentComplete;
                    status.innerText = percentComplete + '% アップロード中...';
                }
            }, false);

            // アップロード完了時
            xhr.addEventListener('load', function(e) {
                if (xhr.status === 200) {
                    status.innerHTML = xhr.responseText;
                    status.style.color = "green";
                } else {
                    status.innerText = 'エラーが発生しました。';
                    status.style.color = "red";
                }
                progressBar.style.display = 'none';
                progressBar.value = 0;
                uploadBtn.disabled = false;
                fileInput.value = ''; // ファイル選択をリセット
            });

            // エラー時
            xhr.addEventListener('error', function(e) {
                status.innerText = '通信エラーが発生しました。';
                status.style.color = "red";
                progressBar.style.display = 'none';
                uploadBtn.disabled = false;
            });

            xhr.open('POST', '/', true);
            xhr.send(formData);
        });
    </script>
</body>
</html>
"""

@app.route('/', methods=['GET', 'POST'])
def upload_file():
    if request.method == 'POST':
        if 'video_file' not in request.files:
            return "ファイルが見つかりません", 400
        
        file = request.files['video_file']

        # filename may be None; ensure it's a string and secure it
        filename = secure_filename(file.filename or '')
        if filename == '':
            return "ファイルが選択されていません", 400

        if file:
            filepath = os.path.join(UPLOAD_FOLDER, filename)
            file.save(filepath)
            # 画面遷移しないので、シンプルな成功メッセージのみを返す
            return f"「{file.filename}」のアップロードが完了しました！"
            
    return render_template_string(HTML_PAGE)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)