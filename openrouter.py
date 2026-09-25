from codecs import utf_8_decode
import json
import requests
import copy
import os
from dotenv import load_dotenv

load_dotenv(override=True)

from model_tools import Toolbox

from callbacks import CallbackHandler

from utils import logger

CACHE_CONTROLS = {
    "none": None,
    "5m": {"type": "ephemeral"},
    "1h": {"type": "ephemeral", "ttl": "1h"},
}

class OpenRouterStream:
    """
    Streams responses from OpenRouter. Iteration over the object yields only valid json.
    """
    def __init__(
        self,
        model_name: str,
        messages: list[dict],
        tools: list[dict],
        thinking_enabled: bool,
        thinking_effort: str,
        key: str
    ):
        self.pending_objects = []
        self.response_stream = requests.post(
            url = "https://openrouter.ai/api/v1/chat/completions",
            headers = {
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json"
            },
            json = {
                "model": model_name,
                "messages": messages,
                "tools": tools,
                "max_tokens": 64000,
                # Opus 4.8 uses adaptive thinking: reasoning.effort/max_tokens are ignored, so just toggle thinking on.
                "reasoning": {
                    "enabled": thinking_enabled,
                    "exclude": False,
                },
                # Effort level (low/medium/high/xhigh/max) is controlled via verbosity, which maps to Anthropic's output_config.effort.
                "verbosity": thinking_effort if thinking_enabled else None,
                # Pin Claude models to Anthropic's own API, not Bedrock/Vertex.
                "provider": {"only": ["anthropic"]} if "claude" in model_name.lower() else None,
                "stream": True
            },
            stream = True
        )
        if self.response_stream.status_code != 200:
            error_msg = self.response_stream.text.replace("\\n ", "\n ").replace("\\\"", "\"")
            logger.error(f"HTTP {self.response_stream.status_code} from OpenRouter: {error_msg}")
            raise RuntimeError(f"HTTP {self.response_stream.status_code} from OpenRouter: {error_msg}")
        self.response_iter = self.response_stream.iter_content(chunk_size=1024, decode_unicode=False)
        
        self.content_stream_finished = False
        self.buffer = ""
        self.decode_retry_count = 0
        self.decode_retry_limit = 1000

    def __iter__(self):
        return self

    def __next__(self) -> dict:
        if self.pending_objects:
            return self.pending_objects.pop(0)

        while True:
            # Ensure we have at least one complete SSE event in the buffer
            def find_event_delimiter(buf: str) -> tuple[int, int]:
                idx_lf = buf.find("\n\n")
                idx_crlf = buf.find("\r\n\r\n")
                if idx_lf == -1 and idx_crlf == -1:
                    return -1, 0
                if idx_lf == -1:
                    return idx_crlf, 4
                if idx_crlf == -1:
                    return idx_lf, 2
                # choose earliest occurrence
                return (idx_lf, 2) if idx_lf < idx_crlf else (idx_crlf, 4)

            while True:
                idx, sep_len = find_event_delimiter(self.buffer)
                if idx != -1:
                    break
                if self.content_stream_finished:
                    break
                try:
                    next_chunk = utf_8_decode(self.response_iter.__next__())[0]
                    self.buffer += next_chunk
                except StopIteration:
                    self.content_stream_finished = True
                    break

            # If stream is finished and buffer is empty or only contains DONE, stop.
            if self.content_stream_finished and (self.buffer.strip() == "" or self.buffer.strip() == "data: [DONE]"):
                raise StopIteration

            # Extract one SSE event (or whatever remains if no delimiter found at end)
            if idx != -1:
                event_chunk = self.buffer[:idx]
                self.buffer = self.buffer[idx + sep_len:]
            else:
                event_chunk = self.buffer
                self.buffer = ""

            if event_chunk.strip() == "":
                # Nothing meaningful in this chunk; continue reading
                continue

            # Parse SSE fields; concatenate multi-line data fields, ignore comments/other fields
            data_lines: list[str] = []
            for raw_line in event_chunk.splitlines():
                line = raw_line.rstrip("\r")
                if not line:
                    continue
                if line.startswith(":"):
                    # SSE comment/keepalive
                    continue
                if line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
                # Ignore other fields like id:, event:, retry:

            if not data_lines:
                # No data lines in this event; continue
                continue

            data_payload = "\n".join(data_lines).strip()

            if data_payload == "[DONE]":
                # Mark finished; only stop if buffer drained
                self.content_stream_finished = True
                if self.buffer.strip() == "":
                    raise StopIteration
                continue

            try:
                # Attempt to parse one or more JSON objects from the payload
                decoder = json.JSONDecoder()
                pos = 0
                while pos < len(data_payload):
                    # Skip whitespace
                    while pos < len(data_payload) and data_payload[pos].isspace():
                        pos += 1
                    if pos >= len(data_payload):
                        break
                    
                    obj, end = decoder.raw_decode(data_payload, idx=pos)
                    self.pending_objects.append(obj)
                    pos = end

                if self.pending_objects:
                    self.decode_retry_count = 0
                    return self.pending_objects.pop(0)
                
                # If we have content but parsed nothing, raise error to hit retry logic
                if data_payload.strip():
                    raise json.JSONDecodeError("No JSON object found", data_payload, 0)

            except json.JSONDecodeError as e:
                if self.pending_objects:
                    # We parsed some objects but failed on the rest. Return what we have.
                    self.decode_retry_count = 0
                    logger.warning(f"Partial parse success. Discarding tail: {data_payload[pos:]}")
                    return self.pending_objects.pop(0)

                self.decode_retry_count += 1
                if self.decode_retry_count > self.decode_retry_limit or self.content_stream_finished:
                    logger.error(f"({self.decode_retry_count}) [{self.content_stream_finished}] Error decoding data payload: {repr(data_payload)}")
                    logger.error(f"Buffer contents: {repr(self.buffer)}")
                    raise e
                # On transient decode issues, keep accumulating more data
                continue
    
    def close(self):
        self.response_stream.close()
    
    def __del__(self):
        self.close()

def _mergeReasoningDetails(stored: list[dict], deltas: list[dict]) -> None:
    """Fold one chunk's reasoning_details into the message's list. A block streams as many chunks
    sharing an `index`: their text (or summary / encrypted data) concatenates, and every other
    field — the signature arrives on the block's last chunk — is taken from the latest chunk that
    carries it. The finished list goes back to the provider with the message, and a block is only
    valid whole."""
    for d in deltas:
        if not isinstance(d, dict) or not d:
            continue
        target = None
        if d.get("index") is not None:
            target = next((s for s in stored if s.get("index") == d["index"]), None)
        elif stored and stored[-1].get("index") is None and stored[-1].get("type") == d.get("type") and stored[-1].get("id") == d.get("id"):
            target = stored[-1]  # no index to match on: a run of same-typed chunks is one block
        if target is None:
            stored.append(dict(d))
            continue
        for k, v in d.items():
            if k in ("text", "summary", "data"):
                if isinstance(v, str):
                    target[k] = (target[k] if isinstance(target.get(k), str) else "") + v
            elif v is not None or k not in target:
                target[k] = v

class OpenRouterProvider():
    def __init__(
            self,
            model_name: str,
            toolbox: Toolbox,
            callback_handler: CallbackHandler,
            thinking_effort: str = "high",
            cache_mode: str = "1h",
            key: str|None = os.getenv("OPENROUTER_API_KEY"),
        ):
        assert key is not None, "OPENROUTER_API_KEY is not set"
        self.key: str = key
        self.cache_mode: str = cache_mode
        self.model_name: str = model_name
        self.tb: Toolbox = toolbox
        self.tool_schemas = self.tb.getToolSchemas()
        self.messages: list[dict] = []
        self.cb = callback_handler
        self.thinking_enabled = thinking_effort != "none"
        self.thinking_effort = thinking_effort
        self.usage_history: list[dict] = []  # Track usage per turn
        self.system_turn_entries: int = 0  # Number of usage entries from the initial system turn
        self.last_turn_cost: float = 0.0

    def getCostStats(self) -> dict:
        """Calculate cost statistics from usage history"""
        if not self.usage_history:
            return {
                "total_tokens": 0,
                "total_cost": 0.0,
                "avg_tokens_per_turn": 0,
                "avg_cost_per_turn": 0.0,
                "turn_count": 0,
                "last_turn_cost": 0.0
            }

        total_tokens = sum(u.get("total_tokens", 0) for u in self.usage_history)
        total_cost = sum(u.get("cost", 0.0) for u in self.usage_history)

        # Exclude system turn entries from averages
        user_entries = self.usage_history[self.system_turn_entries:]
        user_tokens = sum(u.get("total_tokens", 0) for u in user_entries)
        user_cost = sum(u.get("cost", 0.0) for u in user_entries)
        user_turn_count = len(user_entries)

        return {
            "total_tokens": total_tokens,
            "total_cost": total_cost,
            "avg_tokens_per_turn": user_tokens // user_turn_count if user_turn_count > 0 else 0,
            "avg_cost_per_turn": user_cost / user_turn_count if user_turn_count > 0 else 0.0,
            "turn_count": len(self.usage_history),
            "last_turn_cost": self.last_turn_cost
        }

    def cacheControl(self) -> dict | None:
        return CACHE_CONTROLS[self.cache_mode]

    def addUserMessage(self, content: str) -> None:
        self.messages.append({
            "role": "user",
            "content": content,
        })
    def recompute_usage_from_messages(self, messages: list[dict]) -> None:
        """Rebuild usage_history (and the system-turn count) from a flat message list."""
        self.usage_history = []
        self.system_turn_entries = 0
        seen_real_user = False
        for msg in messages:
            if msg.get("role") == "user" and not str(msg.get("content", "")).startswith("System:"):
                seen_real_user = True
            if msg.get("role") == "assistant" and "usage" in msg:
                if not seen_real_user:
                    self.system_turn_entries += 1
                self.usage_history.append(msg["usage"])

    def _with_cache_anchor(self, messages: list[dict]) -> list[dict]:
        """Return a copy of messages with a moving cache breakpoint on the last message.

        The system prompt carries a static cache_control breakpoint; this adds a second,
        sliding one on the final message so the whole conversation prefix (not just the
        system prompt) is read from cache on the next turn. Up to 4 breakpoints are allowed.
        """
        cc = self.cacheControl()
        if not messages or cc is None:
            return messages
        msgs = list(messages)
        last = copy.deepcopy(msgs[-1])
        content = last.get("content")
        if isinstance(content, str) and content:
            last["content"] = [{"type": "text", "text": content, "cache_control": cc}]
        elif isinstance(content, list) and content:
            last["content"][-1] = {**last["content"][-1], "cache_control": cc}
        else:
            return messages  # nothing to anchor on; rely on the system-prompt cache only
        msgs[-1] = last
        return msgs

    def getStream(self) -> OpenRouterStream:
        return OpenRouterStream(
            model_name = self.model_name,
            messages = self._with_cache_anchor(self.messages),
            tools = self.tool_schemas,
            thinking_enabled = self.thinking_enabled,
            thinking_effort = self.thinking_effort,
            key = self.key
        )

    def run(self, system_turn=False) -> None:
        turn_start = len(self.usage_history)
        finish_reason = None
        try:
            finish_reason = self._run()
        except Exception as e:
            logger.error(f"Error during model response: {e}", exc_info=True)
            finish_reason = "error"
        turn_entries = self.usage_history[turn_start:]
        self.last_turn_cost = sum(u.get("cost", 0.0) for u in turn_entries)
        if system_turn:
            self.system_turn_entries = len(turn_entries)
        self.cb.turn_end(cost_stats=self.getCostStats(), finish_reason=finish_reason)

    def _assistantMessage(self) -> dict:
        """The assistant message the current stream is building, started on its first chunk."""
        if self.messages[-1]["role"] != "assistant":
            self.messages.append({"role": "assistant", "content": "", "reasoning": "", "reasoning_details": []})
        return self.messages[-1]

    def _run(self) -> str | None:
        finish_reason = None
        while True:
            pending_tool_calls = False
            stream = self.getStream()

            for event in stream:
                # Usage rides on the final chunk, after finish_reason, and belongs to the assistant
                # message this stream built. A stream that built none (an empty or errored completion)
                # still counts toward the turn's cost, but has no message to hang it on.
                usage = event.get("usage")
                if usage:
                    self.usage_history.append(usage)
                    if self.messages[-1]["role"] == "assistant":
                        self.messages[-1]["usage"] = usage
                    else:
                        logger.warning(f"usage reported for a stream that produced no assistant message: {usage}")

                choices = event.get("choices") or []
                if not choices:
                    continue
                event_item = choices[0]
                delta = event_item.get("delta") or {}

                if delta:
                    msg = self._assistantMessage()

                    delta_content = delta.get("content")
                    if delta_content:
                        msg["content"] += delta_content
                        self.cb.text_output(text=delta_content)

                    # Reasoning comes in one of two shapes. delta.reasoning_details is a list of
                    # blocks, each streamed as many chunks sharing an index, and it goes back to the
                    # provider with the message, so it is merged rather than replaced. Anthropic
                    # models send it AND delta.reasoning, so the text is taken from one or the other.
                    reasoning_delta = None
                    details = delta.get("reasoning_details")
                    if isinstance(details, list) and details:
                        for detail in details:
                            if not isinstance(detail, dict):
                                continue
                            text = detail.get("text") or (detail.get("summary") if detail.get("type") == "reasoning.summary" else None)
                            if text:
                                reasoning_delta = (reasoning_delta or "") + text
                        _mergeReasoningDetails(msg["reasoning_details"], details)
                    if reasoning_delta is None:
                        reasoning_delta = delta.get("reasoning")
                    if reasoning_delta:
                        msg["reasoning"] += reasoning_delta
                        self.cb.think_output(text=reasoning_delta)

                    for tool_call in delta.get("tool_calls") or []:
                        fn = tool_call.get("function") or {}
                        if tool_call.get("id"):
                            # A new call. Stored as its own dict, not the chunk's, so the argument
                            # fragments below append to our copy: a provider may send the whole call,
                            # arguments included, in this one chunk, and they must count once.
                            stored = {**tool_call, "function": {**fn, "arguments": fn.get("arguments") or ""}}
                            msg.setdefault("tool_calls", []).append(stored)
                            self.cb.tool_request(name=fn.get("name", ""), inputs={})
                        elif fn.get("arguments"):
                            msg["tool_calls"][-1]["function"]["arguments"] += fn["arguments"]

                # The finish chunk may carry an empty delta, so this runs whether or not there was one.
                finish = event_item.get("finish_reason")
                if finish:
                    finish_reason = finish
                    # Check for non-streamed reasoning in final message
                    final_reasoning = (event_item.get("message") or {}).get("reasoning") or delta.get("reasoning")
                    if final_reasoning and not self.messages[-1].get("reasoning"):
                        msg = self._assistantMessage()
                        msg["reasoning"] = final_reasoning
                        self.cb.think_output(text=final_reasoning)
                    # Detect tool calls from message content, not finish_reason string
                    if self.messages[-1].get("tool_calls"):
                        pending_tool_calls = True
                    if finish_reason not in ("stop", "tool_calls", "end_turn", "tool_use"):
                        logger.warning(f"Unexpected finish_reason: {finish_reason}")

            stream.close()

            if not pending_tool_calls:
                break

            # Process pending tool calls, then loop to handle model's response
            for tool_call in self.messages[-1]["tool_calls"]:
                tool_name = tool_call["function"]["name"]
                tool_arguments = tool_call["function"]["arguments"]
                call_id = tool_call["id"]

                tool_result = self.tb.getToolResult(tool_name, tool_arguments)
                self.submitToolOutput(call_id, tool_result)

                parsed_arguments = tool_arguments
                if isinstance(tool_arguments, str) and tool_arguments:
                    try:
                        parsed_arguments = json.loads(tool_arguments)
                    except json.JSONDecodeError:
                        pass

                self.cb.tool_submit(names=[tool_name], inputs=[parsed_arguments], results=[tool_result])

        return finish_reason

    def submitToolOutput(self, call_id: str, tool_output: str) -> None:
        self.messages.append({
            "role": "tool",
            "tool_call_id": call_id,
            "content": tool_output,
        })
    

