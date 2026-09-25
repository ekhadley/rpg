#!./.venv/bin/python
import os
import glob
import json
import shutil
from flask_socketio import SocketIO, emit
from flask import Flask, render_template, redirect, url_for
from narrator import Narrator

from utils import (
    logger, listStoryIds, loadStoryInfo, makeNewStoryDir,
    historyExists, isValidGameSystem, listGameSystemNames,
    archiveHistory, copyStory, archiveStoryDir, renameStory,
    loadAllPreviousHistory, systemInstructionFile, readMarkdown,
    listCoreVersions, EVAL_STORIES_DIR, PROMPT_CONTEXT_FILES,
    loadModels, saveModels, unifiedDiff,
)
from studio import listEvalTurns, runStudio, listRuns, loadRun

app = Flask(__name__, template_folder="frontend/templates", static_folder="frontend/static")
app.secret_key = os.urandom(24)
socket = SocketIO(app, cors_allowed_origins="*")

global narrator
narrator = None
# cache_mode: "none" | "5m" | "1h". core: the core-instruction version new stories default to,
# and the one used for stories written before core became a per-story choice.
settings = {"cache_mode": "1h", "core": listCoreVersions()[0]}
models = loadModels()

def init_narrator(story_id: str, story_info: dict, model_name: str) -> Narrator:
    logger.debug(f"{'loading existing' if historyExists(story_id) else 'creating new'} history for story: '{story_id}'")
    return Narrator(
        model_name = model_name,
        system_name = story_info["system"],
        core_version = story_info.get("core") or settings["core"],
        story_id = story_id,
        socket = socket,
        cache_mode = settings["cache_mode"],
    )

@socket.on('set_settings')
def set_settings(data: dict[str, str]):
    cache_mode = data.get('cache_mode')
    if cache_mode not in ("none", "5m", "1h"):
        emit('error', {"message": f"Invalid cache mode: {cache_mode}"})
        return
    settings["cache_mode"] = cache_mode
    if narrator is not None:
        narrator.setCacheMode(cache_mode)
    core = data.get('core')
    if core in listCoreVersions():
        settings["core"] = core
    logger.debug(f"settings: cache mode '{cache_mode}', default core '{settings['core']}'")

@socket.on('set_models')
def set_models(data: dict):
    """Replace the model list with the one the settings popup sent, then broadcast it so every
    client's model pickers rebuild."""
    global models
    models = [m.strip() for m in data['models'] if m.strip()]
    saveModels(models)
    socket.emit('models_updated', {"models": models})
    logger.debug(f"model list updated ({len(models)} models)")

# The story-context sidebar's whole payload: every entry with its size, plus which names the
# system prompt pulls in by name (they get their own group at the top of the list).
def contextListing() -> dict:
    return {
        "story_context": {name: len(text) for name, text in narrator.files.items()},
        "prompt_context_names": list(PROMPT_CONTEXT_FILES),
    }

@socket.on('select_story')
def select_story(data: dict[str, str]):
    logger.debug(f"selected story: '{data['selected_story']}'")
    story_id = data['selected_story']
    model_name = data.get('model_name')
    system_name = data.get('system_name')
    story_info = loadStoryInfo(story_id, model_name=model_name, system_name=system_name)
    system_name = story_info.get("system")
    if not system_name or not isValidGameSystem(system_name):
        emit('error', {"message": f"Invalid or unavailable system for story '{story_id}': {system_name}"})
        return
    model_name = story_info.get("model", data.get('model_name'))

    global narrator
    narrator = init_narrator(story_id, story_info, model_name)
    logger.debug(f"narrator initialized for story: '{story_id}'")
    narrator.loadStory()
    emit('story_locked', {
        "model_name": narrator.model_name,
        "system_name": story_info["system"],
        "core_version": narrator.core_version,
        **contextListing(),
    })
    logger.info(f"narrator initialized: {narrator}")

@socket.on('start_story')
def start_story():
    global narrator
    if narrator is None:
        emit('error', {"message": "No story selected"})
        return
    narrator.startStory()

@socket.on('user_message')
def handle_user_message(data: dict[str, str]):
    global narrator
    if narrator is None:
        emit('error', {"message": "No story selected"})
        return
    narrator.handleUserMessage(data)

# Handlers run in their own threads, so this arrives while user_message (or retry/edit) is still
# blocked inside the provider's stream; the narrator discards the turn as if it had failed.
@socket.on('stop_turn')
def stop_turn():
    global narrator
    if narrator is not None:
        narrator.stop()

@socket.on('create_story')
def create_story(data: dict[str, str]):
    display_name = data['story_name'].strip()
    system = data['system_name']
    model_name = data['model_name']
    core = data.get('core', settings["core"])
    if display_name:
        if not isValidGameSystem(system):
            emit('error', {"message": f"Invalid or unavailable system '{system}'"})
            return
        if core not in listCoreVersions():
            emit('error', {"message": f"Invalid core version '{core}'"})
            return
        story_id = makeNewStoryDir(display_name, system, core, model_name)
        emit('story_created', {
            "id": story_id,
            "name": display_name,
            "system": system,
            "model": model_name,
            "last_activity": story_last_activity(story_id)
        })

@socket.on('copy_story')
def copy_story(data: dict[str, str]):
    source_story_id = data.get('source_story')
    new_name = data.get('new_story_name', '').strip()
    model_name = data.get('model_name')
    mode = data.get('mode', 'duplicate')

    if not source_story_id or not new_name:
        emit('error', {"message": "Source story and new story name are required"})
        return

    if not model_name:
        emit('error', {"message": "Model name is required"})
        return

    new_story_id = copyStory(source_story_id, new_name, model_name, mode)
    if new_story_id is None:
        emit('error', {"message": "This story has no setup to start a run from: its story context was first written during the opening turn, not before it. Duplicate the story instead."})
        return

    story_info = loadStoryInfo(new_story_id)
    emit('story_copied', {
        "id": new_story_id,
        "name": new_name,
        "system": story_info.get('system', 'hp'),
        "model": model_name,
        "last_activity": story_last_activity(new_story_id),
    })

@socket.on('summarize_history')
def summarize_history():
    global narrator
    if narrator is None:
        emit('error', {"message": "No story selected"})
        return
    story_id = narrator.story_id
    logger.debug(f"summarizing story: '{story_id}' -- sending compaction message to model")
    # Let the model write the summary / save any state it needs from the full conversation. If that
    # turn fails, nothing is archived: the conversation would be gone with no summary to replace it.
    if not narrator.handleUserMessage({"message": "System: archive story state"}):
        return
    if archiveHistory(story_id):
        logger.debug(f"archived full history to previous/ for story: '{story_id}', starting fresh conversation")
        # clearMessages rebuilds context, which live-reads the freshly-written summary into the system prompt
        narrator.clearMessages()
        emit('history_summarized', {"success": True})
    else:
        emit('error', {"message": "No history to summarize"})

@socket.on('delete_story')
def delete_story(data: dict[str, str]):
    global narrator
    story_id = data.get('story_id', '').strip()
    if not story_id:
        emit('error', {"message": "Story id is required"})
        return
    if archiveStoryDir(story_id):
        # If the deleted story is currently loaded, clear the narrator
        if narrator is not None and narrator.story_id == story_id:
            narrator = None
        logger.debug(f"archived (deleted) story: '{story_id}'")
        emit('story_deleted', {"story_id": story_id})
    else:
        emit('error', {"message": f"Story '{story_id}' not found"})

@socket.on('rename_story')
def rename_story(data: dict[str, str]):
    story_id = data.get('story_id', '').strip()
    new_name = data.get('new_name', '').strip()
    if not story_id or not new_name:
        emit('error', {"message": "Story id and new name are required"})
        return
    if renameStory(story_id, new_name):
        logger.debug(f"renamed story '{story_id}' -> '{new_name}'")
        emit('story_renamed', {"story_id": story_id, "new_name": new_name})
    else:
        emit('error', {"message": f"Failed to rename story '{story_id}'"})

@socket.on('get_system_instructions')
def get_system_instructions():
    global narrator
    if narrator is None:
        emit('error', {"message": "No story selected"})
        return
    content = readMarkdown(systemInstructionFile(narrator.system_name))
    emit('story_file_content', {"filename": f"{narrator.system_name}.md", "content": content})

@socket.on('get_story_file')
def get_story_file(data: dict[str, str]):
    global narrator
    if narrator is None:
        emit('error', {"message": "No story selected"})
        return
    filename = data.get('filename', '')
    if filename not in narrator.files:
        emit('error', {"message": f"File not found: {filename}"})
        return
    emit('story_file_content', {"filename": filename, "content": narrator.files[filename], "editable": True})

@socket.on('save_story_file')
def save_story_file(data: dict[str, str]):
    global narrator
    if narrator is None:
        emit('error', {"message": "No story selected"})
        return
    filename = data.get('filename', '')
    if filename not in narrator.files:
        emit('error', {"message": f"File not found: {filename}"})
        return
    narrator.editFile(filename, data.get('content', ''))
    emit('story_file_saved', {"filename": filename})
    emit('story_files', contextListing())

@socket.on('create_story_file')
def create_story_file(data: dict[str, str]):
    global narrator
    if narrator is None:
        emit('error', {"message": "No story selected"})
        return
    filename = data.get('filename', '').strip()
    if not filename or filename in narrator.files:
        emit('error', {"message": f"Invalid or duplicate entry name: {filename!r}"})
        return
    narrator.editFile(filename, '')
    emit('story_file_created', {"filename": filename})
    emit('story_files', contextListing())

@socket.on('delete_story_file')
def delete_story_file(data: dict[str, str]):
    global narrator
    if narrator is None:
        emit('error', {"message": "No story selected"})
        return
    filename = data.get('filename', '')
    if filename not in narrator.files:
        emit('error', {"message": f"File not found: {filename}"})
        return
    narrator.deleteFile(filename)
    emit('story_file_deleted', {"filename": filename})
    emit('story_files', contextListing())

@socket.on('rename_story_file')
def rename_story_file(data: dict[str, str]):
    global narrator
    if narrator is None:
        emit('error', {"message": "No story selected"})
        return
    old_name, new_name = data.get('filename', ''), data.get('new_name', '').strip()
    if old_name not in narrator.files or not new_name or new_name in narrator.files:
        emit('error', {"message": f"Cannot rename {old_name!r} to {new_name!r}"})
        return
    narrator.editFile(new_name, narrator.files[old_name])
    narrator.deleteFile(old_name)
    emit('story_files', contextListing())

@socket.on('duplicate_story_file')
def duplicate_story_file(data: dict[str, str]):
    global narrator
    if narrator is None:
        emit('error', {"message": "No story selected"})
        return
    filename = data.get('filename', '')
    if filename not in narrator.files:
        emit('error', {"message": f"File not found: {filename}"})
        return
    copy_name = f"{filename}_copy"
    n = 2
    while copy_name in narrator.files:
        copy_name, n = f"{filename}_copy_{n}", n + 1
    narrator.editFile(copy_name, narrator.files[filename])
    emit('story_files', contextListing())

# The model's file tools mutate narrator.files in place, so the sidebar asks for a fresh listing
# whenever a turn touches them.
@socket.on('list_story_files')
def list_story_files():
    global narrator
    if narrator is None:
        return
    emit('story_files', contextListing())

@socket.on('get_debug_messages')
def get_debug_messages():
    global narrator
    if narrator is None:
        emit('debug_messages', [])
        return

    def truncate_system(msg):
        m = dict(msg)
        if m.get('role') == 'system' and isinstance(m.get('content'), str) and len(m['content']) > 200:
            m['content'] = m['content'][:200] + f'... ({len(msg["content"])} chars total)'
        return m

    result = []
    # Previous (archived) conversations
    previous = loadAllPreviousHistory(narrator.story_id)
    if previous:
        result.extend([truncate_system(m) for m in previous])
        result.append({"role": "_separator", "content": "Current Conversation"})
    # Current/live conversation: system message + active-path nodes, with a marker after each
    # turn that changed files carrying a unified diff of each entry it touched (the story context
    # is replayed down the path the same way file_state_at does, so `before` is the parent's state).
    msgs = narrator.provider.messages
    if msgs and msgs[0].get("role") == "system":
        result.append(truncate_system(msgs[0]))
    state: dict[str, str] = {}
    for nid in narrator.tree.path():
        node = narrator.tree.nodes[nid]
        result.extend(truncate_system(m) for m in node["messages"])
        files = node.get("files") or {}
        if files:
            before = dict(state)
            narrator.tree.apply_delta(state, files)
            result.append({
                "role": "_files",
                "changed": [f for f, c in files.items() if c is not None],
                "removed": [f for f, c in files.items() if c is None],
                "diffs": {f: unifiedDiff(f, before.get(f, ""), state.get(f, "")) for f in files if "/" not in f},
            })
    emit('debug_messages', result)

@socket.on('retry_response')
def retry_response(data=None):
    global narrator
    if narrator is None:
        emit('error', {"message": "No story selected"})
        return
    turn_id = data.get('turn_id') if data else None
    if turn_id:
        narrator.regenerate_turn(turn_id)

@socket.on('rollback_turn')
def rollback_turn(data=None):
    global narrator
    if narrator is None:
        emit('error', {"message": "No story selected"})
        return
    turn_id = data.get('turn_id') if data else None
    if turn_id:
        narrator.rollback_to(turn_id)

@socket.on('fork_story')
def fork_story(data: dict[str, str]):
    global narrator
    new_name = data.get('new_story_name', '').strip()
    turn_id = data.get('turn_id')
    if narrator is None or not new_name or not turn_id:
        emit('error', {"message": "Fork requires an active story, a turn, and a name"})
        return
    new_id = narrator.fork_to(turn_id, new_name)
    if new_id:
        info = loadStoryInfo(new_id)
        emit('story_forked', {"id": new_id, "name": new_name, "system": info.get('system', 'hp'), "model": info.get('model'), "last_activity": story_last_activity(new_id)})

@socket.on('capture_eval_turn')
def capture_eval_turn(data: dict[str, str]):
    """Freeze a turn into ./eval_stories/ for the prompt studio. Same fork the story list uses, just
    written to a different root. Does not switch the view."""
    global narrator
    turn_id = data.get('turn_id')
    if narrator is None or not turn_id:
        emit('error', {"message": "Capture requires an active story and a turn"})
        return
    name = data.get('name', '').strip() or f"{loadStoryInfo(narrator.story_id)['story_name']} — turn"
    new_id = narrator.fork_to(turn_id, name, root=EVAL_STORIES_DIR)
    if new_id:
        emit('eval_turn_captured', {"id": new_id, "name": name})
        emit('eval_turns', listEvalTurns())

@socket.on('list_eval_turns')
def list_eval_turns():
    emit('eval_turns', listEvalTurns())

@socket.on('delete_eval_turn')
def delete_eval_turn(data: dict[str, str]):
    eval_id = data.get('id')
    if eval_id:
        shutil.rmtree(f"./{EVAL_STORIES_DIR}/{eval_id}")
        emit('eval_turns', listEvalTurns())

@socket.on('studio_run')
def studio_run(data: dict):
    """Generate n completions per arm (model + core version) for a captured turn, streamed lane by lane."""
    runStudio(
        socket,
        eval_id=data['eval_id'],
        arms=[{"model": a['model'], "version": a['version']} for a in data['arms']],
        n=int(data.get('n', 1)),
        cache=bool(data.get('cache', True)),
    )

@socket.on('list_studio_runs')
def list_studio_runs(data: dict):
    emit('studio_runs', {"eval_id": data['eval_id'], "runs": listRuns(data['eval_id'])})

@socket.on('load_studio_run')
def load_studio_run(data: dict):
    emit('studio_run_loaded', loadRun(data['eval_id'], data['file']))

@socket.on('get_studio_options')
def get_studio_options():
    emit('studio_options', {"versions": listCoreVersions(), "models": models})

@socket.on('edit_message')
def edit_message(data):
    global narrator
    if narrator is None:
        emit('error', {"message": "No story selected"})
        return
    turn_id = data.get('turn_id')
    new_content = data.get('new_content', '')
    if not turn_id or not new_content:
        emit('error', {"message": "turn_id and new_content are required"})
        return
    narrator.edit_turn(turn_id, new_content)

@socket.on('switch_branch')
def switch_branch(data):
    global narrator
    if narrator is None:
        emit('error', {"message": "No story selected"})
        return
    turn_id = data.get('turn_id')
    direction = data.get('dir', 1)
    if turn_id:
        narrator.switch_branch(turn_id, direction)

def story_last_activity(story_id):
    """Timestamp of the most recent message in the story — current history plus any archived
    ones, so archiving doesn't reset a story's date — falling back to the story's creation time.
    ISO-8601 strings sort chronologically as plain text, so we can compare them directly."""
    story_dir = f"./stories/{story_id}"
    paths = [f"{story_dir}/history.json"] + glob.glob(f"{story_dir}/previous/*.json")
    timestamps = []
    for path in paths:
        if os.path.exists(path):
            with open(path) as f:
                history = json.load(f)
            timestamps += [m.get("timestamp", "") for n in history.get("nodes", []) for m in n["messages"]]
    return max(timestamps) if any(timestamps) else loadStoryInfo(story_id).get("created", "")

def get_stories_with_info():
    """Helper to load all stories with their info. 'id' is the uuid directory, 'name' is the display name."""
    stories_with_info = []
    for story_id in listStoryIds():
        try:
            story_info = loadStoryInfo(story_id)
            stories_with_info.append({
                'id': story_id,
                'name': story_info.get('story_name', story_id),
                'system': story_info.get('system', 'unknown'),
                'model': story_info.get('model', 'unknown'),
                'last_activity': story_last_activity(story_id)
            })
        except Exception as e:
            # If we can't load info, still show the story
            stories_with_info.append({
                'id': story_id,
                'name': story_id,
                'system': 'unknown',
                'model': 'unknown',
                'last_activity': story_last_activity(story_id)
            })
    return sorted(stories_with_info, key=lambda s: s['last_activity'], reverse=True)

@app.route('/')
def index():
    return render_template('index.html', 
                           stories=get_stories_with_info(), 
                           models=models, 
                           systems=listGameSystemNames(),
                           cores=listCoreVersions(),
                           selected_story=None)

@app.route('/stories/<story_id>')
def story_page(story_id):
    # Check if story exists
    if story_id not in listStoryIds():
        # Story doesn't exist, redirect to home
        return redirect(url_for('index'))
    return render_template('index.html',
                           stories=get_stories_with_info(),
                           models=models,
                           systems=listGameSystemNames(),
                           cores=listCoreVersions(),
                           selected_story=story_id)

if __name__ == "__main__":
    socket.run(app, port=5001, debug=True, allow_unsafe_werkzeug=True)
