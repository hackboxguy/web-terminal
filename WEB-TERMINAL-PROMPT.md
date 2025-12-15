# Web Terminal Development Prompt

Use this prompt to start a new Claude Code session for developing the web-terminal application.

---

## Prompt

```
I need help creating a web-based serial terminal application called "web-terminal" for the VMBOX framework. This app will be deployed as a component of an Alpine Linux VirtualBox VM system(VMBOX).

## Project Overview

Create a git repository for a web-based serial terminal that allows users to connect to serial ports (e.g., /dev/ttyUSB0) through a WebUI. The primary use case is accessing Linux SBC (single-board computer) serial consoles.

## Repository Setup

- Repository: https://github.com/hackboxguy/web-terminal.git
- Branch: master
- Will be added to VMBOX via packages.txt entry:
  `web-terminal|https://github.com/hackboxguy/web-terminal.git|master||cmake,python3|8003|26|webapp|Web Serial Terminal`

## Requirements

### Functional Requirements
1. **Single Port Connection**: Connect to one serial port at a time
2. **Device Selection**: Dropdown to select available serial devices (/dev/ttyUSB*, /dev/ttyACM*, /dev/ttyS*)
3. **Baud Rate Selection**: Common rates (9600, 19200, 38400, 57600, 115200, etc.)
4. **Remember Settings**: Persist last used device and baud rate across sessions
5. **ANSI Support**: Full support for ANSI colors and escape sequences
6. **Device Hotplug**: Detect when USB serial devices are plugged/unplugged
7. **Connect/Disconnect**: Clear UI controls to connect and disconnect from ports
8. **Local Echo**: Optional checkbox to enable local echo for half-duplex connections

### Non-Requirements (Keep Simple)
- No log capture to file
- No flow control settings
- No multiple simultaneous connections

## Technical Stack

### Frontend
- **xterm.js**: Terminal emulator with ANSI support
- **xterm-addon-fit**: Auto-resize terminal to container
- **xterm-addon-web-links**: Clickable URLs in terminal
- WebSocket client for real-time communication

### Backend (all pre-installed in VMBOX base system)
- **Flask**: Web framework (`py3-flask`)
- **flask-sock**: WebSocket support for Flask (installed via pip)
- **pyserial**: Serial port communication (`py3-pyserial`)
- **requests**: For token validation (`py3-requests`)
- Python 3.x (`python3`)

### Build System
- **CMake**: Following VMBOX app framework pattern

## Directory Structure

```
web-terminal/
├── CMakeLists.txt
├── manifest.template.json
├── src/
│   └── app.py                    # Flask + WebSocket server
├── share/
│   └── www/
│       ├── index.html            # Main UI
│       ├── css/
│       │   └── style.css
│       └── js/
│           ├── terminal.js       # xterm.js integration
│           ├── xterm.min.js      # (bundled library)
│           ├── xterm.css
│           ├── xterm-addon-fit.min.js
│           └── xterm-addon-web-links.min.js
└── etc/
    └── config.json.default       # Default settings
```

## Dual Access Pattern (CRITICAL)

The web-terminal app is accessed two ways - handle BOTH:

### 1. Direct Access (port 8003)
- User browses to `http://host:8003/`
- HTTP and WebSocket use same host:port
- Auth token obtained from system-mgmt at `http://host:8000/api/session/token`

### 2. Proxy Access (via system-mgmt at port 8000)
- User browses to `http://host:8000/app/web-terminal/`
- HTTP proxied through system-mgmt (works fine)
- WebSocket CANNOT be proxied (most HTTP proxies don't upgrade)
- WebSocket MUST connect directly to port 8003

### JavaScript getBasePath() Pattern (REQUIRED)
```javascript
function getBasePath() {
    const path = window.location.pathname;
    return path.endsWith('/') ? path : path + '/';
}

// Use for all API calls to support both access methods
fetch(getBasePath() + 'api/settings')
```

### JavaScript WebSocket URL (REQUIRED)
```javascript
function getWebSocketUrl(token) {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const host = window.location.hostname;
    const path = window.location.pathname;

    let wsUrl;
    if (path.includes('/app/web-terminal')) {
        // Accessed via proxy - connect directly to web-terminal port
        wsUrl = `${protocol}//${host}:8003/ws`;
    } else {
        // Direct access - use same host:port
        const port = window.location.port || (protocol === 'wss:' ? '443' : '80');
        wsUrl = `${protocol}//${host}:${port}/ws`;
    }

    if (token) {
        wsUrl += `?token=${token}`;
    }
    return wsUrl;
}
```

## WebSocket Authentication

The VMBOX system-mgmt service provides token-based WebSocket authentication. Tokens are **reusable within their 60-second validity period**.

### Authentication Flow:
1. Browser fetches token from system-mgmt: `POST /api/session/token`
2. Token is valid for 60 seconds, **reusable** (not one-time use)
3. WebSocket connects with token: `ws://host:8003/ws?token=<token>`
4. App validates token: `POST http://localhost:8000/api/session/validate-token`
5. If valid, WebSocket connection proceeds

### JavaScript Token Acquisition (handle both access methods):
```javascript
async function getAuthToken() {
    // First check if token is in URL (passed from system-mgmt home page)
    const urlParams = new URLSearchParams(window.location.search);
    const urlToken = urlParams.get('token');
    if (urlToken) {
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
```

### Python Token Validation:
```python
def validate_ws_token(token):
    """Validate WebSocket auth token with system-mgmt."""
    if not config.get('auth_required', False):
        return True  # Auth bypass when disabled

    if not token:
        return False

    try:
        resp = requests.post(
            'http://localhost:8000/api/session/validate-token',
            json={'token': token},
            timeout=5
        )
        data = resp.json()
        return data.get('valid', False)
    except Exception as e:
        logger.warning(f"Token validation failed: {e}")
        return False
```

## CRITICAL: WebSocket JSON Parsing Gotcha

**IMPORTANT**: When mixing JSON control messages with raw terminal data on the same WebSocket:

- `json.loads("2")` returns integer `2` (valid JSON!)
- Calling `.get('type')` on an integer causes `AttributeError`
- This crashes the WebSocket handler when user types single digits

### Python Backend Pattern (REQUIRED):
```python
try:
    msg = json.loads(message)
    # MUST check isinstance - single digits are valid JSON but not control messages
    if not isinstance(msg, dict):
        raise ValueError("Not a control message")
    msg_type = msg.get('type')
    # ... handle control messages
except (json.JSONDecodeError, ValueError):
    # Raw terminal input - send to serial port
    if connected_device:
        serial_connection.write(message.encode('utf-8'))
```

### JavaScript Frontend Pattern (REQUIRED):
```javascript
ws.onmessage = (event) => {
    try {
        const msg = JSON.parse(event.data);
        // Must be an object with 'type' to be a control message
        if (msg && typeof msg === 'object' && msg.type) {
            handleControlMessage(msg);
        } else {
            // Valid JSON but not a control message - treat as terminal data
            if (isConnected) {
                terminal.write(event.data);
            }
        }
    } catch (e) {
        // Raw terminal data
        if (isConnected) {
            terminal.write(event.data);
        }
    }
};
```

## Serial Port Detection (CRITICAL)

Filter out virtual serial ports that don't have real hardware:

```python
def is_real_serial_port(device_path):
    """Check if a serial port is real hardware (not a virtual ttyS port).

    Detects:
    - USB serial adapters (ttyUSB*, ttyACM*)
    - Physical serial ports with hardware backing
    - VirtualBox UART passthrough ports (emulated 16550A)
    """
    import os

    # ttyUSB and ttyACM are always real (USB serial adapters)
    if 'ttyUSB' in device_path or 'ttyACM' in device_path:
        return True

    device_name = os.path.basename(device_path)

    # For ttyS ports, check if there's actual hardware
    # Real hardware ports have a device symlink in /sys/class/tty/
    sys_path = f'/sys/class/tty/{device_name}/device'
    if os.path.exists(sys_path):
        return True

    # Check /proc/tty/driver/serial for active 8250 UART ports
    # VirtualBox UART passthrough shows as "uart:16550A" with a real I/O port
    # Virtual/unused ports show as "uart:unknown" with port 0
    try:
        if device_name.startswith('ttyS'):
            port_num = int(device_name[4:])
            with open('/proc/tty/driver/serial', 'r') as f:
                for line in f:
                    if line.startswith(f'{port_num}:'):
                        if 'uart:unknown' not in line and 'port:00000000' not in line:
                            return True
                        break
    except (FileNotFoundError, PermissionError, ValueError):
        pass

    return False


def list_serial_devices():
    """List available serial devices."""
    devices = []
    seen_paths = set()

    # Get devices from pyserial (mainly USB serial adapters)
    ports = serial.tools.list_ports.comports()
    for port in ports:
        if not is_real_serial_port(port.device):
            continue
        devices.append({
            'path': port.device,
            'description': port.description or port.device,
            'hwid': port.hwid or ''
        })
        seen_paths.add(port.device)

    # Also scan for ttyS ports that pyserial might miss (8250 UARTs)
    import glob
    for tty_path in sorted(glob.glob('/dev/ttyS[0-9]*')):
        if tty_path in seen_paths:
            continue
        if is_real_serial_port(tty_path):
            port_num = tty_path.replace('/dev/ttyS', '')
            devices.append({
                'path': tty_path,
                'description': f'Serial Port COM{int(port_num)+1}',
                'hwid': 'n/a'
            })
            seen_paths.add(tty_path)

    devices.sort(key=lambda x: x['path'])
    return devices
```

## Logging (IMPORTANT)

Avoid duplicate log messages - OpenRC captures stdout and writes to log file. Use file-only logging:

```python
def setup_logging():
    """Configure logging to file (preferred) or console (fallback)."""
    log_format = '%(asctime)s [%(levelname)s] %(name)s: %(message)s'

    log_dir = os.path.dirname(APP_LOG_FILE)
    if log_dir and not os.path.exists(log_dir):
        try:
            os.makedirs(log_dir, exist_ok=True)
        except Exception:
            pass

    # Prefer file logging to avoid duplicates when stdout is also captured
    handlers = []
    try:
        handlers.append(logging.FileHandler(APP_LOG_FILE))
    except Exception:
        # Fall back to stdout only if file logging fails
        handlers.append(logging.StreamHandler(sys.stdout))

    logging.basicConfig(
        level=logging.INFO,
        format=log_format,
        handlers=handlers
    )

    return logging.getLogger(APP_NAME)
```

## API Endpoints

### HTTP Endpoints
- `GET /` - Serve main UI (index.html)
- `GET /<path>` - Serve static files (css, js)
- `GET /health` - Health check: `{"status": "healthy", "timestamp": "...", "uptime_seconds": N}`
- `GET /api/devices` - List available serial devices
- `GET /api/settings` - Get saved settings (last device, baud rate, local_echo)
- `POST /api/settings` - Save settings

### WebSocket Endpoint
- `WS /ws?token=<token>` - Terminal data stream
  - Client->Server: Raw terminal input OR JSON control messages
  - Server->Client: Serial port output OR JSON status messages

## WebSocket Protocol

Use JSON messages for control, raw text for terminal data:

```javascript
// Control messages (JSON objects with 'type' field)
{type: "connect", device: "/dev/ttyUSB0", baudrate: 115200}
{type: "disconnect"}
{type: "resize", cols: 80, rows: 24}
{type: "list_devices"}

// Response messages (JSON)
{type: "status", connected: true, device: "/dev/ttyUSB0", baudrate: 115200}
{type: "status", connected: false, device: null}
{type: "error", message: "Device not found"}
{type: "devices", list: [{path: "/dev/ttyUSB0", description: "USB Serial"}]}

// Terminal data (raw text, not JSON)
// Simply send/receive raw bytes for terminal I/O
```

## UI Design System (CSS Variables)

Use VMBOX UI Design System for consistent styling:

```css
:root {
    /* Background Colors */
    --bg-primary: #1a1a2e;
    --bg-card: #16213e;
    --bg-secondary: #1f3460;
    --bg-tertiary: #0d1421;
    --bg-terminal: #1e1e1e;

    /* Accent Colors */
    --accent-primary: #667eea;
    --accent-secondary: #764ba2;

    /* Text Colors */
    --text-primary: #eee;
    --text-secondary: #888;

    /* Border Colors */
    --border-primary: #1f3460;
    --border-focus: #667eea;

    /* Status Colors */
    --status-success: #34a853;
    --status-warning: #fbbc04;
    --status-error: #ea4335;

    /* Typography */
    --font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    --font-mono: 'Monaco', 'Menlo', 'Consolas', monospace;

    /* Spacing */
    --spacing-xs: 5px;
    --spacing-sm: 10px;
    --spacing-md: 15px;
    --spacing-lg: 20px;

    /* Border Radius */
    --radius-sm: 4px;
    --radius-md: 6px;
}
```

### Status Indicators
- Green dot + pulse animation: Connected
- Yellow dot + blink animation: Connecting
- Red dot (no animation): Disconnected/Error

## Layout

```
┌───────────────────────────────────────────────────────────────┐
│ Web Terminal             [Device ▼] [🔄] [Baud ▼] [☐Echo] [Connect] │
├───────────────────────────────────────────────────────────────┤
│                                                               │
│  ┌─────────────────────────────────────────────────────┐      │
│  │                                                     │      │
│  │              xterm.js Terminal                      │      │
│  │                                                     │      │
│  │                                                     │      │
│  └─────────────────────────────────────────────────────┘      │
│                                                               │
├───────────────────────────────────────────────────────────────┤
│ ● Disconnected                                        [Clear] │
└───────────────────────────────────────────────────────────────┘
```

## Configuration File

`/data/app-config/web-terminal/config.json`:
```json
{
    "last_device": "/dev/ttyUSB0",
    "last_baudrate": 115200,
    "terminal": {
        "font_size": 14,
        "font_family": "monospace",
        "cursor_blink": true,
        "scrollback": 1000
    },
    "local_echo": false,
    "auth_required": false
}
```

## Environment Variables (set by app-manager)

```python
APP_DATA_DIR = os.environ.get('APP_DATA_DIR', f'/data/app-data/{APP_NAME}')
APP_CONFIG_DIR = os.environ.get('APP_CONFIG_DIR', f'/data/app-config/{APP_NAME}')
APP_LOG_FILE = os.environ.get('APP_LOG_FILE', f'/var/log/app/{APP_NAME}.log')
STATIC_DIR = os.environ.get('APP_STATIC_DIR', f'/app/{APP_NAME}/share/www')
```

## Signal Handling (Graceful Shutdown)

```python
def shutdown_handler(signum, frame):
    """Handle shutdown signals gracefully."""
    signal_name = signal.Signals(signum).name
    logger.info(f"Received {signal_name}, shutting down gracefully...")

    # Close serial connection
    global serial_connection
    with serial_lock:
        if serial_connection and serial_connection.is_open:
            serial_connection.close()
            serial_connection = None

    sys.exit(0)

# In main():
signal.signal(signal.SIGTERM, shutdown_handler)
signal.signal(signal.SIGINT, shutdown_handler)
```

## CMakeLists.txt Template

```cmake
cmake_minimum_required(VERSION 3.10)
project(web-terminal VERSION 1.0.0)

# Install Python application
install(PROGRAMS src/app.py
        DESTINATION bin
        RENAME web-terminal-server)

# Install web assets
install(DIRECTORY share/www/
        DESTINATION share/www)

# Install default config
install(FILES etc/config.json.default
        DESTINATION etc)

# Generate manifest from template
configure_file(manifest.template.json
               ${CMAKE_BINARY_DIR}/manifest.json
               @ONLY)
install(FILES ${CMAKE_BINARY_DIR}/manifest.json
        DESTINATION .)
```

## manifest.template.json

```json
{
    "name": "web-terminal",
    "version": "@PROJECT_VERSION@",
    "type": "webapp",
    "description": "Web Serial Terminal",
    "command": "/app/web-terminal/bin/web-terminal-server",
    "args": [
        "--config", "/data/app-config/web-terminal/config.json",
        "--static", "/app/web-terminal/share/www",
        "--port", "8003"
    ],
    "port": 8003,
    "health": {
        "endpoint": "/health",
        "interval": 10,
        "timeout": 5
    },
    "directories": {
        "config": "/data/app-config/web-terminal",
        "data": "/data/app-data/web-terminal"
    },
    "default_config": "/app/web-terminal/etc/config.json.default"
}
```

## xterm.js Integration

Bundle these files in share/www/js/ (download from CDN or npm):
- xterm.min.js, xterm.css (core library)
- xterm-addon-fit.min.js (auto-resize)
- xterm-addon-web-links.min.js (clickable URLs)

```javascript
// Initialize terminal
terminal = new Terminal({
    cursorBlink: true,
    fontSize: 14,
    fontFamily: 'Monaco, Menlo, Consolas, monospace',
    theme: {
        background: '#1e1e1e',
        foreground: '#d4d4d4',
        cursor: '#d4d4d4'
    },
    scrollback: 1000,
    convertEol: false
});

fitAddon = new FitAddon.FitAddon();
terminal.loadAddon(fitAddon);

webLinksAddon = new WebLinksAddon.WebLinksAddon();
terminal.loadAddon(webLinksAddon);

terminal.open(document.getElementById('terminal'));
fitAddon.fit();

// Handle window resize
window.addEventListener('resize', () => fitAddon.fit());

// Handle terminal input
terminal.onData(data => {
    if (ws && ws.readyState === WebSocket.OPEN && isConnected) {
        ws.send(data);

        // Local echo if enabled
        if (localEchoCheckbox.checked) {
            if (data === '\r') {
                terminal.write('\r\n');
            } else if (data === '\x7f') {
                terminal.write('\b \b');
            } else {
                terminal.write(data);
            }
        }
    }
});
```

## Key Implementation Notes

1. **Pre-installed Dependencies**: Flask, flask-sock, pyserial, requests are pre-installed in VMBOX base system
2. **Serial Port Access**: User must be in `dialout` group (handled by VMBOX base system)
3. **Device Detection**: Use `is_real_serial_port()` to filter virtual ports
4. **Hotplug**: Poll for device changes every 3 seconds via WebSocket
5. **Graceful Shutdown**: Handle SIGTERM to close serial connections cleanly
6. **Error Handling**: Handle device disconnection gracefully, show clear error messages
7. **Threading**: Use thread-safe serial access with locks, daemon threads for reading

## Testing

1. Build VMBOX with web-terminal in packages.txt
2. Boot VM, login to system-mgmt WebUI
3. Navigate to Applications panel, click "Open" for web-terminal
4. Test with a USB-serial adapter connected to the host (passed through to VM)
5. Verify ANSI colors work with: `echo -e "\e[31mRed\e[32mGreen\e[34mBlue\e[0m"`
6. **Test single digit input**: Type "1", "2", "3" etc. - should NOT disconnect
7. **Test proxy access**: Access via http://localhost:8000/app/web-terminal/
8. **Test direct access**: Access via http://localhost:8003/

Please create the complete web-terminal application following these specifications.
```

---

## Quick Reference Files

When starting the new session, you may want to share these files for context:
- `VM-APP-DESIGN.md` - Complete app framework documentation
- `apps/hello-world/` - Reference implementation
- `rootfs/opt/system-mgmt/app.py` - WebSocket auth endpoints (search for `validate-token`)

## Key Gotchas Addressed in This Prompt

| Issue | Solution |
|-------|----------|
| Typing "2" disconnects WebSocket | Check `isinstance(msg, dict)` before `.get('type')` |
| Token rejected on reconnect | Tokens are reusable within 60s validity period |
| API calls fail via proxy | Use `getBasePath()` for relative URLs |
| WebSocket fails via proxy | Detect proxy path, connect directly to port 8003 |
| 32 ttyS ports listed | Filter with `is_real_serial_port()` |
| Duplicate log messages | Use file-only logging (OpenRC captures stdout) |
| Device list includes virtual ports | Check /sys/class/tty and /proc/tty/driver/serial |
