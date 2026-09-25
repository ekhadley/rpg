"""TurnTree: the branching history and the per-node story-context deltas."""
import json
from history import TurnTree


def user(text):
    return [{"role": "user", "content": text}]

def assistant(text):
    return [{"role": "assistant", "content": text}]


def linear_tree(n_turns=2):
    """u1 → a1 → u2 → a2 ...; returns (tree, [node ids in order])."""
    tree = TurnTree.empty()
    ids, parent = [], None
    for i in range(1, n_turns + 1):
        parent = tree.add_node(parent, "user", user(f"u{i}"))
        ids.append(parent)
        parent = tree.add_node(parent, "assistant", assistant(f"a{i}"))
        ids.append(parent)
    return tree, ids


def test_add_node_links_parent_and_moves_leaf():
    tree, (u1, a1, u2, a2) = linear_tree()
    assert tree.current_leaf == a2
    assert tree.nodes[u1]["parent"] is None
    assert tree.nodes[u1]["children"] == [a1]
    assert tree.nodes[a1]["children"] == [u2]
    assert tree.path() == [u1, a1, u2, a2]
    assert [m["content"] for m in tree.active_messages()] == ["u1", "a1", "u2", "a2"]
    assert [m["content"] for m in tree.messages_to(a1)] == ["u1", "a1"]


def test_add_node_stamps_messages_with_a_timestamp():
    tree = TurnTree.empty()
    nid = tree.add_node(None, "user", user("hi"))
    assert tree.nodes[nid]["messages"][0]["timestamp"]


def test_sibling_branches_and_branch_info():
    tree, (u1, a1, u2, a2) = linear_tree()
    a2b = tree.add_node(u2, "assistant", assistant("a2 retry"))  # a retry: a new sibling of a2
    assert tree.siblings(a2) == [a2, a2b]
    assert tree.branch_info(a2) == (0, 2)
    assert tree.branch_info(a2b) == (1, 2)
    assert tree.siblings(u1) == [u1]  # the root has no siblings
    assert tree.path() == [u1, a1, u2, a2b]


def test_set_leaf_descends_the_most_recent_child():
    tree, (u1, a1, u2, a2) = linear_tree()
    u3 = tree.add_node(a2, "user", user("u3"))
    a3 = tree.add_node(u3, "assistant", assistant("a3"))
    a2b = tree.add_node(u2, "assistant", assistant("a2 retry"))
    tree.set_leaf(a2)  # switching back to the first branch lands on its deepest, newest leaf
    assert tree.current_leaf == a3
    tree.set_leaf(a2b)
    assert tree.current_leaf == a2b


def test_file_state_replays_deltas_with_tombstones():
    tree = TurnTree.empty()
    u1 = tree.add_node(None, "user", user("u1"))
    a1 = tree.add_node(u1, "assistant", assistant("a1"), files={"pc": "v1", "story_plan": "plan"})
    u2 = tree.add_node(a1, "user", user("u2"))
    a2 = tree.add_node(u2, "assistant", assistant("a2"), files={"pc": "v2", "story_plan": None, "npc": "n"})
    assert tree.file_state_at(u1) == {}
    assert tree.file_state_at(a1) == {"pc": "v1", "story_plan": "plan"}
    assert tree.file_state_at(u2) == {"pc": "v1", "story_plan": "plan"}  # a user node changes nothing
    assert tree.file_state_at(a2) == {"pc": "v2", "npc": "n"}
    assert tree.file_state_at(None) == {}


def test_file_state_is_per_branch():
    tree = TurnTree.empty()
    u1 = tree.add_node(None, "user", user("u1"))
    tree.add_node(u1, "assistant", assistant("a"), files={"pc": "branch a"})
    a1b = tree.add_node(u1, "assistant", assistant("b"), files={"pc": "branch b", "extra": "x"})
    assert tree.file_state_at(tree.nodes[u1]["children"][0]) == {"pc": "branch a"}
    assert tree.file_state_at(a1b) == {"pc": "branch b", "extra": "x"}


def test_file_state_ignores_legacy_path_keys():
    tree = TurnTree.empty()
    u1 = tree.add_node(None, "user", user("u1"), files={"instructions/core.md": "old snapshot", "pc": "ok"})
    assert tree.file_state_at(u1) == {"pc": "ok"}


def test_apply_delta_matches_file_state_at():
    state = {"pc": "v1", "gone": "x"}
    TurnTree.apply_delta(state, {"pc": "v2", "gone": None, "new": "n", "a/b": "ignored"})
    assert state == {"pc": "v2", "new": "n"}


def test_serialize_round_trip():
    tree, ids = linear_tree()
    tree.nodes[ids[1]]["files"] = {"pc": "sheet"}
    data = json.loads(json.dumps(tree.serialize()))
    back = TurnTree.deserialize(data)
    assert back.current_leaf == tree.current_leaf
    assert back.nodes == tree.nodes
    assert back.file_state_at(back.current_leaf) == {"pc": "sheet"}


def test_migrate_from_flat_groups_tool_messages_into_the_assistant_node():
    flat = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]},
        {"role": "tool", "tool_call_id": "c1", "content": "7"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a2"},
    ]
    tree = TurnTree.migrate_from_flat(flat)
    path = tree.path()
    assert [tree.nodes[n]["role"] for n in path] == ["user", "assistant", "user", "assistant"]
    assert [m["role"] for m in tree.nodes[path[1]]["messages"]] == ["assistant", "tool", "assistant"]
    assert all(m["role"] != "system" for m in tree.active_messages())
    assert [m["content"] for m in tree.active_messages() if m["role"] == "user"] == ["u1", "u2"]
