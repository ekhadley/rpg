"""OpenRouterProvider's stream loop, driven by fake SSE events instead of a network stream: the
message assembly it does on the happy path, and every way a turn can fail."""
import pytest
from callbacks import CallbackHandler
from model_tools import SYSTEM_TOOLBOXES
from openrouter import OpenRouterProvider, TurnFailed


class Recorder(CallbackHandler):
    def __init__(self):
        self.text, self.thoughts, self.tools, self.ends = [], [], [], []
    def text_output(self, text):
        self.text.append(text)
    def think_output(self, text):
        self.thoughts.append(text)
    def tool_submit(self, names, inputs, results):
        self.tools.append((names[0], inputs[0], results[0]))
    def turn_end(self, cost_stats=None, finish_reason=None):
        self.ends.append(finish_reason)


class FakeStream:
    """One round's events. An event that is a callable is run instead of yielded (to press stop
    mid-stream); an event that is an exception is raised (a dropped connection)."""
    def __init__(self, events):
        self.events = iter(events)
        self.closed = False
    def __iter__(self):
        return self
    def __next__(self):
        e = next(self.events)
        if isinstance(e, Exception):
            raise e
        if callable(e):
            e()
            return next(self)
        return e
    def close(self):
        self.closed = True


def make_provider(rounds, files=None):
    """A provider whose successive getStream() calls play back `rounds` (a list of event lists)."""
    p = OpenRouterProvider(model_name="m", toolbox=SYSTEM_TOOLBOXES["hp"](files if files is not None else {}),
                           callback_handler=Recorder(), cache_mode="none", key="k")
    p.messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    streams = []
    it = iter(rounds)
    def next_stream():
        s = FakeStream(next(it))
        streams.append(s)
        return s
    p.getStream = next_stream
    p.streams = streams
    return p


def delta(**d):
    return {"choices": [{"delta": d}]}

def finish(reason, **d):
    return {"choices": [{"delta": d, "finish_reason": reason}]}

USAGE = {"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8, "cost": 0.002}}


def test_text_and_reasoning_are_assembled_and_finish_can_ride_an_empty_delta():
    p = make_provider([[delta(role="assistant", reasoning="hmm"), delta(content="Hel"), delta(content="lo"),
                        finish("stop"), USAGE]])
    p.run()
    msg = p.messages[-1]
    assert msg["role"] == "assistant" and msg["content"] == "Hello" and msg["reasoning"] == "hmm"
    assert msg["usage"]["cost"] == 0.002 and p.usage_history == [msg["usage"]]
    assert p.last_turn_cost == 0.002
    assert p.cb.text == ["Hel", "lo"] and p.cb.thoughts == ["hmm"] and p.cb.ends == ["stop"]
    assert p.streams[0].closed


def test_tool_round_executes_and_the_next_stream_continues_the_turn():
    files = {}
    call = {"id": "c1", "type": "function", "function": {"name": "write_file", "arguments": ""}}
    p = make_provider([
        [delta(role="assistant", tool_calls=[call]),
         delta(tool_calls=[{"index": 0, "function": {"arguments": '{"file_name": "pc", '}}]),
         delta(tool_calls=[{"index": 0, "function": {"arguments": '"contents": "HP: 10"}'}}]),
         finish("tool_calls"), USAGE],
        [delta(content="Done."), finish("stop"), USAGE],
    ], files)
    p.run()
    assert [m["role"] for m in p.messages] == ["system", "user", "assistant", "tool", "assistant"]
    assert p.messages[2]["tool_calls"][0]["function"]["arguments"] == '{"file_name": "pc", "contents": "HP: 10"}'
    assert p.messages[3] == {"role": "tool", "tool_call_id": "c1", "content": "File saved successfully."}
    assert files == {"pc": "HP: 10"}
    assert p.cb.tools == [("write_file", {"file_name": "pc", "contents": "HP: 10"}, "File saved successfully.")]
    assert len(p.usage_history) == 2 and p.last_turn_cost == pytest.approx(0.004)


def test_a_stream_that_ends_without_a_finish_reason_fails_the_turn():
    p = make_provider([[delta(content="Half a sent")]])
    with pytest.raises(TurnFailed, match="dropped before the model finished"):
        p.run()
    assert [m["role"] for m in p.messages] == ["system", "user"]
    assert p.cb.ends == ["error"] and p.cb.text == ["Half a sent"]


def test_a_dropped_connection_fails_the_turn():
    p = make_provider([[delta(content="x"), ConnectionError("Connection broken")]])
    with pytest.raises(TurnFailed, match="Connection broken") as info:
        p.run()
    assert info.value.aborted is False
    assert [m["role"] for m in p.messages] == ["system", "user"]


def test_an_error_chunk_fails_the_turn_with_its_message():
    p = make_provider([[delta(content="x"), {"error": {"code": 502, "message": "Provider returned error",
                                                        "metadata": {"raw": "overloaded_error"}}}]])
    with pytest.raises(TurnFailed) as info:
        p.run()
    assert "Provider returned error" in str(info.value) and "overloaded_error" in str(info.value) and "502" in str(info.value)
    p = make_provider([[{"choices": [{"delta": {}, "finish_reason": "error", "error": {"message": "upstream died"}}]}]])
    with pytest.raises(TurnFailed, match="upstream died"):
        p.run()


def test_a_tool_call_left_without_a_result_is_not_kept():
    call = {"id": "c1", "type": "function", "function": {"name": "roll_dice", "arguments": '{"dice": "d2'}}
    p = make_provider([[delta(role="assistant", tool_calls=[call])]])  # the stream dies mid-arguments
    with pytest.raises(TurnFailed):
        p.run()
    assert not any(m.get("tool_calls") for m in p.messages) and p.cb.tools == []


def test_stop_mid_stream_aborts_and_discards():
    p = make_provider([[delta(content="The door "), lambda: p.stop(), delta(content="opens"), finish("stop")]])
    with pytest.raises(TurnFailed, match="stopped") as info:
        p.run()
    assert info.value.aborted is True
    assert [m["role"] for m in p.messages] == ["system", "user"]
    assert p.cb.text == ["The door "] and p.cb.ends == ["aborted"]
    assert p.streams[0].closed


def test_stop_between_tool_rounds_aborts_before_the_next_request():
    call = {"id": "c1", "type": "function", "function": {"name": "roll_dice", "arguments": '{"dice": "d20"}'}}
    p = make_provider([[delta(role="assistant", tool_calls=[call]), lambda: p.stop(), finish("tool_calls")],
                       [delta(content="never streamed"), finish("stop")]])
    with pytest.raises(TurnFailed) as info:
        p.run()
    assert info.value.aborted is True
    assert len(p.streams) == 1  # the second round was never requested
    assert p.cb.tools == []


def test_a_stopped_provider_runs_again_cleanly():
    p = make_provider([[lambda: p.stop(), delta(content="x"), finish("stop")], [delta(content="fine"), finish("stop")]])
    with pytest.raises(TurnFailed):
        p.run()
    p.run()
    assert p.messages[-1]["content"] == "fine" and p.cb.ends == ["aborted", "stop"]


# --- parsing fixes that the failure handling above must not undo -----------------------------

def test_a_tool_call_sent_whole_in_one_chunk_keeps_its_arguments_once():
    call = {"id": "c1", "type": "function", "function": {"name": "roll_dice", "arguments": '{"dice": "d20"}'}}
    p = make_provider([[delta(role="assistant", tool_calls=[call]), finish("tool_calls"), USAGE],
                       [delta(content="ok"), finish("stop"), USAGE]])
    p.run()
    assert p.messages[2]["tool_calls"][0]["function"]["arguments"] == '{"dice": "d20"}'
    assert call["function"]["arguments"] == '{"dice": "d20"}'  # the chunk's own dict is left alone
    assert p.cb.tools[0][:2] == ("roll_dice", {"dice": "d20"})


def test_reasoning_details_blocks_are_merged_by_index_across_chunks():
    p = make_provider([[delta(reasoning_details=[{"type": "reasoning.text", "index": 0, "text": "thin"}]),
                        delta(reasoning_details=[{"type": "reasoning.text", "index": 0, "text": "king", "signature": "sig"}]),
                        delta(reasoning_details=[{"type": "reasoning.text", "index": 1, "text": "second"}]),
                        delta(content="x"), finish("stop"), USAGE]])
    p.run()
    assert p.messages[-1]["reasoning_details"] == [
        {"type": "reasoning.text", "index": 0, "text": "thinking", "signature": "sig"},
        {"type": "reasoning.text", "index": 1, "text": "second"},
    ]
    assert p.messages[-1]["reasoning"] == "thinkingsecond" and p.cb.thoughts == ["thin", "king", "second"]


def test_usage_from_a_stream_with_no_assistant_message_is_counted_but_not_misattributed():
    p = make_provider([[finish("stop"), USAGE]])
    p.run()
    assert [m["role"] for m in p.messages] == ["system", "user"] and "usage" not in p.messages[-1]
    assert p.usage_history == [USAGE["usage"]] and p.last_turn_cost == pytest.approx(0.002)
