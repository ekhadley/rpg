import threading
from flask_socketio import SocketIO
from utils import logger

class CallbackHandler:
    def text_output(self, text):
        pass
    def think_output(self, text):
        pass
    def tool_request(self, name:str, inputs: dict):
        pass
    def tool_submit(self, names: list[str], inputs: list[dict], results: list[str]):
        pass
    def turn_end(self, cost_stats: dict = None, finish_reason: str = None):
        pass

class WebCallbackHandler(CallbackHandler):
    def __init__(self, socket: SocketIO):
        self.socket = socket
        self.outputting_text = False
        self.thinking = False

    # Emit think_end when reasoning gives way to text, a tool call, or turn end.
    def _end_thinking(self):
        if self.thinking:
            self.thinking = False
            self.socket.emit('think_end')

    def think_output(self, text):
        if not self.thinking:
            self.thinking = True
            self.socket.emit('think_start')
        self.emit('think_output', text=text)

    def text_output(self, text):
        self._end_thinking()
        if not self.outputting_text:
            self.outputting_text = True
            self.socket.emit('text_start')
        self.emit('text_output', text=text)

    def tool_request(self, name:str, inputs: dict):
        self._end_thinking()
        self.outputting_text = False
        self.emit('tool_request', name=name, inputs=inputs)

    def tool_submit(self, names: list[str], inputs: list[dict], results: list[str]):
        self.outputting_text = False
        self.emit('tool_submit', tools=[{"name": names[i], "inputs": inputs[i], "result": results[i]} for i in range(len(names))])

    def turn_end(self, cost_stats: dict = None, finish_reason: str = None):
        self._end_thinking()
        self.outputting_text = False
        if finish_reason:
            logger.debug(f"Turn ended with finish_reason: {finish_reason}")
        self.emit('turn_end', cost_stats=cost_stats)

    def emit(self, event, **kwargs):
        self.socket.emit(event, kwargs)
        self.socket.sleep(0)

class StudioCallbackHandler(CallbackHandler):
    """Streams one prompt-studio lane. Events are namespaced `studio_*` and tagged with the lane
    they belong to, so 2N concurrent generations land in the right column and the chat view (which
    listens on the untagged events above) never sees them.

    A primer lane also carries `gate`: the first chunk it produces means the prompt has been processed
    and cached, which is the signal for the rest of its version's lanes to start."""
    def __init__(self, socket: SocketIO, run_id: str, lane: str, gate: threading.Event | None = None):
        self.socket = socket
        self.run_id = run_id
        self.lane = lane
        self.gate = gate
        self.finish_reason: str | None = None  # how the lane's turn ended; "error" if the request failed

    def turn_end(self, cost_stats: dict = None, finish_reason: str = None):
        self.finish_reason = finish_reason

    def think_output(self, text):
        self.emit('studio_think', text=text)

    def text_output(self, text):
        self.emit('studio_text', text=text)

    def tool_submit(self, names: list[str], inputs: list[dict], results: list[str]):
        self.emit('studio_tools', tools=[{"name": names[i], "inputs": inputs[i], "result": results[i]} for i in range(len(names))])

    def emit(self, event, **kwargs):
        if self.gate is not None:
            self.gate.set()
        self.socket.emit(event, {"run_id": self.run_id, "lane": self.lane, **kwargs})
        self.socket.sleep(0)
