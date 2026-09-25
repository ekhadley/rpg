# LM RPG

A text RPG engine where a language model acts as game master. You play a character; the LM runs the world, rolls dice, tracks state, and narrates outcomes. Responses are streamed in real time.

Built with Flask/SocketIO on the backend and vanilla JS on the frontend. LLM calls go through OpenRouter.

## Setup

Requires Python 3.13+. Uses [uv](https://github.com/astral-sh/uv) for dependency management.

```bash
uv sync
cp .env.example .env  # add your OPENROUTER_API_KEY
```

## Running

```bash
uv run python app.py
```

Runs on `http://localhost:5001`. Set `DEBUG=1` for verbose logging.

## Tests

```bash
uv run pytest
```

Covers the history tree, the narrator's turn operations (including what happens when a turn fails), story copying/archiving/forking, the file tools, and the provider's stream loop. Nothing touches the network.

## How It Works

The LM receives a system prompt assembled from the story's core instructions file and (optionally) story-specific context: a story plan, player character sheet, and running summary. It responds with narration and can make tool calls — rolling dice, reading/writing/editing named entries of story context — to manage game state across turns.

A turn that fails (a refused request, a dropped or hung connection, an error from the provider) or is stopped with the stop button is discarded whole: nothing is saved, the partial output is removed from the chat, and your message is put back in the input box.

Story context is not stored as files on disk. It's a keyed collection of text the model reads and writes through its tools; the authoritative copy lives inside the conversation history (see [Story context](#story-context-and-rollback) below). Conversations are stored as JSON in each story's directory and can be archived to start fresh while keeping history accessible.

## Game Systems

A game system is defined by:

- `instructions/systems/{name}.md` — the ruleset and world context the LM follows
- A toolbox factory registered in `model_tools.SYSTEM_TOOLBOXES`, adding the system's dice tool to the shared file tools

The core instructions (`instructions/core/{version}.md`) define system-agnostic GM behavior: how to handle player intent, when to roll dice, how to narrate. Each game system's instructions layer on top of that with specific mechanics and setting.

Each file in `instructions/core/` is one **core version**, named freely (`self_review.md`, `no_review.md`). A story picks its version when you create it and keeps it for life, so different stories can run different core instructions; the [prompt studio](#prompt-studio) runs two versions head to head. New stories default to the version chosen in the settings popup. Game system files (`instructions/systems/{name}.md`) are not versioned.

Game systems are divided into 'hard' systems and 'soft' systems. Hard systems are more like typical DnD (more dice rolling, characters with speficic stat sheets, predefined ability mechanics, etc), soft systems are more like a choose your own adventure type resolution system (outcomes determined more by holistic GM discretion and narrative considerations), although dice are still used where randomness is needed. The core instructions reference both types and how to run them.

Current systems include D&D 5e (hard), Harry Potter (hard), and Game of Thrones (soft).

## Stories

Each story lives in `stories/{name}/` and contains:

- `info.json` — metadata (which game system, model, etc.)
- `history.json` — the conversation **and** all story context (PC sheet, story plan, summary, NPC sheets), stored as per-turn deltas in the history tree
- `previous/` — archived conversation history

Stories are self-contained. You can create, copy, and delete them from the UI. Story context is named with bare identifiers (`pc`, `story_plan`, `story_summary`, `firstname_lastname` for NPCs) — there are no `.md` extensions.

## Frontend

Single-page app, no build step. Left sidebar for story management, center for the chat, right sidebar for viewing the current story context.

The narration uses a book-style layout (EB Garamond). Model thinking, tool calls, and dice rolls appear as small side buttons with hover popups rather than inline, keeping the narrative clean.

Seven themes: Discord, Gruvbox Dark, Leather & Gilt, Tavern, Parchment, Study, Green Lamp.

## Story context and rollback

Story context is a keyed collection of text (`pc`, `story_plan`, NPC sheets, etc.) that the model reads and writes through its tools. It is **never written to disk** — the working copy is an in-memory dict that the tools mutate during a turn, and the authoritative store is the conversation tree itself.

Because the conversation is a branching tree, each turn records the full contents of the entries it changed (a delta); a node's complete context state is a pure function of the active node, reconstructed by replaying deltas root→node. Navigating the tree (regenerate, edit, branch-switch, rollback) just resets the in-memory dict to that node's reconstructed state — no disk side effects. The changed-entries indicator shows up in the debug menu.

Each assistant turn also has a **fork** action (next to rerun/rewind): it spins off a new story whose history is the linear path up to that turn, carrying the story context reconstructed at that point. The source story is left untouched, so forking is a cheap "branch this timeline into its own story."

The model never sees a `.md` extension on context entries — names are bare identifiers throughout.

## Prompt studio

The toggle above the sidebar switches between **Play** and **Studio**. The studio answers one question: what does this exact turn look like under a different version of the instructions, or a different model?

Capture a turn from the chat (the fork action, aimed at `eval_stories/` instead of `stories/`) and it is frozen — the messages up to the player message being answered, plus the story context as of that point. In the studio you pick two **arms** — each a model plus a core version, so the two sides can differ in the prompt, the model, or both — and how many completions to generate per arm. Each completion gets its own column, streaming reasoning, narration, and tool calls live, side by side.

Lanes are fully independent: each builds its own model client over its own copy of the story context, so a run never touches the live game or any story. With caching on, one lane per arm goes first and the others wait for it to start producing output, by which point they read the shared prefix from cache instead of each paying to write it.

Captured turns live in `eval_stories/{id}/`, shaped like a story (`info.json`, `history.json`) plus a `runs/` folder. Finished runs are saved there and can be reloaded from the dropdown.

## TODO

- Cyberpunk RED hard system


- blind scoring on top of the prompt studio
    - lm-arena type loop: generate turns with prompt A and prompt B, pick the preferred one of a pair without knowing which is which, show win% at the end
    - currently the columns are labelled and unscored, so it's just eyeballing
    - maybe also for model benchamrking, but models are way easier to evaluate anyways so less value
    - now seems like it would just be better to make a rubric for a judge that takes all the responses blind and gives numerical ratings for each in the batch. display them sorted

- extremely extensive story plans just break things. need to limit size or have more complicated scaffolding
    - phandelver story plan (copied verbatim from the book, including lots of 'first time GMing' advice) is ~170k tokens on its own
    - my first thought is to condense the story plan and tailor it more for the form factor. no brainer either way
    - my second thought is that we should have the story plan give a ~30k token overview, with lots of references to blocked sections in a much more in depth guide.
        - This would require new tools for querying story files as well and/or extra delimiters in the detailed story plan for finding specified content chunks.

- the default for a lm-based DM is to do extra planning up front, load it all into the story plan itself
    - is this correct?
    - arguments for:
        - heavy planning documents are useful but not efficient for humans, but since AI cognitive labor is so cheap it makes sense to giga plan
        - it seems good to frontload the heavy thinking to the plan (if X has already happened do Y, otherwise Z), rather than making decisions in the moment
        - plan deviation is hard for models becuase becuase of mode seeking. if they've been following the plan as described for many steps, they are less likely to notice the points where it would be best to diverge
    - arguments against:
        - decisions made in the moment (rather than upfront planned paths) ahve the benefit of hindsight, info about current play

    - an alternative/hybrid approach: plan heavily, but prune and tweak the story plan doc itself as play develops
        - story plans have to change after play
            - having an active DM with a static story plan doesnt make much sense
            - just having a story summary doesn't change this
            - the DMing instructions currently don't have much in the way of 'evaluate how play is going, change hard facts as necessary'
            - this is a dangerous capability, but i suspect they would underuse it rather than overuse it

        - a specific kind of important info gained during play: during play, players learn about the world the DM has crafted. *But the DM is also learning about their players*
            - modelling the players is very important for planning and running a story. examples:
                - do you need 10 different clues for this one threead, or are your players really good at picking up on hints?
                - do players tend to get into trouble by running into danger headlong? how strong should the punishment for unpreparedness be given this tendency?
                - are your players apparently uninterested in a particular storyline or character? when should you drop them for something they find cool?

- experimental: explicit combat boards?
    - little interactive widgets that GM models can query or control and show the state of combat live.
        - for example, the model can have a tool for finding the distance between two points. so it can set up the geometry how it wants and then use that to find geometry for unspecified things like 'can i get to xyz with my movement speed'
    - could even be replayable, show combat events in a timeline, etc
    - image gen for backgrounds/character models?
    - could be pre-prepped

- framework for story plan generation?
    - this is of course ideally part of the main app, but I still have no idea what the right workflow is for creating good plans.
        - need to play with the hp system more I think to nail this down
        - it's really hard
    - part of the dream here is that it could be used fully autonomously create story plans without requiring me to audit the full thing so I can remain spoilerless but add still add direction
        - realistic?
        - I could see many a multiple-agents-with-divergent-personalities iterative debate type solution being pretty good
            - check out what cursor did for their web browser demo project thing

- note in `core.md` that replying out of narration in <md> is also appropriate when the user's requested action is impossible or doesn't make sense.

- harry potter ruleset update:
    - there really should be cantrips, i think
    - there needs to be a way to have a much larger variety of spells
        - for cooling a room, levitating an object, summoning an object, starting a small fire, repairing things
        - little utility things. there aren't enough existing utility spells and even if there were, none of these are useful to justify taking them over combat/stronger spells
    - not sure if they should still use MS. leaning yes, becuase magic should be used everywhere in this system, and MS is the only thing that makes it kind of costly

- dialogue writing is still downright BAD
    - maybe have the models do a few sample lines/passages in voice as each major character?
        - or exchanges, or full on test scenes in made up scenarios?
    - fable just makes every character sound like fable
    - so many kicks. both eye kicks and some other kind. rhetorical kicks?

