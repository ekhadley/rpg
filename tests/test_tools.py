"""The story-context file tools: pure operations on the in-memory dict."""
import json
from model_tools import SYSTEM_TOOLBOXES, Tool, edit_story_file_tool_handler


def toolbox(files):
    return SYSTEM_TOOLBOXES["hp"](files)


def call(tb, name, **args):
    return tb.getToolResult(name, json.dumps(args))


def test_every_system_has_the_file_tools():
    for factory in SYSTEM_TOOLBOXES.values():
        names = {t["function"]["name"] for t in factory({}).getToolSchemas()}
        assert {"list_files", "read_file", "write_file", "edit_file", "append_file"} <= names


def test_edit_file_schema_requires_all_three_arguments():
    params = Tool(edit_story_file_tool_handler).schema["function"]["parameters"]
    assert params["required"] == ["file_name", "old_text", "new_text"]
    assert set(params["properties"]) == {"file_name", "old_text", "new_text"}


def test_edit_replaces_exactly_one_passage():
    files = {"pc": "# Sheet\nHP: 10\nMS: 12\n"}
    assert call(toolbox(files), "edit_file", file_name="pc", old_text="HP: 10", new_text="HP: 7") == "File edited successfully."
    assert files["pc"] == "# Sheet\nHP: 7\nMS: 12\n"


def test_edit_deletes_when_new_text_is_empty():
    files = {"pc": "a\nb\nc\n"}
    call(toolbox(files), "edit_file", file_name="pc", old_text="b\n", new_text="")
    assert files["pc"] == "a\nc\n"


def test_edit_refuses_ambiguous_missing_or_empty_matches():
    files = {"pc": "HP: 10\nMax HP: 10\n"}
    tb = toolbox(files)
    ambiguous = call(tb, "edit_file", file_name="pc", old_text="HP: 10", new_text="x")
    assert "2 times" in ambiguous and files["pc"] == "HP: 10\nMax HP: 10\n"
    missing = call(tb, "edit_file", file_name="pc", old_text="Gold", new_text="x")
    assert "not found" in missing
    empty = call(tb, "edit_file", file_name="pc", old_text="", new_text="x")
    assert "empty" in empty
    no_file = call(tb, "edit_file", file_name="npc", old_text="a", new_text="b")
    assert "no file named 'npc'" in no_file and "pc" in no_file
    assert files == {"pc": "HP: 10\nMax HP: 10\n"}


def test_write_append_and_list():
    files = {}
    tb = toolbox(files)
    assert call(tb, "write_file", file_name="pc", contents="v1") == "File saved successfully."
    assert call(tb, "write_file", file_name="pc", contents="v2") == "File edited successfully."
    assert call(tb, "append_file", file_name="pc", contents="more").startswith("Contents appended")
    assert call(tb, "append_file", file_name="story_summary", contents="day 1").startswith("File created")
    assert files == {"pc": "v2\nmore", "story_summary": "day 1"}
    assert json.loads(call(tb, "list_files").replace("'", '"')) == ["pc", "story_summary"]
    assert call(tb, "read_file", file_name="pc") == "v2\nmore"


def test_toolbox_shares_the_callers_dict():
    """The narrator resets the dict in place on navigation; the tools must see that, not a copy."""
    files = {"pc": "old"}
    tb = toolbox(files)
    files.clear()
    files["pc"] = "new"
    assert call(tb, "read_file", file_name="pc") == "new"
