"""チャットのバックエンド（claude / ollama / mock），Prompt Caching，履歴のトリミング，使用量集計，チャート抽出．
"""

from __future__ import annotations

import json
import os
import uuid

import httpx
from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from .aggregate import _get_simulation_data
from .prompts import CLAUDE_TOOLS, MOCK_SCENARIOS, SYSTEM_PROMPT, _mock_llm_response
from .results import _store_sim
from .schema import ChatInput, SimulationInput
from .simulation import _run_uxsim_async
from .tools import (
    _collect_tool_results,
    _conversation_context_block,
    _conversation_sim_id,
    _dispatch_tool_blocks,
    _last_user_message_text,
    _ToolTurnState,
)

# LLM バックエンド: "mock" / "claude" / "ollama"
LLM_BACKEND = os.environ.get("LLM_BACKEND", "claude")

# Ollama 設定（他の設定と同じく環境変数で上書きできる．README §8）
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
OLLAMA_MODEL    = os.getenv("OLLAMA_MODEL", "qwen2.5:3b")

# Claude 設定
CLAUDE_MODEL = "claude-sonnet-5"  # 高精度・ツール呼び出し安定（claude-sonnet-4 は 2026-06 廃止）
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")


async def _chat_mock(user_text: str):
    """モック LLM: キーワードマッチでシナリオを選択し，実際の UXsim を実行"""
    mock = _mock_llm_response(user_text)

    # テキスト応答のみ（シミュレーション不要）
    if mock["scenario_key"] is None:
        return {"role": "assistant", "content": mock["content"], "sim_id": None}

    # シミュレーション実行
    scenario_data = MOCK_SCENARIOS[mock["scenario_key"]]
    sim_input = SimulationInput(**scenario_data["scenario"])
    result = await _run_uxsim_async(sim_input)
    sim_id = str(uuid.uuid4())[:8]
    _store_sim(sim_id, result, {
        "type": "llm",
        "llm_backend": "mock",
        "scenario_key": mock["scenario_key"],
        "llm_user_message": user_text,
    })

    stats = result["stats"]
    description = scenario_data["description"]
    summary = (
        f"{description}\n\n"
        f"【統計情報】\n"
        f"・総トリップ数: {stats['total_trips']}\n"
        f"・完了トリップ数: {stats['completed_trips']}\n"
        f"・平均旅行時間: {stats['average_travel_time_s']} 秒\n"
        f"・計算時間: {stats['simulation_time_s']} 秒"
    )

    return {"role": "assistant", "content": summary, "sim_id": sim_id}


# ──────────────────────────────────────────────
# Prompt Caching ヘルパー
# ──────────────────────────────────────────────
# Anthropic の Prompt Caching は system プロンプト + tool 定義をキャッシュすることで
# 入力トークンの 90% OFF を実現する（5 分 TTL）．
# RISU は system が ~3,000 tokens，tools が ~2,000 tokens なので効果絶大．
# https://docs.anthropic.com/en/docs/prompt-caching
def _cache_ctl() -> dict:
    """cache_control ブロック（TTL は CACHE_TTL）"""
    cc = {"type": "ephemeral"}
    if CACHE_TTL and CACHE_TTL != "5m":
        cc["ttl"] = CACHE_TTL
    return cc


def _cached_system(text: str) -> list:
    """system プロンプトをキャッシュ有効形式で返す"""
    return [{
        "type": "text",
        "text": text,
        "cache_control": _cache_ctl(),
    }]

def _cached_tools() -> list:
    """tool 定義の末尾に cache_control を付与（全 tool 定義を一括キャッシュ）"""
    if not CLAUDE_TOOLS:
        return []
    tools = [dict(t) for t in CLAUDE_TOOLS]
    tools[-1] = {**tools[-1], "cache_control": _cache_ctl()}
    return tools


# ──────────────────────────────────────────────
# 会話履歴のトークン節約
#
# キャッシュ順序は tools → system → messages．以前は sim_id ごとに変わる
# 【現在のコンテキスト】を system に足していたため，rerun のたびに system 以降の
# キャッシュが全滅していた．今は
#   1. system は SYSTEM_PROMPT のみ（不変）
#   2. 動的コンテキストは最後のユーザーメッセージに別ブロックとして付ける
#   3. 履歴の最後の assistant メッセージに cache_control（ターン跨ぎで prefix ヒット）
#   4. 各リクエストの最後のメッセージに cache_control（同一ターン内の tool ラウンドで
#      prefix ヒット．以前は tool ラウンドごとに履歴全体を再課金していた）
# breakpoint は tools / system / 履歴 assistant / 末尾 の 4 つ（API 上限）．
# ──────────────────────────────────────────────
def _msg_text(content) -> str:
    """メッセージ content（str または block list）からテキストを取り出す"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            (b.get("text", "") if isinstance(b, dict) else getattr(b, "text", "") or "")
            for b in content
        )
    return str(content or "")


def _trim_history(msgs: list[dict]) -> list[dict]:
    """文字数予算を超えた履歴を古いターンから落とす．

    予算超過時は予算の半分まで落とす（ヒステリシス）．毎ターン 1 件ずつ落とすと
    prefix が毎回変わってキャッシュが一度も当たらないため．先頭は必ず user．
    """
    if MAX_HISTORY_CHARS <= 0 or not msgs:
        return msgs
    sizes = [len(_msg_text(m["content"])) for m in msgs]
    if sum(sizes) <= MAX_HISTORY_CHARS:
        return msgs
    budget = MAX_HISTORY_CHARS // 2
    kept = []
    acc = 0
    for m, sz in zip(reversed(msgs), reversed(sizes)):
        if kept and acc + sz > budget:
            break
        kept.append(m)
        acc += sz
    kept.reverse()
    while kept and kept[0]["role"] != "user":
        kept.pop(0)
    if not kept:
        kept = [msgs[-1]]
    if len(kept) < len(msgs):
        first = kept[0]
        kept[0] = {"role": "user",
                   "content": "（これより前の会話は省略）\n\n" + _msg_text(first["content"])}
        print(f"[RISU] history trimmed: {len(msgs)} -> {len(kept)} messages "
              f"({sum(sizes)} -> {sum(len(_msg_text(m['content'])) for m in kept)} chars)")
    return kept


def _build_llm_messages(body: ChatInput) -> list[dict]:
    """ChatInput → Claude API messages（履歴トリミング + キャッシュ境界 + 動的コンテキスト）"""
    msgs = _trim_history([{"role": m.role, "content": m.content} for m in body.messages])
    # ターン跨ぎのキャッシュ境界: 履歴の最後の assistant メッセージ
    last_asst = None
    for i, m in enumerate(msgs):
        if m["role"] == "assistant":
            last_asst = i
    if last_asst is not None:
        msgs[last_asst] = {"role": "assistant", "content": [
            {"type": "text", "text": _msg_text(msgs[last_asst]["content"]),
             "cache_control": _cache_ctl()},
        ]}
    # 動的コンテキストは最後の user メッセージの追加ブロック（system を不変に保つ）
    if msgs and msgs[-1]["role"] == "user":
        blocks = [{"type": "text", "text": _msg_text(msgs[-1]["content"])}]
        ctx = _conversation_context_block(body).strip()
        if ctx:
            blocks.append({"type": "text", "text": ctx})
        msgs[-1] = {"role": "user", "content": blocks}
    return _mark_cache_tail(msgs)


def _mark_cache_tail(msgs: list[dict]) -> list[dict]:
    """リクエスト末尾メッセージの最後のブロックに cache_control を付ける（同一ターン内の
    tool ラウンドで prefix がヒットする）．前のリクエストで付けた末尾の印は外す
    （履歴 assistant の印は _tail_marked を持たないので残る）．breakpoint は API 上限 4 つ:
    tools / system / 履歴 assistant / 末尾．"""
    for m in msgs[:-1]:
        c = m.get("content")
        if isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and b.pop("_tail_marked", False):
                    b.pop("cache_control", None)
    last = msgs[-1]
    c = last.get("content")
    if isinstance(c, str):
        last["content"] = [{"type": "text", "text": c}]
        c = last["content"]
    if isinstance(c, list) and c and isinstance(c[-1], dict):
        if "cache_control" not in c[-1]:
            c[-1]["cache_control"] = _cache_ctl()
            c[-1]["_tail_marked"] = True
    return msgs


def _api_messages(msgs: list[dict]) -> list[dict]:
    """内部フラグ（_tail_marked）を除いた API 送信用メッセージ"""
    out = []
    for m in msgs:
        c = m.get("content")
        if isinstance(c, list):
            c = [({k: v for k, v in b.items() if k != "_tail_marked"} if isinstance(b, dict) else b)
                 for b in c]
        out.append({"role": m["role"], "content": c})
    return out


class _UsageTally:
    """1 ターン（1 回の /chat）で使ったトークンの集計．done イベントでフロントに返す"""

    def __init__(self):
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_read = 0
        self.cache_creation = 0
        self.cost_jpy = 0.0

    def add(self, context: str, response) -> None:
        u = _log_usage(context, response)
        if not u:
            return
        self.calls += 1
        self.input_tokens += u["input_tokens"]
        self.output_tokens += u["output_tokens"]
        self.cache_read += u["cache_read_input_tokens"]
        self.cache_creation += u["cache_creation_input_tokens"]
        self.cost_jpy += u["cost_jpy"]

    def as_dict(self) -> dict:
        total_in = self.input_tokens + self.cache_read + self.cache_creation
        return {
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "cache_read_tokens": self.cache_read,
            "cache_creation_tokens": self.cache_creation,
            "output_tokens": self.output_tokens,
            "cache_hit_pct": round(self.cache_read / max(1, total_in) * 100),
            "cost_jpy": round(self.cost_jpy, 2),
        }


# ──────────────────────────────────────────────
# チャートの $data 参照
#
# LLM が get_simulation_data の配列を Chart.js 設定に書き写すと，出力トークン
# （入力の 5 倍単価）を大量に使う．{"$data": "network_avg_speed"} のような参照を
# サーバー側で実データに置き換える．
# ──────────────────────────────────────────────
def _resolve_chart_refs(obj, data_cache: dict, default_sim_id: str | None):
    """chart JSON 内の {"$data": "<path>", "sim_id"?: "..."} を集計データで置換する"""
    if isinstance(obj, dict):
        if "$data" in obj and isinstance(obj["$data"], str):
            sim_id = str(obj.get("sim_id") or default_sim_id or "")
            data = data_cache.get(sim_id)
            if data is None and sim_id:
                data = _get_simulation_data(sim_id)
                if data is not None:
                    data_cache[sim_id] = data
            cur = data
            for part in obj["$data"].split("."):
                if isinstance(cur, dict) and part in cur:
                    cur = cur[part]
                else:
                    print(f"[RISU] chart $data unresolved: {obj['$data']!r} (sim {sim_id!r})")
                    return []
            return cur
        return {k: _resolve_chart_refs(v, data_cache, default_sim_id) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_resolve_chart_refs(v, data_cache, default_sim_id) for v in obj]
    return obj


_CHART_PATTERN = None

def _extract_charts(text: str, data_cache: dict, default_sim_id: str | None) -> tuple[list, str]:
    """```chart ... ``` ブロックを抽出して $data を解決し，(charts, 本文) を返す"""
    global _CHART_PATTERN
    if _CHART_PATTERN is None:
        import re
        _CHART_PATTERN = re.compile(r'```\s*chart\w*\s*\n?(.*?)\n?\s*```', re.DOTALL | re.IGNORECASE)
    charts = []
    for m in _CHART_PATTERN.finditer(text):
        try:
            chart_json = json.loads(m.group(1))
        except json.JSONDecodeError as e:
            print(f"[RISU] chart JSON parse failed: {e}; raw={m.group(1)[:200]!r}")
            continue
        charts.append(_resolve_chart_refs(chart_json, data_cache, default_sim_id))
    clean_text = _CHART_PATTERN.sub('', text).strip()
    return charts, clean_text


# ──────────────────────────────────────────────
# トークン使用量ロガー
# ──────────────────────────────────────────────
def _log_usage(context: str, response) -> dict:
    """Claude レスポンスから usage を抽出してログ出力．将来 DB 保存フックに繋げられる"""
    usage = getattr(response, "usage", None)
    if usage is None:
        return {}
    in_tok  = getattr(usage, "input_tokens", 0) or 0
    out_tok = getattr(usage, "output_tokens", 0) or 0
    cache_read    = getattr(usage, "cache_read_input_tokens", 0) or 0
    cache_create  = getattr(usage, "cache_creation_input_tokens", 0) or 0
    # claude-sonnet-5 の概算単価: 入力 $2 / 出力 $10 per MTok，キャッシュ読み $0.20，キャッシュ書き $2.50．
    # usage.input_tokens はキャッシュ読み・書きの分を含まない（重複して引くと負になる）．
    # JPY @ 150円/USD 概算
    cost_usd = (
        in_tok * 2 / 1_000_000
        + cache_read * 0.20 / 1_000_000
        + cache_create * 2.50 / 1_000_000
        + out_tok * 10 / 1_000_000
    )
    cost_jpy = cost_usd * 150
    total_in = in_tok + cache_read + cache_create
    cache_pct = (cache_read / max(1, total_in)) * 100
    try:
        msg = (
            f"[RISU usage] {context}: "
            f"in={in_tok}+cache_read={cache_read}+cache_create={cache_create}/out={out_tok} "
            f"(cache {cache_pct:.0f}%) ~JPY {cost_jpy:.2f}"
        )
        # Windows cp932 コンソール対策: エンコード不能文字は置換
        try:
            print(msg)
        except UnicodeEncodeError:
            import sys
            sys.stdout.buffer.write(msg.encode("utf-8", errors="replace") + b"\n")
            sys.stdout.flush()
    except Exception as e:
        print(f"[RISU usage] log failed: {e!r}")
    return {
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_create,
        "cost_jpy": cost_jpy,
    }


# ──────────────────────────────────────────────
# tool_use ループ上限（暴走・コスト爆弾対策）
# ──────────────────────────────────────────────
MAX_TOOL_ROUNDS = int(os.getenv("RISU_MAX_TOOL_ROUNDS", "3"))

# ──────────────────────────────────────────────
# トークン節約の設定
# ──────────────────────────────────────────────
# LLM に送る会話履歴の文字数予算．超過したら古いターンから落とす（ヒステリシス付き，
# _trim_history 参照）．0 で無制限．
MAX_HISTORY_CHARS = int(os.getenv("RISU_MAX_HISTORY_CHARS", "24000"))
# Prompt Caching の TTL: "5m"（既定）または "1h"．考えながら操作して 5 分以上空くことが
# 多いなら "1h" の方が安い（書き込み単価は 2 倍だが，再作成が要らない）．
CACHE_TTL = os.getenv("RISU_CACHE_TTL", "5m").strip() or "5m"


def _sse_event(data: dict) -> str:
    """SSE イベント文字列を生成"""
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _chat_claude_stream(body: ChatInput):
    """Claude API チャット — SSE ストリーミングで進捗を返す"""

    import anthropic

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    # 履歴トリミング + キャッシュ境界 + 動的コンテキスト（system は不変に保つ）
    messages = _build_llm_messages(body)
    system = SYSTEM_PROMPT
    usage = _UsageTally()
    # ツール実行の状態（sim_id / 集計キャッシュ）は _dispatch_tool_blocks と共有する
    state = _ToolTurnState(body)

    async def event_generator():
        try:
            # ── 1回目: LLM 呼び出し（ストリーミング）──
            yield _sse_event({"type": "progress", "message": "Step 1/3: シナリオを設計中..."})

            first_streamed_text = False

            with client.messages.stream(
                model=CLAUDE_MODEL,
                max_tokens=64000,
                system=_cached_system(system),
                messages=_api_messages(messages),
                tools=_cached_tools(),
            ) as first_stream:
                for event in first_stream:
                    if event.type == "content_block_delta":
                        if event.delta.type == "text_delta":
                            if not first_streamed_text:
                                yield _sse_event({"type": "stream_start"})
                                first_streamed_text = True
                            yield _sse_event({"type": "text_delta", "text": event.delta.text})

                response = first_stream.get_final_message()
                usage.add("stream first-round", response)

            # テキストのみの応答（ツール呼び出しなし）
            if response.stop_reason != "tool_use":
                text = "".join(b.text for b in response.content if b.type == "text")

                # シミュレーション意図がありそうならリトライ（tool_choice で強制）
                last_user_msg = _last_user_message_text(body) or ""
                sim_keywords = ["シミュレーション", "シミュレート", "実行", "グリッド", "ネットワーク", "渋滞", "ボトルネック", "道路", "交通"]
                if any(k in last_user_msg for k in sim_keywords):
                    if first_streamed_text:
                        yield _sse_event({"type": "stream_end_partial"})
                    yield _sse_event({"type": "progress", "message": "Step 1/3: シナリオを再設計中..."})
                    messages.append({"role": "assistant", "content": text})
                    messages.append({"role": "user", "content": "run_simulation ツールを使って今すぐシミュレーションを実行してください．"})
                    response = client.messages.create(
                        model=CLAUDE_MODEL,
                        max_tokens=32000,
                        system=_cached_system(system),
                        messages=_api_messages(_mark_cache_tail(messages)),
                        tools=_cached_tools(),
                        tool_choice={"type": "tool", "name": "run_simulation"},
                    )
                    usage.add("stream retry (tool_choice)", response)
                    if response.stop_reason != "tool_use":
                        yield _sse_event({"type": "done", "role": "assistant", "content": text,
                                          "sim_id": None, "usage": usage.as_dict()})
                        return
                    # 下のツール実行に続行
                else:
                    yield _sse_event({"type": "done", "role": "assistant", "content": text,
                                      "sim_id": None, "usage": usage.as_dict()})
                    return

            # ── ツール実行（同期経路と同じ _dispatch_tool_blocks を通す）──
            tool_blocks = [b for b in response.content if b.type == "tool_use"]
            tool_results = []
            async for _kind, _payload in _dispatch_tool_blocks(tool_blocks, state):
                if _kind == "progress":
                    yield _sse_event({"type": "progress", "message": _payload})
                else:
                    tool_results = _payload

            # ── ツール結果を渡して最終回答をストリーミング生成 ──
            yield _sse_event({"type": "progress", "message": "Step 3/3: 結果を分析中..."})

            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})
            _mark_cache_tail(messages)

            # ストリーミングで最終回答を生成するヘルパー
            async def _stream_final_response(msgs):
                """messages を渡して streaming 呼び出し．テキストは text_delta で逐次送信し，
                ツール呼び出しがあれば蓄積して返す．最終テキストも返す．"""
                collected_text = []
                collected_tool_blocks = []

                with client.messages.stream(
                    model=CLAUDE_MODEL,
                    max_tokens=16000,
                    system=_cached_system(system),
                    messages=_api_messages(msgs),
                    tools=_cached_tools(),
                ) as stream:
                    for event in stream:
                        if event.type == "content_block_start":
                            if event.content_block.type == "tool_use":
                                collected_tool_blocks.append({
                                    "index": event.index,
                                    "id": event.content_block.id,
                                    "name": event.content_block.name,
                                    "input_json": "",
                                })
                        elif event.type == "content_block_delta":
                            if event.delta.type == "text_delta":
                                collected_text.append(event.delta.text)
                                yield {"event": "text_delta", "data": event.delta.text}
                            elif event.delta.type == "input_json_delta":
                                if collected_tool_blocks:
                                    collected_tool_blocks[-1]["input_json"] += event.delta.partial_json

                    # stream 終了後，最終 response を取得
                    final_response = stream.get_final_message()
                    usage.add("stream post-tool", final_response)

                # ツールブロックを anthropic オブジェクトとして返す
                real_tool_blocks = [b for b in final_response.content if b.type == "tool_use"]
                full_text = "".join(collected_text)
                yield {"event": "stream_done", "text": full_text, "tool_blocks": real_tool_blocks, "response": final_response}

            # 1回目のストリーミング
            yield _sse_event({"type": "stream_start"})
            stream_result = None
            async for item in _stream_final_response(messages):
                if item["event"] == "text_delta":
                    yield _sse_event({"type": "text_delta", "text": item["data"]})
                elif item["event"] == "stream_done":
                    stream_result = item

            final_text = stream_result["text"]
            next_tool_blocks = stream_result["tool_blocks"]

            # さらにツール呼び出しがある場合はループで追加処理（最大3ラウンド）
            for _round in range(MAX_TOOL_ROUNDS):
                if not next_tool_blocks:
                    break
                yield _sse_event({"type": "stream_end_partial"})
                next_tool_results = []
                async for _kind, _payload in _dispatch_tool_blocks(
                        next_tool_blocks, state, follow_up=True):
                    if _kind == "progress":
                        yield _sse_event({"type": "progress", "message": _payload})
                    else:
                        next_tool_results = _payload

                messages.append({"role": "assistant", "content": stream_result["response"].content})
                messages.append({"role": "user", "content": next_tool_results})
                _mark_cache_tail(messages)

                # 次ラウンドもストリーミング
                yield _sse_event({"type": "stream_start"})
                next_tool_blocks = []
                async for item in _stream_final_response(messages):
                    if item["event"] == "text_delta":
                        yield _sse_event({"type": "text_delta", "text": item["data"]})
                    elif item["event"] == "stream_done":
                        final_text = item["text"]
                        next_tool_blocks = item["tool_blocks"]
                        stream_result = item

            # チャート抽出（$data 参照はこのターンで取得した集計データで解決）
            charts, clean_text = _extract_charts(
                final_text, state.sim_data_cache,
                state.last_data_sim_id or state.sim_id or _conversation_sim_id(body))
            if "```chart" in clean_text.lower() or "```\nchart" in clean_text.lower():
                # 抽出漏れの兆候．ログに残してデバッグ可能に
                idx = clean_text.lower().find("```")
                print(f"[RISU] WARN: chart fence still present after strip; near={clean_text[max(0,idx-20):idx+200]!r}")
            print(f"[RISU] chat done: charts={len(charts)}, clean_text_len={len(clean_text)}")

            # このターンでシミュレーションを実行していなければ sim_id は None のまま
            # （グローバル最新へのフォールバックは無関係な結果を表示させるため廃止）
            resp = {"type": "done", "role": "assistant", "content": clean_text, "sim_id": state.sim_id,
                    "usage": usage.as_dict()}
            if charts:
                resp["charts"] = charts
            yield _sse_event(resp)

        except anthropic.AuthenticationError:
            print("[RISU] CRITICAL: ANTHROPIC_API_KEY invalid!")
            yield _sse_event({"type": "error", "message": "サーバー側で LLM に接続できません．運営にお問い合わせください．"})
        except anthropic.RateLimitError:
            yield _sse_event({"type": "error", "message": "LLM が混雑しています．しばらく待って再試行してください．"})
        except anthropic.APIStatusError as e:
            status = getattr(e, "status_code", None) or getattr(e, "status", None)
            if status == 529:
                yield _sse_event({"type": "error", "message": "LLM が一時的に過負荷です．1〜2 分後に再試行してください．"})
            elif status and 500 <= status < 600:
                yield _sse_event({"type": "error", "message": "LLM サーバー側のエラーです．しばらく後で再試行してください．"})
            else:
                print(f"[RISU] Claude APIStatusError {status}: {e}")
                yield _sse_event({"type": "error", "message": f"LLM エラー（コード: {status}）"})
        except anthropic.APIConnectionError:
            yield _sse_event({"type": "error", "message": "LLM に接続できません．ネットワーク接続を確認してください．"})
        except Exception as e:
            import traceback
            traceback.print_exc()
            # ユーザーには詳細を伏せる
            print(f"[RISU] chat error: {e.__class__.__name__}: {e}")
            yield _sse_event({"type": "error", "message": "処理中にエラーが発生しました．もう一度お試しください．"})

    return StreamingResponse(event_generator(), media_type="text/event-stream")


async def _chat_claude(body: ChatInput):
    """Claude API を使ったチャット（tool_use 対応）"""
    import anthropic

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # 履歴トリミング + キャッシュ境界 + 動的コンテキスト（system は不変に保つ）
    messages = _build_llm_messages(body)
    system = SYSTEM_PROMPT
    usage = _UsageTally()
    # ツール実行の状態はストリーミング経路と共通（_dispatch_tool_blocks）
    state = _ToolTurnState(body)

    try:
        # 1回目：ツール付きリクエスト
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=8192,
            system=_cached_system(system),
            messages=_api_messages(messages),
            tools=_cached_tools(),
        )
        usage.add("sync first-round", response)

        # テキストのみの応答（ツール呼び出しなし）
        if response.stop_reason != "tool_use":
            text = "".join(b.text for b in response.content if b.type == "text")

            # LLM が「実行します」と言ったのにツールを呼ばなかった場合，再試行
            sim_keywords = ["実行します", "作成します", "シミュレーション", "構築します"]
            if any(k in text for k in sim_keywords):
                messages.append({"role": "assistant", "content": text})
                messages.append({"role": "user", "content": "run_simulation ツールを呼び出して，今すぐシミュレーションを実行してください．テキストだけでなくツールを使ってください．"})
                retry = client.messages.create(
                    model=CLAUDE_MODEL,
                    max_tokens=8192,
                    system=_cached_system(system),
                    messages=_api_messages(_mark_cache_tail(messages)),
                    tools=_cached_tools(),
                )
                usage.add("sync retry", retry)
                if retry.stop_reason == "tool_use":
                    response = retry
                    # 下のツール処理に続行
                else:
                    return {"role": "assistant", "content": text, "sim_id": None, "usage": usage.as_dict()}
            else:
                return {"role": "assistant", "content": text, "sim_id": None, "usage": usage.as_dict()}

        # ツール呼び出しがある場合（複数ツール呼び出しにも対応）
        # dispatch はストリーミング経路と共通．進捗イベントはここでは捨てる．
        tool_blocks = [b for b in response.content if b.type == "tool_use"]
        tool_results = await _collect_tool_results(tool_blocks, state)

        # ツール結果を渡して次の回答を生成（最大3ラウンド）
        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})
        _mark_cache_tail(messages)

        for _round in range(MAX_TOOL_ROUNDS):
            resp_next = client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=16000,
                system=_cached_system(system),
                messages=_api_messages(messages),
                tools=_cached_tools(),
            )
            usage.add(f"sync round {_round + 1}", resp_next)

            # テキスト部分を収集
            final_text = "".join(b.text for b in resp_next.content if b.type == "text")

            # さらにツール呼び出しがある場合は処理を続ける
            next_tool_blocks = [b for b in resp_next.content if b.type == "tool_use"]
            if not next_tool_blocks:
                break

            next_tool_results = await _collect_tool_results(
                next_tool_blocks, state, follow_up=True)

            messages.append({"role": "assistant", "content": resp_next.content})
            messages.append({"role": "user", "content": next_tool_results})
            _mark_cache_tail(messages)

        # ```chart ... ``` ブロックからChart.js設定を抽出（$data 参照を解決）
        charts, clean_text = _extract_charts(
            final_text, state.sim_data_cache,
            state.last_data_sim_id or state.sim_id or _conversation_sim_id(body))

        resp = {"role": "assistant", "content": clean_text, "sim_id": state.sim_id,
                "usage": usage.as_dict()}
        if charts:
            resp["charts"] = charts
        return resp

    except anthropic.AuthenticationError:
        raise HTTPException(401, detail="Anthropic API キーが無効です．ANTHROPIC_API_KEY を確認してください")
    except anthropic.RateLimitError:
        raise HTTPException(429, detail="Claude API のレートリミットに達しました．しばらく待ってください")
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(500, detail=f"Claude エラー: {str(e)}")


async def _chat_ollama(body: ChatInput):
    """本番用: Ollama LLM と対話"""
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages += [{"role": m.role, "content": m.content} for m in body.messages]

    TOOLS_FOR_OLLAMA = [
        {
            "type": "function",
            "function": {
                "name": "run_simulation",
                "description": "UXsim 交通流シミュレーションを実行する",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name":    {"type": "string"},
                        "tmax":    {"type": "integer"},
                        "deltan":  {"type": "integer"},
                        "reaction_time": {"type": "number"},
                        "random_seed":   {"type": "integer"},
                        "nodes":   {"type": "array", "items": {"type": "object", "properties": {"name": {"type": "string"}, "x": {"type": "number"}, "y": {"type": "number"}, "flow_capacity": {"type": "number"}}, "required": ["name", "x", "y"]}},
                        "links":   {"type": "array", "items": {"type": "object", "properties": {"name": {"type": "string"}, "start": {"type": "string"}, "end": {"type": "string"}, "length": {"type": "number"}, "free_flow_speed": {"type": "number"}}, "required": ["name", "start", "end", "length"]}},
                        "demands": {"type": "array", "items": {"type": "object", "properties": {"orig": {"type": "string"}, "dest": {"type": "string"}, "t_start": {"type": "number"}, "t_end": {"type": "number"}, "flow": {"type": "number"}}, "required": ["orig", "dest", "t_start", "t_end", "flow"]}},
                    },
                    "required": ["nodes", "links", "demands"],
                },
            },
        }
    ]

    try:
        async with httpx.AsyncClient(timeout=600) as client:
            resp = await client.post(
                f"{OLLAMA_BASE_URL}/api/chat",
                json={"model": OLLAMA_MODEL, "messages": messages, "tools": TOOLS_FOR_OLLAMA, "stream": False},
            )
            resp.raise_for_status()
            data = resp.json()
            assistant_msg = data["message"]

            if not assistant_msg.get("tool_calls"):
                return {"role": "assistant", "content": assistant_msg["content"], "sim_id": None}

            tool_call = assistant_msg["tool_calls"][0]
            fn_args = tool_call["function"]["arguments"]
            if isinstance(fn_args, str):
                fn_args = json.loads(fn_args)

            sim_input = SimulationInput(**fn_args)
            result = await _run_uxsim_async(sim_input)
            sim_id = str(uuid.uuid4())[:8]
            _store_sim(sim_id, result, {
                "type": "llm",
                "llm_backend": "ollama",
                "tool": "run_simulation",
                "llm_user_message": _last_user_message_text(body),
            })

            messages.append({"role": "assistant", "content": "", "tool_calls": assistant_msg["tool_calls"]})
            messages.append({"role": "tool", "content": json.dumps(result["stats"], ensure_ascii=False)})

            resp2 = await client.post(
                f"{OLLAMA_BASE_URL}/api/chat",
                json={"model": OLLAMA_MODEL, "messages": messages, "stream": False},
            )
            resp2.raise_for_status()
            final_msg = resp2.json()["message"]["content"]

            return {"role": "assistant", "content": final_msg, "sim_id": sim_id}

    except httpx.TimeoutException:
        raise HTTPException(504, detail="Ollama の応答がタイムアウトしました")
    except httpx.ConnectError:
        raise HTTPException(502, detail="Ollama に接続できません．ollama serve が起動しているか確認してください")
    except Exception as e:
        raise HTTPException(500, detail=f"チャットエラー: {str(e)}")
