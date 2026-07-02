# LocalCoder

ローカルLLM (Windows側 Ollama) だけで動く、GUIコーディングエージェント。
codex / claude code のようにファイル読み書き・コマンド実行を全自動で行う。
外部APIは一切使わない。依存ライブラリなし (Python標準ライブラリのみ)。

## 起動方法

デスクトップの **LocalCoder.bat** をダブルクリック。
(WSL内でサーバーが起動し、Edgeのアプリウィンドウが開く)

手動起動する場合:

```
wsl -d ubuntu-24.04 -- bash -lc "python3 ~/localcoder/server.py"
→ ブラウザで http://localhost:8765
```

## 使い方

1. 画面上部でモデルを選択 (推奨: gpt-oss:20b。ツール呼び出しが最も安定)
2. 作業フォルダを指定 (例: /home/fuyuki/pico_dvl/codex)
3. やりたいことを日本語で入力して送信
4. エージェントが自動でファイル作成・編集・コマンド実行・検証まで行う
   (承認プロンプトは一切なし。ツール実行内容は 🔧 カードで確認できる)

## 構成

- `server.py` — HTTPサーバー + エージェントループ (Ollama /api/chat + tools)
- `index.html` — チャットGUI (SSEストリーミング表示)
- ツール: run_command / read_file / write_file / list_dir
  - ファイル操作は作業フォルダ内に制限
  - コマンドは作業フォルダをcwdとして実行 (タイムアウト180秒)

## 前提

- Windows側 Ollama (localhost:11434)。WSLは mirrored ネットワークなので直結。
- コンテキスト長はWindows環境変数 OLLAMA_CONTEXT_LENGTH=32768 で拡大済み。

## モデルの目安 (RTX 3070 8GB VRAM)

- `gpt-oss:20b` — 推奨。MoEで実質高速、ツール呼び出しが確実
- `glm-4.7-flash` — 高性能だが19GBなのでCPU分担が大きく遅め
- `qwen3:8b` — 軽量。簡単なタスク向け
