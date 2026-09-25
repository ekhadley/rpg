import os
import json
import uuid
import threading
from datetime import datetime
from flask_socketio import SocketIO
from utils import EVAL_STORIES_DIR, getFullStoryInstruction, logger
from history import TurnTree
from model_tools import SYSTEM_TOOLBOXES
from openrouter import OpenRouterProvider
from callbacks import StudioCallbackHandler

# The prompt studio regenerates one captured turn under two arms at once, so they can be read side
# by side. An arm is a (model, core version) pair, so a run can vary the model, the prompt, or both.
# A captured turn lives in ./eval_stories/<id>/ with the exact on-disk shape of a story (info.json +
# history.json) — it is a fork of the source story trimmed to the turn, so the state is frozen and
# the source is untouched.
#
# A studio run never touches the live narrator or any story tree: each lane builds its own provider
# over its own copy of the turn's story context.

def _evalDir(eval_id: str) -> str:
    return f"./{EVAL_STORIES_DIR}/{eval_id}"

def listEvalTurns() -> list[dict]:
    """Captured turns, newest first (the studio sidebar list)."""
    if not os.path.isdir(f"./{EVAL_STORIES_DIR}"):
        return []
    turns = []
    for eid in os.listdir(f"./{EVAL_STORIES_DIR}"):
        info_path = f"{_evalDir(eid)}/info.json"
        if not os.path.exists(info_path):
            continue
        with open(info_path) as f:
            info = json.load(f)
        turns.append({"id": eid, "name": info.get("story_name", eid), "system": info["system"],
                      "model": info["model"], "core": info.get("core"), "created": info.get("created", "")})
    return sorted(turns, key=lambda t: t["created"], reverse=True)

def _turnContext(eval_id: str) -> tuple[dict, list[dict], dict[str, str]]:
    """The frozen state a captured turn regenerates from: the messages up to and including the user
    message it responds to, plus the story context as of that point."""
    with open(f"{_evalDir(eval_id)}/info.json") as f:
        info = json.load(f)
    with open(f"{_evalDir(eval_id)}/history.json") as f:
        tree = TurnTree.deserialize(json.load(f))
    parent = tree.nodes[tree.current_leaf]["parent"]  # the user node the captured turn answers
    assert parent is not None, f"captured turn {eval_id} has no user message to respond to"
    return info, tree.messages_to(parent), tree.file_state_at(parent)

def _saveRun(cfg: dict, lanes: dict) -> str | None:
    """Write a finished run under its turn's runs/ folder. None if the turn was deleted while the
    run was in flight — recreating its directory would leave a turn with runs but no info.json."""
    eval_dir = _evalDir(cfg['eval_id'])
    if not os.path.isdir(eval_dir):
        return None
    runs_dir = f"{eval_dir}/runs"
    os.makedirs(runs_dir, exist_ok=True)
    path = f"{runs_dir}/{cfg['started'].replace(':', '-')}.json"
    with open(path, "w") as f:
        json.dump({**cfg, "lanes": lanes}, f, indent=4)
    return path

def listRuns(eval_id: str) -> list[dict]:
    """Saved runs for a captured turn, newest first — what the studio's past-run dropdown lists."""
    runs_dir = f"{_evalDir(eval_id)}/runs"
    if not os.path.isdir(runs_dir):
        return []
    runs = []
    for name in os.listdir(runs_dir):
        with open(f"{runs_dir}/{name}") as f:
            run = json.load(f)
        runs.append({"file": name, "started": run["started"], "n": run["n"], "arms": run["arms"],
                     "cost": sum(l["cost"] for l in run["lanes"].values())})
    return sorted(runs, key=lambda r: r["started"], reverse=True)

def _toolInputs(arguments: str) -> dict | str:
    """A saved tool call's arguments as the dict the live run showed. Reads the first JSON object
    when the string carries more than one — runs saved before the streaming loop stopped doubling
    single-chunk arguments — and hands back the raw string if it isn't JSON at all."""
    try:
        return json.loads(arguments)
    except json.JSONDecodeError:
        try:
            return json.JSONDecoder().raw_decode(arguments)[0]
        except json.JSONDecodeError:
            return arguments

def _replayEvents(messages: list[dict]) -> list[dict]:
    """Flatten one lane's saved provider messages into the same ordered stream the live run emits:
    reasoning, narration, and tool calls paired back up with their results."""
    results = {m["tool_call_id"]: m["content"] for m in messages if m.get("role") == "tool"}
    events = []
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        if reasoning := (msg.get("reasoning") or "").strip():
            events.append({"kind": "think", "text": reasoning})
        if content := (msg.get("content") or "").strip():
            events.append({"kind": "text", "text": content})
        for call in msg.get("tool_calls") or []:
            events.append({"kind": "tool", "name": call["function"]["name"],
                           "inputs": _toolInputs(call["function"]["arguments"]),
                           "result": results.get(call["id"], "")})
    return events

def loadRun(eval_id: str, file: str) -> dict:
    """A saved run, each lane flattened into render-ready events (the raw messages stay on disk)."""
    with open(f"{_evalDir(eval_id)}/runs/{file}") as f:
        run = json.load(f)
    run["lanes"] = {lane: {"cost": l["cost"], "error": l.get("error"), "events": _replayEvents(l["messages"])}
                    for lane, l in run["lanes"].items()}
    return run

def _runLane(socket: SocketIO, cfg: dict, lane: str, arm: dict, prefix: list[dict], before: dict, state: dict,
             gate: threading.Event | None, is_primer: bool) -> None:
    # With caching on, one lane per arm goes first and the rest wait for it to start producing
    # output — by then the shared prefix is cached, so they read it instead of paying to write it again.
    if gate is not None and not is_primer:
        gate.wait()
    files = dict(before)  # this lane's own copy — the file tools mutate it during the run
    result = {"messages": [], "cost": 0.0}
    # Whatever goes wrong, the lane must still be counted, or the run never ends and the client
    # waits on it forever. A failure is recorded on the lane and shown in its header.
    try:
        tb = SYSTEM_TOOLBOXES[cfg["system"]](files)
        cb = StudioCallbackHandler(socket, cfg["run_id"], lane, gate if is_primer else None)
        provider = OpenRouterProvider(
            model_name=arm["model"],
            toolbox=tb,
            callback_handler=cb,
            thinking_effort="max",
            cache_mode=cfg["cache_mode"],
        )
        system_prompt = getFullStoryInstruction(cfg["system"], arm["version"], before)
        block = {"type": "text", "text": system_prompt}
        if (cc := provider.cacheControl()) is not None:
            block["cache_control"] = cc
        provider.messages = [{"role": "system", "content": [block]}] + prefix
        provider.run()  # a failed request is logged inside and ends the turn with finish_reason "error"
        result = {"messages": provider.messages[len(prefix) + 1:], "cost": provider.getCostStats()["total_cost"]}
        if cb.finish_reason == "error":
            result["error"] = "model request failed (see the server log)"
    except Exception as e:
        logger.error(f"studio lane {lane} of run {cfg['run_id']} failed: {e}", exc_info=True)
        result["error"] = str(e)
    finally:
        if is_primer and gate is not None:
            gate.set()  # a primer that produced nothing must still release the lanes behind it

    socket.emit('studio_lane_end', {"run_id": cfg["run_id"], "lane": lane, "cost": result["cost"], "error": result.get("error")})
    with state["lock"]:
        state["lanes"][lane] = result
        done = len(state["lanes"]) == state["total"]
    if done:
        path = _saveRun(cfg, state["lanes"])
        if path:
            logger.info(f"studio run {cfg['run_id']} complete → {path}")
        else:
            logger.warning(f"studio run {cfg['run_id']} complete, but its turn {cfg['eval_id']} was deleted mid-run: not saved")
        socket.emit('studio_run_end', {"run_id": cfg["run_id"], "eval_id": cfg["eval_id"], "path": path, "saved": path is not None,
                                       "cost": sum(l["cost"] for l in state["lanes"].values())})

def runStudio(socket: SocketIO, eval_id: str, arms: list[dict], n: int, cache: bool) -> str:
    """Generate n completions per arm for a captured turn, streaming each into its own lane.
    Lanes run concurrently as background tasks (SocketIO is in threading mode, so the blocking
    request in each provider gets its own thread). Each arm has its own model and its own system
    prompt — the first block — so arms share no cacheable prefix and get a staggering gate each."""
    info, prefix, before = _turnContext(eval_id)
    cfg = {"run_id": uuid.uuid4().hex[:8], "eval_id": eval_id, "arms": arms, "n": n,
           "system": info["system"], "cache": cache, "cache_mode": "5m" if cache else "none",
           "started": datetime.now().isoformat()}
    sides = list(zip("ab", arms))
    gates = {side: threading.Event() for side, _ in sides} if cache and n > 1 else {}
    lanes = [(f"{side}-{i}", side, arm, i == 0) for side, arm in sides for i in range(n)]
    state = {"lanes": {}, "total": len(lanes), "lock": threading.Lock()}

    socket.emit('studio_run_started', {**cfg, "lanes": [lane for lane, _, _, _ in lanes]})
    for lane, side, arm, is_primer in lanes:
        socket.start_background_task(_runLane, socket, cfg, lane, arm, prefix, before, state, gates.get(side), is_primer)
    return cfg["run_id"]
