import os
import re
import json
import uuid
import shutil
import logging
from datetime import datetime
from history import TurnTree

purple = '\x1b[38;2;255;0;255m'
blue = '\x1b[38;2;0;0;255m'
brown = '\x1b[38;2;128;128;0m'
cyan = '\x1b[38;2;0;255;255m'
lime = '\x1b[38;2;0;255;0m'
yellow = '\x1b[38;2;255;255;0m'
red = '\x1b[38;2;255;0;0m'
pink = '\x1b[38;2;255;51;204m'
orange = '\x1b[38;2;255;51;0m'
green = '\x1b[38;2;0;128;0m'
gray = '\x1b[38;2;127;127;127m'
magenta = '\x1b[38;2;128;0;128m'
white = '\x1b[38;2;255;255;255m'
bold = '\033[1m'
underline = '\033[4m'
endc = '\033[0m'

# Story-context entries that are pulled into the system prompt by name, and the XML tag each gets.
# Any other entry is only visible to the model through its file tools.
PROMPT_CONTEXT_FILES = {"story_plan": "story_plan", "pc": "player_character", "story_summary": "story_summary"}

# === Logging Setup ===

class ColoredFormatter(logging.Formatter):
    """Custom formatter that adds colors to log messages based on level."""
    LEVEL_COLORS = {
        logging.DEBUG: gray,
        logging.INFO: cyan,
        logging.WARNING: orange,
        logging.ERROR: red,
    }
    
    def format(self, record):
        color = self.LEVEL_COLORS.get(record.levelno, white)
        message = super().format(record)
        return f"{color}{message}{endc}"

def _debug_enabled() -> bool:
    """Check if debug mode is enabled (used at module load time)."""
    return os.environ.get("DEBUG", "0").lower() == "1"

def setup_logger() -> logging.Logger:
    """Configure and return the application logger."""
    logger = logging.getLogger("lm_rpg")
    if not logger.handlers:  # Avoid adding handlers multiple times
        handler = logging.StreamHandler()
        handler.setFormatter(ColoredFormatter("%(levelname)s: %(message)s"))
        logger.addHandler(handler)
    logger.setLevel(logging.DEBUG if _debug_enabled() else logging.WARNING)
    return logger

logger = setup_logger()

# === Constants ===

STORIES_ROOT_DIR = "stories"
STORIES_ARCHIVE_DIR = "stories/.archived"
EVAL_STORIES_DIR = "eval_stories"
INSTRUCTIONS_DIR = "instructions"
CORE_DIR = f"{INSTRUCTIONS_DIR}/core"        # one file per core version, freely named
SYSTEMS_DIR = f"{INSTRUCTIONS_DIR}/systems"  # one file per game system

# The narrator models offered in every model picker. The settings popup rewrites the file on every
# edit, and it is checked in, so it is the only copy of the list: a checkout without it starts with
# an empty list to be filled from the settings popup.
MODELS_FILE = "models.json"

def loadModels() -> list[str]:
    if not os.path.exists(MODELS_FILE):
        logger.warning(f"{MODELS_FILE} not found: the model list starts empty; add models in the settings popup")
        return []
    with open(MODELS_FILE) as f:
        return json.load(f)

def saveModels(models: list[str]) -> None:
    with open(MODELS_FILE, 'w') as f:
        json.dump(models, f, indent=4)

def listStoryIds() -> list[str]:
    return sorted(f for f in os.listdir("./stories") if not f.startswith('.'))

def readMarkdown(path: str) -> str:
    """Read a markdown file off disk with `<!-- comments -->` stripped out. A comment takes any
    newline directly after it with it, so whole-line comments don't leave a blank line behind."""
    with open(path) as f:
        return re.sub(r"^[ \t]*<!--.*?-->[ \t]*\n|<!--.*?-->\n?", "", f.read(), flags=re.DOTALL | re.MULTILINE)

def coreInstructionFile(version: str) -> str:
    """Path of a core-instruction version. The version is the bare filename stem, so versions are
    named freely (`self_review`, `no_style_guide`); the name never reaches the prompt."""
    path = f"{CORE_DIR}/{version}.md"
    assert os.path.exists(path), f"no core instruction file {path}"
    return path

def systemInstructionFile(system_name: str) -> str:
    path = f"{SYSTEMS_DIR}/{system_name}.md"
    assert os.path.exists(path), f"no system instruction file {path}"
    return path

def listCoreVersions() -> list[str]:
    """The core versions a story can be created with (the settings + New Story pickers)."""
    return sorted(f[:-3] for f in os.listdir(CORE_DIR) if f.endswith(".md") and not f.startswith("_"))

# The core version that stands in wherever none has been chosen: a browser's settings before the
# user picks one, arm A of a studio run, and the server's fallback for stories written before core
# became a per-story choice. Pinned by name, so adding a core file can't move it.
DEFAULT_CORE = "no_review"

def defaultCoreVersion() -> str:
    versions = listCoreVersions()
    if DEFAULT_CORE in versions:
        return DEFAULT_CORE
    logger.warning(f"default core version '{DEFAULT_CORE}' has no file under {CORE_DIR}/; using '{versions[0]}'")
    return versions[0]

def isValidGameSystem(system_name: str) -> bool:
    """Public validator to ensure the requested system has the required assets."""
    return os.path.exists(f"{SYSTEMS_DIR}/{system_name}.md")

def listGameSystemNames() -> list[str]:
    return sorted(f[:-3] for f in os.listdir(SYSTEMS_DIR) if f.endswith(".md") and not f.startswith("_"))

def makeNewStoryDir(display_name: str, system: str, core: str, model_name: str, root: str = STORIES_ROOT_DIR) -> str:
    """Create a new story directory (named by a fresh uuid) under `root` and return its id.
    `root` is EVAL_STORIES_DIR for turns captured into the prompt studio."""
    story_id = uuid.uuid4().hex[:16]
    story_dir = f"./{root}/{story_id}"
    os.makedirs(story_dir)
    with open(os.path.join(story_dir, "info.json"), "w") as f:
        json.dump({
            "system": system,
            "core": core,
            "model": model_name,
            "story_name": display_name,
            "created": datetime.now().isoformat(),
        }, f, indent=4)
    return story_id

def archiveStoryDir(story_id: str) -> bool:
    story_dir = f"./{STORIES_ROOT_DIR}/{story_id}"
    if not os.path.exists(story_dir):
        return False
    os.makedirs(f"./{STORIES_ARCHIVE_DIR}", exist_ok=True)
    shutil.move(story_dir, f"./{STORIES_ARCHIVE_DIR}/{story_id}")
    return True

def renameStory(story_id: str, new_name: str) -> bool:
    """Rename is just changing the display name in info.json; the uuid directory is unchanged."""
    info_path = os.path.join(f"./{STORIES_ROOT_DIR}/{story_id}", "info.json")
    if not os.path.exists(info_path):
        return False
    with open(info_path, "r") as f:
        info = json.load(f)
    info["story_name"] = new_name
    with open(info_path, "w") as f:
        json.dump(info, f, indent=4)
    return True

def loadStoryInfo(story_id: str, model_name: str = None, system_name: str = None) -> dict[str, str]:
    info_path = os.path.join(f"./stories/{story_id}", "info.json")
    if not os.path.exists(info_path):
        # Create info.json if it doesn't exist, using provided model and system
        if model_name and system_name:
            story_dir = f"./stories/{story_id}"
            os.makedirs(story_dir, exist_ok=True)
            with open(info_path, "w") as f:
                json.dump({
                    "system": system_name,
                    "model": model_name,
                    "story_name": story_id,
                }, f, indent=4)
        else:
            raise FileNotFoundError(f"info.json not found for story '{story_id}' and no model/system provided to create it")
    with open(info_path, "r") as f:
        return json.load(f)

def historyExists(story_id: str) -> bool:
    return os.path.exists(f"./stories/{story_id}/history.json")

def getFullStoryInstruction(system_name: str, core_version: str, files: dict[str, str]) -> str:
    """Fetches the system instructions and appends the named story-context entries (pc,
    story_plan, story_summary) pulled from the in-memory `files` dict.

    `core_version` picks which file in instructions/core/ to load — a story's own version for
    play, and the version under test for a prompt-studio lane. Only its contents go into the
    prompt; the version name is never shown to the model.

    Each section is wrapped in XML tags for clarity:
    - <core_instructions>/<system_instructions>: base instructions (real files on disk)
    - <story_plan>: The story plan/outline
    - <player_character>: The player character details
    - <story_summary>: Summary of story events so far
    """
    result_parts = []

    # Load core instructions (required, shared across all systems)
    core_instructions = readMarkdown(coreInstructionFile(core_version))
    result_parts.append(f"<core_instructions>\n{core_instructions}\n</core_instructions>")

    # Load system-specific instructions (required)
    system_instructions = readMarkdown(systemInstructionFile(system_name))
    result_parts.append(f"<system_instructions>\n{system_instructions}\n</system_instructions>")

    for fname, tag in PROMPT_CONTEXT_FILES.items():
        if fname in files:
            result_parts.append(f"<{tag}>\n{files[fname]}\n</{tag}>")

    return "\n\n".join(result_parts)

# === History Archive Functions ===

def getPreviousHistoryDir(story_id: str) -> str:
    """Get the path to the previous history directory for a story."""
    return f"./{STORIES_ROOT_DIR}/{story_id}/previous"

def getNextArchiveNumber(story_id: str) -> int:
    """Get the next available archive number (0, 1, 2...).

    Archives are numbered sequentially starting from 0.
    0 = oldest archived, higher numbers = more recently archived.
    """
    prev_dir = getPreviousHistoryDir(story_id)
    if not os.path.exists(prev_dir):
        return 0
    existing = [f for f in os.listdir(prev_dir) if f.endswith('.json')]
    return len(existing)

def archiveHistory(story_id: str) -> bool:
    """Move history.json to previous/{n}.json and delete history.json.

    Returns True if archive was successful, False if no history to archive.
    """
    history_path = f"./{STORIES_ROOT_DIR}/{story_id}/history.json"
    if not os.path.exists(history_path):
        return False
    prev_dir = getPreviousHistoryDir(story_id)
    os.makedirs(prev_dir, exist_ok=True)
    archive_num = getNextArchiveNumber(story_id)
    shutil.move(history_path, f"{prev_dir}/{archive_num}.json")
    return True

def loadAllPreviousHistory(story_id: str) -> list[dict]:
    """Load all previous history files in order (oldest first).
    
    Returns a flat list of all messages from all previous history files,
    combined in chronological order (0.json first, then 1.json, etc.).
    """
    prev_dir = getPreviousHistoryDir(story_id)
    if not os.path.exists(prev_dir):
        return []
    
    # Get all json files and sort by number
    files = [f for f in os.listdir(prev_dir) if f.endswith('.json')]
    files.sort(key=lambda f: int(f.replace('.json', '')))
    
    all_messages = []
    for filename in files:
        filepath = os.path.join(prev_dir, filename)
        try:
            with open(filepath, 'r') as f:
                data = json.load(f)
                if "nodes" in data:  # new branching format — flatten the active path
                    all_messages.extend(TurnTree.deserialize(data).active_messages())
                else:
                    all_messages.extend(data.get('messages', []))
        except (json.JSONDecodeError, IOError):
            continue
    
    return all_messages

def _sourceFileState(source_dir: str, at_start: bool = False) -> dict[str, str]:
    """Reconstruct a story's story-context files from its history tree (empty if no tree or a
    legacy flat history, which carries no file deltas). `at_start` gives the state at the root of
    the active path (pre-turn-1, i.e. just the seeded context) instead of the current leaf; for an
    archived story that root lives in the oldest archive, since archiving starts a fresh tree whose
    root carries the whole story context as it stood at compaction time."""
    history_path = os.path.join(source_dir, "history.json")
    oldest_archive = os.path.join(source_dir, "previous", "0.json")
    if at_start and os.path.exists(oldest_archive):
        history_path = oldest_archive
    if not os.path.exists(history_path):
        return {}
    with open(history_path) as f:
        data = json.load(f)
    if "nodes" not in data:
        return {}
    tree = TurnTree.deserialize(data)
    path = tree.path_to(tree.current_leaf)
    return tree.file_state_at(path[0] if at_start and path else tree.current_leaf)

def copyStory(source_story_id: str, new_name: str, new_model_name: str, mode: str) -> str | None:
    """Copy a story into a fresh uuid directory.

    Args:
        source_story_id: Id (uuid directory) of the source story to copy
        new_name: Display name for the new story
        new_model_name: Model name for the new story
        mode: "duplicate" for an exact copy (story context + full message history), or "run" for a
            fresh story seeded with the source's setup — its story context from before the first
            turn — and no messages

    Returns:
        The new story's id, or None in "run" mode when the source has no setup to seed from.
    """
    assert mode in ("duplicate", "run"), f"unknown copy mode: {mode}"
    source_dir = f"./{STORIES_ROOT_DIR}/{source_story_id}"
    assert os.path.exists(source_dir), f"no such story: {source_story_id}"

    setup = _sourceFileState(source_dir, at_start=True) if mode == "run" else None
    if setup == {}:  # the story context was first written during turn 1, so there is no setup
        return None

    new_story_id = uuid.uuid4().hex[:16]
    new_dir = f"./{STORIES_ROOT_DIR}/{new_story_id}"
    os.makedirs(new_dir)
    source_info = loadStoryInfo(source_story_id)
    source_system = source_info.get('system', 'hp')
    with open(os.path.join(new_dir, "info.json"), "w") as f:
        json.dump({
            "system": source_system,
            "core": source_info.get('core'),
            "model": new_model_name,
            "story_name": new_name,
            "created": datetime.now().isoformat(),
        }, f, indent=4)

    # Story context lives inside the history tree, so a duplicate gets it for free along with the
    # history; a run synthesizes a fresh tree whose hidden root carries the setup and nothing else.
    if mode == "duplicate":
        source_history = os.path.join(source_dir, "history.json")
        if os.path.exists(source_history):  # absent for a story that was never played
            shutil.copy2(source_history, os.path.join(new_dir, "history.json"))
        source_prev_dir = getPreviousHistoryDir(source_story_id)
        if os.path.exists(source_prev_dir):
            shutil.copytree(source_prev_dir, getPreviousHistoryDir(new_story_id))
    else:
        tree = TurnTree.empty()
        tree.add_node(None, "user", [], files=setup)
        with open(os.path.join(new_dir, "history.json"), "w") as f:
            json.dump({"model_name": new_model_name, "system_name": source_system, **tree.serialize()}, f, indent=4)

    return new_story_id
