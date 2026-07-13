"""テスト共通ヘルパー。

pytest等の外部依存を増やさず、標準ライブラリのunittestのみでテストを完結させる
(LocalCoder本体の「依存ライブラリなし」方針に合わせる)。fixtureの読み込みと、
Ollama呼び出しのスタブ化をここにまとめる。
"""
import json
import unittest
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load_fixture(name: str) -> dict:
    """tests/fixtures/<name> を読み込む。

    fixturesは実際に問題が起きたセッションの実データ(パスのみ匿名化)を含むため
    .gitignore対象で、このマシンにしか存在しない。git clone直後など該当
    ファイルが無い環境では、このテストだけを(スイート全体を落とさずに)
    スキップする。
    """
    path = FIXTURES / name
    if not path.is_file():
        raise unittest.SkipTest(
            f"{name} が無いためスキップ(tests/fixtures/は.gitignore対象のローカル専用データ)")
    return json.loads(path.read_text(encoding="utf-8"))


def load_fixture_messages(name: str) -> list:
    return load_fixture(name)["messages"]


class FakeOllama:
    """ollama_askの差し替え用スタブ。

    呼び出しごとのプロンプトを`calls`に記録し、`responses`を順に返す
    (尽きたら`default`を返し続ける)。実際のOllama/GPUを使わずに圧縮ロジックを
    検証するために使う。
    """

    def __init__(self, responses=None, default="SUMMARY"):
        self.calls = []
        self._responses = list(responses or [])
        self._default = default

    def __call__(self, model, prompt):
        self.calls.append(prompt)
        if self._responses:
            return self._responses.pop(0)
        return self._default
