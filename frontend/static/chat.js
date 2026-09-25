import {
    socket, chatHistory, userInput, messageForm,
    conversationHistory, setConversationHistory,
    currentNarratorMessageElement, setCurrentNarratorMessageElement,
    accumulatedContent, setAccumulatedContent,
    setIsToolCallInProgress, setIsThinkingInProgress,
} from './state.js';
import { scrollToBottom, scrollToBottomIfStuck, showTypingIndicator, hideTypingIndicator, updateCostDisplay, showErrorPopup, showToast } from './ui.js';
import {
    ensureLiveWrapper, ensureRow, appendReasoning, appendTool, appendDice, closeRows,
} from './reasoningRow.js';
import { addRetryButton, addEditButton, addRollbackButton, addForkButton, addCaptureButton } from './messageActions.js';

// Render an out-of-narration <md> block as its own boxed markdown
function mdBox(inner) {
    try { return '<div class="md-block">' + marked.parse(inner.trim()) + '</div>'; }
    catch (e) { return '<div class="md-block">' + inner + '</div>'; }
}

// Parse narration tags and markdown
function processNarration(content) {
    let s = content.trim();
    if (s.includes('<narration>')) {
        return s
            .replace(/<md>([\s\S]*?)<\/md>/g, (_, inner) => mdBox(inner))
            .replace(/<narration>([\s\S]*?)<\/narration>/g, (_, n) => {
                try { return '<div class="book-narration">' + marked.parse(n) + '</div>'; }
                catch (e) { return '<div class="book-narration">' + n + '</div>'; }
            }).trim();
    }
    // Narration by default: pull out <md> blocks, markdown-render the rest as-is
    let html = '', last = 0, m;
    const re = /<md>([\s\S]*?)<\/md>/g;
    try {
        while ((m = re.exec(s)) !== null) {
            const before = s.slice(last, m.index).trim();
            if (before) html += marked.parse(before);
            html += mdBox(m[1]);
            last = re.lastIndex;
        }
        const after = s.slice(last).trim();
        if (after) html += marked.parse(after);
        return html.trim();
    } catch (e) { return s; }
}

// Add user message to chat
function addUserMessage(message, disableInput = true) {
    if (!chatHistory) return null;
    const messageContainer = document.createElement('div');
    messageContainer.className = 'message';
    messageContainer.style.display = 'flex';
    messageContainer.style.justifyContent = 'flex-end';
    const messageElement = document.createElement('div');
    messageElement.className = 'user-message';
    messageElement.textContent = message;
    messageContainer.appendChild(messageElement);
    chatHistory.appendChild(messageContainer);
    if (disableInput && userInput) userInput.disabled = true;
    return messageContainer;
}

// Append a narration block to a turn wrapper and return it
// Render narration that is still streaming: the model's tags arrive unterminated, so close them
// off before rendering or the markdown pipeline sees a half-open block.
export function renderStreamedNarration(el, content) {
    let s = content.trim();
    if (s.includes('<narration>') && !s.includes('</narration>')) s += '</narration>';
    if (s.lastIndexOf('<md>') > s.lastIndexOf('</md>')) s += '</md>';
    el.innerHTML = processNarration(s);
}

export function appendNarration(wrapper, content) {
    const el = document.createElement('div');
    el.className = 'message narrator-message';
    el.innerHTML = processNarration(content);
    wrapper.appendChild(el);
    return el;
}

// Close the currently streaming narration so the next reasoning/tool starts a fresh row
function finalizeLiveNarration() {
    if (!currentNarratorMessageElement) return;
    if (accumulatedContent) {
        const hist = [...conversationHistory];
        hist.push({ role: 'assistant', content: accumulatedContent, timestamp: new Date().toISOString() });
        setConversationHistory(hist);
    }
    setCurrentNarratorMessageElement(null);
    setAccumulatedContent('');
}

// Render an assistant turn's messages into a wrapper as ordered reasoning rows + narration blocks
function renderAssistantBlocks(wrapper, messages) {
    const pending = [];
    messages.forEach(function(message) {
        if (message.type === 'thinking') {
            appendReasoning(wrapper, message.content);
        } else if (message.type === 'tool_use') {
            pending.push({ name: message.name, input: message.input });
        } else if (message.type === 'tool_result') {
            const tu = pending.shift();
            if (!tu) return;
            let inputs = tu.input;
            if (typeof inputs === 'string') { try { inputs = JSON.parse(inputs); } catch (e) { inputs = {}; } }
            if (tu.name === 'roll_dice') {
                appendDice(wrapper, [{ expr: inputs.dice || inputs.expression || '?', result: message.content }]);
            } else {
                appendTool(wrapper, { name: tu.name, inputs, result: message.content });
            }
        } else if (message.type === 'assistant') {
            closeRows(wrapper);
            appendNarration(wrapper, message.content);
        }
    });
    closeRows(wrapper);
}

// Anchor retry/branch controls on a wrapper's last narration (creating an empty one if tool-only)
function finalizeWrapper(wrapper, node) {
    let narr = [...wrapper.querySelectorAll('.narrator-message')].pop();
    if (!narr) narr = appendNarration(wrapper, '');
    if (node) {
        wrapper.dataset.turnId = node.id;
        addRetryButton(narr);
        addRollbackButton(narr);
        addForkButton(narr);
        addCaptureButton(narr);
        if (node.count > 1) attachBranchSwitch(narr, node);
    }
    return narr;
}

// Render a flat list of history messages (archived "previous" conversations), grouping each
// run of assistant-side messages between user messages into one turn wrapper.
function renderHistoryMessages(messages, addRetry = false) {
    let i = 0;
    while (i < messages.length) {
        const m = messages[i];
        if (m.type === 'user') {
            const container = addUserMessage(m.content, false);
            if (addRetry && container) addEditButton(container);
            i++;
        } else {
            const group = [];
            while (i < messages.length && messages[i].type !== 'user') group.push(messages[i++]);
            const wrapper = document.createElement('div');
            wrapper.className = 'assistant-turn-wrapper';
            chatHistory.appendChild(wrapper);
            renderAssistantBlocks(wrapper, group);
            finalizeWrapper(wrapper, null);
        }
    }
}

// Switch-arrow control for a branched (sibling-having) node
function attachBranchSwitch(anchorEl, node) {
    if (!anchorEl) return;
    const ctrl = document.createElement('div');
    ctrl.className = 'branch-switch';
    const prev = document.createElement('button');
    prev.className = 'branch-arrow';
    prev.innerHTML = '<i class="fas fa-chevron-left"></i>';
    prev.addEventListener('click', () => doSwitch(node.id, -1));
    const label = document.createElement('span');
    label.className = 'branch-label';
    label.textContent = (node.idx + 1) + '/' + node.count;
    const next = document.createElement('button');
    next.className = 'branch-arrow';
    next.innerHTML = '<i class="fas fa-chevron-right"></i>';
    next.addEventListener('click', () => doSwitch(node.id, 1));
    ctrl.appendChild(prev);
    ctrl.appendChild(label);
    ctrl.appendChild(next);
    anchorEl.appendChild(ctrl);
}

function doSwitch(id, dir) {
    if (userInput && userInput.disabled) return;
    socket.emit('switch_branch', { turn_id: id, dir: dir });
}

// Render one tree node (a user message or a narrator response) with its arrows
function renderNode(node) {
    if (node.role === 'user') {
        const um = node.messages.find(m => m.type === 'user');
        if (!um) return;  // hidden synthetic turn
        const container = addUserMessage(um.content, false);
        if (!container) return;
        container.dataset.turnId = node.id;
        addEditButton(container);
        if (node.count > 1) attachBranchSwitch(container.querySelector('.user-message'), node);
    } else {
        const wrapper = document.createElement('div');
        wrapper.className = 'assistant-turn-wrapper';
        chatHistory.appendChild(wrapper);
        renderAssistantBlocks(wrapper, node.messages);
        finalizeWrapper(wrapper, node);
    }
}

// Replace the current (live) section with a fresh render of the active branch
function renderNodes(nodes) {
    const currentSep = chatHistory.querySelector('.history-separator.current');
    if (currentSep) {
        while (currentSep.nextSibling) currentSep.nextSibling.remove();
    } else {
        chatHistory.innerHTML = '';
    }
    nodes.forEach(renderNode);
}

// Wire up all socket listeners and form handler
export function initChat() {
    socket.on('error', (data) => showErrorPopup(data.message));

    // The stop button is only visible while a turn is running; the server discards the turn and
    // re-sends the history, which drops the partial output from the chat.
    const stopButton = document.getElementById('stop-button');
    if (stopButton) stopButton.addEventListener('click', () => socket.emit('stop_turn'));

    // A turn that failed or was stopped: nothing was saved (the history re-render that precedes
    // this event already removed the partial turn), so put the message back to resend.
    socket.on('turn_failed', function(data) {
        hideTypingIndicator();
        if (data.aborted) showToast('Turn stopped');
        else showErrorPopup(data.message || 'The turn failed.');
        if (data.user_message && userInput && !userInput.value) userInput.value = data.user_message;
    });

    // Message form submit
    if (messageForm) {
        messageForm.addEventListener('submit', function(e) {
            e.preventDefault();
            const message = userInput.value.trim();
            if (message) {
                const startPrompt = chatHistory.querySelector('.start-story-prompt');
                if (startPrompt) startPrompt.remove();
                socket.emit('user_message', { message });
                userInput.value = '';
                const msgContainer = addUserMessage(message);
                if (msgContainer) addEditButton(msgContainer);
                const hist = [...conversationHistory];
                hist.push({ role: 'user', content: message, timestamp: new Date().toISOString() });
                setConversationHistory(hist);
                scrollToBottom();
            }
        });
    }

    socket.on('previous_history', function(history) {
        if (!history || history.length === 0) return;
        const separator = document.createElement('div');
        separator.className = 'history-separator';
        separator.innerHTML = '<span>Previous Conversations</span>';
        chatHistory.appendChild(separator);
        renderHistoryMessages(history);
        const currentSeparator = document.createElement('div');
        currentSeparator.className = 'history-separator current';
        chatHistory.appendChild(currentSeparator);
    });

    socket.on('conversation_history', function(nodes) {
        renderNodes(nodes);
        socket.emit('list_story_files');  // navigation rebuilds the story context from the tree
        const hist = [];
        nodes.forEach(function(node) {
            node.messages.forEach(function(m) {
                if (m.type === 'user') hist.push({ role: 'user', content: m.content });
                else if (m.type === 'assistant') hist.push({ role: 'assistant', content: m.content });
            });
        });
        setConversationHistory(hist);
        scrollToBottomIfStuck();
    });

    socket.on('assistant_ready', function() {
        hideTypingIndicator();
    });

    socket.on('story_empty', function() {
        const prompt = document.createElement('div');
        prompt.className = 'start-story-prompt';
        prompt.innerHTML = '<p>This story hasn\'t begun yet.</p>';
        const btn = document.createElement('button');
        btn.className = 'btn btn-primary start-story-btn';
        btn.innerHTML = '<i class="fas fa-feather-alt"></i> Start Story';
        btn.addEventListener('click', function() {
            prompt.remove();
            if (userInput) userInput.disabled = true;
            showTypingIndicator();
            socket.emit('start_story');
        });
        prompt.appendChild(btn);
        chatHistory.appendChild(prompt);
    });

    socket.on('history_summarized', function(data) {
        console.log('History summarized:', data);
        if (data.success) {
            if (chatHistory) {
                const existingSeparator = chatHistory.querySelector('.history-separator');
                if (!existingSeparator) {
                    const prevSeparator = document.createElement('div');
                    prevSeparator.className = 'history-separator';
                    prevSeparator.innerHTML = '<span>Previous Conversations</span>';
                    chatHistory.insertBefore(prevSeparator, chatHistory.firstChild);
                }
                const currentSeparator = document.createElement('div');
                currentSeparator.className = 'history-separator current';
                chatHistory.appendChild(currentSeparator);
                scrollToBottom();
            }
            setConversationHistory([]);
            setCurrentNarratorMessageElement(null);
            setAccumulatedContent('');
            if (userInput) { userInput.disabled = false; userInput.focus(); }
        }
    });

    socket.on('think_start', function() {
        setIsThinkingInProgress(true);
        const w = ensureLiveWrapper();
        finalizeLiveNarration();  // output-then-reasoning: close any streamed narration first
        ensureRow(w);             // open a live row so the "..." shows immediately
    });

    socket.on('think_end', function() {
        setIsThinkingInProgress(false);
    });

    socket.on('think_output', function(data) {
        appendReasoning(ensureLiveWrapper(), data.text);
        scrollToBottomIfStuck();
    });

    socket.on('text_start', function() {
        setIsThinkingInProgress(false);
        const w = ensureLiveWrapper();
        closeRows(w);
        if (!currentNarratorMessageElement) {
            const el = appendNarration(w, '');
            el.style.display = 'none';
            setCurrentNarratorMessageElement(el);
        }
    });

    socket.on('text_output', function(data) {
        const typingIndicator = document.querySelector('.typing-indicator');
        if (typingIndicator) typingIndicator.remove();
        if (currentNarratorMessageElement) {
            currentNarratorMessageElement.style.display = 'block';
            setAccumulatedContent(accumulatedContent + data.text);
            renderStreamedNarration(currentNarratorMessageElement, accumulatedContent);
            scrollToBottomIfStuck();
        }
    });

    socket.on('tool_request', function(data) {
        const typingIndicator = document.querySelector('.typing-indicator');
        if (typingIndicator) typingIndicator.remove();
        setIsToolCallInProgress(true);
        const w = ensureLiveWrapper();
        finalizeLiveNarration();  // output-then-tool: close any streamed narration, start a new row
        ensureRow(w);
    });

    socket.on('tool_submit', function(data) {
        setAccumulatedContent('');
        const typingIndicator = document.querySelector('.typing-indicator');
        if (typingIndicator) typingIndicator.remove();
        setIsToolCallInProgress(false);

        const w = ensureLiveWrapper();
        const hist = [...conversationHistory];
        data.tools.forEach(tool => {
            let inputs = tool.inputs;
            if (typeof inputs === 'string') {
                try { inputs = JSON.parse(inputs); } catch (e) { inputs = {}; }
            }
            hist.push({ role: 'tool', name: tool.name, inputs: tool.inputs, result: tool.result, timestamp: new Date().toISOString() });
            if (tool.name === 'roll_dice') {
                appendDice(w, [{ expr: inputs.dice || inputs.expression || '?', result: tool.result }]);
            } else {
                // the tools mutate the narrator's context in place, so pull the fresh listing
                if (tool.name === 'write_file' || tool.name === 'append_file') socket.emit('list_story_files');
                appendTool(w, { name: tool.name, inputs, result: tool.result });
            }
        });
        setConversationHistory(hist);
        scrollToBottomIfStuck();
    });

    socket.on('turn_end', function(data) {
        if (data && data.cost_stats) updateCostDisplay(data.cost_stats);

        if (currentNarratorMessageElement) {
            const hist = [...conversationHistory];
            hist.push({ role: 'assistant', content: accumulatedContent, timestamp: new Date().toISOString() });
            setConversationHistory(hist);
        }
        const w = chatHistory.querySelector('.assistant-turn-wrapper.in-progress');
        if (w) { closeRows(w); w.classList.remove('in-progress'); }

        setCurrentNarratorMessageElement(null);
        setAccumulatedContent('');
        setIsToolCallInProgress(false);
        setIsThinkingInProgress(false);

        if (userInput) { userInput.disabled = false; userInput.focus(); }
    });
}
