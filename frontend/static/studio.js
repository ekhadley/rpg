import { socket } from './state.js';
import { ensureLiveWrapper, appendReasoning, appendTool, appendDice, closeRows } from './reasoningRow.js';
import { appendNarration, renderStreamedNarration } from './chat.js';
import { showToast, showErrorPopup } from './ui.js';

// The prompt studio: pick a captured turn, regenerate it under two arms at once — an arm being a
// model plus a core-instruction version — and read the results side by side. Lanes stream through
// the same renderers the chat uses; a lane is just a wrapper element in a column.

const modeToggle = document.getElementById('mode-toggle');
const evalTurnList = document.getElementById('eval-turn-list');
const storyList = document.getElementById('story-list');
const studioWrapper = document.getElementById('studio-wrapper');
const chatWrapper = document.getElementById('chat-wrapper');
const studioColumns = document.getElementById('studio-columns');
const studioTurnName = document.getElementById('studio-turn-name');
const studioCost = document.getElementById('studio-cost');
const studioRunBtn = document.getElementById('studio-run-btn');
const verASel = document.getElementById('studio-ver-a');
const verBSel = document.getElementById('studio-ver-b');
const modelASel = document.getElementById('studio-model-a');
const modelBSel = document.getElementById('studio-model-b');
const nInput = document.getElementById('studio-n');
const cacheCheck = document.getElementById('studio-cache');
const historySel = document.getElementById('studio-history');
const warningEl = document.getElementById('studio-warning');

let selectedTurn = null;
let running = false;     // a run is in flight in the columns
let activeRunId = null;  // the run the columns show; live events for any other run are dropped
let laneWrappers = {};   // lane id -> the in-progress wrapper it streams into

// What the column area says while it is empty, before and after a turn is picked.
const EMPTY_NO_TURN = 'Select a captured turn from the sidebar.';
const EMPTY_TURN = 'Generate to regenerate this turn under both arms, or load a past run.';

function options(sel, values, selected) {
    sel.innerHTML = '';
    for (const v of values) {
        const o = document.createElement('option');
        o.value = v;
        o.textContent = v;
        if (v === selected) o.selected = true;
        sel.appendChild(o);
    }
}

function setMode(mode) {
    const studio = mode === 'studio';
    const filterBtn = document.getElementById('story-filter-btn');
    if (studio && document.querySelector('.sidebar-filter.open')) filterBtn.click();  // clears the story filter too
    filterBtn.style.display = studio ? 'none' : '';
    evalTurnList.style.display = studio ? '' : 'none';
    storyList.style.display = studio ? 'none' : '';
    studioWrapper.style.display = studio ? '' : 'none';
    chatWrapper.style.display = studio ? 'none' : '';
    modeToggle.querySelectorAll('.mode-option').forEach(b => b.classList.toggle('active', b.dataset.mode === mode));
    if (studio) {
        socket.emit('list_eval_turns');
        socket.emit('get_studio_options');
    }
}

function renderEvalTurns(turns) {
    evalTurnList.innerHTML = '';
    if (!turns.length) {
        const li = document.createElement('li');
        li.className = 'eval-turn-empty';
        li.textContent = 'No captured turns yet. Use the flask button on a turn.';
        evalTurnList.appendChild(li);
        return;
    }
    for (const t of turns) {
        const li = document.createElement('li');
        li.className = 'eval-turn-item' + (t.id === selectedTurn ? ' active' : '');
        li.dataset.id = t.id;
        li.innerHTML = '<div class="eval-turn-content">'
            + '<span class="eval-turn-name"></span>'
            + '<span class="eval-turn-meta">' + t.system + ' · ' + t.model.split('/').pop() + '</span>'
            + '</div>'
            + '<button class="eval-turn-delete" title="Delete captured turn"><i class="fas fa-trash"></i></button>';
        li.querySelector('.eval-turn-name').textContent = t.name;
        li.addEventListener('click', () => selectTurn(t));
        li.querySelector('.eval-turn-delete').addEventListener('click', (e) => {
            e.stopPropagation();
            if (selectedTurn === t.id) clearTurn();
            socket.emit('delete_eval_turn', { id: t.id });
        });
        evalTurnList.appendChild(li);
    }
}

// Caching costs a write premium that only pays off once a second lane reads it back.
function updateWarning() {
    const wasteful = cacheCheck.checked && parseInt(nInput.value, 10) < 2;
    warningEl.textContent = wasteful ? 'N=1 with caching on: you pay the cache write premium and nothing reads it.' : '';
}

// The control's caption already says "Past runs", so the placeholder only carries the count.
function renderRunHistory(runs) {
    historySel.innerHTML = '';
    const placeholder = document.createElement('option');
    placeholder.value = '';
    placeholder.textContent = runs.length ? runs.length + ' saved' : 'none yet';
    historySel.appendChild(placeholder);
    for (const r of runs) {
        const o = document.createElement('option');
        o.value = r.file;
        o.textContent = r.arms.map(a => a.version + ' · ' + a.model.split('/').pop()).join('  vs  ')
            + ' · n=' + r.n
            + ' · ' + new Date(r.started).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
            + ' · $' + r.cost.toFixed(3);
        historySel.appendChild(o);
    }
}

// Default an arm's model to the one the turn was captured from, adding it if it isn't in the list.
function pickModel(sel, model) {
    if (![...sel.options].some(o => o.value === model)) {
        const o = document.createElement('option');
        o.value = model;
        o.textContent = model;
        sel.appendChild(o);
    }
    sel.value = model;
}

// The Generate button and the past-runs picker follow two facts — is a turn selected, is a run in
// flight — and are set nowhere else, so every path that changes either fact agrees with the others.
function syncControls() {
    studioRunBtn.disabled = running || !selectedTurn;
    studioRunBtn.title = !selectedTurn ? 'Select a captured turn first' : running ? 'A run is in progress' : '';
    historySel.disabled = running;
}

function setRunning(r) {
    running = r;
    syncControls();
}

// Let go of the live run: its events are dropped from here on. The server still finishes and saves
// it, and it shows up under its turn's past runs.
function detachRun() {
    activeRunId = null;
    laneWrappers = {};
    setRunning(false);
}

// Empty the column area; `hint` is what it says while empty.
function clearColumns(hint) {
    detachRun();
    studioColumns.innerHTML = '';
    studioColumns.dataset.empty = hint;
    studioCost.textContent = '';
}

function clearTurn() {
    selectedTurn = null;
    studioTurnName.textContent = 'no turn selected';
    clearColumns(EMPTY_NO_TURN);
    renderRunHistory([]);
}

function selectTurn(turn) {
    if (turn.id === selectedTurn) return;  // a re-click must not wipe a live run
    selectedTurn = turn.id;
    studioTurnName.textContent = turn.name;
    clearColumns(EMPTY_TURN);
    socket.emit('list_studio_runs', { eval_id: turn.id });
    pickModel(modelASel, turn.model);
    pickModel(modelBSel, turn.model);
    if (turn.core && [...verASel.options].some(o => o.value === turn.core)) verASel.value = turn.core;  // arm A defaults to the core version the turn was played under
    evalTurnList.querySelectorAll('.eval-turn-item').forEach(li => li.classList.toggle('active', li.dataset.id === turn.id));
}

// Build the two columns and their lanes up front, so streaming just fills them in.
function buildColumns(cfg) {
    studioColumns.innerHTML = '';
    laneWrappers = {};
    cfg.arms.forEach((arm, a) => {
        const col = document.createElement('div');
        col.className = 'studio-column';
        const head = document.createElement('div');
        head.className = 'studio-column-header';
        head.textContent = arm.version;
        const model = document.createElement('span');
        model.className = 'studio-column-model';
        model.textContent = arm.model;
        head.appendChild(model);
        col.appendChild(head);
        for (let i = 0; i < cfg.n; i++) {
            const lane = 'ab'[a] + '-' + i;
            const laneEl = document.createElement('div');
            laneEl.className = 'studio-lane running';
            laneEl.innerHTML = '<div class="studio-lane-header">'
                + '<span>#' + (i + 1) + '</span>'
                + '<span class="studio-lane-spinner"><i class="fas fa-circle-notch fa-spin"></i></span>'
                + '<span class="studio-lane-status"></span>'
                + '</div>';
            const body = document.createElement('div');
            body.className = 'studio-lane-body';
            laneEl.appendChild(body);
            col.appendChild(laneEl);
            laneWrappers[lane] = ensureLiveWrapper(body);
        }
        studioColumns.appendChild(col);
    });
}

function laneEl(lane) {
    return laneWrappers[lane] ? laneWrappers[lane].closest('.studio-lane') : null;
}

// A lane that failed says so where its cost would go, with the reason on hover.
function finishLane(lane, cost, error) {
    if (!laneWrappers[lane]) return;
    closeRows(laneWrappers[lane]);
    const el = laneEl(lane);
    el.classList.remove('running');
    el.classList.toggle('failed', !!error);
    const status = el.querySelector('.studio-lane-status');
    status.textContent = error ? 'failed' : '$' + cost.toFixed(3);
    status.title = error || '';
}

// Replay a lane's saved events through the same renderers the live stream uses.
function replayLane(lane, events) {
    const w = laneWrappers[lane];
    if (!w) return;
    for (const e of events) {
        if (e.kind === 'think') appendReasoning(w, e.text);
        else if (e.kind === 'tool') {
            if (e.name === 'roll_dice' || e.name === 'dnd_dice') appendDice(w, [{ expr: JSON.stringify(e.inputs), result: e.result }]);
            else appendTool(w, e);
        } else {
            closeRows(w);
            renderStreamedNarration(appendNarration(w, ''), e.text);
        }
    }
}

export function initStudio() {
    modeToggle.querySelectorAll('.mode-option').forEach(btn => {
        btn.addEventListener('click', () => setMode(btn.dataset.mode));
    });

    cacheCheck.addEventListener('change', updateWarning);
    nInput.addEventListener('input', updateWarning);
    updateWarning();
    clearTurn();  // the no-turn state is owned here, not by the markup

    historySel.addEventListener('change', () => {
        if (historySel.value) socket.emit('load_studio_run', { eval_id: selectedTurn, file: historySel.value });
    });

    studioRunBtn.addEventListener('click', () => {
        if (!selectedTurn || running) return;
        socket.emit('studio_run', {
            eval_id: selectedTurn,
            arms: [{ model: modelASel.value, version: verASel.value },
                   { model: modelBSel.value, version: verBSel.value }],
            n: parseInt(nInput.value, 10),
            cache: cacheCheck.checked,
        });
        setRunning(true);  // before studio_run_started arrives, so a second click can't start a second run
        studioCost.textContent = '';
    });

    socket.on('eval_turns', renderEvalTurns);
    // The server sends the refreshed eval_turns list right after this; only the notice is ours.
    socket.on('eval_turn_captured', (d) => showToast('Captured "' + d.name + '" for the studio'));

    socket.on('studio_options', function(data) {
        if (!modelASel.options.length) options(modelASel, data.models, data.models[1]);
        if (!modelBSel.options.length) options(modelBSel, data.models, data.models[1]);
        const vers = data.versions;
        options(verASel, vers, data.default_version);
        options(verBSel, vers, vers[vers.length - 1]);
    });

    socket.on('models_updated', (data) => {
        options(modelASel, data.models, modelASel.value);
        options(modelBSel, data.models, modelBSel.value);
    });

    socket.on('studio_runs', function(d) {
        if (d.eval_id === selectedTurn) renderRunHistory(d.runs);
    });

    // The server could not start the run, so no studio_run_started / studio_run_end will follow.
    socket.on('studio_run_failed', function(d) {
        setRunning(false);
        showErrorPopup(d.message);
    });

    // Runs are broadcast, so this can be another tab's run, or one for a turn no longer selected.
    socket.on('studio_run_started', function(cfg) {
        if (cfg.eval_id !== selectedTurn) return;
        activeRunId = cfg.run_id;
        setRunning(true);
        historySel.value = '';
        buildColumns(cfg);
    });

    // A replayed run is inert: detaching keeps any late live events from landing in it.
    socket.on('studio_run_loaded', function(run) {
        detachRun();
        buildColumns(run);
        for (const [lane, l] of Object.entries(run.lanes)) {
            replayLane(lane, l.events);
            finishLane(lane, l.cost, l.error);
        }
        studioCost.textContent = 'run total $' + Object.values(run.lanes).reduce((s, l) => s + l.cost, 0).toFixed(3);
    });

    // Lane streaming — same renderers as the chat, pointed at this lane's wrapper.
    socket.on('studio_think', function(d) {
        if (d.run_id !== activeRunId) return;
        appendReasoning(laneWrappers[d.lane], d.text);
    });

    socket.on('studio_text', function(d) {
        if (d.run_id !== activeRunId) return;
        const w = laneWrappers[d.lane];
        closeRows(w);
        let el = [...w.querySelectorAll('.narrator-message')].pop();
        if (!el || el.dataset.done) el = appendNarration(w, '');
        el.dataset.raw = (el.dataset.raw || '') + d.text;
        renderStreamedNarration(el, el.dataset.raw);
    });

    socket.on('studio_tools', function(d) {
        if (d.run_id !== activeRunId) return;
        const w = laneWrappers[d.lane];
        [...w.querySelectorAll('.narrator-message')].forEach(el => el.dataset.done = '1');
        for (const t of d.tools) {
            if (t.name === 'roll_dice' || t.name === 'dnd_dice') appendDice(w, [{ expr: JSON.stringify(t.inputs), result: t.result }]);
            else appendTool(w, t);
        }
    });

    socket.on('studio_lane_end', function(d) {
        if (d.run_id !== activeRunId) return;
        finishLane(d.lane, d.cost, d.error);
    });

    socket.on('studio_run_end', function(d) {
        // A detached run's turn may be selected again by now: its past-runs list grew either way.
        if (d.eval_id === selectedTurn) socket.emit('list_studio_runs', { eval_id: selectedTurn });
        if (d.run_id !== activeRunId) return;
        setRunning(false);
        studioCost.textContent = 'run total $' + d.cost.toFixed(3) + (d.saved ? '' : ' · not saved: its turn was deleted');
    });
}
