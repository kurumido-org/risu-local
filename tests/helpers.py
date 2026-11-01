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

サーバーの起動は不要（HTTP の契約は TestClient で実行する．起動済みサーバーを叩きたいときは
RISU_TEST_API_URL を設定する）．実 LLM も呼ばない（下の偽クライアント）．
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


# ============================================================
# 要求・ツール履歴を記録する偽クライアント（テスト設計 §5 / §13）
#
# script の各要素は次のいずれか:
#   _FakeMessage          そのまま応答として返す
#   Exception のインスタンス  その呼び出しで raise する（接続失敗・レート制限などの注入）
#   callable(kwargs) -> _FakeMessage | Exception   要求を見て応答を決める
# 全ての要求（kind, kwargs）は .messages.calls に残る．ツール呼び出しは tool_uses() で取り出せる．
# ============================================================

class _RecordingMessages(_FakeMessages):
    def _next(self, kind, kwargs):
        self.calls.append((kind, kwargs))
        if not self._script:
            raise AssertionError("スタブの応答が尽きた（想定より多く API を呼んでいる）")
        item = self._script.pop(0)
        if callable(item) and not isinstance(item, _FakeMessage):
            item = item(kwargs)
        if isinstance(item, BaseException):
            raise item
        return item


class RecordingFakeAnthropic(_FakeAnthropic):
    """要求を記録し，例外注入もできる偽 anthropic.Anthropic．"""

    def __init__(self, script):
        self.messages = _RecordingMessages(script)

    @property
    def calls(self):
        return self.messages.calls

    def tool_uses(self):
        """アプリが API へ返した tool_use → tool_result の対応を，送信した messages から復元する．
        戻り値: [(tool_name, tool_input, tool_result_content, is_error)]"""
        out = []
        seen_ids = set()
        for _kind, kw in self.calls:
            msgs = kw.get("messages") or []
            for i, m in enumerate(msgs):
                if m["role"] != "assistant" or not isinstance(m["content"], list):
                    continue
                uses = {getattr(b, "id", None): b for b in m["content"]
                        if getattr(b, "type", None) == "tool_use"}
                if not uses or i + 1 >= len(msgs):
                    continue
                for tr in msgs[i + 1]["content"] if isinstance(msgs[i + 1]["content"], list) else []:
                    if isinstance(tr, dict) and tr.get("type") == "tool_result" and tr.get("tool_use_id") in uses:
                        if tr["tool_use_id"] in seen_ids:
                            continue
                        seen_ids.add(tr["tool_use_id"])
                        b = uses[tr["tool_use_id"]]
                        out.append((b.name, b.input, tr.get("content"), bool(tr.get("is_error"))))
        return out

    def forced_tool_choices(self):
        """tool_choice を付けた要求（強制再試行）の一覧．"""
        return [kw["tool_choice"] for _k, kw in self.calls if kw.get("tool_choice")]


def collect_sse(body, script, *, fake=None):
    """偽クライアントで chat_claude_stream を回し，(SSE イベント列, fake) を返す．

    body は risu.schema.ChatInput．script は RecordingFakeAnthropic の script．
    anthropic.Anthropic を差し替えて実 API は呼ばない．
    """
    import asyncio
    import json
    import sys

    import risu.llm

    anthropic_mod = sys.modules.get("anthropic")
    if anthropic_mod is None:
        import anthropic as anthropic_mod  # noqa: F811
    fake = fake or RecordingFakeAnthropic(script)
    orig = anthropic_mod.Anthropic
    orig_key = risu.llm.ANTHROPIC_API_KEY
    anthropic_mod.Anthropic = lambda **kw: fake
    risu.llm.ANTHROPIC_API_KEY = "dummy-key-for-tests"
    try:
        async def go():
            resp = await risu.llm.chat_claude_stream(body)
            chunks = []
            async for c in resp.body_iterator:
                chunks.append(c if isinstance(c, str) else c.decode("utf-8"))
            return "".join(chunks)
        raw = asyncio.run(go())
    finally:
        anthropic_mod.Anthropic = orig
        risu.llm.ANTHROPIC_API_KEY = orig_key
    events = []
    for part in raw.split("\n\n"):
        part = part.strip()
        if part.startswith("data: "):
            events.append(json.loads(part[6:]))
    return events, fake


def chat_body(text, *, history=None, last_sim_id=None):
    """ChatInput を組み立てる（history は [(role, content), ...]）．"""
    from risu.schema import ChatInput
    msgs = [{"role": r, "content": c} for r, c in (history or [])]
    msgs.append({"role": "user", "content": text})
    return ChatInput(messages=msgs, last_sim_id=last_sim_id)


def text_msg(text, stop="end_turn"):
    return _FakeMessage([_FakeBlock("text", text=text)], stop)


def tool_msg(name, args, tid="tu_1", text=None):
    blocks = ([_FakeBlock("text", text=text)] if text else []) + [_FakeBlock("tool_use", name=name, input=args, id=tid)]
    return _FakeMessage(blocks, "tool_use")
