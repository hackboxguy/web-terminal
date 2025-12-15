/**
 * Web Terminal - xterm.js Integration
 *
 * Handles terminal emulation and WebSocket communication for serial console.
 */

(function() {
    'use strict';

    // DOM Elements
    const terminalEl = document.getElementById('terminal');
    const deviceSelect = document.getElementById('device-select');
    const baudrateSelect = document.getElementById('baudrate-select');
    const localEchoCheckbox = document.getElementById('local-echo');
    const connectBtn = document.getElementById('connect-btn');
    const refreshDevicesBtn = document.getElementById('refresh-devices');
    const clearBtn = document.getElementById('clear-btn');
    const statusDot = document.getElementById('status-dot');
    const statusText = document.getElementById('status-text');

    // State
    let terminal = null;
    let fitAddon = null;
    let webLinksAddon = null;
    let ws = null;
    let isConnected = false;
    let isConnecting = false;
    let devicePollInterval = null;
    let wsConnected = false;  // WebSocket connection state (separate from serial)

    // Configuration
    const DEVICE_POLL_INTERVAL = 3000; // 3 seconds
    const WS_RECONNECT_DELAY = 3000;   // 3 seconds

    /**
     * Get base path for API calls (works with both direct and proxied access)
     */
    function getBasePath() {
        const path = window.location.pathname;
        return path.endsWith('/') ? path : path + '/';
    }

    /**
     * Get WebSocket URL
     * When accessed via proxy (/app/web-terminal/), connect directly to port 8003
     * since most HTTP proxies don't support WebSocket upgrade
     */
    function getWebSocketUrl(token) {
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const host = window.location.hostname;
        const path = window.location.pathname;

        let wsUrl;
        if (path.includes('/app/web-terminal')) {
            // Accessed via system-mgmt proxy - connect directly to web-terminal port
            wsUrl = `${protocol}//${host}:8003/ws`;
        } else {
            // Direct access - use same host:port
            const port = window.location.port || (window.location.protocol === 'https:' ? '443' : '80');
            wsUrl = `${protocol}//${host}:${port}/ws`;
        }

        if (token) {
            wsUrl += `?token=${token}`;
        }
        return wsUrl;
    }

    /**
     * Initialize xterm.js terminal
     */
    function initTerminal() {
        terminal = new Terminal({
            cursorBlink: true,
            fontSize: 14,
            fontFamily: 'Monaco, Menlo, Consolas, monospace',
            theme: {
                background: '#1e1e1e',
                foreground: '#d4d4d4',
                cursor: '#d4d4d4',
                cursorAccent: '#1e1e1e',
                selection: 'rgba(255, 255, 255, 0.3)',
                black: '#000000',
                red: '#cd3131',
                green: '#0dbc79',
                yellow: '#e5e510',
                blue: '#2472c8',
                magenta: '#bc3fbc',
                cyan: '#11a8cd',
                white: '#e5e5e5',
                brightBlack: '#666666',
                brightRed: '#f14c4c',
                brightGreen: '#23d18b',
                brightYellow: '#f5f543',
                brightBlue: '#3b8eea',
                brightMagenta: '#d670d6',
                brightCyan: '#29b8db',
                brightWhite: '#ffffff'
            },
            scrollback: 1000,
            convertEol: false,
            allowProposedApi: true
        });

        // Initialize addons
        fitAddon = new FitAddon.FitAddon();
        terminal.loadAddon(fitAddon);

        webLinksAddon = new WebLinksAddon.WebLinksAddon();
        terminal.loadAddon(webLinksAddon);

        // Open terminal in container
        terminal.open(terminalEl);
        fitAddon.fit();

        // Handle terminal input
        terminal.onData(data => {
            if (ws && ws.readyState === WebSocket.OPEN && isConnected) {
                // Send raw data to WebSocket
                ws.send(data);

                // Local echo if enabled
                if (localEchoCheckbox.checked) {
                    // Echo the character, handle special keys
                    if (data === '\r') {
                        terminal.write('\r\n');
                    } else if (data === '\x7f') {
                        // Backspace
                        terminal.write('\b \b');
                    } else {
                        terminal.write(data);
                    }
                }
            }
        });

        // Handle terminal resize
        terminal.onResize(size => {
            if (ws && ws.readyState === WebSocket.OPEN) {
                ws.send(JSON.stringify({
                    type: 'resize',
                    cols: size.cols,
                    rows: size.rows
                }));
            }
        });

        // Handle window resize
        window.addEventListener('resize', () => {
            if (fitAddon) {
                fitAddon.fit();
            }
        });

        // Initial message
        terminal.writeln('\x1b[1;34m=== Web Terminal ===\x1b[0m');
        terminal.writeln('\x1b[90mSelect a device and click Connect to start.\x1b[0m');
        terminal.writeln('');
    }

    /**
     * Get auth token - either from URL query parameter or fetch from system-mgmt
     * When opened from system-mgmt home page, token is passed in URL
     */
    async function getAuthToken() {
        // First check if token is in URL (passed from system-mgmt home page)
        const urlParams = new URLSearchParams(window.location.search);
        const urlToken = urlParams.get('token');
        if (urlToken) {
            console.log('Using token from URL');
            return urlToken;
        }

        // Otherwise try to fetch from system-mgmt
        try {
            const path = window.location.pathname;
            let tokenUrl;

            if (path.includes('/app/web-terminal')) {
                // Via proxy - use relative URL (goes through system-mgmt)
                tokenUrl = '/api/session/token';
            } else {
                // Direct access - explicitly use system-mgmt port
                const host = window.location.hostname;
                tokenUrl = `http://${host}:8000/api/session/token`;
            }

            const response = await fetch(tokenUrl, { method: 'POST' });
            if (response.ok) {
                const data = await response.json();
                return data.token;
            }
        } catch (e) {
            // Auth not available, continue without token
            console.log('Auth token not available, connecting without authentication');
        }
        return null;
    }

    /**
     * Connect WebSocket
     */
    async function connectWebSocket() {
        if (ws && ws.readyState === WebSocket.OPEN) {
            return;
        }

        // Try to get auth token (may fail if not in VMBOX environment)
        const token = await getAuthToken();
        const wsUrl = getWebSocketUrl(token);

        ws = new WebSocket(wsUrl);

        ws.onopen = () => {
            console.log('WebSocket connected');
            wsConnected = true;
            // Request device list
            ws.send(JSON.stringify({ type: 'list_devices' }));
        };

        ws.onmessage = (event) => {
            const data = event.data;

            // Try to parse as JSON (control message)
            try {
                const msg = JSON.parse(data);
                // Must be an object with 'type' to be a control message
                // (Raw terminal data like "2" is valid JSON but not a control msg)
                if (msg && typeof msg === 'object' && msg.type) {
                    handleControlMessage(msg);
                } else {
                    // Valid JSON but not a control message - treat as terminal data
                    if (isConnected) {
                        terminal.write(data);
                    }
                }
            } catch (e) {
                // Raw terminal data
                if (isConnected) {
                    terminal.write(data);
                }
            }
        };

        ws.onerror = (error) => {
            console.error('WebSocket error:', error);
            // Only show error in terminal if we were connected to a serial device
            if (isConnected) {
                terminal.writeln('\x1b[31mWebSocket error\x1b[0m');
            }
        };

        ws.onclose = () => {
            console.log('WebSocket closed');
            const wasConnectedToDevice = isConnected;
            wsConnected = false;

            if (isConnected) {
                updateStatus('disconnected');
                isConnected = false;
                updateConnectButton();
                terminal.writeln('\x1b[31m\r\n=== Connection lost ===\x1b[0m');
            }

            // Reconnect after delay (silently in background)
            setTimeout(() => {
                if (!ws || ws.readyState === WebSocket.CLOSED) {
                    connectWebSocket();
                }
            }, WS_RECONNECT_DELAY);
        };
    }

    /**
     * Handle WebSocket control messages
     */
    function handleControlMessage(msg) {
        switch (msg.type) {
            case 'devices':
                updateDeviceList(msg.list);
                break;

            case 'status':
                if (msg.connected) {
                    isConnected = true;
                    isConnecting = false;
                    updateStatus('connected', msg.device, msg.baudrate);
                    terminal.writeln(`\x1b[32m\r\n=== Connected to ${msg.device} @ ${msg.baudrate} ===\x1b[0m\r\n`);
                    terminal.focus();
                } else {
                    isConnected = false;
                    isConnecting = false;
                    updateStatus('disconnected');
                    terminal.writeln('\x1b[33m\r\n=== Disconnected ===\x1b[0m');
                }
                updateConnectButton();
                break;

            case 'error':
                isConnecting = false;
                updateConnectButton();
                terminal.writeln(`\x1b[31mError: ${msg.message}\x1b[0m`);
                break;
        }
    }

    /**
     * Update device dropdown list
     */
    function updateDeviceList(devices) {
        const currentValue = deviceSelect.value;
        deviceSelect.innerHTML = '<option value="">Select device...</option>';

        devices.forEach(device => {
            const option = document.createElement('option');
            option.value = device.path;
            option.textContent = `${device.path} - ${device.description}`;
            deviceSelect.appendChild(option);
        });

        // Restore selection if still available
        if (currentValue) {
            const exists = devices.some(d => d.path === currentValue);
            if (exists) {
                deviceSelect.value = currentValue;
            }
        }
    }

    /**
     * Update connection status display
     */
    function updateStatus(status, device, baudrate) {
        statusDot.className = 'status-dot';

        switch (status) {
            case 'connected':
                statusDot.classList.add('connected');
                statusText.textContent = `Connected to ${device} @ ${baudrate}`;
                document.title = `${device} - Web Terminal`;
                break;
            case 'connecting':
                statusDot.classList.add('connecting');
                statusText.textContent = 'Connecting...';
                break;
            case 'disconnected':
            default:
                statusText.textContent = 'Disconnected';
                document.title = 'Web Terminal - Serial Console';
                break;
        }
    }

    /**
     * Update connect button state
     */
    function updateConnectButton() {
        connectBtn.classList.remove('connected', 'connecting');

        if (isConnecting) {
            connectBtn.textContent = 'Connecting...';
            connectBtn.classList.add('connecting');
            connectBtn.disabled = true;
        } else if (isConnected) {
            connectBtn.textContent = 'Disconnect';
            connectBtn.classList.add('connected');
            connectBtn.disabled = false;
        } else {
            connectBtn.textContent = 'Connect';
            connectBtn.disabled = false;
        }
    }

    /**
     * Connect to serial device
     */
    function connectToDevice() {
        const device = deviceSelect.value;
        const baudrate = parseInt(baudrateSelect.value, 10);

        if (!device) {
            terminal.writeln('\x1b[31mPlease select a device\x1b[0m');
            return;
        }

        if (!ws || ws.readyState !== WebSocket.OPEN) {
            terminal.writeln('\x1b[31mWebSocket not connected\x1b[0m');
            return;
        }

        isConnecting = true;
        updateStatus('connecting');
        updateConnectButton();

        ws.send(JSON.stringify({
            type: 'connect',
            device: device,
            baudrate: baudrate
        }));

        // Save settings
        saveSettings();
    }

    /**
     * Disconnect from serial device
     */
    function disconnectFromDevice() {
        if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: 'disconnect' }));
        }
    }

    /**
     * Request device list refresh
     */
    function refreshDevices() {
        if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: 'list_devices' }));
        }
    }

    /**
     * Load saved settings
     */
    async function loadSettings() {
        try {
            const response = await fetch(getBasePath() + 'api/settings');
            if (response.ok) {
                const settings = await response.json();

                if (settings.last_device) {
                    // Will be set after device list is loaded
                    deviceSelect.dataset.lastDevice = settings.last_device;
                }
                if (settings.last_baudrate) {
                    baudrateSelect.value = settings.last_baudrate;
                }
                if (settings.local_echo !== undefined) {
                    localEchoCheckbox.checked = settings.local_echo;
                }
                if (settings.terminal) {
                    // Apply terminal settings
                    if (settings.terminal.font_size && terminal) {
                        terminal.options.fontSize = settings.terminal.font_size;
                    }
                    if (settings.terminal.scrollback && terminal) {
                        terminal.options.scrollback = settings.terminal.scrollback;
                    }
                }
            }
        } catch (e) {
            console.error('Failed to load settings:', e);
        }
    }

    /**
     * Save current settings
     */
    async function saveSettings() {
        try {
            await fetch(getBasePath() + 'api/settings', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    last_device: deviceSelect.value,
                    last_baudrate: parseInt(baudrateSelect.value, 10),
                    local_echo: localEchoCheckbox.checked
                })
            });
        } catch (e) {
            console.error('Failed to save settings:', e);
        }
    }

    /**
     * Start device polling for hotplug detection
     */
    function startDevicePolling() {
        devicePollInterval = setInterval(() => {
            if (ws && ws.readyState === WebSocket.OPEN) {
                ws.send(JSON.stringify({ type: 'list_devices' }));
            }
        }, DEVICE_POLL_INTERVAL);
    }

    /**
     * Initialize event listeners
     */
    function initEventListeners() {
        // Connect/Disconnect button
        connectBtn.addEventListener('click', () => {
            if (isConnected) {
                disconnectFromDevice();
            } else {
                connectToDevice();
            }
        });

        // Refresh devices button
        refreshDevicesBtn.addEventListener('click', refreshDevices);

        // Clear terminal button
        clearBtn.addEventListener('click', () => {
            terminal.clear();
        });

        // Local echo change - save immediately
        localEchoCheckbox.addEventListener('change', saveSettings);

        // Device select - restore last device when list updates
        deviceSelect.addEventListener('change', () => {
            // Clear the stored last device after user makes a selection
            delete deviceSelect.dataset.lastDevice;
        });

        // Watch for device list updates to restore last device
        const observer = new MutationObserver(() => {
            const lastDevice = deviceSelect.dataset.lastDevice;
            if (lastDevice) {
                const option = Array.from(deviceSelect.options).find(o => o.value === lastDevice);
                if (option) {
                    deviceSelect.value = lastDevice;
                    delete deviceSelect.dataset.lastDevice;
                }
            }
        });
        observer.observe(deviceSelect, { childList: true });

        // Focus terminal on click
        terminalEl.addEventListener('click', () => {
            if (terminal) {
                terminal.focus();
            }
        });

        // Keyboard shortcut: Ctrl+Shift+C to clear
        document.addEventListener('keydown', (e) => {
            if (e.ctrlKey && e.shiftKey && e.key === 'C') {
                e.preventDefault();
                terminal.clear();
            }
        });
    }

    /**
     * Initialize application
     */
    async function init() {
        initTerminal();
        initEventListeners();
        await loadSettings();
        await connectWebSocket();
        startDevicePolling();
    }

    // Start when DOM is ready
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
