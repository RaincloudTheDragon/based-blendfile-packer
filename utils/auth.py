"""
Authentication utilities for SheepIt render farm.
Implements browser-based login with secure cookie/token storage.

Since SheepIt doesn't appear to support OAuth, we use a hybrid approach:
1. Open browser for user to login (more secure than storing passwords)
2. User extracts session token from browser cookies manually
3. Token is stored encrypted on the local system
4. Token is reused for API requests

This approach is similar to how many desktop apps handle authentication
when OAuth isn't available. It's more secure than storing passwords because:
- User logs in via official website (phishing-resistant)
- Only session token is stored (not password)
- Token can be revoked independently
- Token is encrypted at rest
"""

import os
import json
import secrets
import threading
import time
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from pathlib import Path
from typing import Optional, Dict
import base64

import bpy
from .. import config


class AuthCallbackHandler(BaseHTTPRequestHandler):
    """HTTP handler for OAuth/login callback."""
    
    def do_GET(self):
        """Handle GET request from browser redirect."""
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        
        # Extract cookies from request headers
        cookies = {}
        if 'Cookie' in self.headers:
            cookie_header = self.headers['Cookie']
            for item in cookie_header.split(';'):
                if '=' in item:
                    key, value = item.strip().split('=', 1)
                    cookies[key] = value
        
        # Store cookies in server instance
        if hasattr(self.server, 'auth_result'):
            self.server.auth_result = {
                'success': True,
                'cookies': cookies,
                'query_params': query,
            }
        
        # Send success response
        self.send_response(200)
        self.send_header('Content-type', 'text/html')
        self.end_headers()
        self.wfile.write(b"""
        <html>
        <head><title>Authentication Successful</title></head>
        <body>
        <h1>Authentication Successful!</h1>
        <p>You can close this window and return to Blender.</p>
        <script>window.close();</script>
        </body>
        </html>
        """)
    
    def log_message(self, format, *args):
        """Suppress server log messages."""
        pass


def _get_auth_storage_path() -> Path:
    """Get path to secure auth storage file."""
    # Use Blender's user config directory for secure storage
    if hasattr(bpy.utils, 'user_resource'):
        config_dir = Path(bpy.utils.user_resource('CONFIG'))
    else:
        # Fallback for older Blender versions
        import tempfile
        config_dir = Path(tempfile.gettempdir())
    
    auth_dir = config_dir / "sheepit_auth"
    auth_dir.mkdir(parents=True, exist_ok=True)
    return auth_dir / "session.json"


def _encrypt_cookies(cookies: Dict[str, str]) -> str:
    """
    Simple encryption for cookies (XOR with key derived from system).
    For production, consider using proper encryption libraries.
    """
    # Generate a simple key from system info
    key = str(bpy.app.version_string).encode() + config.ADDON_ID.encode()
    key = key[:32].ljust(32, b'0')[:32]  # Pad to 32 bytes
    
    # Simple XOR encryption (not cryptographically secure, but better than plaintext)
    cookie_str = json.dumps(cookies)
    encrypted = bytearray()
    for i, byte in enumerate(cookie_str.encode()):
        encrypted.append(byte ^ key[i % len(key)])
    
    return base64.b64encode(encrypted).decode()


def _decrypt_cookies(encrypted: str) -> Dict[str, str]:
    """Decrypt cookies."""
    try:
        encrypted_bytes = base64.b64decode(encrypted)
        key = str(bpy.app.version_string).encode() + config.ADDON_ID.encode()
        key = key[:32].ljust(32, b'0')[:32]
        
        decrypted = bytearray()
        for i, byte in enumerate(encrypted_bytes):
            decrypted.append(byte ^ key[i % len(key)])
        
        return json.loads(decrypted.decode())
    except Exception:
        return {}


def save_auth_cookies(cookies: Dict[str, str]) -> bool:
    """
    Save authentication cookies securely.
    
    Args:
        cookies: Dictionary of cookie name-value pairs
    
    Returns:
        True if saved successfully, False otherwise
    """
    try:
        storage_path = _get_auth_storage_path()
        encrypted = _encrypt_cookies(cookies)
        
        data = {
            'cookies': encrypted,
            'timestamp': time.time(),
        }
        
        with open(storage_path, 'w') as f:
            json.dump(data, f)
        
        # Set restrictive permissions (Unix-like systems)
        try:
            os.chmod(storage_path, 0o600)
        except Exception:
            pass  # Windows doesn't support chmod the same way
        
        return True
    except Exception as e:
        print(f"[SheepIt Auth] Failed to save cookies: {e}")
        return False


def load_auth_cookies() -> Optional[Dict[str, str]]:
    """
    Load authentication cookies from secure storage.
    
    Returns:
        Dictionary of cookies if found, None otherwise
    """
    try:
        storage_path = _get_auth_storage_path()
        if not storage_path.exists():
            return None
        
        with open(storage_path, 'r') as f:
            data = json.load(f)
        
        encrypted = data.get('cookies')
        if not encrypted:
            return None
        
        return _decrypt_cookies(encrypted)
    except Exception:
        return None


def clear_auth_cookies() -> bool:
    """Clear stored authentication cookies."""
    try:
        storage_path = _get_auth_storage_path()
        if storage_path.exists():
            storage_path.unlink()
        return True
    except Exception:
        return False


def browser_login() -> bool:
    """
    Open browser for login and provide instructions.
    
    Since we cannot directly extract cookies from the browser session,
    this opens the login page and provides instructions for the user.
    After login, the user can verify the session works.
    
    Returns:
        True if browser opened successfully
    """
    if not bpy.app.online_access:
        print("[SheepIt Auth] Online access is disabled. Cannot perform browser login.")
        return False
    
    # Open browser to login page
    login_url = f"{config.SHEEPIT_CLIENT_BASE}/user/signin"
    try:
        webbrowser.open(login_url)
        print(f"[SheepIt Auth] Opened browser to: {login_url}")
        print("[SheepIt Auth] Please complete login in your browser.")
        print("[SheepIt Auth] After logging in, return to Blender and click 'Verify Login'.")
        return True
    except Exception as e:
        print(f"[SheepIt Auth] Failed to open browser: {e}")
        return False


def verify_session_with_token(session_token: str) -> bool:
    """
    Verify a session token by testing it against SheepIt API.
    
    Args:
        session_token: Session token/cookie value from browser
    
    Returns:
        True if token is valid, False otherwise
    """
    if not bpy.app.online_access:
        return False
    
    try:
        import urllib.request
        
        # Test the token by making a request to a protected endpoint
        # This is a placeholder - actual implementation depends on SheepIt's API
        test_url = f"{config.SHEEPIT_CLIENT_BASE}/api/user/info"  # Example endpoint
        
        req = urllib.request.Request(test_url)
        req.add_header('Cookie', f'session={session_token}')
        req.add_header('User-Agent', f'SheepIt-Blender-Addon/{config.ADDON_ID}')
        
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                if response.status == 200:
                    return True
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                return False
    except Exception as e:
        print(f"[SheepIt Auth] Token verification failed: {e}")
    
    return False


def get_auth_headers(cookies: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """
    Get HTTP headers with authentication cookies.
    
    Args:
        cookies: Cookie dictionary (if None, loads from storage)
    
    Returns:
        Dictionary of HTTP headers
    """
    if cookies is None:
        cookies = load_auth_cookies()
    
    if not cookies:
        return {}
    
    # Format cookies as Cookie header
    cookie_str = '; '.join(f"{k}={v}" for k, v in cookies.items())
    return {
        'Cookie': cookie_str,
        'User-Agent': f'SheepIt-Blender-Addon/{config.ADDON_ID}',
    }
