#!/usr/bin/env python3
"""
Web Terminal - Serial Console over WebSocket

A web-based serial terminal application for the VMBOX framework.
Allows connecting to serial ports (e.g., /dev/ttyUSB0) through a browser.

Features:
- Connect to serial ports via WebSocket
- xterm.js compatible terminal protocol
- Device hotplug detection
- Settings persistence
- Token-based WebSocket authentication (with bypass option)
"""

import argparse
import json
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime

import requests
import serial
import serial.tools.list_ports
from flask import Flask, jsonify, request, send_from_directory
from flask_sock import Sock

# Application metadata
APP_NAME = 'web-terminal'
APP_VERSION = '1.0.0'

# Default configuration
DEFAULT_PORT = 8003
DEFAULT_HOST = '0.0.0.0'

# Environment variables
APP_DATA_DIR = os.environ.get('APP_DATA_DIR', f'/data/app-data/{APP_NAME}')
APP_CONFIG_DIR = os.environ.get('APP_CONFIG_DIR', f'/data/app-config/{APP_NAME}')
APP_LOG_FILE = os.environ.get('APP_LOG_FILE', f'/var/log/app/{APP_NAME}.log')
STATIC_DIR = os.environ.get('APP_STATIC_DIR', f'/app/{APP_NAME}/share/www')

# Start time for uptime calculation
START_TIME = datetime.now()

# Global state
config = {
    'last_device': '',
    'last_baudrate': 115200,
    'terminal': {
        'font_size': 14,
        'font_family': 'monospace',
        'cursor_blink': True,
        'scrollback': 1000
    },
    'local_echo': False,
    'auth_required': False
}
config_path = None
serial_connection = None
serial_lock = threading.Lock()

# Flask app setup
app = Flask(__name__, static_folder=None)
sock = Sock(app)


def setup_logging():
    """Configure logging to file and console."""
    log_format = '%(asctime)s [%(levelname)s] %(name)s: %(message)s'

    log_dir = os.path.dirname(APP_LOG_FILE)
    if log_dir and not os.path.exists(log_dir):
        try:
            os.makedirs(log_dir, exist_ok=True)
        except Exception:
            pass

    handlers = [logging.StreamHandler(sys.stdout)]

    try:
        handlers.append(logging.FileHandler(APP_LOG_FILE))
    except Exception:
        pass

    logging.basicConfig(
        level=logging.INFO,
        format=log_format,
        handlers=handlers
    )

    return logging.getLogger(APP_NAME)


logger = setup_logging()


def load_config(path):
    """Load configuration from JSON file."""
    global config, config_path
    config_path = path

    if os.path.exists(path):
        try:
            with open(path, 'r') as f:
                user_config = json.load(f)
                # Deep merge for nested 'terminal' config
                if 'terminal' in user_config:
                    config['terminal'].update(user_config['terminal'])
                    del user_config['terminal']
                config.update(user_config)
                logger.info(f"Loaded configuration from {path}")
        except Exception as e:
            logger.warning(f"Failed to load config from {path}: {e}")
    else:
        logger.info(f"No config file at {path}, using defaults")


def save_config():
    """Save current configuration to file."""
    if config_path:
        try:
            config_dir = os.path.dirname(config_path)
            if config_dir and not os.path.exists(config_dir):
                os.makedirs(config_dir, exist_ok=True)
            with open(config_path, 'w') as f:
                json.dump(config, f, indent=2)
            logger.info(f"Saved configuration to {config_path}")
        except Exception as e:
            logger.warning(f"Failed to save config: {e}")


def validate_ws_token(token):
    """Validate WebSocket auth token with system-mgmt."""
    if not config.get('auth_required', False):
        return True

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
                    # Format: "0: uart:16550A port:000003F8 irq:4 ..."
                    if line.startswith(f'{port_num}:'):
                        # Check if it's a real UART (not "unknown") with a real port
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

    try:
        # First, get devices from pyserial (mainly USB serial adapters)
        ports = serial.tools.list_ports.comports()
        for port in ports:
            # Filter out virtual ttyS ports
            if not is_real_serial_port(port.device):
                continue

            devices.append({
                'path': port.device,
                'description': port.description or port.device,
                'hwid': port.hwid or ''
            })
            seen_paths.add(port.device)
    except Exception as e:
        logger.error(f"Failed to list serial devices via pyserial: {e}")

    # Also scan for ttyS ports that pyserial might miss (8250 UARTs)
    # This catches VirtualBox UART passthrough and native serial ports
    try:
        import glob
        for tty_path in sorted(glob.glob('/dev/ttyS[0-9]*')):
            if tty_path in seen_paths:
                continue
            if is_real_serial_port(tty_path):
                # Get port number for description
                port_num = tty_path.replace('/dev/ttyS', '')
                devices.append({
                    'path': tty_path,
                    'description': f'Serial Port COM{int(port_num)+1}',
                    'hwid': 'n/a'
                })
                seen_paths.add(tty_path)
    except Exception as e:
        logger.error(f"Failed to scan ttyS ports: {e}")

    # Sort by device path
    devices.sort(key=lambda x: x['path'])
    return devices


# HTTP Routes

@app.route('/')
def index():
    """Serve the main UI."""
    return send_from_directory(STATIC_DIR, 'index.html')


@app.route('/<path:filename>')
def static_files(filename):
    """Serve static files."""
    return send_from_directory(STATIC_DIR, filename)


@app.route('/health')
def health():
    """Health check endpoint."""
    uptime = (datetime.now() - START_TIME).total_seconds()
    return jsonify({
        'status': 'healthy',
        'timestamp': datetime.now().isoformat(),
        'uptime_seconds': uptime
    })


@app.route('/api/devices')
def get_devices():
    """List available serial devices."""
    return jsonify({
        'devices': list_serial_devices()
    })


@app.route('/api/settings', methods=['GET'])
def get_settings():
    """Get current settings."""
    return jsonify({
        'last_device': config.get('last_device', ''),
        'last_baudrate': config.get('last_baudrate', 115200),
        'terminal': config.get('terminal', {}),
        'local_echo': config.get('local_echo', False)
    })


@app.route('/api/settings', methods=['POST'])
def save_settings():
    """Save settings."""
    data = request.get_json()
    if not data:
        return jsonify({'error': 'Invalid JSON'}), 400

    if 'last_device' in data:
        config['last_device'] = data['last_device']
    if 'last_baudrate' in data:
        config['last_baudrate'] = int(data['last_baudrate'])
    if 'local_echo' in data:
        config['local_echo'] = bool(data['local_echo'])
    if 'terminal' in data:
        config['terminal'].update(data['terminal'])

    save_config()
    return jsonify({'status': 'ok'})


# WebSocket Handler

@sock.route('/ws')
def websocket_handler(ws):
    """Handle WebSocket connections for terminal I/O."""
    global serial_connection

    # Validate token if auth is required
    token = request.args.get('token', '')
    if not validate_ws_token(token):
        logger.warning("WebSocket connection rejected: invalid token")
        try:
            ws.send(json.dumps({'type': 'error', 'message': 'Authentication required'}))
        except Exception:
            pass
        return

    logger.info("WebSocket client connected")
    connected_device = None
    reader_thread = None
    stop_reader = threading.Event()

    def serial_reader():
        """Read from serial port and send to WebSocket."""
        global serial_connection
        nonlocal connected_device
        while not stop_reader.is_set():
            try:
                with serial_lock:
                    if serial_connection and serial_connection.is_open:
                        if serial_connection.in_waiting > 0:
                            data = serial_connection.read(serial_connection.in_waiting)
                            if data:
                                try:
                                    ws.send(data.decode('utf-8', errors='replace'))
                                except Exception:
                                    break
                time.sleep(0.01)  # Small delay to prevent busy loop
            except Exception as e:
                logger.error(f"Serial read error: {e}")
                try:
                    ws.send(json.dumps({
                        'type': 'error',
                        'message': f'Serial read error: {str(e)}'
                    }))
                    ws.send(json.dumps({
                        'type': 'status',
                        'connected': False,
                        'device': None
                    }))
                except Exception:
                    pass
                with serial_lock:
                    if serial_connection:
                        try:
                            serial_connection.close()
                        except Exception:
                            pass
                        serial_connection = None
                connected_device = None
                break

    try:
        # Send initial device list
        ws.send(json.dumps({
            'type': 'devices',
            'list': list_serial_devices()
        }))

        while True:
            try:
                message = ws.receive(timeout=1)
            except Exception:
                message = None

            if message is None:
                continue

            # Try to parse as JSON control message
            try:
                msg = json.loads(message)
                msg_type = msg.get('type')

                if msg_type == 'connect':
                    device = msg.get('device')
                    baudrate = msg.get('baudrate', 115200)

                    if not device:
                        ws.send(json.dumps({
                            'type': 'error',
                            'message': 'No device specified'
                        }))
                        continue

                    # Close existing connection
                    with serial_lock:
                        if serial_connection and serial_connection.is_open:
                            stop_reader.set()
                            if reader_thread:
                                reader_thread.join(timeout=1)
                            serial_connection.close()
                            serial_connection = None

                    stop_reader.clear()

                    # Open new connection
                    try:
                        with serial_lock:
                            serial_connection = serial.Serial(
                                port=device,
                                baudrate=baudrate,
                                bytesize=serial.EIGHTBITS,
                                parity=serial.PARITY_NONE,
                                stopbits=serial.STOPBITS_ONE,
                                timeout=0.1
                            )
                        connected_device = device

                        # Save settings
                        config['last_device'] = device
                        config['last_baudrate'] = baudrate
                        save_config()

                        # Start reader thread
                        reader_thread = threading.Thread(target=serial_reader, daemon=True)
                        reader_thread.start()

                        logger.info(f"Connected to {device} @ {baudrate}")
                        ws.send(json.dumps({
                            'type': 'status',
                            'connected': True,
                            'device': device,
                            'baudrate': baudrate
                        }))

                    except Exception as e:
                        logger.error(f"Failed to connect to {device}: {e}")
                        ws.send(json.dumps({
                            'type': 'error',
                            'message': f'Failed to connect: {str(e)}'
                        }))

                elif msg_type == 'disconnect':
                    with serial_lock:
                        if serial_connection and serial_connection.is_open:
                            stop_reader.set()
                            if reader_thread:
                                reader_thread.join(timeout=1)
                            serial_connection.close()
                            serial_connection = None
                            logger.info(f"Disconnected from {connected_device}")

                    connected_device = None
                    ws.send(json.dumps({
                        'type': 'status',
                        'connected': False,
                        'device': None
                    }))

                elif msg_type == 'resize':
                    # Terminal resize - we don't need to do anything for serial
                    # but log it for debugging
                    cols = msg.get('cols', 80)
                    rows = msg.get('rows', 24)
                    logger.debug(f"Terminal resize: {cols}x{rows}")

                elif msg_type == 'list_devices':
                    ws.send(json.dumps({
                        'type': 'devices',
                        'list': list_serial_devices()
                    }))

            except json.JSONDecodeError:
                # Raw terminal input - send to serial port
                if connected_device:
                    with serial_lock:
                        if serial_connection and serial_connection.is_open:
                            try:
                                # Convert CR to CR for serial terminal (Enter key)
                                data = message.replace('\r\n', '\r').replace('\n', '\r')
                                serial_connection.write(data.encode('utf-8'))
                            except Exception as e:
                                logger.error(f"Serial write error: {e}")

    except Exception as e:
        logger.error(f"WebSocket error: {e}")
    finally:
        # Cleanup on disconnect
        stop_reader.set()
        if reader_thread:
            reader_thread.join(timeout=1)
        with serial_lock:
            if serial_connection and serial_connection.is_open:
                serial_connection.close()
                serial_connection = None
        logger.info("WebSocket client disconnected")


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


def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description='Web Terminal - Serial Console over WebSocket')
    parser.add_argument('--config', type=str,
                        default=os.path.join(APP_CONFIG_DIR, 'config.json'),
                        help='Path to configuration file')
    parser.add_argument('--port', type=int, default=DEFAULT_PORT,
                        help=f'Port to listen on (default: {DEFAULT_PORT})')
    parser.add_argument('--host', type=str, default=DEFAULT_HOST,
                        help=f'Host to bind to (default: {DEFAULT_HOST})')
    parser.add_argument('--static', type=str, default=STATIC_DIR,
                        help=f'Static files directory (default: {STATIC_DIR})')
    return parser.parse_args()


def main():
    """Main entry point."""
    global STATIC_DIR

    args = parse_args()
    STATIC_DIR = args.static

    # Setup signal handlers
    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)

    # Load configuration
    load_config(args.config)

    # Create data directories
    for dir_path in [APP_DATA_DIR, APP_CONFIG_DIR]:
        if not os.path.exists(dir_path):
            try:
                os.makedirs(dir_path, exist_ok=True)
                logger.info(f"Created directory: {dir_path}")
            except Exception as e:
                logger.warning(f"Could not create directory {dir_path}: {e}")

    logger.info(f"Web Terminal server starting on http://{args.host}:{args.port}")
    logger.info(f"Static files: {STATIC_DIR}")
    logger.info(f"Auth required: {config.get('auth_required', False)}")

    # Run Flask app
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == '__main__':
    main()
