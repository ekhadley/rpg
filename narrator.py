import os
import json
from utils import getFullStoryInstruction, loadAllPreviousHistory, makeNewStoryDir, STORIES_ROOT_DIR
from model_tools import Toolbox, SYSTEM_TOOLBOXES
from callbacks import WebCallbackHandler
from openrouter import OpenRouterProvider, TurnFailed
from history import TurnTree
from flask_socketio import SocketIO
    
class Narrator:
    def __init__(self, model_name: str, story_id: str, system_name: str, core_version: str, socket: SocketIO, cache_mode: str = "1h"):
        self.model_name: str = model_name
        self.system_name: str = system_name
        self.core_version: str = core_version  # which instructions/core/*.md this story runs on, fixed at creation
        self.story_id: str = story_id
        self.files: dict[str, str] = {}  # in-memory story context (the single source of truth, reconstructed from the tree)
        self.tb: Toolbox = SYSTEM_TOOLBOXES[system_name](self.files)
        self.socket: SocketIO = socket
        self.system_prompt = getFullStoryInstruction(system_name, core_version, self.files)
        self.story_history_path = f"./stories/{story_id}/history.json"
        self.thinking_effort = "max"
        self.tree = TurnTree.empty()

        self.provider = OpenRouterProvider(
            model_name=model_name,
            thinking_effort=self.thinking_effort,
            cache_mode=cache_mode,
            toolbox=self.tb,
            callback_handler=WebCallbackHandler(socket)
        )

    def _systemMessage(self) -> dict:
        # Re-read the instruction files live (so edits take effect next turn); story context comes from self.files.
        self.system_prompt = getFullStoryInstruction(self.system_name, self.core_version, self.files)
        block = {"type": "text", "text": self.system_prompt}
        if self.provider.cacheControl():
            block["cache_control"] = self.provider.cacheControl()
        return {"role": "system", "content": [block]}

    def setCacheMode(self, mode: str) -> None:
        self.provider.cache_mode = mode
        self._rebuildContext()  # refresh the system message's cache_control

    def _rebuildContext(self):
        """Set the provider's flat message list to [system] + the active branch."""
        self.provider.messages = [self._systemMessage()] + self.tree.active_messages()
        self.provider.recompute_usage_from_messages(self.provider.messages)

    # === Per-turn file snapshots ===========================================
    # Story context (self.files) is a function of the active node. Each assistant turn records the
    # full contents of the files it changed (a delta); a node's full state is reconstructed by
    # replaying deltas root→node (TurnTree.file_state_at). self.files is the single source of truth:
    # tools mutate it in place during a turn, and it is reset from the tree on every navigation.
    # Shared instruction files (core/{version}.md, systems/{system}.md) stay real files, live-read in _systemMessage.

    def _setFiles(self, state: dict) -> None:
        """Reset the in-memory story context in place (the toolbox holds the same dict reference)."""
        self.files.clear()
        self.files.update(state)

    @staticmethod
    def _diffFiles(before: dict, after: dict) -> dict:
        """Files created/changed (full new contents) plus tombstones (None) for deletions."""
        delta = {f: after[f] for f in after if after.get(f) != before.get(f)}
        delta.update({f: None for f in before if f not in after})
        return delta

    def _restoreFor(self, base_id) -> dict:
        """Reset story context to base_id's state so an upcoming re-run reads the correct branch.
        Returns the reconstructed state (the diff baseline)."""
        state = self.tree.file_state_at(base_id)
        self._setFiles(state)
        return state

    def _materialize(self, node_id) -> None:
        """Make the world match a node: restore its story context, then rebuild context (the
        live-read in _systemMessage picks up the restored files). Used by pure-navigation ops
        (branch-switch, rollback) that don't re-run the model."""
        self._restoreFor(node_id)
        self._rebuildContext()

    def saveMessages(self):
        with open(self.story_history_path, "w+") as f:
            json.dump({"model_name": self.model_name, "system_name": self.system_name, **self.tree.serialize()}, f, indent=4)

    def loadMessages(self) -> TurnTree | None:
        if not os.path.exists(self.story_history_path):
            return None
        with open(self.story_history_path) as f:
            data = json.load(f)
        if "nodes" in data:
            self.tree = TurnTree.deserialize(data)
        elif "messages" in data:
            self.tree = TurnTree.migrate_from_flat(data["messages"])
        else:
            return None
        self._setFiles(self.tree.file_state_at(self.tree.current_leaf))
        self._rebuildContext()
        return self.tree

    def clearMessages(self):
        """Reset to a fresh tree after summarization. Carry the current story context forward as a
        hidden synthetic root (empty messages + files delta) so it survives into the new conversation.
        Written to disk immediately: until the next turn ends, that root is the only copy of the
        story context, and a reload/restart would otherwise reinitialize the narrator without it."""
        self.tree = TurnTree.empty()
        if self.files:
            self.tree.add_node(None, "user", [], files=dict(self.files))
        self._rebuildContext()
        self.saveMessages()

    def _commitFiles(self) -> None:
        """Persist a manual story-context change by folding the whole context into the current leaf's
        delta, so the change is local to this branch. A story with no turns yet gets a hidden
        synthetic root (empty messages) to carry the context, same as clearMessages."""
        if self.tree.current_leaf is None:
            self.tree.add_node(None, "user", [], files=dict(self.files))
        else:
            leaf = self.tree.current_leaf
            before = self.tree.file_state_at(self.tree.nodes[leaf]["parent"])
            self.tree.nodes[leaf]["files"] = self._diffFiles(before, self.files)
        self._rebuildContext()  # so the next turn's system prompt reflects the change
        self.saveMessages()

    def editFile(self, filename: str, content: str) -> None:
        """Write a story-context entry, creating it if it doesn't exist."""
        self.files[filename] = content
        self._commitFiles()

    def deleteFile(self, filename: str) -> None:
        del self.files[filename]
        self._commitFiles()

    def _emitHistory(self):
        self.socket.emit('conversation_history', self._transformTreeForFrontend())

    def stop(self) -> None:
        """Abort the turn in progress (called from the socket thread while a run blocks another)."""
        self.provider.stop()

    def _runModel(self, user_message: str | None = None) -> bool:
        """Run the provider over its current message list. A turn that fails is discarded whole:
        the provider has already dropped its partial messages, and nothing was added to the tree,
        so this resets the story context and the provider's messages to the active leaf, re-renders
        the chat from the tree (which removes the partial output and the unsaved user message), and
        sends `turn_failed` — carrying the user's message so the client can put it back in the input
        box. Returns whether the turn completed."""
        try:
            self.provider.run()
            return True
        except TurnFailed as e:
            self._materialize(self.tree.current_leaf)
            self._emitHistory()
            if not self.tree.active_messages() and not loadAllPreviousHistory(self.story_id):
                self.socket.emit('story_empty')  # a failed opening turn: offer the Start button again
            self.socket.emit('turn_failed', {"message": str(e), "user_message": user_message, "aborted": e.aborted})
            return False

    def _runIntoTurn(self, parent_id, before=None, user_message: str | None = None) -> str | None:
        """Run the model, then capture the [user, assistant...] messages just produced
        as a user node + an assistant-node child. The assistant node records the files that
        changed this turn (diff of the parent's reconstructed state vs self.files after the run).
        Returns the new assistant node id, or None if the turn failed (nothing is added then)."""
        if before is None:
            before = self.tree.file_state_at(parent_id)
        self._setFiles(before)  # working copy the tools mutate this turn
        # the user message is already the last entry; capture from there after the run
        user_start = len(self.provider.messages) - 1
        if not self._runModel(user_message):
            return None
        new = self.provider.messages[user_start:]
        delta = self._diffFiles(before, self.files)
        u = self.tree.add_node(parent_id, "user", new[:1])
        return self.tree.add_node(u, "assistant", new[1:], files=delta)

    def loadStory(self):
        # Load previous history for UI display (not sent to model)
        previous_messages = loadAllPreviousHistory(self.story_id)
        if previous_messages:
            frontend_previous = self._transformMessagesForFrontend(previous_messages)
            self.socket.emit('previous_history', frontend_previous)

        history = self.loadMessages()
        # A tree with no actual turns (e.g. a copy-without-history or post-summarization story whose
        # only node is the hidden synthetic root carrying the story context) is empty to the player.
        if history is not None and self.tree.active_messages():
            self.saveMessages()  # persist migration / live system prompt
            self._emitHistory()
        elif not previous_messages:
            self.socket.emit('story_empty')
        self.socket.emit('assistant_ready')
        self.socket.emit('turn_end', {"cost_stats": self.provider.getCostStats()})

    def startStory(self):
        """Kick off a brand-new story (triggered by the frontend's Start Story button)."""
        self._rebuildContext()  # seed [system]; the provider no longer holds a system message of its own
        self.provider.addUserMessage("System: start of story")
        # Parent on the existing leaf (the hidden synthetic root of a copied/summarized story carries
        # the seeded story context); None only for a genuinely fresh tree.
        if self._runIntoTurn(self.tree.current_leaf) is None:
            return
        self._rebuildContext()
        self.saveMessages()
        self._emitHistory()
        self.socket.emit('turn_end', {"cost_stats": self.provider.getCostStats()})
    
    def _transformMessagesForFrontend(self, messages: list[dict]) -> list[dict]:
        """Transform messages from OpenRouter format to frontend format"""
        frontend_messages = []
        
        # Special messages that should not be displayed to the user
        SKIP_MESSAGES = {"System: start of story"}
        
        for msg in messages:
            role = msg.get("role")
            content = msg.get("content", "")
            
            # Skip system messages
            if role == "system":
                continue
            
            # Skip special internal messages
            if content in SKIP_MESSAGES:
                continue
            
            # Handle user messages
            if role == "user":
                frontend_messages.append({
                    "type": "user",
                    "content": content,
                    "timestamp": msg.get("timestamp", "")
                })
            
            # Handle assistant messages
            elif role == "assistant":
                reasoning = msg.get("reasoning", "").strip()
                if reasoning != "":
                    frontend_messages.append({
                        "type": "thinking",
                        "content": reasoning,
                        "timestamp": msg.get("timestamp", "")
                    })
                
                # Check if there are tool calls
                if msg.get("tool_calls"):
                    for tool_call in msg.get("tool_calls", []):
                        frontend_messages.append({
                            "type": "tool_use",
                            "name": tool_call["function"]["name"],
                            "input": tool_call["function"]["arguments"],
                            "timestamp": msg.get("timestamp", "")
                        })
                
                # Add the text content if present
                if content and content.strip():
                    frontend_messages.append({
                        "type": "assistant",
                        "content": content,
                        "timestamp": msg.get("timestamp", "")
                    })
            
            # Handle tool result messages
            elif role == "tool":
                frontend_messages.append({
                    "type": "tool_result",
                    "content": content,
                    "timestamp": msg.get("timestamp", "")
                })
        
        return frontend_messages

    def _transformTreeForFrontend(self) -> list[dict]:
        """Active-path nodes, each with branch info and per-node frontend messages."""
        nodes = []
        for nid in self.tree.path():
            node = self.tree.nodes[nid]
            idx, count = self.tree.branch_info(nid)
            nodes.append({
                "id": nid,
                "role": node["role"],
                "idx": idx,
                "count": count,
                "messages": self._transformMessagesForFrontend(node["messages"]),
            })
        return nodes

    def _leafCost(self) -> float:
        node = self.tree.nodes.get(self.tree.current_leaf)
        if not node:
            return 0.0
        return sum(m.get("usage", {}).get("cost", 0.0) for m in node["messages"])

    def handleUserMessage(self, data: dict[str, str]) -> bool:
        """Answer a user message as a new turn on the active leaf. Returns whether the turn completed."""
        parent = self.tree.current_leaf
        self._rebuildContext()  # rebuild [system] + active branch before appending the new user turn
        self.provider.addUserMessage(data['message'])
        if self._runIntoTurn(parent, user_message=data['message']) is None:
            return False
        self._rebuildContext()
        self.saveMessages()
        self._emitHistory()
        return True

    def regenerate_turn(self, node_id: str) -> None:
        node = self.tree.nodes.get(node_id)
        if not node or node["role"] != "assistant":
            return
        parent = node["parent"]  # the user node this responds to
        before = self._restoreFor(parent)  # reset story context so the re-run reads the right branch
        self.provider.messages = [self._systemMessage()] + self.tree.messages_to(parent)
        self.provider.recompute_usage_from_messages(self.provider.messages)
        n = len(self.provider.messages)
        if not self._runModel():
            return  # the leaf never moved, so the failure path restored the branch being shown
        delta = self._diffFiles(before, self.files)
        self.tree.add_node(parent, "assistant", self.provider.messages[n:], files=delta)
        self._rebuildContext()
        self.saveMessages()
        self._emitHistory()

    def edit_turn(self, node_id: str, new_content: str) -> None:
        node = self.tree.nodes.get(node_id)
        if not node or node["role"] != "user":
            return
        grandparent = node["parent"]  # the assistant node before it (or None)
        before = self._restoreFor(grandparent)  # reset story context so the re-run reads the right branch
        self.provider.messages = [self._systemMessage()] + self.tree.messages_to(grandparent)
        self.provider.addUserMessage(new_content)
        self.provider.recompute_usage_from_messages(self.provider.messages)
        if self._runIntoTurn(grandparent, before=before, user_message=new_content) is None:
            return
        self._rebuildContext()
        self.saveMessages()
        self._emitHistory()

    def _finishNav(self) -> None:
        """Shared tail for pure-navigation ops (branch-switch, rollback): materialize the active
        leaf's files, refresh cost, persist, and emit the updated history + turn end."""
        self._materialize(self.tree.current_leaf)
        self.provider.last_turn_cost = self._leafCost()
        self.saveMessages()
        self._emitHistory()
        self.socket.emit('turn_end', {"cost_stats": self.provider.getCostStats()})

    def switch_branch(self, node_id: str, direction: int) -> None:
        sibs = self.tree.siblings(node_id)
        if node_id not in sibs or len(sibs) < 2:
            return
        self.tree.set_leaf(sibs[(sibs.index(node_id) + direction) % len(sibs)])
        self._finishNav()

    def rollback_to(self, node_id: str) -> None:
        """Jump the active leaf back to an earlier turn and restore its file state to disk.
        Descendant branches are preserved; a later message branches from this node."""
        node = self.tree.nodes.get(node_id)
        if not node or node["role"] != "assistant":
            return
        self.tree.current_leaf = node_id
        self._finishNav()

    def fork_to(self, node_id: str, new_name: str, root: str = STORIES_ROOT_DIR) -> str | None:
        """Fork into a fresh story whose history is the linear path root→node_id. The story context
        rides along inside the copied node deltas. The source story is untouched. Returns the new id.
        `root` is EVAL_STORIES_DIR when capturing a turn into the prompt studio."""
        node = self.tree.nodes.get(node_id)
        if not node or node["role"] != "assistant" or not new_name:
            return None
        new_id = makeNewStoryDir(new_name, self.system_name, self.core_version, self.model_name, root=root)
        path = self.tree.path_to(node_id)
        nodes = []  # copy each node on the path, trimming children to just the next path node
        for i, nid in enumerate(path):
            n = dict(self.tree.nodes[nid])
            n["children"] = [path[i + 1]] if i + 1 < len(path) else []
            nodes.append(n)
        with open(f"./{root}/{new_id}/history.json", "w") as f:
            json.dump({"model_name": self.model_name, "system_name": self.system_name, "current_leaf": node_id, "nodes": nodes}, f, indent=4)
        return new_id

    def __str__(self) -> str:
        return f"Narrator(model_name={self.model_name}, system={self.system_name}, tools=Toolbox[{len(self.tb.tools)}])"
