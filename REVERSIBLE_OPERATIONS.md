# 可逆操作によるエージェント安全設計

LocalCoder の危険性を、禁止コマンド一覧ではなく「操作前の状態へ確実に戻せるか」で捉えるための設計文書。

## 1. 基本原則

エージェントにとって危険な操作とは、次のいずれかを引き起こす操作である。

- 操作前の状態を完全に復元できない
- 操作対象や移動先を追跡できない
- データの支配権が管理範囲外へ移る
- 復元方法は存在しても、確実に利用できる保証がない

一言で表すと、危険なのは **不可逆な状態変更** である。

読み取りは対象の状態を変更しないため、読み取りだけでは原則として危険を生まない。危険は、読み取った情報を外部へ送信する、公開領域へ複製する、または読み取った情報を使って不可逆な操作を行う時に発生する。

## 2. LocalCoder に必要な改造方針

LocalCoder の自律性を保ったまま弱点を抑制するには、承認ダイアログを増やすのではなく、通常のローカル変更をすべて記録し、ターン単位で戻せるようにする。

中心となる構造は **可逆操作レイヤー** である。

```text
ユーザーの依頼
    ↓
トランザクション開始
    ↓
変更前状態を保存
    ↓
エージェントが操作
    ↓
変更内容を記録
    ↓
検証
    ↓
確定、またはロールバック
```

1回の `/api/chat` リクエストを1トランザクションとして扱い、その中で行われたファイル作成・編集・削除・移動をまとめて管理する。

## 3. トランザクション保存形式

作業フォルダ配下に `.localcoder/transactions/` を設ける。

```text
.localcoder/
└── transactions/
    └── 20260713-153012-a83f/
        ├── manifest.json
        ├── before/
        ├── trash/
        └── patches/
```

`manifest.json` には次を記録する。

```json
{
  "transaction_id": "20260713-153012-a83f",
  "started_at": "2026-07-13T15:30:12+09:00",
  "workspace": "/mnt/c/project",
  "status": "open",
  "operations": [
    {
      "type": "write",
      "path": "src/main.py",
      "existed_before": true,
      "before_sha256": "...",
      "backup_path": "before/src/main.py"
    },
    {
      "type": "create",
      "path": "src/new_file.py",
      "existed_before": false
    }
  ],
  "external_sends": []
}
```

同じファイルを1ターン中に何度変更しても、保存する変更前状態は最初の1回だけとする。これにより、トランザクション開始前の状態へ戻せる。

## 4. ファイル書き込み

### 4.1 変更前スナップショット

`write_file` と `edit_file` は書き込み前に必ず対象ファイルを保存する。

- 既存ファイル: 内容、権限、更新時刻、SHA-256を保存
- 新規ファイル: `existed_before=false` を記録
- 親ディレクトリを新規作成した場合: 作成したディレクトリも記録

### 4.2 原子的書き込み

対象ファイルへ直接書き込まず、同一ディレクトリの一時ファイルへ書いた後に `os.replace` で置き換える。

```python
def atomic_write(path: Path, content: str):
    temp = path.with_name(f".{path.name}.localcoder-tmp")
    with temp.open("w", encoding="utf-8") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)
```

これにより、プロセス停止や書き込みエラーによる中途半端なファイルを防ぐ。

## 5. 削除

削除は即時消去ではなく、トランザクション内の `trash/` へ移動する。

専用ツールとして次を追加する。

- `delete_file`
- `delete_directory`
- `restore_path`

`rm` や PowerShell の `Remove-Item` を単に禁止するのではなく、明白な削除操作を `run_command` で検出した場合は、専用ツールを使うようエラーを返す。

```text
ERROR: この削除操作は可逆化できません。
delete_file または delete_directory を使用してください。
```

## 6. 移動

移動は、移動元・移動先・内容が追跡されている限り可逆である。

専用ツール `move_file` を追加し、次を記録する。

- 移動元の絶対パス
- 移動先の絶対パス
- 移動元のSHA-256
- 移動先に既存オブジェクトがあったか
- 移動先の既存内容のバックアップ
- 移動完了後のSHA-256

別ドライブ間の移動は、単純な rename ではなく次の順序で実行する。

1. 移動元を記録
2. 移動先を確認
3. 移動先に既存オブジェクトがあれば保存
4. 移動先へコピー
5. ハッシュ照合
6. 移動元を `trash/` へ移動
7. 台帳を確定

これにより、CドライブからDドライブへの移動でも元へ戻せる。

## 7. `run_command` の扱い

任意のシェルコマンドは、ファイル操作以外にもレジストリ、サービス、データベース、ネットワーク、外部APIへ影響できる。したがって完全な自動ロールバックは保証できない。

`run_command` は次の3分類で扱う。

### A. 観測のみ

例:

- `ls`
- `find`
- `grep`
- `cat`
- `git status`
- `git diff`
- テストやビルド

原則としてそのまま実行する。

### B. 作業フォルダ内の状態変更

例:

- formatter
- code generator
- package install
- `git checkout`
- `git reset`

実行前後で作業フォルダの差分を採取する。

Gitリポジトリでは次を保存する。

- `git rev-parse HEAD`
- `git status --porcelain=v1 -z`
- `git diff --binary`
- `git diff --cached --binary`

非Gitディレクトリでは、ファイルパス、サイズ、更新時刻、SHA-256を比較する。

### C. 管理範囲外への操作

例:

- `git push`
- HTTP POST/PUT/PATCH
- メール送信
- ファイルアップロード
- `scp` / `ssh`
- 作業フォルダ外の書き換え
- レジストリやサービスの変更
- グローバルインストール

これらはローカルスナップショットだけでは戻せないため、外部送信・外部状態変更として別管理する。

## 8. 外部送信

外部送信は、管理範囲外へ不可逆なコピーまたは状態変更を生成する操作である。

取得操作と送信操作を分離する。

### 原則許可する取得

- `web_search`
- `fetch_url`
- HTTP GET

### 外部送信として扱う操作

- HTTP POST/PUT/PATCH
- `git push`
- メール送信
- ファイルアップロード
- 認証情報付き通信

専用ツール化する場合は次のように分ける。

- `send_http_request`
- `git_push`
- `upload_file`
- `send_email`

各操作は、送信先、送信内容、公開範囲、対象ブランチやファイルを台帳へ記録する。

ポリシーは次の3段階とする。

```text
external_send_policy:
  deny
  ask
  allow_recorded
```

- `deny`: 外部送信を拒否
- `ask`: 実行前に確認
- `allow_recorded`: 無確認で実行するが、送信内容を必ず記録

## 9. Gitリポジトリの退避

未コミット変更を守るため、エージェントが勝手にコミットを作るのではなく、LocalCoder自身のトランザクション保存を正とする。

作業開始時に次を記録する。

1. 現在のHEAD
2. ステージ状態
3. 未追跡ファイルを含む変更前スナップショット
4. `git diff --binary`
5. `git diff --cached --binary`

`git stash` だけに依存しない。未追跡ファイル、無視ファイル、ステージ状態を含めて確実に戻せるようにする。

## 10. トランザクションの状態

トランザクションは次の状態を持つ。

```text
open
completed
stopped
error
disconnected
rolled_back
committed
```

- `completed`: エージェント処理が正常終了
- `stopped`: ユーザー停止
- `error`: エラー終了
- `disconnected`: 接続切断
- `rolled_back`: 操作前へ復元済み
- `committed`: 変更を保持すると確定

停止・エラー時に自動ロールバックはしない。途中までの変更が有益な場合があるため、変更内容を残してユーザーが保持または復元を選べるようにする。

## 11. UI

各ターン終了時に、操作の要約を表示する。

```text
今回の操作
変更: 6ファイル
新規: 2ファイル
削除: 1ファイル
移動: 1ファイル
外部送信: 0件

[差分を見る] [変更を保持] [今回の操作を元に戻す]
```

履歴サイドバーにもターンごとの操作記録を表示する。

```text
15:32 コード修正
  変更 4 / 新規 1 / 削除 0 / 外部送信 0
  [差分を見る] [この操作を戻す]
```

ロールバック後は `再適用` を可能にし、undo/redoとして扱う。

## 12. API案

```text
GET  /api/transactions
GET  /api/transactions/<id>
POST /api/transactions/<id>/commit
POST /api/transactions/<id>/rollback
POST /api/transactions/<id>/reapply
GET  /api/transactions/<id>/diff
```

既存のCSRFトークンを、すべてのPOST操作で引き続き要求する。

## 13. 実装順序

### 第1段階: ファイル編集の可逆化 【実装済み 2026-07-15】

1. ターン単位のtransaction IDを発行 ✅
2. `write_file` / `edit_file` の変更前バックアップ ✅
3. 原子的書き込み ✅
4. `manifest.json`への操作記録 ✅
5. ロールバックAPI ✅ (`POST /api/transaction/rollback`。§12のREST形パスではなく
   既存エンドポイントと同じフラット形式にした。再適用=redoの
   `POST /api/transaction/reapply`も同時に実装——ロールバック自体を可逆にする
   ため、復元直前の状態を`after/`へ退避してから戻す)
6. UIに「今回の操作を元に戻す」を追加 ✅ (ターン終了サマリーカード内。
   履歴を開き直した後も`turns`の`txn_id`から同じ操作が可能)

この段階でLocalCoder自身が直接行う主要なファイル編集は可逆になった。
実装メモ: 台帳領域(`.localcoder/`)自体へのwrite_file/edit_fileは拒否する
(モデルが台帳を書き換えると可逆性の保証が壊れるため)。`.localcoder/.gitignore`
(内容は`*`)を自動作成し、ユーザーのgitリポジトリを台帳で汚さない。
単体テストは`tests/test_transactions.py`(26件)。

### 第2段階: 削除・移動の可逆化 【実装済み 2026-07-15】

1. `delete_file` ✅
2. `delete_directory` ✅ (配下の全ファイルをtrash/へ相対パスごと退避し、
   ロールバックでサブツリーを丸ごと復元。ワークスペースルート自体の削除は拒否)
3. `move_file` ✅
4. `copy_file` ✅
5. 専用ゴミ箱 ✅ (トランザクションの`trash/`。削除は即時消去せずここへ退避)
6. 移動元・移動先・ハッシュの台帳管理 ✅ (moveは移動元/移動先/上書きされた
   既存内容を記録。別ドライブ間の段階的コピーは未実装——WSL内は同一
   ファイルシステムが基本で`os.replace`が使えるため。跨ぐ場合が必要になったら追加)

実装メモ: 新規4ツールはシステムプロンプトで「`rm`/`mv`より優先。dedicated
ツールなら1ターンを丸ごとundoできるが生の`rm`は戻せない」と明示。台帳領域
(`.localcoder/`)の削除・移動先指定は拒否。ロールバックは操作を逆順に適用
(delete→復元、move→逆移動+上書き復元)し、再適用(redo)は再度削除/移動する。
サマリーカードの変更ファイル数はdelete/move/copyのdstも数える。単体テスト
14件を追加(第1段階と合わせて`tests/test_transactions.py`が40件)。

### 第3段階: 任意コマンドと外部送信 【一部実装済み 2026-07-15】

1. `run_command`前後のファイル差分検出 ⏸ 未実装(観測用。可逆化には直結しない
   ため後回し。run_commandの変更は元々自動ロールバック対象外)
2. Git差分保存 ⏸ 未実装(同上)
3. 外部送信コマンドの分類 ✅ `classify_external_send(cmd)`。git push /
   curl・wgetの送信系(POST/PUT/PATCH/--data/--form/-T/アップロード) /
   scp・sftp / rsync・sshのリモート / npm・yarn・twine・gh release・docker
   push等の公開 / aws s3・gsutilアップロード / メール送信 を検出する
   ヒューリスティック(取得=GETは対象外。難読化・変数展開は捕捉外の安全網)
4. `git push`等の専用ツール化 ⏸ 未実装(run_commandでの検出+ポリシーで
   当面の安全目標は満たせるため、専用ツール化は必要になった時点で追加)
5. 外部送信ポリシーの追加 ✅ 環境変数`LOCALCODER_EXTERNAL_SEND_POLICY`。
   `allow_recorded`(既定・従来通り実行するが送信内容を台帳へ必ず記録)と
   `deny`(実行前に拒否)を実装。`ask`(実行前にUIで同期確認)はSSEの往復承認が
   必要で複雑なため未実装——`deny`/`allow_recorded`で安全上意味のある選択は
   カバーできる

実装メモ: 「危険なのは取り消せないネットへの書き込み」という原則の中核。
外部送信は`manifest.json`の`external_sends`配列(コマンド全文・検出理由・
ポリシー・実際に実行したか)に記録され、サマリーカードに「📤 外部送信: N件」を表示。
起動時セルフチェックに「外部送信ポリシー」項目を追加。単体テスト9件を追加
(`tests/test_external_send.py`。可逆操作レイヤー全体で計49件)。

## 14. 設計上の到達点

LocalCoderの安全設計は、操作を増やさないことではなく、操作後もユーザーが状態の支配権を失わないことを目標とする。

```text
通常のローカル変更
    → 無確認で実行
    → 必ず記録
    → ターン単位で復元可能

外部送信・外部状態変更
    → 別分類
    → 送信内容を確定
    → ポリシーに従って実行
```

この構造により、LocalCoderの「確認を挟まず最後まで作業する」という性格を保ったまま、不可逆な操作だけを明確に分離できる。