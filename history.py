import uuid
from datetime import datetime

# A branching conversation tree. Each node is one user message OR one narrator
# response (the assistant/tool messages from a single tool-calling loop). Nodes
# alternate role down a path. The displayed/active path is reconstructed by
# walking `parent` links up from `current_leaf`. The system message is never
# stored here — it is prepended live when building model context.
class TurnTree:
    def __init__(self, nodes=None, current_leaf=None):
        self.nodes: dict[str, dict] = nodes or {}
        self.current_leaf: str | None = current_leaf

    @classmethod
    def empty(cls):
        return cls()

    def add_node(self, parent_id, role, messages, files=None) -> str:
        node_id = uuid.uuid4().hex[:8]
        now = datetime.now().isoformat()
        for m in messages:
            m.setdefault("timestamp", now)
        self.nodes[node_id] = {"id": node_id, "role": role, "parent": parent_id, "children": [], "messages": messages, "files": files or {}}
        if parent_id is not None:
            self.nodes[parent_id]["children"].append(node_id)
        self.current_leaf = node_id
        return node_id

    def path_to(self, node_id) -> list[str]:
        ids = []
        while node_id is not None:
            ids.append(node_id)
            node_id = self.nodes[node_id]["parent"]
        ids.reverse()
        return ids

    def path(self) -> list[str]:
        return self.path_to(self.current_leaf)

    def messages_to(self, node_id) -> list[dict]:
        msgs = []
        for nid in self.path_to(node_id):
            msgs.extend(self.nodes[nid]["messages"])
        return msgs

    def active_messages(self) -> list[dict]:
        return self.messages_to(self.current_leaf)

    # Apply one node's `files` delta to a story-context state in place. A delta value of None is a
    # tombstone (file deleted that turn). Keys are bare filenames; '/'-containing keys (legacy
    # instruction-file snapshots written by an older version) are ignored.
    @staticmethod
    def apply_delta(state: dict[str, str], delta: dict) -> None:
        for fname, contents in delta.items():
            if "/" in fname:
                continue
            if contents is None:
                state.pop(fname, None)
            else:
                state[fname] = contents

    # Reconstruct the full story-context file state at a node by replaying each node's `files`
    # delta down the path root→node. Returns {filename: full_contents}, empty for nodes with no records.
    def file_state_at(self, node_id) -> dict[str, str]:
        state: dict[str, str] = {}
        for nid in self.path_to(node_id):
            self.apply_delta(state, self.nodes[nid].get("files", {}))
        return state

    def set_leaf(self, node_id) -> None:
        # Descend via the most-recently-created child to a leaf.
        while self.nodes[node_id]["children"]:
            node_id = self.nodes[node_id]["children"][-1]
        self.current_leaf = node_id

    def siblings(self, node_id) -> list[str]:
        parent = self.nodes[node_id]["parent"]
        if parent is None:
            return [node_id]
        return self.nodes[parent]["children"]

    def branch_info(self, node_id) -> tuple[int, int]:
        sibs = self.siblings(node_id)
        return (sibs.index(node_id), len(sibs))

    def serialize(self) -> dict:
        return {"current_leaf": self.current_leaf, "nodes": list(self.nodes.values())}

    @classmethod
    def deserialize(cls, data) -> "TurnTree":
        nodes = {n["id"]: n for n in data.get("nodes", [])}
        return cls(nodes=nodes, current_leaf=data.get("current_leaf"))

    # Build a linear chain from a legacy flat message list (system dropped).
    # Every user message starts a turn; the assistant/tool messages that follow
    # become its assistant-node child.
    @classmethod
    def migrate_from_flat(cls, old_messages) -> "TurnTree":
        tree = cls()
        parent = None
        i, n = 0, len(old_messages)
        while i < n:
            msg = old_messages[i]
            role = msg.get("role")
            if role == "system":
                i += 1
                continue
            if role == "user":
                parent = tree.add_node(parent, "user", [msg])
                i += 1
            j = i
            asst = []
            while j < n and old_messages[j].get("role") in ("assistant", "tool"):
                asst.append(old_messages[j])
                j += 1
            if asst:
                parent = tree.add_node(parent, "assistant", asst)
                i = j
            elif role != "user":
                i += 1  # stray non-user/non-assistant message; skip
        return tree
