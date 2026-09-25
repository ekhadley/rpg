"""Narrator turn operations over the tree: new turns, retry, edit, branch switching, rollback,
manual context edits, and what happens to all of that when a turn fails."""
import os
import json
import pytest
from conftest import say, tool, fail, fail_after, stopped_after, assistant_nodes, narration

PC = {"file_name": "pc", "contents": "HP: 10"}


def test_user_message_adds_a_turn_with_its_file_delta(make_narrator):
    n, sock = make_narrator([tool("write_file", PC, then=say("You wake."))])
    assert n.handleUserMessage({"message": "I wake up"}) is True
    u, a = n.tree.path()
    assert n.tree.nodes[u]["role"] == "user" and n.tree.nodes[a]["role"] == "assistant"
    assert n.tree.nodes[u]["messages"][0]["content"] == "I wake up"
    assert [m["role"] for m in n.tree.nodes[a]["messages"]] == ["assistant", "tool", "assistant"]
    assert n.tree.nodes[a]["files"] == {"pc": "HP: 10"}
    assert n.files == {"pc": "HP: 10"}
    assert os.path.exists(n.story_history_path)
    assert sock.names()[-1] == "conversation_history"
    # the provider's flat list is [system] + the active branch
    assert n.provider.messages[0]["role"] == "system"
    assert n.provider.messages[1:] == n.tree.active_messages()


def test_unchanged_files_produce_an_empty_delta(make_narrator):
    n, _ = make_narrator([tool("write_file", PC, then=say("one")), say("two")])
    n.handleUserMessage({"message": "a"})
    n.handleUserMessage({"message": "b"})
    a1, a2 = assistant_nodes(n.tree)
    assert n.tree.nodes[a1]["files"] == {"pc": "HP: 10"}
    assert n.tree.nodes[a2]["files"] == {}
    assert n.files == {"pc": "HP: 10"}


def test_regenerate_makes_a_sibling_from_the_parents_context(make_narrator):
    n, _ = make_narrator([
        tool("write_file", PC, then=say("first")),
        tool("edit_file", {"file_name": "pc", "old_text": "10", "new_text": "3"}, then=say("second: hurt")),
        say("second: retry"),
    ])
    n.handleUserMessage({"message": "a"})
    n.handleUserMessage({"message": "b"})
    a2 = n.tree.current_leaf
    assert n.files == {"pc": "HP: 3"}
    n.regenerate_turn(a2)
    a2b = n.tree.current_leaf
    assert a2b != a2 and n.tree.siblings(a2) == [a2, a2b]
    assert narration(n.tree, a2b) == "second: retry"
    # the retry ran from the parent's context, so the first attempt's edit is not in it
    assert n.files == {"pc": "HP: 10"}
    assert n.tree.nodes[a2b]["files"] == {}
    assert n.tree.file_state_at(a2) == {"pc": "HP: 3"}


def test_edit_branches_from_the_grandparent(make_narrator):
    n, _ = make_narrator([say("a1"), say("a2"), say("a2 edited")])
    n.handleUserMessage({"message": "one"})
    n.handleUserMessage({"message": "two"})
    u1, a1, u2, a2 = n.tree.path()
    n.edit_turn(u2, "two, but different")
    path = n.tree.path()
    assert path[:2] == [u1, a1]
    u2b = path[2]
    assert u2b != u2 and n.tree.siblings(u2) == [u2, u2b]
    assert n.tree.nodes[u2b]["messages"][0]["content"] == "two, but different"
    assert narration(n.tree, path[3]) == "a2 edited"


def test_switch_branch_materializes_that_branchs_context(make_narrator):
    n, sock = make_narrator([
        tool("write_file", PC, then=say("first")),
        tool("write_file", {"file_name": "pc", "contents": "HP: 1"}, then=say("retry")),
    ])
    n.handleUserMessage({"message": "a"})
    a1 = n.tree.current_leaf
    n.regenerate_turn(a1)
    a1b = n.tree.current_leaf
    assert n.files == {"pc": "HP: 1"}
    sock.clear()
    n.switch_branch(a1b, 1)  # wraps around to the first sibling
    assert n.tree.current_leaf == a1
    assert n.files == {"pc": "HP: 10"}
    assert "conversation_history" in sock.names() and "turn_end" in sock.names()
    n.switch_branch(a1, -1)
    assert n.tree.current_leaf == a1b and n.files == {"pc": "HP: 1"}


def test_rollback_moves_the_leaf_and_keeps_descendants(make_narrator):
    n, _ = make_narrator([tool("write_file", PC, then=say("a1")), say("a2"), say("a3")])
    for m in "abc":
        n.handleUserMessage({"message": m})
    a1, a2, a3 = assistant_nodes(n.tree)
    n.files["pc"] = "tampered"  # in-memory only; rollback must reset it from the tree
    n.rollback_to(a1)
    assert n.tree.current_leaf == a1 and n.files == {"pc": "HP: 10"}
    assert n.tree.nodes[a1]["children"]  # the later turns still hang off it
    n.handleUserMessage({"message": "d"})  # a new message branches from the rollback point
    assert n.tree.path()[:2] == [n.tree.path()[0], a1]
    assert len(n.tree.nodes[a1]["children"]) == 2


def test_manual_edits_fold_into_the_leafs_delta_and_stay_local_to_the_branch(make_narrator):
    n, _ = make_narrator([tool("write_file", PC, then=say("a1")), say("retry")])
    n.handleUserMessage({"message": "a"})
    a1 = n.tree.current_leaf
    n.editFile("pc", "HP: 10\nGold: 5")
    n.editFile("notes", "remember the key")
    assert n.tree.nodes[a1]["files"] == {"pc": "HP: 10\nGold: 5", "notes": "remember the key"}
    n.deleteFile("notes")
    assert "notes" not in n.tree.nodes[a1]["files"] and "notes" not in n.files
    # a sibling branch never sees the manual edit
    n.regenerate_turn(a1)
    assert n.files == {}
    n.switch_branch(n.tree.current_leaf, 1)
    assert n.files == {"pc": "HP: 10\nGold: 5"}
    with open(n.story_history_path) as f:
        assert "Gold: 5" in f.read()


def test_manual_edit_on_an_empty_story_seeds_a_hidden_root(make_narrator):
    n, _ = make_narrator([say("opening")])
    n.editFile("story_plan", "a plan")
    root = n.tree.current_leaf
    assert n.tree.nodes[root]["messages"] == [] and n.tree.nodes[root]["files"] == {"story_plan": "a plan"}
    assert n.tree.active_messages() == []
    n.startStory()  # the opening turn parents on the seeded root
    assert n.tree.path()[0] == root and n.files == {"story_plan": "a plan"}
    assert "a plan" in n.provider.messages[0]["content"][0]["text"]


def test_clear_messages_carries_context_into_a_fresh_tree(make_narrator):
    n, _ = make_narrator([tool("write_file", PC, then=say("a1"))])
    n.handleUserMessage({"message": "a"})
    n.clearMessages()
    assert n.tree.active_messages() == []
    assert n.files == {"pc": "HP: 10"}
    assert n.tree.file_state_at(n.tree.current_leaf) == {"pc": "HP: 10"}
    with open(n.story_history_path) as f:
        assert json.load(f)["nodes"][0]["files"] == {"pc": "HP: 10"}


def test_load_messages_restores_tree_and_context(make_narrator):
    n, _ = make_narrator([tool("write_file", PC, then=say("a1"))])
    n.handleUserMessage({"message": "a"})
    m, _ = make_narrator(story_id=n.story_id)
    assert m.loadMessages() is not None
    assert m.tree.path() == n.tree.path() and m.files == {"pc": "HP: 10"}
    assert m.provider.messages[1:] == m.tree.active_messages()


# --- failed turns ---------------------------------------------------------------------------

def test_failed_turn_is_discarded_and_the_message_handed_back(make_narrator):
    n, sock = make_narrator([tool("write_file", PC, then=say("a1")), fail("HTTP 502 from OpenRouter: overloaded")])
    n.handleUserMessage({"message": "a"})
    leaf, history_before = n.tree.current_leaf, open(n.story_history_path).read()
    sock.clear()
    assert n.handleUserMessage({"message": "I open the door"}) is False
    assert n.tree.current_leaf == leaf and len(n.tree.nodes) == 2
    assert n.files == {"pc": "HP: 10"}
    assert n.provider.messages[1:] == n.tree.active_messages()  # the failed user message is gone too
    assert open(n.story_history_path).read() == history_before
    failed = sock.last("turn_failed")
    assert failed["user_message"] == "I open the door" and failed["aborted"] is False
    assert "502" in failed["message"]
    names = sock.names()
    assert names.index("turn_end") < names.index("conversation_history") < names.index("turn_failed")


def test_a_turn_that_fails_after_a_tool_round_leaves_no_trace(make_narrator):
    n, sock = make_narrator([
        tool("write_file", PC, then=say("a1")),
        tool("write_file", {"file_name": "pc", "contents": "HP: 0"}, then=fail_after("You fall...")),
    ])
    n.handleUserMessage({"message": "a"})
    n.handleUserMessage({"message": "b"})
    assert n.files == {"pc": "HP: 10"}  # the tool's write was rolled back with the turn
    assert len(assistant_nodes(n.tree)) == 1
    # no tool call is left without its result, and the failed round's write is nowhere
    msgs = n.provider.messages
    call_ids = {c["id"] for m in msgs if m.get("tool_calls") for c in m["tool_calls"]}
    result_ids = {m["tool_call_id"] for m in msgs if m["role"] == "tool"}
    assert call_ids == result_ids and len(call_ids) == 1
    assert not any("HP: 0" in json.dumps(m) for m in msgs)
    assert sock.last("turn_end")["cost_stats"]["turn_count"] == 1


def test_failed_regenerate_keeps_the_branch_being_shown(make_narrator):
    n, sock = make_narrator([tool("write_file", PC, then=say("a1")), fail()])
    n.handleUserMessage({"message": "a"})
    a1 = n.tree.current_leaf
    n.regenerate_turn(a1)
    assert n.tree.current_leaf == a1 and n.tree.siblings(a1) == [a1]
    assert n.files == {"pc": "HP: 10"}
    assert n.provider.messages[1:] == n.tree.active_messages()
    assert sock.last("turn_failed")["user_message"] is None


def test_failed_edit_keeps_the_original_branch(make_narrator):
    n, sock = make_narrator([say("a1"), say("a2"), fail()])
    n.handleUserMessage({"message": "one"})
    n.handleUserMessage({"message": "two"})
    path = n.tree.path()
    n.edit_turn(path[2], "two, edited")
    assert n.tree.path() == path
    assert sock.last("turn_failed")["user_message"] == "two, edited"


def test_failed_opening_turn_offers_the_start_button_again(make_narrator):
    n, sock = make_narrator([fail()])
    n.startStory()
    assert n.tree.nodes == {} and n.tree.current_leaf is None
    assert not os.path.exists(n.story_history_path)
    assert "story_empty" in sock.names() and "turn_failed" in sock.names()


def test_stop_discards_the_turn_as_an_abort(make_narrator):
    n, sock = make_narrator([stopped_after("The door creaks")])
    assert n.handleUserMessage({"message": "I listen"}) is False
    assert n.tree.nodes == {}
    failed = sock.last("turn_failed")
    assert failed["aborted"] is True and failed["user_message"] == "I listen"
    assert sock.last("turn_end")["cost_stats"]["total_cost"] == 0


def test_turn_failed_is_raised_with_partial_messages_dropped():
    """The provider-level contract on its own: run() strips what the failed attempt added."""
    from conftest import FakeProvider
    from callbacks import CallbackHandler
    from openrouter import TurnFailed
    from model_tools import SYSTEM_TOOLBOXES
    files = {}
    p = FakeProvider(model_name="m", toolbox=SYSTEM_TOOLBOXES["hp"](files), callback_handler=CallbackHandler(), cache_mode="none")
    p.messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    p.scripts = [tool("write_file", PC, then=fail_after("partial"))]
    with pytest.raises(TurnFailed) as info:
        p.run()
    assert info.value.aborted is False
    assert [m["role"] for m in p.messages] == ["system", "user"]
    assert p.usage_history == [] and p.last_turn_cost == 0.0
    assert files == {"pc": "HP: 10"}  # the toolbox dict itself is the caller's to reset
