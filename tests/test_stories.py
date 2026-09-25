"""Story directories on disk: copying (duplicate / run), archiving, forking, and the studio's
capture of a turn."""
import os
import json
from conftest import say, tool, CORE, SYSTEM, MODEL, assistant_nodes
from history import TurnTree
from utils import copyStory, loadStoryInfo, archiveHistory, loadAllPreviousHistory, _sourceFileState, EVAL_STORIES_DIR
from studio import _turnContext, listEvalTurns

PC = {"file_name": "pc", "contents": "HP: 10"}


def played_story(make_narrator, seeded: bool):
    """A story with two turns. `seeded` gives it a story plan from before turn 1 (the setup a
    "run" copy needs); otherwise its context is first written during turn 1."""
    n, _ = make_narrator([tool("write_file", PC, then=say("a1")), say("a2")])
    if seeded:
        n.editFile("story_plan", "the plan")
    n.handleUserMessage({"message": "one"})
    n.handleUserMessage({"message": "two"})
    return n


def test_duplicate_copies_history_verbatim(make_narrator):
    n = played_story(make_narrator, seeded=True)
    new_id = copyStory(n.story_id, "Copy", "other/model", "duplicate")
    assert new_id != n.story_id
    info = loadStoryInfo(new_id)
    assert info["system"] == SYSTEM and info["core"] == CORE and info["model"] == "other/model"
    assert info["story_name"] == "Copy"
    with open(f"stories/{new_id}/history.json") as f, open(n.story_history_path) as g:
        assert json.load(f) == json.load(g)


def test_run_copy_seeds_a_fresh_tree_with_the_setup(make_narrator):
    n = played_story(make_narrator, seeded=True)
    new_id = copyStory(n.story_id, "Run 2", MODEL, "run")
    with open(f"stories/{new_id}/history.json") as f:
        tree = TurnTree.deserialize(json.load(f))
    assert tree.active_messages() == []
    assert tree.file_state_at(tree.current_leaf) == {"story_plan": "the plan"}  # not the pc written in turn 1


def test_run_copy_refuses_a_story_with_no_setup(make_narrator):
    n = played_story(make_narrator, seeded=False)
    assert copyStory(n.story_id, "Run 2", MODEL, "run") is None
    assert sorted(os.listdir("stories")) == [n.story_id]  # nothing half-created


def test_archive_numbers_sequentially_and_previous_history_flattens_the_active_path(make_narrator):
    n = played_story(make_narrator, seeded=False)
    assert archiveHistory(n.story_id) is True
    assert os.listdir(f"stories/{n.story_id}/previous") == ["0.json"]
    assert not os.path.exists(n.story_history_path)
    assert archiveHistory(n.story_id) is False  # nothing left to archive
    n.clearMessages()  # writes a fresh history.json carrying the context
    assert archiveHistory(n.story_id) is True
    assert sorted(os.listdir(f"stories/{n.story_id}/previous")) == ["0.json", "1.json"]
    previous = loadAllPreviousHistory(n.story_id)
    assert [m["content"] for m in previous if m["role"] == "user"] == ["one", "two"]


def test_setup_of_an_archived_story_comes_from_the_oldest_archive(make_narrator):
    n = played_story(make_narrator, seeded=True)
    archiveHistory(n.story_id)
    n.clearMessages()  # the new tree's root carries the whole context as of now, not the setup
    assert _sourceFileState(f"stories/{n.story_id}") == {"story_plan": "the plan", "pc": "HP: 10"}
    assert _sourceFileState(f"stories/{n.story_id}", at_start=True) == {"story_plan": "the plan"}


def test_fork_trims_to_the_linear_path_and_carries_context(make_narrator):
    n, _ = make_narrator([
        tool("write_file", PC, then=say("a1")),
        tool("edit_file", {"file_name": "pc", "old_text": "10", "new_text": "3"}, then=say("a2")),
        say("a2 retry"),
        say("a3"),
    ])
    n.handleUserMessage({"message": "one"})
    n.handleUserMessage({"message": "two"})
    a2 = n.tree.current_leaf
    n.regenerate_turn(a2)
    n.handleUserMessage({"message": "three"})
    source_nodes = json.loads(json.dumps(n.tree.serialize()))

    new_id = n.fork_to(a2, "Forked")
    with open(f"stories/{new_id}/history.json") as f:
        fork = TurnTree.deserialize(json.load(f))
    assert fork.current_leaf == a2
    assert fork.path() == n.tree.path_to(a2)
    assert set(fork.nodes) == set(n.tree.path_to(a2))  # the sibling and the later turn are not copied
    assert fork.nodes[a2]["children"] == []
    assert all(len(fork.nodes[nid]["children"]) <= 1 for nid in fork.nodes)
    assert fork.file_state_at(a2) == {"pc": "HP: 3"}
    assert loadStoryInfo(new_id)["core"] == CORE
    assert n.tree.serialize() == source_nodes  # the source is untouched
    assert n.fork_to(n.tree.path()[0], "not an assistant node") is None


def test_capture_for_the_studio_freezes_the_turns_context(make_narrator):
    n = played_story(make_narrator, seeded=True)
    a1, a2 = assistant_nodes(n.tree)
    eval_id = n.fork_to(a2, "captured", root=EVAL_STORIES_DIR)
    assert os.path.exists(f"{EVAL_STORIES_DIR}/{eval_id}/history.json")
    assert [t["id"] for t in listEvalTurns()] == [eval_id]
    info, prefix, files = _turnContext(eval_id)
    assert info["system"] == SYSTEM and info["core"] == CORE
    assert prefix[-1] == {k: v for k, v in n.tree.nodes[n.tree.nodes[a2]["parent"]]["messages"][0].items()}
    assert [m["content"] for m in prefix if m["role"] == "user"] == ["one", "two"]
    assert files == {"story_plan": "the plan", "pc": "HP: 10"}  # context as of before the captured turn
