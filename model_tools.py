import random
import json
import inspect
from collections.abc import Callable

from utils import logger

def parse_handler_metadata(func: Callable) -> dict:
    doc = inspect.getdoc(func)
    if not doc:
        raise ValueError("Tool handlers must have a docstring.")
    lines = doc.strip().splitlines()
    first_line = lines[0]
    if ':' not in first_line:
        raise ValueError("First line of docstring must be in the format 'tool_name: description'")
    
    tool_name, description = map(str.strip, first_line.split(':', 1))

    arg_properties = {}
    for line in lines[1:]:
        line = line.strip()
        if not line:
            continue
        try:
            name_type, param_desc = line.split(":", 1)
            name, type_str = name_type.strip().split("(", 1)
            name = name.strip()
            type_str = type_str.strip(") ").lower()
        except ValueError:
            raise ValueError(f"Invalid argument docstring line: '{line}'")
        arg_properties[name] = {
            "type": type_str,
            "description": param_desc.strip()
        }
    return {"name": tool_name, "description": description, "arg_properties": arg_properties, "handler": func}


class Tool:
    def __init__(self, handler: Callable, default_kwargs: None|dict = None):
        handler_props = parse_handler_metadata(handler)
        self.name = handler_props['name']
        self.description = handler_props['description']
        self.handler = handler
        self.arg_properties = handler_props['arg_properties']
        sig = inspect.signature(handler)
        required = [name for name in self.arg_properties if sig.parameters[name].default is inspect.Parameter.empty]
        self.schema = {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": self.arg_properties,
                    "required": required,
                },
            }
        }
        self.kwargs = default_kwargs if default_kwargs is not None else {}

    def getResult(self, parameters: dict) -> str:
        try:
            tool_result = str(self.handler(**parameters, **self.kwargs))
            return tool_result
        except Exception as e:
            logger.error(f"error in tool {self.name}: {str(e)}")
            return f"error in tool {self.name}: {str(e)}"
    
    
class Toolbox:
    def __init__(self, handlers: list[Callable], default_kwargs: dict | None = None):
        default_kwargs = default_kwargs or {}
        self.tools = [Tool(handler, default_kwargs) for handler in handlers]
        self.kwargs = default_kwargs
        self.tool_map = {tool.name: tool for tool in self.tools}
    
    def getToolResult(self, tool_name: str, parameters: dict) -> str:
        logger.info(f"Tool: {tool_name}({parameters})")
        if isinstance(parameters, str):
            if parameters != "":
                try:
                    parameters = json.loads(parameters)
                except json.JSONDecodeError:
                    try:
                        parameters, _ = json.JSONDecoder().raw_decode(parameters)
                    except json.JSONDecodeError:
                        # If we can't parse it, it might be that the model sent a string that isn't JSON.
                        # We'll leave it as is and let the tool handler deal with it or fail.
                        # But actually the current code assumes parameters becomes a dict.
                        # If parsing fails completely, we should probably raise or return an error string.
                        raise
            else:
                parameters = {}
        if tool_name in self.tool_map:
            return self.tool_map[tool_name].getResult(parameters)
        else:
            logger.error(f"attempt to call nonexistent tool: {tool_name}")
            return f"error: Tool {tool_name} not found."

    def getToolSchemas(self) -> list[dict[str, str]]:
        return [tool.schema for tool in self.tools]

############## story tools ################

def list_story_files_tool_handler(**kwargs) -> list[str]:
    """list_files: Lists all files in the current story context.
    """
    return list(kwargs['files'].keys())

def read_story_file_tool_handler(file_name: str, **kwargs) -> str:
    """read_file: Read the contents of a file in the current story context.
    file_name (string): Name of the file to be read, with no file extension and no subfolders.
    """
    return kwargs['files'][file_name]

def write_story_file_tool_handler(file_name: str, contents: str, **kwargs) -> str:
    """write_file: Create a file in the current story context, or replace the whole contents of an existing one. The previous contents, if any, are deleted permanently. Use this for new files and full rewrites only: to change part of a file use edit_file, and to add to the end of one use append_file.
    file_name (string): Name of the file to save to, with no file extension and no subfolders.
    contents (string): The contents to write to the file. Do not include backticks around the contents to be saved.
    """
    file_name = file_name
    files = kwargs['files']
    exists = file_name in files
    files[file_name] = contents
    return "File edited successfully." if exists else "File saved successfully."

def append_story_file_tool_handler(file_name: str, contents: str, **kwargs) -> str:
    """append_file: Append contents to the end of an existing file in the current story context. If the file doesn't exist, it will be created.
    file_name (string): Name of the file to append to, with no file extension and no subfolders.
    contents (string): The contents to append to the file. Do not include backticks around the contents to be appended.
    """
    file_name = file_name
    files = kwargs['files']
    exists = file_name in files
    files[file_name] = files[file_name] + "\n" + contents if exists else contents
    return "Contents appended to file successfully." if exists else "File created and contents added successfully."

def edit_story_file_tool_handler(file_name: str, old_text: str, new_text: str, **kwargs) -> str:
    """edit_file: Replace one passage of an existing file in the current story context with new text, leaving the rest of the file untouched. Prefer this over write_file for any change smaller than a full rewrite (a changed stat, an inventory line, a paragraph of an NPC sheet). old_text must match the file exactly once; if it matches nowhere or more than once the file is left unchanged and the error says which.
    file_name (string): Name of the file to edit, with no file extension and no subfolders.
    old_text (string): The exact passage to replace, copied verbatim from the file (line breaks and whitespace included). Include enough surrounding text to make it unique.
    new_text (string): The text to put in its place. An empty string deletes the passage.
    """
    files = kwargs['files']
    if file_name not in files:
        raise ValueError(f"no file named '{file_name}'. Existing files: {', '.join(files) or 'none'}.")
    if old_text == "":
        raise ValueError("old_text is empty. Use append_file to add to a file, or write_file to replace it.")
    count = files[file_name].count(old_text)
    if count == 0:
        raise ValueError("old_text was not found in the file. Read the file and copy the passage exactly as it appears.")
    if count > 1:
        raise ValueError(f"old_text appears {count} times in the file. Include more of the surrounding text so it matches exactly once.")
    files[file_name] = files[file_name].replace(old_text, new_text, 1)
    return "File edited successfully."

def roll_dice_tool_handler(dice: str, **kwargs) -> int:
    """roll_dice: Roll a set of dice with the given number of sides and return the sum of the rolls.
    dice (string): A string describing the set of dice to roll, of the form 'dX' or 'XdY'.
    """
    dice = dice.lower()
    num, sides = dice.strip().split('d')
    if num == '':
        num = 1
    else:
        try:
            num = int(num)
        except ValueError:
            raise ValueError("Invalid number of dice.")
    try:
        sides = int(sides)
    except ValueError:
        raise ValueError("Invalid number of sides.")

    if num < 1:
        raise ValueError("Number of dice must be greater than 0.")
    if sides < 1:
        raise ValueError("Number of sides must be greater than 0.")

    rolls = [random.randint(1, sides) for _ in range(num)]
    return sum(rolls)

def dnd_dice_tool_handler(sides: int, count: int = 1, multiplier: int = 1, bonus: int = 0, advantage: bool = False, disadvantage: bool = False, desc: str = "", **kwargs) -> int:
    """dnd_dice: Roll dice for a D&D 5e check, attack, or damage. Rolls `count` dice of `sides` sides, multiplies the sum by `multiplier`, then adds `bonus`. Returns the final total.
    sides (integer): Number of sides on each die. E.g. 20 for a d20, 8 for a d8.
    count (integer): How many dice to roll. Defaults to 1.
    multiplier (integer): Multiplies the summed dice before the bonus is added (e.g. 2 for a critical hit). Defaults to 1.
    bonus (integer): Flat modifier added after multiplying (ability modifier + proficiency, etc.). Defaults to 0.
    advantage (boolean): If true, roll the whole dice set twice and keep the higher sum before applying multiplier and bonus. Defaults to false.
    disadvantage (boolean): If true, roll the whole dice set twice and keep the lower sum before applying multiplier and bonus. Defaults to false.
    desc (string): Short label for what the roll is for, e.g. 'Elara longsword attack'. Defaults to empty.
    """
    if sides < 1:
        raise ValueError("Number of sides must be greater than 0.")
    if count < 1:
        raise ValueError("Number of dice must be greater than 0.")
    if advantage and disadvantage:
        raise ValueError("Cannot roll with both advantage and disadvantage.")
    roll_set = lambda: sum(random.randint(1, sides) for _ in range(count))
    if advantage:
        total = max(roll_set(), roll_set())
    elif disadvantage:
        total = min(roll_set(), roll_set())
    else:
        total = roll_set()
    return total * multiplier + bonus


############## system toolboxes ################

BASE_HANDLERS = [
    list_story_files_tool_handler,
    read_story_file_tool_handler,
    write_story_file_tool_handler,
    edit_story_file_tool_handler,
    append_story_file_tool_handler,
]

def _make_toolbox(files: dict[str, str], extra_handlers: list[Callable] = []) -> Toolbox:
    return Toolbox(BASE_HANDLERS + extra_handlers, default_kwargs={"files": files})

def hp_toolbox(files: dict[str, str]) -> Toolbox:
    return _make_toolbox(files, [roll_dice_tool_handler])

def dnd5e_toolbox(files: dict[str, str]) -> Toolbox:
    return _make_toolbox(files, [dnd_dice_tool_handler])

def twd_toolbox(files: dict[str, str]) -> Toolbox:
    return _make_toolbox(files, [roll_dice_tool_handler])

SYSTEM_TOOLBOXES: dict[str, Callable] = {
    "hp": hp_toolbox,
    "dnd5e": dnd5e_toolbox,
    "twd": twd_toolbox,
}
