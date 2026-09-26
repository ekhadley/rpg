import { userInput, summarizePopup, summarizeButton, selectStoryConfigModal, createStoryModal, copyStoryModal, exportButton, socket, fileViewerOverlay, debugModal, debugModalClose, settingsModal, modelEditorPopup } from './state.js';
import { setPendingStoryName } from './state.js';
import { initAllDropdowns } from './dropdowns.js';
import { initStory, selectStoryDirectly, closeCopyStoryModal } from './story.js';
import { initChat } from './chat.js';
import { initStudio } from './studio.js';
import { exportConversation, initDebugPopup } from './debugViewer.js';
import {
    setupCostPopupBehavior, initTheme, initSettings,
    showSummarizePopup, hideSummarizePopup, positionSummarizePopup,
    initConfirmPopup, hideConfirmPopup, initForkPopup, initErrorPopup,
} from './ui.js';

// Initialize dropdowns
initAllDropdowns();

// Initialize story sidebar and modals
initStory();

// Initialize chat socket handlers
initChat();

// Initialize the prompt studio (sidebar mode toggle + side-by-side comparison view)
initStudio();

// Auto-select initial story if provided via URL
if (window.INITIAL_STORY) {
    selectStoryDirectly(window.INITIAL_STORY);
}

// Setup on load
window.onload = function() {
    if (userInput) userInput.focus();
    setupCostPopupBehavior();
    initConfirmPopup();
    initForkPopup();
    initErrorPopup();
    initTheme();
    initSettings();

    if (exportButton) exportButton.addEventListener('click', exportConversation);
    initDebugPopup();
    if (debugModalClose) debugModalClose.addEventListener('click', () => debugModal.classList.remove('show'));

    // Summarize button
    if (summarizeButton) {
        summarizeButton.addEventListener('click', function(e) {
            e.stopPropagation();
            if (summarizePopup && summarizePopup.classList.contains('visible')) hideSummarizePopup();
            else showSummarizePopup();
        });
    }

    const summarizePopupCancel = document.getElementById('summarize-popup-cancel');
    const summarizePopupConfirm = document.getElementById('summarize-popup-confirm');

    if (summarizePopupCancel) {
        summarizePopupCancel.addEventListener('click', function(e) {
            e.stopPropagation();
            hideSummarizePopup();
        });
    }
    if (summarizePopupConfirm) {
        summarizePopupConfirm.addEventListener('click', function(e) {
            e.stopPropagation();
            socket.emit('summarize_history');
            hideSummarizePopup();
        });
    }

    // Close popups on outside click
    document.addEventListener('click', function(e) {
        if (summarizePopup && summarizePopup.classList.contains('visible')) {
            if (!summarizePopup.contains(e.target) && e.target !== summarizeButton) {
                hideSummarizePopup();
            }
        }
    });

    // Escape key handler
    document.addEventListener('keydown', function(e) {
        if (e.key === 'Escape') {
            if (summarizePopup && summarizePopup.classList.contains('visible')) hideSummarizePopup();
            if (selectStoryConfigModal && selectStoryConfigModal.classList.contains('show')) {
                selectStoryConfigModal.classList.remove('show');
                setPendingStoryName(null);
            }
            if (createStoryModal) createStoryModal.classList.remove('show');
            if (copyStoryModal && copyStoryModal.classList.contains('show')) closeCopyStoryModal();
            if (fileViewerOverlay && fileViewerOverlay.classList.contains('show')) fileViewerOverlay.classList.remove('show');
            if (debugModal && debugModal.classList.contains('show')) debugModal.classList.remove('show');
            if (modelEditorPopup && modelEditorPopup.classList.contains('show')) modelEditorPopup.classList.remove('show');
            else if (settingsModal && settingsModal.classList.contains('show')) settingsModal.classList.remove('show');
            hideConfirmPopup();
        }
    });

    // Reposition summarize popup on resize
    window.addEventListener('resize', function() {
        if (summarizePopup && summarizePopup.classList.contains('visible') && summarizeButton) {
            positionSummarizePopup(summarizeButton);
        }
    });
};
