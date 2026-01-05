/**
 * Yaad - Main JavaScript file
 */

// Toast notification helper
const Toast = {
    container: document.getElementById('toast-container'),

    show(message, type = 'info', duration = 3000) {
        const toast = document.createElement('div');
        const colors = {
            info: 'bg-blue-600',
            success: 'bg-green-600',
            warning: 'bg-yellow-600',
            error: 'bg-red-600',
        };

        toast.className = `${colors[type]} text-white px-4 py-3 rounded-lg shadow-lg toast-enter flex items-center space-x-2`;
        toast.innerHTML = `
            <span>${message}</span>
            <button onclick="this.parentElement.remove()" class="ml-2 hover:opacity-80">
                <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/>
                </svg>
            </button>
        `;

        this.container.appendChild(toast);

        if (duration > 0) {
            setTimeout(() => {
                toast.classList.remove('toast-enter');
                toast.classList.add('toast-exit');
                setTimeout(() => toast.remove(), 300);
            }, duration);
        }
    },

    success(message) {
        this.show(message, 'success');
    },

    error(message) {
        this.show(message, 'error');
    },

    info(message) {
        this.show(message, 'info');
    },

    warning(message) {
        this.show(message, 'warning');
    },
};

// HTMX event handlers
document.body.addEventListener('htmx:responseError', (event) => {
    Toast.error('An error occurred. Please try again.');
});

document.body.addEventListener('htmx:sendError', (event) => {
    Toast.error('Network error. Please check your connection.');
});

// Handle HTMX success messages from headers
document.body.addEventListener('htmx:afterRequest', (event) => {
    const message = event.detail.xhr.getResponseHeader('HX-Trigger-Message');
    if (message) {
        Toast.success(message);
    }
});

// Keyboard shortcuts
document.addEventListener('keydown', (event) => {
    // Ctrl/Cmd + K for search focus
    if ((event.ctrlKey || event.metaKey) && event.key === 'k') {
        event.preventDefault();
        const searchInput = document.querySelector('input[type="text"][placeholder*="Search"]');
        if (searchInput) {
            searchInput.focus();
        }
    }

    // Escape to close modals
    if (event.key === 'Escape') {
        const modal = document.querySelector('[x-data*="modal"]');
        if (modal && modal.__x) {
            modal.__x.$data.open = false;
        }
    }
});

// Initialize on DOM ready
document.addEventListener('DOMContentLoaded', () => {
    console.log('Yaad initialized');
});
