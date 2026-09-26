const stripProvider = (model) => model ? model.split('/').pop() : model;
// The sidebar has room for the model but not its family name, and its brand mark says the rest.
const shortModel = (model) => stripProvider(model).replace(/^claude-/, '');

// Date for a story's last activity as mm/dd/yy. Empty for stories with no timestamp.
const formatStoryDate = (ts) => {
    if (!ts) return '';
    const d = new Date(ts);
    return isNaN(d) ? '' : d.toLocaleDateString('en-US', { month: '2-digit', day: '2-digit', year: '2-digit' });
};

// Display name and icon class per game system - the one place the frontend maps a system to its look.
const SYSTEMS = {
    hp: { label: 'Harry Potter', icon: 'hp-logo-icon' },
    dnd5e: { label: 'D&D 5e', icon: 'fab fa-d-and-d' },
    twd: { label: 'The Walking Dead', icon: 'twd-logo-icon' },
};
const systemInfo = (name) => SYSTEMS[name] || { label: name || 'unknown', icon: 'fas fa-scroll' };

import {
    socket, storyList, chatHistory, chatHeader, welcomeWrapper, userInput,
    newStoryBtn, createStoryBtn, createStoryModal, createStoryModalCancel,
    createModelSelect, createSystemSelect, createCoreSelect,
    selectStoryConfigModal, selectStoryConfigModalClose, selectStoryConfigModalCancel,
    selectStoryConfigBtn, selectStoryModelSelect, selectStorySystemSelect,
    copyStoryModal, copyStoryModalCancel,
    copyStoryBtn, copyStoryNameInput, copyStoryModelSelect,
    copyStoryHint,
    currentStory, setCurrentStory,
    pendingStoryName, setPendingStoryName,
    fileList, newContextFileBtn, rightSidebar,
    fileViewerOverlay, fileViewerTitle, fileViewerBody, fileViewerToc, fileViewerClose,
    fileViewerModes, fileViewerMeta, fileViewerSave, fileViewerEditor,
} from './state.js';
import { showTypingIndicator, showConfirmPopup, positionPopupNear, pointerAnchor, getDefaultCore } from './ui.js';
import { brandIcon } from './brands.js';

// The two ways to copy a story. Both open the same modal; the mode picks its wording and is the
// only thing the server needs, so there is nothing to tick.
const COPY_MODES = {
    duplicate: {
        button: 'Duplicate', suffix: ' (copy)',
        hint: 'An exact copy: story context and the whole message history as they stand now.',
    },
    run: {
        button: 'Create Run', suffix: ' (run 2)',
        hint: 'The setup only: story context as it was before the first turn, with no messages. A fresh playthrough of the same starting point.',
    },
};
let copyMode = 'duplicate';

// Currently-open file in the viewer (for the raw/rendered toggle + editing).
let currentFile = null, currentContent = '', rawMode = false, dirty = false, editable = false;
// Entry names the system prompt pulls in by name (sent with story_locked).
let promptContextNames = new Set();
// Set to a filename to open it in raw/edit mode when its content arrives.
let openRawOnLoad = null;

// Byte size of a context entry, as the sidebar and the viewer header show it.
const formatSize = (n) => n < 1024 ? n + ' B' : n < 1048576 ? (n / 1024).toFixed(n < 10240 ? 1 : 0) + ' KB' : (n / 1048576).toFixed(1) + ' MB';
// An entry past this is big enough to be worth noticing in the system prompt, so its size reads amber.
const SIZE_WARN = 32 * 1024;

// name -> byte size for every entry in the current branch's story context.
let contextSizes = {};

function contextRow(name) {
    const li = document.createElement('li');
    li.className = 'file-item';
    li.dataset.filename = name;
    const size = contextSizes[name] || 0;
    const inPrompt = promptContextNames.has(name);
    li.innerHTML = (inPrompt ? '<span class="file-item-dot' + (size >= SIZE_WARN ? ' warn' : '') + '"></span>' : '')
        + '<span class="file-item-name"></span>'
        + '<span class="file-item-size' + (size >= SIZE_WARN ? ' warn' : '') + '">' + formatSize(size) + '</span>'
        + '<button class="file-item-menu" title="Entry options"><i class="fas fa-ellipsis-v"></i></button>';
    li.querySelector('.file-item-name').textContent = name;
    return li;
}

function groupCaption(text, count) {
    const li = document.createElement('li');
    li.className = 'file-group';
    li.innerHTML = '<span></span><span class="file-group-count">' + (count > 1 ? count : '') + '</span>';
    li.firstChild.textContent = text;
    return li;
}

// The sidebar list: entries the system prompt pulls in by name first, then everything the narrator
// has written (NPC sheets, mostly), then the row that creates a new entry.
function renderFileList() {
    if (!fileList) return;
    const names = Object.keys(contextSizes);
    const inPrompt = [...promptContextNames].filter(n => names.includes(n));
    const rest = names.filter(n => !promptContextNames.has(n)).sort();
    fileList.innerHTML = '';
    if (inPrompt.length) {
        fileList.appendChild(groupCaption('Core Files'));
        inPrompt.forEach(n => fileList.appendChild(contextRow(n)));
    }
    if (rest.length) {
        fileList.appendChild(groupCaption('Characters', rest.length));
        rest.forEach(n => fileList.appendChild(contextRow(n)));
    }
    const add = document.createElement('li');
    add.className = 'file-item file-item-add';
    add.innerHTML = '<i class="fas fa-plus"></i><span>New entry</span>';
    fileList.appendChild(add);
    markActiveFile();
    applyContextFilter();
}

// The open entry keeps an accent bar in the list while the viewer is up.
function markActiveFile() {
    if (!fileList) return;
    fileList.querySelectorAll('.file-item').forEach(el => el.classList.toggle('active', !!currentFile && el.dataset.filename === currentFile));
}

// Hide rows that don't match the filter box, and any group caption left with nothing under it.
function applyContextFilter() {
    const box = document.getElementById('context-filter');
    if (!fileList || !box) return;
    const q = box.value.trim().toLowerCase();
    let group = null, shown = 0;
    fileList.querySelectorAll('li').forEach(el => {
        if (el.classList.contains('file-group')) {
            if (group) group.classList.toggle('filtered-out', shown === 0);
            group = el; shown = 0;
            return;
        }
        if (!el.dataset.filename) return;  // the add row always stays
        const hit = !q || el.dataset.filename.toLowerCase().includes(q);
        el.classList.toggle('filtered-out', !hit);
        if (hit) shown++;
    });
    if (group) group.classList.toggle('filtered-out', shown === 0);
}

// Inline row for naming a new story-context entry. The hint beneath the input lights up when the
// typed name is one the system prompt pulls in by name, or is already taken.
function startNewContextFile() {
    if (!fileList || fileList.querySelector('.file-item-new')) return;
    const existing = new Set(Object.keys(contextSizes));
    const li = document.createElement('li');
    li.className = 'file-item file-item-new';
    const input = document.createElement('input');
    input.className = 'file-item-input';
    input.placeholder = 'entry name';
    const hint = document.createElement('div');
    hint.className = 'file-item-hint';
    li.append(input, hint);
    fileList.insertBefore(li, fileList.querySelector('.file-item-add'));
    input.focus();

    input.addEventListener('input', () => {
        const name = input.value.trim();
        const taken = existing.has(name), reserved = promptContextNames.has(name);
        li.classList.toggle('taken', taken);
        li.classList.toggle('reserved', reserved && !taken);
        hint.textContent = taken ? 'already exists' : reserved ? 'goes straight into the system prompt' : '';
    });
    input.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') input.blur();
        if (e.key !== 'Enter') return;
        const name = input.value.trim();
        if (!name || existing.has(name)) return;
        socket.emit('create_story_file', { filename: name });
        input.blur();
    });
    input.addEventListener('blur', () => li.remove());
}

// Rename in place: the row's name becomes an input, Enter commits, anything else drops it.
function startEntryRename(li) {
    const name = li.dataset.filename;
    const span = li.querySelector('.file-item-name');
    if (!span) return;
    const input = document.createElement('input');
    input.className = 'file-item-input';
    input.value = name;
    span.replaceWith(input);
    input.focus();
    input.select();
    input.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') input.blur();
        if (e.key !== 'Enter') return;
        const next = input.value.trim();
        input.blur();
        if (next && next !== name) socket.emit('rename_story_file', { filename: name, new_name: next });
    });
    input.addEventListener('blur', () => renderFileList());
}

function setHeaderIcon(system) {
    const icon = document.getElementById('current-story-icon');
    if (!icon) return;
    icon.className = 'story-header-icon ' + systemInfo(system).icon;
    icon.style.display = '';
}

// Switch the UI into the chat view for a selected story (title, icon, model, panels).
function enterStoryView({ storyId, displayName, model, system }) {
    setCurrentStory(storyId);
    renderStoryList();  // moves the sidebar's active row to this story
    const titleEl = document.getElementById('current-story-title');
    titleEl.textContent = displayName;
    titleEl.title = storyId;
    if (system) setHeaderIcon(system);
    document.getElementById('current-story-model').textContent = model ? stripProvider(model) : '';
    if (chatHistory) chatHistory.innerHTML = '';
    if (welcomeWrapper) welcomeWrapper.style.display = 'none';
    if (chatHeader) chatHeader.style.display = 'flex';
    if (rightSidebar) rightSidebar.classList.add('visible');
    showTypingIndicator();
}

// Select a story directly (when info.json exists)
export function selectStoryDirectly(storyId) {
    socket.emit('select_story', { "selected_story": storyId });
    const story = storyById(storyId);
    enterStoryView({ storyId, displayName: story.name, model: story.model, system: story.system });
}

// Every story the sidebar knows about, newest activity first (the order the server sends them in).
let stories = window.STORIES || [];
let storyFilter = '';
const storyById = (id) => stories.find(s => s.id === id);

// The story list, grouped by game system: a group per system, ordered by its most recent story,
// with the stories inside it in the same activity order. Filter text matches on the story name.
export function renderStoryList() {
    if (!storyList) return;
    storyList.innerHTML = '';
    const shown = stories.filter(s => s.name.toLowerCase().includes(storyFilter));
    for (const system of new Set(shown.map(s => s.system))) {
        const info = systemInfo(system);
        const group = shown.filter(s => s.system === system);
        const bar = document.createElement('li');
        bar.className = 'story-group';
        bar.dataset.system = system;
        bar.innerHTML = `<i class="story-group-icon ${info.icon}"></i><span class="story-group-name"></span><span class="story-group-count">${group.length}</span>`;
        bar.querySelector('.story-group-name').textContent = info.label;
        storyList.appendChild(bar);
        group.forEach(story => storyList.appendChild(storyRow(story)));
    }
}

// One story: its name, then the model it runs on. Hovering adds when it was last played (CSS).
function storyRow(story) {
    const li = document.createElement('li');
    li.className = 'story-item' + (story.id === currentStory ? ' active' : '');
    li.dataset.story = story.id;
    li.dataset.system = story.system;  // the active row's edge takes its system's colour
    const name = document.createElement('span');
    name.className = 'story-name';
    name.textContent = story.name;
    const tail = document.createElement('span');
    tail.className = 'story-tail';
    tail.innerHTML = brandIcon(story.model) + '<span class="story-model"></span><span class="story-date"></span>';
    tail.querySelector('.story-model').textContent = shortModel(story.model);
    tail.querySelector('.story-date').textContent = formatStoryDate(story.last_activity);
    const menuBtn = document.createElement('button');
    menuBtn.className = 'story-menu-btn';
    menuBtn.title = 'Story options';
    menuBtn.innerHTML = '<i class="fas fa-ellipsis-v"></i>';
    li.append(name, tail, menuBtn);
    return li;
}

// Add a new story to the sidebar list
export function addNewStory(story) {
    stories.unshift(story);  // newest activity, so it heads the list and its system's group
    renderStoryList();
}

// The Duplicate / New Run modal, filled in from its preset and the story it was opened on.
function openCopyStoryModal(story, mode, anchor) {
    if (!copyStoryModal) return;
    copyMode = mode;
    const preset = COPY_MODES[mode];
    setPendingStoryName(story.id);
    copyStoryHint.textContent = preset.hint;
    copyStoryBtn.textContent = preset.button;
    if (copyStoryNameInput) copyStoryNameInput.value = story.name + preset.suffix;
    if (copyStoryModelSelect && copyStoryModelSelect.querySelector('option[value="' + CSS.escape(story.model) + '"]')) {
        copyStoryModelSelect.value = story.model;
        copyStoryModelSelect.dispatchEvent(new Event('change', { bubbles: true }));
    }
    positionPopupNear(copyStoryModal, anchor);
}

function closeCopyStoryModal() {
    if (copyStoryModal) copyStoryModal.classList.remove('show');
}

// Turn a story-name span into an editable field for renaming
function startRename(nameSpan) {
    const original = nameSpan.textContent;
    nameSpan.contentEditable = 'true';
    nameSpan.classList.add('editing');
    nameSpan.focus();
    document.getSelection().selectAllChildren(nameSpan);

    let done = false;
    const finish = (commit) => {
        if (done) return;
        done = true;
        nameSpan.contentEditable = 'false';
        nameSpan.classList.remove('editing');
        const newName = nameSpan.textContent.trim();
        if (commit && newName && newName !== original) {
            const storyId = nameSpan.closest('.story-item').dataset.story;
            socket.emit('rename_story', { story_id: storyId, new_name: newName });
            nameSpan.textContent = newName; // optimistic; server confirms via story_renamed
        } else {
            nameSpan.textContent = original;
        }
    };

    nameSpan.addEventListener('keydown', function(e) {
        if (e.key === 'Enter') { e.preventDefault(); finish(true); }
        else if (e.key === 'Escape') { e.preventDefault(); finish(false); }
    });
    nameSpan.addEventListener('blur', () => finish(true));
}

const closeRowMenus = () => document.querySelectorAll('.story-context-menu.show').forEach(m => m.classList.remove('show'));

export function initStory() {
    renderStoryList();

    // New Story button opens create modal
    if (newStoryBtn) {
        newStoryBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            createCoreSelect.value = getDefaultCore();  // the settings default, overridable per story
            createCoreSelect.dispatchEvent(new Event('change'));
            if (createStoryModal) positionPopupNear(createStoryModal, newStoryBtn);
        });
    }

    // Story list: the menu button opens the shared row menu, anywhere else on a row opens the story.
    const storyMenu = document.getElementById('story-item-menu');
    let storyMenuTarget = null;
    if (storyList) {
        storyList.addEventListener('click', function(e) {
            if (e.target.closest('.story-name.editing')) return;
            const item = e.target.closest('.story-item');
            if (!item) return;
            const menuBtn = e.target.closest('.story-menu-btn');
            if (menuBtn) {
                e.stopPropagation();  // else the document listener that dismisses the popup sees this same click
                storyMenuTarget = item;
                positionPopupNear(storyMenu, menuBtn);
                return;
            }
            const story = storyById(item.dataset.story);
            if (story.model === 'unknown') {
                setPendingStoryName(story.id);
                if (selectStoryConfigModal) selectStoryConfigModal.classList.add('show');
            } else {
                selectStoryDirectly(story.id);
            }
        });
    }

    // Right-clicking a row opens the same menu its button does, at the cursor.
    if (storyList) {
        storyList.addEventListener('contextmenu', function(e) {
            const item = e.target.closest('.story-item');
            if (!item) return;
            e.preventDefault();
            closeRowMenus();
            storyMenuTarget = item;
            positionPopupNear(storyMenu, pointerAnchor(e));
        });
    }

    // Row menu: rename in place, the two copy presets, delete.
    if (storyMenu) {
        storyMenu.addEventListener('click', function(e) {
            const action = e.target.closest('.context-menu-item');
            if (!action || !storyMenuTarget) return;
            e.stopPropagation();
            const row = storyMenuTarget, story = storyById(row.dataset.story);
            storyMenu.classList.remove('show');
            if (action.dataset.action === 'rename-story') startRename(row.querySelector('.story-name'));
            if (action.dataset.action === 'copy-story') openCopyStoryModal(story, action.dataset.mode, row);
            if (action.dataset.action === 'delete-story') {
                showConfirmPopup('Delete "' + story.name + '"? It will be moved to the archive.',
                    () => socket.emit('delete_story', { story_id: story.id }), row);
            }
        });
        document.addEventListener('click', () => storyMenu.classList.remove('show'));
    }

    // The filter box lives collapsed above the list until there is something to look for.
    const storyFilterBox = document.getElementById('story-filter'), storyFilterBtn = document.getElementById('story-filter-btn');
    if (storyFilterBtn && storyFilterBox) {
        storyFilterBtn.addEventListener('click', function() {
            const open = storyFilterBox.parentElement.classList.toggle('open');
            if (open) storyFilterBox.focus();
            else { storyFilterBox.value = ''; storyFilter = ''; renderStoryList(); }
        });
        storyFilterBox.addEventListener('input', function() {
            storyFilter = storyFilterBox.value.trim().toLowerCase();
            renderStoryList();
        });
        storyFilterBox.addEventListener('keydown', (e) => { if (e.key === 'Escape') storyFilterBtn.click(); });
    }

    // Select story config modal
    if (selectStoryConfigBtn) {
        selectStoryConfigBtn.addEventListener('click', function() {
            if (!pendingStoryName) return;
            const modelName = selectStoryModelSelect.value;
            const systemName = selectStorySystemSelect.value;

            const story = storyById(pendingStoryName);
            story.model = modelName;
            story.system = systemName;

            if (selectStoryConfigModal) selectStoryConfigModal.classList.remove('show');

            socket.emit('select_story', {
                "selected_story": pendingStoryName,
                "model_name": modelName,
                "system_name": systemName
            });

            enterStoryView({ storyId: pendingStoryName, displayName: story.name, model: modelName, system: systemName });
            setPendingStoryName(null);
        });
    }

    if (selectStoryConfigModalClose) {
        selectStoryConfigModalClose.addEventListener('click', function() {
            if (selectStoryConfigModal) selectStoryConfigModal.classList.remove('show');
            setPendingStoryName(null);
        });
    }

    if (selectStoryConfigModalCancel) {
        selectStoryConfigModalCancel.addEventListener('click', function() {
            if (selectStoryConfigModal) selectStoryConfigModal.classList.remove('show');
            setPendingStoryName(null);
        });
    }

    if (selectStoryConfigModal) {
        selectStoryConfigModal.addEventListener('click', function(e) {
            if (e.target === selectStoryConfigModal) {
                selectStoryConfigModal.classList.remove('show');
                setPendingStoryName(null);
            }
        });
    }

    // Create story
    if (createStoryBtn) {
        createStoryBtn.addEventListener('click', function() {
            const nameInput = document.getElementById('new_story_name');
            const newStoryName = nameInput.value.trim();
            if (!newStoryName) {
                nameInput.classList.add('input-error');
                nameInput.addEventListener('input', () => nameInput.classList.remove('input-error'), { once: true });
                nameInput.focus();
                return;
            }
            const modelName = createModelSelect.value;
            const systemName = createSystemSelect.value;
            socket.emit('create_story', { story_name: newStoryName, model_name: modelName, system_name: systemName, core: createCoreSelect.value });
            if (createStoryModal) createStoryModal.classList.remove('show');
            const newStoryInput = document.getElementById('new_story_name');
            if (newStoryInput) newStoryInput.value = '';
        });
    }

    // Create modal cancel
    if (createStoryModalCancel) createStoryModalCancel.addEventListener('click', () => createStoryModal && createStoryModal.classList.remove('show'));
    // Close side popups on outside click (their open triggers stopPropagation, so the opening click never lands here)
    document.addEventListener('click', function(e) {
        for (const modal of [createStoryModal, copyStoryModal]) {
            if (modal && modal.classList.contains('show') && !modal.contains(e.target)) modal.classList.remove('show');
        }
    });

    // Copy story modal handlers
    if (copyStoryModalCancel) copyStoryModalCancel.addEventListener('click', closeCopyStoryModal);
    if (copyStoryBtn) {
        copyStoryBtn.addEventListener('click', function() {
            const newName = copyStoryNameInput ? copyStoryNameInput.value.trim() : '';
            if (!newName || !pendingStoryName) return;
            const modelName = copyStoryModelSelect.value;
            socket.emit('copy_story', {
                source_story: pendingStoryName,
                new_story_name: newName,
                model_name: modelName,
                mode: copyMode,
            });
            closeCopyStoryModal();
        });
    }

    // Socket listeners
    socket.on('story_created', function(data) { addNewStory(data); });

    socket.on('story_deleted', function(data) {
        const storyId = data.story_id;
        stories = stories.filter(s => s.id !== storyId);
        renderStoryList();
        if (currentStory === storyId) {
            setCurrentStory(null);
            if (chatHistory) chatHistory.innerHTML = '';
            if (welcomeWrapper) welcomeWrapper.style.display = '';
            if (chatHeader) chatHeader.style.display = 'none';
            if (rightSidebar) rightSidebar.classList.remove('visible');
            if (fileList) fileList.innerHTML = '';
        }
    });

    socket.on('story_copied', function(data) { addNewStory(data); });

    socket.on('story_forked', function(data) { addNewStory(data); selectStoryDirectly(data.id); });

    socket.on('story_renamed', function(data) {
        const { story_id, new_name } = data;
        storyById(story_id).name = new_name;
        renderStoryList();
        if (currentStory === story_id) {
            const titleEl = document.getElementById('current-story-title');
            if (titleEl) titleEl.textContent = new_name;
        }
    });

    socket.on('story_locked', function(data) {
        console.log('Story locked with data:', data);
        const story = currentStory ? storyById(currentStory) : null;
        if (story && data.model_name) {
            story.model = data.model_name;
            renderStoryList();
        }
        // Populate right sidebar file list
        promptContextNames = new Set(data.prompt_context_names || []);
        contextSizes = data.story_context || {};
        renderFileList();
        const badge = document.getElementById('context-story-badge');
        if (badge) badge.textContent = [data.system_name, data.core_version].filter(Boolean).join(' \u00b7 ');
    });

    // Any change to the story context (a save, a tool write, a rename) comes back as the whole listing.
    socket.on('story_files', function(data) {
        promptContextNames = new Set(data.prompt_context_names || []);
        contextSizes = data.story_context || {};
        renderFileList();
        if (currentFile && !(currentFile in contextSizes) && fileViewerOverlay) fileViewerOverlay.classList.remove('show');
        updateViewerMeta();
    });

    // File list click handler
    const rowMenu = document.getElementById('file-item-menu');
    let menuTarget = null;
    if (fileList) {
        fileList.addEventListener('click', function(e) {
            if (e.target.closest('.file-item-add')) { startNewContextFile(); return; }
            const item = e.target.closest('.file-item');
            if (!item || !item.dataset.filename) return;
            if (e.target.closest('.file-item-menu')) {
                e.stopPropagation();  // else the document listener that dismisses the popup sees this same click
                menuTarget = item;
                positionPopupNear(rowMenu, e.target.closest('.file-item-menu'));
                return;
            }
            socket.emit('get_story_file', { filename: item.dataset.filename });
        });
    }
    if (fileList) {
        fileList.addEventListener('contextmenu', function(e) {
            const item = e.target.closest('.file-item');
            if (!item || !item.dataset.filename) return;
            e.preventDefault();
            closeRowMenus();
            menuTarget = item;
            positionPopupNear(rowMenu, pointerAnchor(e));
        });
    }
    if (newContextFileBtn) newContextFileBtn.addEventListener('click', startNewContextFile);

    // Row menu: rename in place, duplicate, jump straight to the raw editor, delete from this branch.
    if (rowMenu) {
        rowMenu.addEventListener('click', function(e) {
            const action = e.target.closest('.context-menu-item');
            if (!action || !menuTarget) return;
            e.stopPropagation();
            const name = menuTarget.dataset.filename, row = menuTarget;
            rowMenu.classList.remove('show');
            if (action.dataset.action === 'rename') startEntryRename(row);
            if (action.dataset.action === 'duplicate') socket.emit('duplicate_story_file', { filename: name });
            if (action.dataset.action === 'raw') {
                openRawOnLoad = name;
                socket.emit('get_story_file', { filename: name });
            }
            if (action.dataset.action === 'delete') {
                showConfirmPopup('Delete "' + name + '" from this branch?', () => socket.emit('delete_story_file', { filename: name }), row);
            }
        });
        document.addEventListener('click', () => rowMenu.classList.remove('show'));
    }

    // The filter box lives collapsed behind the magnifier until there is something to look for.
    const filterBox = document.getElementById('context-filter'), filterBtn = document.getElementById('context-filter-btn');
    if (filterBtn && filterBox) {
        filterBtn.addEventListener('click', function() {
            const open = filterBox.classList.toggle('open');
            if (open) filterBox.focus();
            else { filterBox.value = ''; applyContextFilter(); }
        });
        filterBox.addEventListener('input', applyContextFilter);
        filterBox.addEventListener('keydown', (e) => { if (e.key === 'Escape') filterBtn.click(); });
    }

    socket.on('story_file_created', function(data) {
        openRawOnLoad = data.filename;  // a new entry is empty, so drop straight into the editor
        socket.emit('get_story_file', { filename: data.filename });
    });

    // System instructions click handler
    const sysInstrBtn = document.getElementById('system-instructions-btn');
    if (sysInstrBtn) {
        sysInstrBtn.addEventListener('click', () => socket.emit('get_system_instructions'));
    }

    // File viewer response
    socket.on('story_file_content', function(data) {
        if (!fileViewerOverlay) return;
        currentFile = data.filename;
        currentContent = data.content;
        editable = !!data.editable;
        fileViewerTitle.textContent = data.filename;
        fileViewerModes.style.display = editable ? '' : 'none';
        setDirty(false);
        setRawMode(openRawOnLoad === data.filename);
        openRawOnLoad = null;
        updateViewerMeta();
        markActiveFile();
        fileViewerOverlay.classList.add('show');
    });

    // Size of the open entry, beside its name in the viewer header.
    function updateViewerMeta() {
        if (!fileViewerMeta) return;
        const size = contextSizes[currentFile];
        fileViewerMeta.textContent = size === undefined ? '' : formatSize(size);
        fileViewerMeta.classList.toggle('warn', size >= SIZE_WARN);
    }

    // Save only offers itself once there is an edit to save; otherwise it reads as the file's state.
    function setDirty(d) {
        dirty = d;
        fileViewerSave.textContent = d ? 'Save' : 'Saved';
        fileViewerSave.classList.toggle('dirty', d);
    }

    // Raw mode shows the editable textarea (+ Save button); rendered mode shows the markdown + TOC.
    function setRawMode(raw) {
        rawMode = raw;
        fileViewerModes.querySelectorAll('.fv-mode').forEach(b => b.classList.toggle('active', (b.dataset.raw === '1') === raw));
        fileViewerSave.style.visibility = raw && editable ? '' : 'hidden';  // keeps its space, so the toggle beside it never moves
        if (raw) renderRaw(currentContent); else renderFile(currentContent);
    }

    function renderFile(content) {
        fileViewerEditor.style.display = 'none';
        fileViewerBody.style.display = '';
        fileViewerToc.style.display = '';
        fileViewerBody.innerHTML = marked.parse(content);

        // Build TOC from rendered headings
        fileViewerToc.innerHTML = '';
        // Add resize handle
        const tocResize = document.createElement('div');
        tocResize.className = 'file-viewer-toc-resize';
        fileViewerToc.appendChild(tocResize);

        const headings = fileViewerBody.querySelectorAll('h1, h2, h3, h4');
        const tocLinks = [];
        headings.forEach((h, i) => {
            const id = 'fv-heading-' + i;
            const level = parseInt(h.tagName[1]);
            h.id = id;
            const a = document.createElement('a');
            a.href = '#' + id;
            a.className = 'toc-' + h.tagName.toLowerCase();
            a.dataset.level = level;
            a.addEventListener('click', function(e) {
                e.preventDefault();
                h.scrollIntoView({ behavior: 'smooth', block: 'start' });
            });
            // Text goes in a span so chevron + text are separate
            const span = document.createElement('span');
            span.textContent = h.textContent;
            a.appendChild(span);
            fileViewerToc.appendChild(a);
            tocLinks.push(a);
        });

        // Add chevrons and collapse all by default
        tocLinks.forEach((link, i) => {
            const level = parseInt(link.dataset.level);
            const next = tocLinks[i + 1];
            if (next && parseInt(next.dataset.level) > level) {
                const chevron = document.createElement('i');
                chevron.className = 'fas fa-chevron-down toc-chevron';
                link.prepend(chevron);
                link.classList.add('collapsed');
                // Hide all children
                let sibling = link.nextElementSibling;
                while (sibling && parseInt(sibling.dataset.level) > level) {
                    sibling.classList.add('toc-hidden');
                    sibling = sibling.nextElementSibling;
                }
            }
        });
    }

    // One-time TOC wiring (renderFile recreates the TOC's children, so these delegate)
    if (fileViewerToc) {
        // Drag the resize handle to change TOC width
        let tocResizing = false;
        fileViewerToc.addEventListener('mousedown', function(e) {
            const handle = e.target.closest('.file-viewer-toc-resize');
            if (!handle) return;
            tocResizing = true;
            handle.classList.add('active');
            document.body.style.cursor = 'col-resize';
            document.body.style.userSelect = 'none';
            e.preventDefault();
        });
        document.addEventListener('mousemove', function(e) {
            if (!tocResizing) return;
            const tocRect = fileViewerToc.getBoundingClientRect();
            const newWidth = Math.max(100, Math.min(400, e.clientX - tocRect.left));
            fileViewerToc.style.width = newWidth + 'px';
        });
        document.addEventListener('mouseup', function() {
            if (!tocResizing) return;
            tocResizing = false;
            const handle = fileViewerToc.querySelector('.file-viewer-toc-resize');
            if (handle) handle.classList.remove('active');
            document.body.style.cursor = '';
            document.body.style.userSelect = '';
        });

        // Collapse/expand on chevron click
        fileViewerToc.addEventListener('click', function(e) {
            const chevron = e.target.closest('.toc-chevron');
            if (!chevron) return;
            e.preventDefault();
            e.stopPropagation();
            const link = chevron.closest('a');
            const level = parseInt(link.dataset.level);
            const collapsed = link.classList.toggle('collapsed');
            // Toggle all following deeper-level siblings
            let sibling = link.nextElementSibling;
            while (sibling && parseInt(sibling.dataset.level) > level) {
                sibling.classList.toggle('toc-hidden', collapsed);
                // If collapsing, also collapse any nested parents
                if (collapsed && sibling.querySelector('.toc-chevron')) {
                    sibling.classList.add('collapsed');
                }
                sibling = sibling.nextElementSibling;
            }
        });
    }

    function renderRaw(content) {
        fileViewerToc.style.display = 'none';
        fileViewerBody.style.display = 'none';
        fileViewerEditor.style.display = '';
        fileViewerEditor.value = content;
    }

    // Toggle between rendered markdown and a raw editable textarea
    if (fileViewerModes) {
        fileViewerModes.addEventListener('click', function(e) {
            const btn = e.target.closest('.fv-mode');
            if (!btn) return;
            if (rawMode) currentContent = fileViewerEditor.value;  // preview unsaved edits
            setRawMode(btn.dataset.raw === '1');
        });
    }
    if (fileViewerEditor) fileViewerEditor.addEventListener('input', () => { if (!dirty) setDirty(true); });

    // Save the edited raw content back into the story's history tree
    if (fileViewerSave) {
        fileViewerSave.addEventListener('click', function() {
            if (!currentFile) return;
            currentContent = fileViewerEditor.value;
            socket.emit('save_story_file', { filename: currentFile, content: currentContent });
        });
    }
    socket.on('story_file_saved', () => setDirty(false));

    // File viewer close
    if (fileViewerClose) {
        fileViewerClose.addEventListener('click', closeViewer);
    }
    function closeViewer() {
        fileViewerOverlay.classList.remove('show');
        currentFile = null;
        markActiveFile();
    }
    if (fileViewerOverlay) {
        fileViewerOverlay.addEventListener('click', function(e) {
            if (e.target === fileViewerOverlay) closeViewer();
        });
    }

    // Left sidebar resize
    const leftResizeHandle = document.getElementById('sidebar-resize');
    const leftSidebar = document.querySelector('.sidebar');
    if (leftResizeHandle && leftSidebar) {
        let isResizing = false;
        leftResizeHandle.addEventListener('mousedown', function(e) {
            isResizing = true;
            leftResizeHandle.classList.add('active');
            document.body.style.cursor = 'col-resize';
            document.body.style.userSelect = 'none';
            e.preventDefault();
        });
        document.addEventListener('mousemove', function(e) {
            if (!isResizing) return;
            const newWidth = Math.max(180, Math.min(500, e.clientX));
            leftSidebar.style.width = newWidth + 'px';
        });
        document.addEventListener('mouseup', function() {
            if (!isResizing) return;
            isResizing = false;
            leftResizeHandle.classList.remove('active');
            document.body.style.cursor = '';
            document.body.style.userSelect = '';
        });
    }

    // Right sidebar resize
    const resizeHandle = document.getElementById('right-sidebar-resize');
    if (resizeHandle && rightSidebar) {
        let isResizing = false;
        resizeHandle.addEventListener('mousedown', function(e) {
            isResizing = true;
            resizeHandle.classList.add('active');
            document.body.style.cursor = 'col-resize';
            document.body.style.userSelect = 'none';
            e.preventDefault();
        });
        document.addEventListener('mousemove', function(e) {
            if (!isResizing) return;
            const newWidth = Math.max(140, Math.min(500, window.innerWidth - e.clientX));
            document.documentElement.style.setProperty('--right-sidebar-width', newWidth + 'px');
        });
        document.addEventListener('mouseup', function() {
            if (!isResizing) return;
            isResizing = false;
            resizeHandle.classList.remove('active');
            document.body.style.cursor = '';
            document.body.style.userSelect = '';
        });
    }
}

// Expose closeCopyStoryModal for Escape key handler
export { closeCopyStoryModal };
