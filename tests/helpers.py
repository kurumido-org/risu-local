"""テスト共通の定数とフェイク．

RISU 安定性テスト
================
これまでの開発で発見・修正した問題をテストとして記録．
新機能追加時にこのテストが全て通ることを確認すること．

テスト実行:
    cd risu-local
    .venv/Scripts/activate
    pip install pytest httpx
    pytest tests/ -v

注意: サーバー (python server.py) が起動している必要があるテストは
      test_api_* で始まるもの．それ以外はサーバー不要．
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from risu.schema import SimulationInput  # noqa: E402

# ============================================================
# テスト用シナリオ
# ============================================================

BOTTLENECK_SCENARIO = SimulationInput(
    name="test_bottleneck",
    tmax=1000,
    deltan=5,
    nodes=[
        {"name": "A", "x": 0, "y": 0},
        {"name": "B", "x": 5000, "y": 0, "flow_capacity": 0.4},
        {"name": "C", "x": 7500, "y": 0},
    ],
    links=[
        {"name": "r1", "start": "A", "end": "B", "length": 5000},
        {"name": "r2", "start": "B", "end": "C", "length": 2500, "free_flow_speed": 10},
    ],
    demands=[
        {"orig": "A", "dest": "C", "t_start": 0, "t_end": 600, "flow": 0.8},
    ],
)

GRID_BIDIRECTIONAL_SCENARIO = SimulationInput(
    name="test_grid",
    tmax=1000,
    deltan=5,
    nodes=[
        {"name": "n00", "x": 0, "y": 0},
        {"name": "n10", "x": 2000, "y": 0},
        {"name": "n01", "x": 0, "y": 2000},
        {"name": "n11", "x": 2000, "y": 2000},
    ],
    links=[
        {"name": "h0", "start": "n00", "end": "n10", "length": 2000},
        {"name": "h0r", "start": "n10", "end": "n00", "length": 2000},
        {"name": "v0", "start": "n00", "end": "n01", "length": 2000},
        {"name": "v0r", "start": "n01", "end": "n00", "length": 2000},
        {"name": "h1", "start": "n01", "end": "n11", "length": 2000},
        {"name": "h1r", "start": "n11", "end": "n01", "length": 2000},
        {"name": "v1", "start": "n10", "end": "n11", "length": 2000},
        {"name": "v1r", "start": "n11", "end": "n10", "length": 2000},
    ],
    demands=[
        {"orig": "n00", "dest": "n11", "t_start": 0, "t_end": 500, "flow": 0.4},
        {"orig": "n11", "dest": "n00", "t_start": 0, "t_end": 500, "flow": 0.3},
    ],
)


# ============================================================
# ストリーミング経路の通しテスト（Anthropic クライアントをスタブ化）
# ============================================================

class _FakeUsage:
    input_tokens = 100
    output_tokens = 50
    cache_read_input_tokens = 0
    cache_creation_input_tokens = 0


class _FakeBlock:
    """anthropic の content block 相当．"""

    def __init__(self, type_, *, text=None, name=None, input=None, id=None):
        self.type = type_
        self.text = text
        self.name = name
        self.input = input
        self.id = id


class _FakeMessage:
    def __init__(self, content, stop_reason):
        self.content = content
        self.stop_reason = stop_reason
        self.usage = _FakeUsage()


class _FakeStream:
    """client.messages.stream(...) の戻り値（context manager かつ iterable）．"""

    def __init__(self, message, events):
        self._message = message
        self._events = events

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __iter__(self):
        return iter(self._events)

    def get_final_message(self):
        return self._message


class _FakeMessages:
    def __init__(self, script):
        # script: 呼び出しごとに返す _FakeMessage のリスト
        self._script = list(script)
        self.calls = []

    def _next(self, kind, kwargs):
        self.calls.append((kind, kwargs))
        if not self._script:
            raise AssertionError("スタブの応答が尽きた（想定より多く API を呼んでいる）")
        return self._script.pop(0)

    def create(self, **kwargs):
        return self._next("create", kwargs)

    def stream(self, **kwargs):
        msg = self._next("stream", kwargs)
        events = []
        for b in msg.content:
            if b.type == "text":
                events.append(types_ns(
                    type="content_block_delta",
                    delta=types_ns(type="text_delta", text=b.text),
                ))
        return _FakeStream(msg, events)


def types_ns(**kw):
    import types
    return types.SimpleNamespace(**kw)


class _FakeAnthropic:
    def __init__(self, script):
        self.messages = _FakeMessages(script)
