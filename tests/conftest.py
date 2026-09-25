"""Shared fixtures for the local test suite (`uv run pytest`).

Nothing here touches the network: the narrator's provider is swapped for FakeProvider, which runs
scripted turns in place of the OpenRouter stream, and every test works in its own scratch project
root, since the code addresses `stories/` and `instructions/` relative to the cwd.
"""
import os
import sys
import json
import uuid

# openrouter.py reads the key at import time (a default argument), so it has to exist before the
# import below; the fake never makes a request with it.
os.environ.setdefault("OPENROUTER_API_KEY", "test-key")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import narrator as narrator_module
from narrator import Narrator
from openrouter import OpenRouterProvider
from utils import makeNewStoryDir

CORE = "test_core"
SYSTEM = "hp"
MODEL = "test/model"


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    """A scratch project root with the two instruction files a story needs and an empty stories/."""
    (tmp_path / "instructions" / "core").mkdir(parents=True)
    (tmp_path / "instructions" / "systems").mkdir(parents=True)
    (tmp_path / "instructions" / "core" / f"{CORE}.md").write_text("# core instructions\n")
    (tmp_path / "instructions" / "systems" / f"{SYSTEM}.md").write_text("# hp rules\n")
    (tmp_path / "stories").mkdir()
    monkeypatch.chdir(tmp_path)
    return tmp_path


class FakeSocket:
    """Records every emit so a test can assert on the events a narrator operation sends."""
    def __init__(self):
        self.events: list[tuple[str, object]] = []

    def emit(self, event, data=None):
        self.events.append((event, data))

    def sleep(self, _seconds):
        pass

    def names(self) -> list[str]:
        return [e for e, _ in self.events]

    def last(self, event):
        for e, d in reversed(self.events):
            if e == event:
                return d
        raise AssertionError(f"no {event!r} event was emitted; got {self.names()}")

    def clear(self):
        self.events.clear()


# --- scripted turns -------------------------------------------------------------------------
# A script is a callable run in place of OpenRouterProvider._run. It appends provider messages the
# way the real stream loop does and returns a finish reason, or raises to model a failure. run()'s
# real bookkeeping around it (usage, failure cleanup, turn_end) is what gets exercised.

def _assistant(text: str, **extra) -> dict:
    return {"role": "assistant", "content": text, "reasoning": "", "reasoning_details": [{}], **extra}

def say(text: str, cost: float = 0.01):
    """The model narrates `text` and stops."""
    def run(p):
        usage = {"total_tokens": 10, "cost": cost}
        p.messages.append(_assistant(text, usage=usage))
        p.usage_history.append(usage)
        p.cb.text_output(text)
        return "stop"
    return run

def tool(name: str, args: dict, then=None):
    """One tool round (executed against the provider's real toolbox), followed by `then` (another
    script) or a bare stop."""
    def run(p):
        call = {"id": "call_" + uuid.uuid4().hex[:8], "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)}}
        p.messages.append(_assistant("", tool_calls=[call]))
        result = p.tb.getToolResult(name, call["function"]["arguments"])
        p.submitToolOutput(call["id"], result)
        p.cb.tool_submit(names=[name], inputs=[args], results=[result])
        return then(p) if then else "stop"
    return run

def fail(message: str = "HTTP 500 from OpenRouter"):
    """The request fails before producing anything."""
    def run(p):
        raise RuntimeError(message)
    return run

def fail_after(text: str):
    """The model streams `text`, then the connection drops."""
    def run(p):
        p.messages.append(_assistant(text))
        p.cb.text_output(text)
        raise RuntimeError("The connection dropped before the model finished its response")
    return run

def stopped_after(text: str):
    """The model streams `text`, then the user presses stop."""
    def run(p):
        p.messages.append(_assistant(text))
        p.cb.text_output(text)
        p.stop()
        p._checkStopped()
    return run


class FakeProvider(OpenRouterProvider):
    """OpenRouterProvider with the network replaced by a queue of scripted turns."""
    def __init__(self, *args, **kwargs):
        kwargs.pop("key", None)
        super().__init__(*args, key="test-key", **kwargs)
        self.scripts: list = []

    def _run(self):
        script = self.scripts.pop(0) if self.scripts else say("...")
        return script(self)


@pytest.fixture
def make_narrator(workdir, monkeypatch):
    """Build a narrator over a fresh story dir, its provider pre-loaded with `scripts` (one per turn
    the test will run). Returns (narrator, socket)."""
    monkeypatch.setattr(narrator_module, "OpenRouterProvider", FakeProvider)

    def make(scripts=(), story_id: str | None = None):
        story_id = story_id or makeNewStoryDir("Test Story", SYSTEM, CORE, MODEL)
        socket = FakeSocket()
        n = Narrator(model_name=MODEL, story_id=story_id, system_name=SYSTEM, core_version=CORE,
                     socket=socket, cache_mode="none")
        n.provider.scripts = list(scripts)
        return n, socket
    return make


def assistant_nodes(tree) -> list[str]:
    return [nid for nid in tree.path() if tree.nodes[nid]["role"] == "assistant"]


def narration(tree, node_id) -> str:
    """The narration text of an assistant node (its last assistant message's content)."""
    return [m for m in tree.nodes[node_id]["messages"] if m["role"] == "assistant"][-1]["content"]
