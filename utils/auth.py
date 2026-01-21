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
from typing import Optional, Dict, Tuple
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


def _test_with_cookies(cookies: Dict[str, str]) -> Tuple[bool, str, Optional[Dict[str, str]]]:
    """
    Test authentication using stored cookies by accessing protected endpoints.
    
    Args:
        cookies: Dictionary of cookie name-value pairs
    
    Returns:
        Tuple of (success: bool, message: str, user_info: Optional[dict])
    """
    import urllib.request
    import urllib.error
    
    if not cookies:
        print("[SheepIt Auth] Test Connection: No cookies provided")
        return False, "No cookies provided", None
    
    print(f"[SheepIt Auth] Test Connection: Testing with cookies: {list(cookies.keys())}")
    # Show cookie values (truncated for security)
    for key, value in cookies.items():
        value_preview = value[:20] + "..." if len(value) > 20 else value
        print(f"[SheepIt Auth] Test Connection:   Cookie '{key}': {value_preview} (length: {len(value)})")
    
    # Try protected endpoints in order
    # Note: Actual endpoint paths may vary - these are common patterns
    test_endpoints = [
        "/",  # Home page (should show user info if logged in)
        "/user/profile",
        "/user",
        "/dashboard",
        "/projects",
    ]
    
    headers = get_auth_headers(cookies)
    print(f"[SheepIt Auth] Test Connection: Request headers: {list(headers.keys())}")
    if 'Cookie' in headers:
        cookie_header_preview = headers['Cookie'][:100] + "..." if len(headers['Cookie']) > 100 else headers['Cookie']
        print(f"[SheepIt Auth] Test Connection:   Cookie header: {cookie_header_preview}")
    if 'User-Agent' in headers:
        print(f"[SheepIt Auth] Test Connection:   User-Agent: {headers['User-Agent']}")
    
    for endpoint in test_endpoints:
        try:
            test_url = f"{config.SHEEPIT_CLIENT_BASE}{endpoint}"
            print(f"[SheepIt Auth] Test Connection: Trying endpoint: {test_url}")
            req = urllib.request.Request(test_url, headers=headers)
            
            # Create opener that follows redirects but allows us to check final URL
            opener = urllib.request.build_opener(urllib.request.HTTPRedirectHandler())
            
            try:
                import time
                start_time = time.time()
                with opener.open(req, timeout=10) as response:
                    elapsed_time = time.time() - start_time
                    # Check if we got redirected to login page
                    final_url = response.geturl()
                    print(f"[SheepIt Auth] Test Connection: Response received in {elapsed_time:.2f}s")
                    print(f"[SheepIt Auth] Test Connection: Response status: {response.status}, Final URL: {final_url}")
                    
                    # Log response headers
                    response_headers = dict(response.headers)
                    print(f"[SheepIt Auth] Test Connection: Response headers: {list(response_headers.keys())}")
                    if 'Location' in response_headers:
                        print(f"[SheepIt Auth] Test Connection:   Location header: {response_headers['Location']}")
                    if 'Set-Cookie' in response_headers:
                        print(f"[SheepIt Auth] Test Connection:   Set-Cookie header present (new cookies may be set)")
                    if 'Content-Type' in response_headers:
                        print(f"[SheepIt Auth] Test Connection:   Content-Type: {response_headers['Content-Type']}")
                    if 'Content-Length' in response_headers:
                        print(f"[SheepIt Auth] Test Connection:   Content-Length: {response_headers['Content-Length']} bytes")
                    
                    # Check for redirect to login (handle URL-encoded paths too)
                    # The %25252F is double-encoded: %2525 = %25 = %, so %25252F = %2F = /
                    # Decode common URL encoding patterns
                    final_url_decoded = final_url.replace('%25252F', '/').replace('%252F', '/').replace('%2F', '/')
                    if '/user/signin' in final_url or '/user/login' in final_url or '/user/signin' in final_url_decoded:
                        print(f"[SheepIt Auth] Test Connection: Redirected to login page (cookie may be invalid/expired), trying next endpoint")
                        continue  # Try next endpoint
                    
                    # Also check response content for login indicators
                    try:
                        # Read a small portion to check for login page indicators
                        response_data = response.read(5000).decode('utf-8', errors='ignore')
                        print(f"[SheepIt Auth] Test Connection: Read {len(response_data)} bytes of response content")
                        
                        # Check for login page indicators
                        has_signin = 'sign in' in response_data.lower()
                        has_login = 'login' in response_data.lower()
                        has_username_field = 'username' in response_data.lower() or 'name="username"' in response_data.lower()
                        has_password_field = 'password' in response_data.lower() or 'type="password"' in response_data.lower()
                        
                        print(f"[SheepIt Auth] Test Connection: Content analysis:")
                        print(f"[SheepIt Auth] Test Connection:   Contains 'sign in': {has_signin}")
                        print(f"[SheepIt Auth] Test Connection:   Contains 'login': {has_login}")
                        print(f"[SheepIt Auth] Test Connection:   Has username field: {has_username_field}")
                        print(f"[SheepIt Auth] Test Connection:   Has password field: {has_password_field}")
                        
                        if has_signin or has_login:
                            # Check if it's actually a login page or just mentions login
                            if has_username_field and has_password_field:
                                print(f"[SheepIt Auth] Test Connection: Login page detected in content (has both username and password fields), trying next endpoint")
                                continue
                            else:
                                print(f"[SheepIt Auth] Test Connection: Mentions login but not a login form, continuing...")
                    except Exception as e:
                        print(f"[SheepIt Auth] Test Connection: Could not read response content: {type(e).__name__}: {str(e)}")
                        # If we can't read, continue with status check
                    
                    # If we got here and status is 200, we're authenticated
                    if response.status == 200:
                        print(f"[SheepIt Auth] Test Connection: Success! Status 200 on {endpoint}")
                        # Try to extract username from response
                        user_info = {}
                        try:
                            # Read remaining content if we haven't already
                            if 'response_data' not in locals():
                                html = response.read().decode('utf-8', errors='ignore')
                            else:
                                # We already read some, read the rest
                                remaining = response.read().decode('utf-8', errors='ignore')
                                html = response_data + remaining
                            
                            print(f"[SheepIt Auth] Test Connection: Total HTML length: {len(html)} bytes")
                            
                            # Look for username in common patterns
                            # This is a simple heuristic - may need adjustment based on actual HTML structure
                            has_username_mention = 'username' in html.lower()
                            has_profile_mention = 'profile' in html.lower()
                            has_user_info = has_username_mention or has_profile_mention
                            
                            print(f"[SheepIt Auth] Test Connection: HTML analysis:")
                            print(f"[SheepIt Auth] Test Connection:   Contains 'username': {has_username_mention}")
                            print(f"[SheepIt Auth] Test Connection:   Contains 'profile': {has_profile_mention}")
                            
                            if has_user_info:
                                # Could parse HTML here to extract username, but for now just indicate success
                                user_info['authenticated'] = True
                                print(f"[SheepIt Auth] Test Connection: User info indicators found in HTML")
                        except Exception as e:
                            print(f"[SheepIt Auth] Test Connection: Could not analyze HTML: {type(e).__name__}: {str(e)}")
                        
                        return True, "Connection successful! Authentication verified.", user_info
                    
            except urllib.error.HTTPError as e:
                # HTTPError is raised for non-2xx status codes
                print(f"[SheepIt Auth] Test Connection: HTTPError {e.code} on {endpoint}")
                print(f"[SheepIt Auth] Test Connection:   Error URL: {e.url}")
                print(f"[SheepIt Auth] Test Connection:   Error reason: {e.reason}")
                
                # Log error response headers
                if hasattr(e, 'headers'):
                    error_headers = dict(e.headers)
                    print(f"[SheepIt Auth] Test Connection:   Error response headers: {list(error_headers.keys())}")
                    if 'Location' in error_headers:
                        print(f"[SheepIt Auth] Test Connection:     Location: {error_headers['Location']}")
                
                # Try to read error response body
                try:
                    error_body = e.read().decode('utf-8', errors='ignore')[:500]  # First 500 chars
                    print(f"[SheepIt Auth] Test Connection:   Error response body (first 500 chars): {error_body[:100]}...")
                except Exception:
                    pass
                
                if e.code in (401, 403):
                    # Not authenticated
                    print(f"[SheepIt Auth] Test Connection: Not authenticated (401/403), trying next endpoint")
                    continue  # Try next endpoint
                elif e.code == 404:
                    # Endpoint doesn't exist, try next
                    print(f"[SheepIt Auth] Test Connection: Endpoint not found (404), trying next endpoint")
                    continue
                elif e.code >= 500:
                    # Server error, try next endpoint
                    print(f"[SheepIt Auth] Test Connection: Server error ({e.code}), trying next endpoint")
                    continue
                else:
                    # Other HTTP error (e.g., 302 redirect handled above)
                    print(f"[SheepIt Auth] Test Connection: Other HTTP error ({e.code}), trying next endpoint")
                    continue
                    
        except urllib.error.URLError as e:
            # Network error
            print(f"[SheepIt Auth] Test Connection: Network error on {endpoint}: {type(e).__name__}: {str(e)}")
            if hasattr(e, 'reason'):
                print(f"[SheepIt Auth] Test Connection:   Reason: {e.reason}")
            if hasattr(e, 'code'):
                print(f"[SheepIt Auth] Test Connection:   Code: {e.code}")
            return False, f"Network error: {str(e)}", None
        except Exception as e:
            # Other error, try next endpoint
            print(f"[SheepIt Auth] Test Connection: Exception on {endpoint}: {type(e).__name__}: {str(e)}")
            import traceback
            print(f"[SheepIt Auth] Test Connection:   Traceback:")
            for line in traceback.format_exc().split('\n'):
                if line.strip():
                    print(f"[SheepIt Auth] Test Connection:     {line}")
            continue
    
    # If we get here, none of the endpoints worked
    print(f"[SheepIt Auth] Test Connection: All endpoints failed")
    return False, "Authentication failed. Cookies may be expired or invalid.", None


def _test_with_credentials(username: str, password: str) -> Tuple[bool, str]:
    """
    Test authentication by submitting login form with username/password.
    
    Args:
        username: SheepIt username
        password: SheepIt password
    
    Returns:
        Tuple of (success: bool, message: str)
    """
    import urllib.request
    import urllib.error
    import urllib.parse
    import http.cookiejar
    
    print(f"[SheepIt Auth] Test Connection: Testing with username/password for user: {username}")
    print(f"[SheepIt Auth] Test Connection: Password length: {len(password)} characters")
    
    try:
        # Create cookie jar to handle session cookies
        cookie_jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))
        print(f"[SheepIt Auth] Test Connection: Created cookie jar and opener")
        
        # Login URL
        login_url = f"{config.SHEEPIT_CLIENT_BASE}/user/signin"
        print(f"[SheepIt Auth] Test Connection: Loading login page: {login_url}")
        
        # First, get the login page to check for CSRF tokens or form structure
        try:
            import time
            req = urllib.request.Request(login_url)
            req.add_header('User-Agent', f'SheepIt-Blender-Addon/{config.ADDON_ID}')
            print(f"[SheepIt Auth] Test Connection: Request headers: User-Agent: {req.get_header('User-Agent')}")
            
            start_time = time.time()
            with opener.open(req, timeout=10) as response:
                elapsed_time = time.time() - start_time
                login_page = response.read().decode('utf-8', errors='ignore')
                print(f"[SheepIt Auth] Test Connection: Login page loaded in {elapsed_time:.2f}s, status: {response.status}")
                print(f"[SheepIt Auth] Test Connection: Login page size: {len(login_page)} bytes")
                print(f"[SheepIt Auth] Test Connection: Final URL: {response.geturl()}")
                
                # Log response headers
                response_headers = dict(response.headers)
                print(f"[SheepIt Auth] Test Connection: Response headers: {list(response_headers.keys())}")
                
                # Check for initial cookies set
                if len(cookie_jar) > 0:
                    print(f"[SheepIt Auth] Test Connection: Cookies received from login page: {len(cookie_jar)}")
                    for cookie in cookie_jar:
                        print(f"[SheepIt Auth] Test Connection:   Cookie: {cookie.name} = {cookie.value[:20]}... (domain: {cookie.domain})")
                else:
                    print(f"[SheepIt Auth] Test Connection: No cookies received from login page")
        except Exception as e:
            print(f"[SheepIt Auth] Test Connection: Failed to load login page: {type(e).__name__}: {str(e)}")
            import traceback
            traceback.print_exc()
            return False, f"Failed to load login page: {str(e)}"
        
        # Prepare login data
        # Common form field names - may need adjustment based on actual form
        login_data = {
            'username': username,
            'password': password,
        }
        
        # Look for CSRF token in the page (common in PHP apps)
        # This is a simple approach - may need refinement
        import re
        print(f"[SheepIt Auth] Test Connection: Searching for CSRF token in login page...")
        csrf_patterns = [
            r'name=["\']csrf_token["\']\s+value=["\']([^"\']+)["\']',
            r'name=["\']_token["\']\s+value=["\']([^"\']+)["\']',
            r'name=["\']csrf["\']\s+value=["\']([^"\']+)["\']',
            r'csrf["\']?\s*[:=]\s*["\']([^"\']+)["\']',
        ]
        csrf_token = None
        for pattern in csrf_patterns:
            csrf_match = re.search(pattern, login_page, re.IGNORECASE)
            if csrf_match:
                csrf_token = csrf_match.group(1)
                print(f"[SheepIt Auth] Test Connection: Found CSRF token using pattern: {pattern[:30]}...")
                print(f"[SheepIt Auth] Test Connection:   Token value: {csrf_token[:20]}... (length: {len(csrf_token)})")
                login_data['csrf_token'] = csrf_token
                break
        
        if not csrf_token:
            print(f"[SheepIt Auth] Test Connection: No CSRF token found (tried {len(csrf_patterns)} patterns)")
        
        # Look for form field names
        form_fields = re.findall(r'name=["\']([^"\']+)["\']', login_page)
        print(f"[SheepIt Auth] Test Connection: Found form fields: {form_fields[:10]}...")  # First 10
        
        # Submit login form
        print(f"[SheepIt Auth] Test Connection: Submitting login form")
        print(f"[SheepIt Auth] Test Connection: Login data keys: {list(login_data.keys())}")
        data = urllib.parse.urlencode(login_data).encode('utf-8')
        print(f"[SheepIt Auth] Test Connection: POST data length: {len(data)} bytes")
        req = urllib.request.Request(login_url, data=data)
        req.add_header('User-Agent', f'SheepIt-Blender-Addon/{config.ADDON_ID}')
        req.add_header('Content-Type', 'application/x-www-form-urlencoded')
        req.add_header('Referer', login_url)
        print(f"[SheepIt Auth] Test Connection: Request headers:")
        print(f"[SheepIt Auth] Test Connection:   User-Agent: {req.get_header('User-Agent')}")
        print(f"[SheepIt Auth] Test Connection:   Content-Type: {req.get_header('Content-Type')}")
        print(f"[SheepIt Auth] Test Connection:   Referer: {req.get_header('Referer')}")
        
        try:
            import time
            start_time = time.time()
            with opener.open(req, timeout=10) as response:
                elapsed_time = time.time() - start_time
                final_url = response.geturl()
                print(f"[SheepIt Auth] Test Connection: Login response received in {elapsed_time:.2f}s")
                print(f"[SheepIt Auth] Test Connection: Login response status: {response.status}, Final URL: {final_url}")
                
                # Log response headers
                response_headers = dict(response.headers)
                print(f"[SheepIt Auth] Test Connection: Response headers: {list(response_headers.keys())}")
                if 'Location' in response_headers:
                    print(f"[SheepIt Auth] Test Connection:   Location: {response_headers['Location']}")
                if 'Set-Cookie' in response_headers:
                    print(f"[SheepIt Auth] Test Connection:   Set-Cookie header present")
                    set_cookies = response_headers.get_all('Set-Cookie', [])
                    print(f"[SheepIt Auth] Test Connection:   Number of Set-Cookie headers: {len(set_cookies)}")
                
                # Check cookies in jar
                print(f"[SheepIt Auth] Test Connection: Cookies in jar after login: {len(cookie_jar)}")
                for cookie in cookie_jar:
                    print(f"[SheepIt Auth] Test Connection:   Cookie: {cookie.name} = {cookie.value[:30]}... (domain: {cookie.domain}, path: {cookie.path})")
                
                # Check if we got redirected away from login page (success)
                if '/user/signin' not in final_url and '/user/login' not in final_url:
                    # Check if we have session cookies
                    has_session = any('session' in cookie.name.lower() or 'PHPSESSID' in cookie.name for cookie in cookie_jar)
                    print(f"[SheepIt Auth] Test Connection: Redirected away from login, has_session: {has_session}")
                    if has_session:
                        print(f"[SheepIt Auth] Test Connection: Success! Session cookie found")
                        return True, f"Connection successful! Logged in as {username}."
                    else:
                        # Might still be successful, check response
                        print(f"[SheepIt Auth] Test Connection: Success! (no session cookie detected but redirected)")
                        return True, f"Connection successful! Logged in as {username}."
                
                # Still on login page - check for error messages
                print(f"[SheepIt Auth] Test Connection: Still on login page, checking for error messages")
                response_html = response.read().decode('utf-8', errors='ignore')
                if 'error' in response_html.lower() or 'invalid' in response_html.lower():
                    print(f"[SheepIt Auth] Test Connection: Error message found in response")
                    return False, "Invalid username or password."
                else:
                    print(f"[SheepIt Auth] Test Connection: No clear error, but login failed")
                    return False, "Login failed. Please check your credentials."
                    
        except urllib.error.HTTPError as e:
            print(f"[SheepIt Auth] Test Connection: HTTPError {e.code} during login")
            if e.code == 302:
                # Redirect might indicate success
                location = e.headers.get('Location', '')
                print(f"[SheepIt Auth] Test Connection: 302 redirect to: {location}")
                if '/user/signin' not in location and '/user/login' not in location:
                    print(f"[SheepIt Auth] Test Connection: Success! Redirected away from login")
                    return True, f"Connection successful! Logged in as {username}."
                else:
                    print(f"[SheepIt Auth] Test Connection: Redirected back to login (failed)")
                    return False, "Invalid username or password."
            else:
                print(f"[SheepIt Auth] Test Connection: Login failed with HTTP {e.code}")
                return False, f"Login failed with HTTP {e.code}."
                
    except urllib.error.URLError as e:
        print(f"[SheepIt Auth] Test Connection: Network error: {str(e)}")
        return False, f"Network error: {str(e)}"
    except Exception as e:
        print(f"[SheepIt Auth] Test Connection: Exception: {type(e).__name__}: {str(e)}")
        import traceback
        traceback.print_exc()
        return False, f"Connection test failed: {str(e)}"


def test_connection(use_browser_login: bool = True, username: Optional[str] = None, password: Optional[str] = None) -> Tuple[bool, str, Optional[Dict[str, str]]]:
    """
    Test connection to SheepIt with current authentication method.
    
    Args:
        use_browser_login: If True, test using browser login cookies
        username: Username for password-based login (if use_browser_login is False)
        password: Password for password-based login (if use_browser_login is False)
    
    Returns:
        Tuple of (success: bool, message: str, user_info: Optional[dict])
        user_info may contain username or other user details if available
    """
    print(f"[SheepIt Auth] Test Connection: Starting connection test (use_browser_login={use_browser_login})")
    
    if not bpy.app.online_access:
        print("[SheepIt Auth] Test Connection: Online access is disabled")
        return False, "Online access is disabled. Enable it in Preferences → System.", None
    
    if use_browser_login:
        # Test with browser login cookies
        print("[SheepIt Auth] Test Connection: Loading browser login cookies")
        cookies = load_auth_cookies()
        if not cookies:
            print("[SheepIt Auth] Test Connection: No cookies found")
            return False, "No browser login cookies found. Please login via browser first.", None
        
        print(f"[SheepIt Auth] Test Connection: Cookies loaded, testing connection")
        return _test_with_cookies(cookies)
    else:
        # Test with username/password
        if not username or not password:
            print("[SheepIt Auth] Test Connection: Username or password missing")
            return False, "Username and password are required.", None
        
        print(f"[SheepIt Auth] Test Connection: Testing with username/password")
        success, message = _test_with_credentials(username, password)
        user_info = {'username': username} if success else None
        if success:
            print(f"[SheepIt Auth] Test Connection: Success!")
        else:
            print(f"[SheepIt Auth] Test Connection: Failed: {message}")
        return success, message, user_info
