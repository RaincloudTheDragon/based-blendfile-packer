"""
API submission functions for SheepIt render farm.
Handles file upload (ZIP or blend) to SheepIt API.
"""

import os
from pathlib import Path
from typing import Optional, Dict, Tuple
import urllib.request
import urllib.error
import urllib.parse

import bpy
from .. import config
from ..utils.auth import get_auth_headers, load_auth_cookies


def submit_file_to_sheepit(
    file_path: Path,
    submit_settings,
    auth_cookies: Optional[Dict[str, str]] = None,
    username: Optional[str] = None,
    password: Optional[str] = None
) -> Tuple[bool, str]:
    """
    Submit a file (ZIP or blend) to SheepIt render farm via API.
    
    Args:
        file_path: Path to the file to submit (ZIP or .blend)
        submit_settings: Scene submit settings (frame range, compute method, etc.)
        auth_cookies: Authentication cookies (if using browser login)
        username: Username (if using username/password)
        password: Password (if using username/password)
    
    Returns:
        Tuple of (success: bool, message: str)
    """
    print(f"[SheepIt API] Starting file submission...")
    print(f"[SheepIt API] File: {file_path}")
    
    if not file_path.exists():
        error_msg = f"File does not exist: {file_path}"
        print(f"[SheepIt API] ERROR: {error_msg}")
        return False, error_msg
    
    file_size = file_path.stat().st_size
    file_size_mb = file_size / (1024 * 1024)
    print(f"[SheepIt API] File size: {file_size_mb:.2f} MB ({file_size:,} bytes)")
    
    # Get authentication headers
    if auth_cookies:
        print(f"[SheepIt API] Using browser login cookies")
        headers = get_auth_headers(auth_cookies)
    elif username and password:
        print(f"[SheepIt API] Using username/password authentication")
        # For username/password, we need to login first to get session cookies
        # Use the same login flow as _test_with_credentials but get cookies directly
        import urllib.request
        import urllib.error
        import urllib.parse
        import http.cookiejar
        
        cookie_jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))
        
        login_url = f"{config.SHEEPIT_CLIENT_BASE}/user/signin"
        
        # Get login page for CSRF token
        try:
            req = urllib.request.Request(login_url)
            req.add_header('User-Agent', f'SheepIt-Blender-Addon/{config.ADDON_ID}')
            with opener.open(req, timeout=10) as response:
                login_page = response.read().decode('utf-8', errors='ignore')
        except Exception as e:
            error_msg = f"Failed to load login page: {str(e)}"
            print(f"[SheepIt API] ERROR: {error_msg}")
            return False, error_msg
        
        # Prepare login data
        login_data = {'username': username, 'password': password}
        
        # Look for CSRF token
        import re
        csrf_match = re.search(r'name=["\']csrf_token["\']\s+value=["\']([^"\']+)["\']', login_page, re.IGNORECASE)
        if csrf_match:
            login_data['csrf_token'] = csrf_match.group(1)
        
        # Submit login form
        data = urllib.parse.urlencode(login_data).encode('utf-8')
        req = urllib.request.Request(login_url, data=data)
        req.add_header('User-Agent', f'SheepIt-Blender-Addon/{config.ADDON_ID}')
        req.add_header('Content-Type', 'application/x-www-form-urlencoded')
        req.add_header('Referer', login_url)
        
        try:
            with opener.open(req, timeout=10) as response:
                final_url = response.geturl()
                # Check if login was successful
                if '/user/signin' in final_url or '/user/login' in final_url:
                    error_msg = "Login failed: Invalid username or password"
                    print(f"[SheepIt API] ERROR: {error_msg}")
                    return False, error_msg
                
                # Extract cookies from cookie jar
                cookies = {}
                for cookie in cookie_jar:
                    cookies[cookie.name] = cookie.value
                
                if not cookies:
                    error_msg = "Login succeeded but no session cookies received"
                    print(f"[SheepIt API] ERROR: {error_msg}")
                    return False, error_msg
                
                print(f"[SheepIt API] Login successful, got cookies: {list(cookies.keys())}")
                headers = get_auth_headers(cookies)
        except urllib.error.HTTPError as e:
            error_msg = f"Login failed with HTTP {e.code}"
            print(f"[SheepIt API] ERROR: {error_msg}")
            return False, error_msg
        except Exception as e:
            error_msg = f"Login failed: {type(e).__name__}: {str(e)}"
            print(f"[SheepIt API] ERROR: {error_msg}")
            return False, error_msg
    else:
        error_msg = "No authentication method provided"
        print(f"[SheepIt API] ERROR: {error_msg}")
        return False, error_msg
    
    # Prepare frame range
    if submit_settings.frame_range_mode == 'FULL':
        frame_start = bpy.context.scene.frame_start
        frame_end = bpy.context.scene.frame_end
        frame_step = bpy.context.scene.frame_step
    else:
        frame_start = submit_settings.frame_start
        frame_end = submit_settings.frame_end
        frame_step = submit_settings.frame_step
    
    print(f"[SheepIt API] Frame range: {frame_start} - {frame_end} (step: {frame_step})")
    print(f"[SheepIt API] Compute method: {submit_settings.compute_method}")
    print(f"[SheepIt API] Renderable by all: {submit_settings.renderable_by_all}")
    print(f"[SheepIt API] Generate MP4: {submit_settings.generate_mp4}")
    if submit_settings.memory_used_mb:
        print(f"[SheepIt API] Memory used: {submit_settings.memory_used_mb} MB")
    
    # Prepare multipart/form-data
    # Note: API endpoint needs to be discovered - using placeholder for now
    api_endpoint = f"{config.SHEEPIT_CLIENT_BASE}/api/project/submit"
    print(f"[SheepIt API] Submitting to: {api_endpoint}")
    
    try:
        # Create multipart form data using proper encoding
        boundary = '----WebKitFormBoundary' + os.urandom(16).hex()
        
        # Build form data parts
        body_parts = []
        crlf = b'\r\n'
        
        # File field
        body_parts.append(f'--{boundary}'.encode('utf-8'))
        body_parts.append(crlf)
        body_parts.append(f'Content-Disposition: form-data; name="file"; filename="{file_path.name}"'.encode('utf-8'))
        body_parts.append(crlf)
        body_parts.append(b'Content-Type: application/octet-stream')
        body_parts.append(crlf)
        body_parts.append(crlf)
        
        # Read and add file content
        print(f"[SheepIt API] Reading file content...")
        with open(file_path, 'rb') as f:
            file_content = f.read()
        body_parts.append(file_content)
        body_parts.append(crlf)
        
        # Form fields
        form_fields = {
            'frame_start': str(frame_start),
            'frame_end': str(frame_end),
            'frame_step': str(frame_step),
            'compute_method': submit_settings.compute_method,
            'renderable_by_all': '1' if submit_settings.renderable_by_all else '0',
            'generate_mp4': '1' if submit_settings.generate_mp4 else '0',
        }
        
        if submit_settings.memory_used_mb:
            form_fields['memory_used_mb'] = submit_settings.memory_used_mb
        
        for key, value in form_fields.items():
            body_parts.append(f'--{boundary}'.encode('utf-8'))
            body_parts.append(crlf)
            body_parts.append(f'Content-Disposition: form-data; name="{key}"'.encode('utf-8'))
            body_parts.append(crlf)
            body_parts.append(crlf)
            body_parts.append(str(value).encode('utf-8'))
            body_parts.append(crlf)
        
        # Close boundary
        body_parts.append(f'--{boundary}--'.encode('utf-8'))
        body_parts.append(crlf)
        
        # Build request body
        request_body = b''.join(body_parts)
        
        # Set content type header
        headers['Content-Type'] = f'multipart/form-data; boundary={boundary}'
        
        print(f"[SheepIt API] Request body size: {len(request_body) / (1024*1024):.2f} MB")
        print(f"[SheepIt API] Sending POST request...")
        
        # Create request
        req = urllib.request.Request(api_endpoint, data=request_body, headers=headers, method='POST')
        
        # Make request
        try:
            with urllib.request.urlopen(req, timeout=300) as response:  # 5 minute timeout for large files
                response_data = response.read().decode('utf-8', errors='ignore')
                print(f"[SheepIt API] Response status: {response.status}")
                print(f"[SheepIt API] Response: {response_data[:500]}...")  # First 500 chars
                
                if response.status == 200:
                    success_msg = f"Project submitted successfully! Response: {response_data[:200]}"
                    print(f"[SheepIt API] SUCCESS: {success_msg}")
                    return True, success_msg
                else:
                    error_msg = f"Submission failed with status {response.status}: {response_data[:200]}"
                    print(f"[SheepIt API] ERROR: {error_msg}")
                    return False, error_msg
                    
        except urllib.error.HTTPError as e:
            error_body = e.read().decode('utf-8', errors='ignore') if hasattr(e, 'read') else str(e)
            error_msg = f"HTTP error {e.code}: {error_body[:200]}"
            print(f"[SheepIt API] ERROR: {error_msg}")
            return False, error_msg
            
    except urllib.error.URLError as e:
        error_msg = f"Network error: {str(e)}"
        print(f"[SheepIt API] ERROR: {error_msg}")
        return False, error_msg
    except Exception as e:
        error_msg = f"Submission failed: {type(e).__name__}: {str(e)}"
        print(f"[SheepIt API] ERROR: {error_msg}")
        import traceback
        traceback.print_exc()
        return False, error_msg
