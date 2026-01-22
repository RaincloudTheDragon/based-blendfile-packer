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
import webbrowser

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
    
    # Initialize cookie jar and opener (will be created if needed)
    cookie_jar = None
    session_opener = None
    
    # Get authentication headers
    if auth_cookies:
        print(f"[SheepIt API] Using browser login cookies")
        print(f"[SheepIt API] Cookie keys: {list(auth_cookies.keys())}")
        headers = get_auth_headers(auth_cookies)
        # Add Referer header (some servers require this for CSRF protection)
        headers['Referer'] = f"{config.SHEEPIT_CLIENT_BASE}/getstarted"
        print(f"[SheepIt API] Request headers: {list(headers.keys())}")
        # Don't print full cookie values for security, but show if they exist
        if 'Cookie' in headers:
            cookie_preview = headers['Cookie'][:100] + "..." if len(headers['Cookie']) > 100 else headers['Cookie']
            print(f"[SheepIt API] Cookie header preview: {cookie_preview}")
    elif username and password:
        print(f"[SheepIt API] Using username/password authentication")
        # For username/password, we need to login via /user/authenticate endpoint
        # This matches the JavaScript login flow (login.js)
        import http.cookiejar
        import time
        
        cookie_jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))
        session_opener = opener  # Store for reuse later
        
        # First, get the login page to establish session and get PHPSESSID
        login_page_url = f"{config.SHEEPIT_CLIENT_BASE}/user/signin"
        try:
            req = urllib.request.Request(login_page_url)
            req.add_header('User-Agent', f'SheepIt-Blender-Addon/{config.ADDON_ID}')
            with opener.open(req, timeout=10) as response:
                # Read response to ensure cookies are captured
                response.read()
                print(f"[SheepIt API] Loaded login page, got {len(cookie_jar)} cookies")
        except Exception as e:
            error_msg = f"Failed to load login page: {str(e)}"
            print(f"[SheepIt API] ERROR: {error_msg}")
            return False, error_msg
        
        # Prepare login data - use 'login' field name (not 'username') and include timezone
        # This matches the JavaScript login.js implementation
        # Get timezone - use UTC as default (server should handle it)
        # Note: JavaScript uses jstz.determine().name() which returns IANA timezone names
        # For now, we use UTC as a safe default
        timezone = 'UTC'
        print(f"[SheepIt API] Using timezone: {timezone} (default)")
        
        authenticate_url = f"{config.SHEEPIT_CLIENT_BASE}/user/authenticate"
        login_data = {
            'login': username,  # Note: field name is 'login', not 'username'
            'password': password,
            'timezone': timezone
        }
        
        # Submit login to /user/authenticate endpoint
        data = urllib.parse.urlencode(login_data).encode('utf-8')
        req = urllib.request.Request(authenticate_url, data=data, method='POST')
        req.add_header('User-Agent', f'SheepIt-Blender-Addon/{config.ADDON_ID}')
        req.add_header('Content-Type', 'application/x-www-form-urlencoded')
        req.add_header('Referer', login_page_url)
        req.add_header('X-Requested-With', 'XMLHttpRequest')  # Match AJAX request
        
        try:
            with opener.open(req, timeout=10) as response:
                response_text = response.read().decode('utf-8', errors='ignore')
                final_url = response.geturl()
                
                print(f"[SheepIt API] Authenticate response: status={response.status}, final_url={final_url}")
                print(f"[SheepIt API] Response text: {response_text[:100]}")
                
                # Check if login was successful
                # The endpoint returns 'OK' on success, or an error message
                if response_text.strip() == 'OK':
                    # Extract cookies from cookie jar
                    cookies = {}
                    for cookie in cookie_jar:
                        cookies[cookie.name] = cookie.value
                    
                    if not cookies:
                        error_msg = "Login succeeded but no session cookies received"
                        print(f"[SheepIt API] ERROR: {error_msg}")
                        return False, error_msg
                    
                    print(f"[SheepIt API] Login successful, got cookies: {list(cookies.keys())}")
                    # Set auth_cookies so the rest of the code can use them
                    auth_cookies = cookies
                    headers = get_auth_headers(cookies)
                    # Also keep the cookie jar and opener for reuse
                    # (cookie_jar and opener are already created above)
                else:
                    error_msg = f"Login failed: {response_text.strip()}"
                    print(f"[SheepIt API] ERROR: {error_msg}")
                    return False, error_msg
        except urllib.error.HTTPError as e:
            error_msg = f"Login failed with HTTP {e.code}: {e.read().decode('utf-8', errors='ignore')[:100]}"
            print(f"[SheepIt API] ERROR: {error_msg}")
            return False, error_msg
        except Exception as e:
            error_msg = f"Login failed: {type(e).__name__}: {str(e)}"
            print(f"[SheepIt API] ERROR: {error_msg}")
            import traceback
            traceback.print_exc()
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
    # First, get the /getstarted page to extract form field names and CSRF tokens
    # Use www subdomain to match manual browser test (user confirmed this works)
    getstarted_url = f"{config.SHEEPIT_API_BASE}/getstarted"
    print(f"[SheepIt API] Fetching form page: {getstarted_url}")
    
    # Initialize defaults
    api_endpoint = getstarted_url
    file_field_name = 'file'
    csrf_token = None
    
    # Create ONE cookie jar for the entire submission flow
    # This ensures session state is maintained across all requests
    # If we already have a cookie_jar from username/password login, reuse it
    import http.cookiejar
    if cookie_jar is None:
        cookie_jar = http.cookiejar.CookieJar()
        
        # Add our initial cookies to the jar (if we have auth_cookies)
        if auth_cookies:
            for name, value in auth_cookies.items():
                cookie = http.cookiejar.Cookie(
                    version=0, name=name, value=value,
                    port=None, port_specified=False,
                    domain='.sheepit-renderfarm.com', domain_specified=True, domain_initial_dot=True,
                    path='/', path_specified=True,
                    secure=True, expires=None, discard=False,
                    comment=None, comment_url=None,
                    rest={'HttpOnly': None}, rfc2109=False
                )
                cookie_jar.set_cookie(cookie)
    
    # Create opener that will be reused for all requests (if not already created)
    if session_opener is None:
        session_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))
    
    # WARM UP: First hit a protected endpoint to establish session for authenticated requests
    # /home is public, so try /project which requires authentication (like the upload endpoint)
    # Use www subdomain to match manual browser test (user confirmed this works)
    warmup_url = f"{config.SHEEPIT_API_BASE}/project"  # Protected endpoint - requires auth
    print(f"[SheepIt API] Warming up session with GET to protected endpoint: {warmup_url}...")
    print(f"[SheepIt API] Using cookie jar to capture session updates...")
    try:
        warmup_req = urllib.request.Request(warmup_url, headers=headers)
        with session_opener.open(warmup_req, timeout=10) as warmup_response:
            warmup_final_url = warmup_response.geturl()
            warmup_status = warmup_response.status
            print(f"[SheepIt API] Warmup response: status={warmup_status}, final_url={warmup_final_url}")
            
            # Read response body to fully establish session (Symfony might need this)
            warmup_body = warmup_response.read()
            print(f"[SheepIt API] Warmup response body length: {len(warmup_body)} bytes")
            
            # Check for Set-Cookie headers
            response_headers = dict(warmup_response.headers)
            if 'Set-Cookie' in response_headers:
                print(f"[SheepIt API] Set-Cookie from warmup: {response_headers['Set-Cookie'][:100]}...")
            
            # Check if session is valid (if redirected to sign-in, session is invalid)
            if 'signin' in warmup_final_url.lower() or 'login' in warmup_final_url.lower():
                # Session not valid for protected endpoint - try public endpoint to get PHPSESSID
                # This might establish a session that we can use
                print(f"[SheepIt API] Protected endpoint redirected to sign-in, trying public /home to establish session...")
                warmup_url_fallback = f"{config.SHEEPIT_API_BASE}/home"
                warmup_req_fallback = urllib.request.Request(warmup_url_fallback, headers=headers)
                with session_opener.open(warmup_req_fallback, timeout=10) as warmup_response_fallback:
                    warmup_final_url_fallback = warmup_response_fallback.geturl()
                    warmup_status_fallback = warmup_response_fallback.status
                    print(f"[SheepIt API] Fallback warmup response: status={warmup_status_fallback}, final_url={warmup_final_url_fallback}")
                    warmup_body_fallback = warmup_response_fallback.read()
                    print(f"[SheepIt API] Fallback warmup response body length: {len(warmup_body_fallback)} bytes")
                    
                    # Even if fallback works, the protected endpoint failed, so warn the user
                    print(f"[SheepIt API] WARNING: Protected endpoint redirected to sign-in. Session may not be valid for authenticated requests.")
                    print(f"[SheepIt API] Continuing anyway - upload may fail if session is invalid.")
            
            print(f"[SheepIt API] Session confirmed valid!")
            
            # Show cookies in jar after warmup
            jar_cookies = {c.name: c.value for c in cookie_jar}
            print(f"[SheepIt API] Cookies in jar after warmup: {list(jar_cookies.keys())}")
            for name, value in jar_cookies.items():
                print(f"[SheepIt API]   {name}: {value[:30]}...")
            
            # Update headers with cookies from jar (in case any were updated)
            if jar_cookies:
                cookie_str = '; '.join(f"{k}={v}" for k, v in jar_cookies.items())
                headers['Cookie'] = cookie_str
                print(f"[SheepIt API] Updated Cookie header with jar contents: {cookie_str[:50]}...")
    except Exception as warmup_e:
        print(f"[SheepIt API] Warmup failed: {type(warmup_e).__name__}: {warmup_e}")
        import traceback
        traceback.print_exc()
        # Continue anyway - maybe the upload will work
    
    try:
        # Get the form page to extract field names and CSRF tokens
        # Use the SAME cookie jar and opener from warmup
        print(f"[SheepIt API] Fetching form page with cookie jar (reusing from warmup)...")
        
        form_opener = session_opener  # Reuse the same opener
        
        form_req = urllib.request.Request(getstarted_url, headers=headers)
        print(f"[SheepIt API] Request URL: {getstarted_url}")
        print(f"[SheepIt API] Request headers: {list(headers.keys())}")
        if 'Cookie' in headers:
            print(f"[SheepIt API] Cookie header: {headers['Cookie']}")
        
        # Use opener with cookie jar to capture any Set-Cookie headers
        with form_opener.open(form_req, timeout=10) as form_response:
            form_html = form_response.read().decode('utf-8', errors='ignore')
            final_form_url = form_response.geturl()
            print(f"[SheepIt API] Form page loaded, length: {len(form_html)} bytes")
            print(f"[SheepIt API] Final form page URL: {final_form_url}")
            print(f"[SheepIt API] Response status: {form_response.status}")
            
            # Check for Set-Cookie headers
            form_response_headers = dict(form_response.headers)
            if 'Set-Cookie' in form_response_headers:
                print(f"[SheepIt API] Set-Cookie from getstarted: {form_response_headers['Set-Cookie'][:100]}...")
            
            # Check if we got redirected to sign-in
            if 'signin' in final_form_url.lower() or 'login' in final_form_url.lower():
                print(f"[SheepIt API] ERROR: Form page redirected to sign-in! Authentication may have expired.")
                print(f"[SheepIt API] Redirected to: {final_form_url}")
                # Try to extract error message
                if 'You need to be logged in' in form_html or 'sign in' in form_html.lower():
                    print(f"[SheepIt API] Form page indicates authentication required")
                # Return error - can't proceed without valid session
                error_msg = "Authentication expired. Please re-authenticate via browser login in preferences."
                print(f"[SheepIt API] ERROR: {error_msg}")
                return False, error_msg
            
            # Check if we're authenticated on the getstarted page
            # Look for indicators of logged-in state
            has_upload_form = 'enctype="multipart/form-data"' in form_html or 'addproject_archive' in form_html
            has_logout = 'logout' in form_html.lower() or 'sign out' in form_html.lower()
            has_user_menu = '/user/profile' in form_html or 'My account' in form_html
            print(f"[SheepIt API] Getstarted page authentication check:")
            print(f"[SheepIt API]   Has upload form: {has_upload_form}")
            print(f"[SheepIt API]   Has logout link: {has_logout}")
            print(f"[SheepIt API]   Has user menu: {has_user_menu}")
            
            if not has_upload_form:
                print(f"[SheepIt API] WARNING: Upload form not found on getstarted page!")
                print(f"[SheepIt API] This suggests we're not authenticated on this page.")
                # Search for the actual upload form
                if 'addproject' in form_html.lower():
                    print(f"[SheepIt API] Found 'addproject' in page - partial form may be present")
                else:
                    print(f"[SheepIt API] No 'addproject' elements found - likely not logged in")
            
            # Update cookies from response (might get new session cookies)
            jar_cookies = {c.name: c.value for c in cookie_jar}
            if jar_cookies:
                print(f"[SheepIt API] Cookies in jar after form page fetch: {list(jar_cookies.keys())}")
                # Update headers with cookies from jar
                cookie_str = '; '.join(f"{k}={v}" for k, v in jar_cookies.items())
                headers['Cookie'] = cookie_str
                print(f"[SheepIt API] Updated Cookie header with jar contents: {cookie_str[:50]}...")
            
            # Extract form action (where to submit)
            import re
            form_action_match = re.search(r'<form[^>]*action=["\']([^"\']+)["\']', form_html, re.IGNORECASE)
            if form_action_match:
                form_action = form_action_match.group(1)
                # Skip JavaScript handlers (javascript:, #, empty, etc.)
                if form_action.startswith('javascript:') or form_action == '#' or form_action == '':
                    print(f"[SheepIt API] Form uses JavaScript handler ({form_action}), submitting to page itself")
                    api_endpoint = getstarted_url
                # Handle relative URLs
                elif form_action.startswith('/'):
                    api_endpoint = f"{config.SHEEPIT_API_BASE}{form_action}"
                elif form_action.startswith('http'):
                    api_endpoint = form_action
                else:
                    api_endpoint = f"{config.SHEEPIT_API_BASE}/{form_action}"
                print(f"[SheepIt API] Found form action: {api_endpoint}")
            else:
                # Default to /getstarted if no action found (form submits to itself)
                api_endpoint = getstarted_url
                print(f"[SheepIt API] No form action found, using: {api_endpoint}")
            
            # Extract CSRF token if present
            csrf_patterns = [
                r'name=["\']csrf_token["\']\s+value=["\']([^"\']+)["\']',
                r'name=["\']_token["\']\s+value=["\']([^"\']+)["\']',
                r'name=["\']csrf["\']\s+value=["\']([^"\']+)["\']',
                r'csrf["\']?\s*[:=]\s*["\']([^"\']+)["\']',
                r'_token["\']?\s*[:=]\s*["\']([^"\']+)["\']',  # Laravel-style tokens
                r'<meta[^>]*name=["\']csrf-token["\'][^>]*content=["\']([^"\']+)["\']',  # Meta tag
            ]
            for pattern in csrf_patterns:
                csrf_match = re.search(pattern, form_html, re.IGNORECASE)
                if csrf_match:
                    csrf_token = csrf_match.group(1)
                    print(f"[SheepIt API] Found CSRF token using pattern: {pattern[:50]}...")
                    print(f"[SheepIt API] CSRF token value: {csrf_token[:30]}... (length: {len(csrf_token)})")
                    break
            else:
                print(f"[SheepIt API] No CSRF token found (tried {len(csrf_patterns)} patterns)")
                # Print a snippet of the HTML to help debug
                form_start = form_html.find('<form')
                if form_start != -1:
                    form_snippet = form_html[form_start:form_start+3000]
                    print(f"[SheepIt API] Form HTML snippet (first 3000 chars after <form): {form_snippet}")
                else:
                    print(f"[SheepIt API] No <form> tag found in HTML")
                    # Look for any input fields that might be relevant
                    input_fields = re.findall(r'<input[^>]+>', form_html, re.IGNORECASE)
                    print(f"[SheepIt API] Found {len(input_fields)} input fields in HTML")
                    for idx, inp in enumerate(input_fields[:10]):  # First 10
                        print(f"[SheepIt API] Input {idx+1}: {inp[:200]}")
            
            # Extract form field names to see what's expected
            # Try multiple patterns for file input
            file_patterns = [
                r'<input[^>]*type=["\']file["\'][^>]*name=["\']([^"\']+)["\']',  # Standard pattern
                r'name=["\']([^"\']+)["\'][^>]*type=["\']file["\']',  # Reversed order
                r'<input[^>]*name=["\']([^"\']+)["\'][^>]*type=["\']file["\']',  # Name first
            ]
            for pattern in file_patterns:
                file_field_match = re.search(pattern, form_html, re.IGNORECASE)
                if file_field_match:
                    file_field_name = file_field_match.group(1)
                    print(f"[SheepIt API] File field name: {file_field_name}")
                    break
            else:
                print(f"[SheepIt API] File field name not found in form, using default: {file_field_name}")
            
            # Also look for any file upload endpoints or UID references in the HTML/JS
            # The JavaScript might show a file upload endpoint that happens before add_internal
            uid_patterns = [
                r'uid["\']?\s*[:=]\s*["\']([^"\']+)["\']',
                r'upload.*uid',
                r'file.*upload.*endpoint',
            ]
            print(f"[SheepIt API] Searching for file upload patterns in form HTML...")
            # Look for any references to file uploads or UIDs in script tags
            script_tags = re.findall(r'<script[^>]*>([^<]+)</script>', form_html, re.IGNORECASE | re.DOTALL)
            for script_content in script_tags[:5]:  # Check first 5 script tags
                if 'upload' in script_content.lower() or 'file' in script_content.lower():
                    print(f"[SheepIt API] Found script with upload/file references: {script_content[:500]}...")
            
            # Also look for any JavaScript that might reveal the endpoint or field names
            # Search for common AJAX patterns - be more thorough
            ajax_patterns = [
                r'\.ajax\(["\']([^"\']+)["\']',  # jQuery .ajax('/project/add_internal', ...) - FIRST to catch this pattern
                r'\.post\(["\']([^"\']+)["\']',  # jQuery .post()
                r'\.ajax\([^}]*url:\s*["\']([^"\']+)["\']',  # jQuery .ajax({url: '...'})
                r'fetch\(["\']([^"\']+)["\']',  # Fetch API
                r'XMLHttpRequest[^}]*open\(["\']POST["\'][^,]*,\s*["\']([^"\']+)["\']',  # XHR
                r'action:\s*["\']([^"\']+)["\']',  # Generic action
                r'url:\s*["\']([^"\']+)["\']',  # Generic url
                r'["\']([^"\']*project[^"\']*submit[^"\']*)["\']',  # Anything with "project" and "submit"
                r'["\']([^"\']*submit[^"\']*project[^"\']*)["\']',  # Anything with "submit" and "project"
            ]
            found_endpoints = []
            for pattern in ajax_patterns:
                for ajax_match in re.finditer(pattern, form_html, re.IGNORECASE | re.DOTALL):
                    ajax_url = ajax_match.group(1)
                    # Filter out common false positives
                    if ajax_url and not ajax_url.startswith('javascript') and ajax_url not in found_endpoints:
                        if ajax_url.startswith('/'):
                            potential_endpoint = f"{config.SHEEPIT_API_BASE}{ajax_url}"
                            found_endpoints.append(ajax_url)
                            print(f"[SheepIt API] Found potential AJAX endpoint in JS: {potential_endpoint}")
                            # If we find a project/submit endpoint, use it
                            if 'project' in ajax_url.lower() and 'submit' in ajax_url.lower():
                                api_endpoint = potential_endpoint
                                print(f"[SheepIt API] Using discovered project submission endpoint: {api_endpoint}")
                        elif ajax_url.startswith('http'):
                            found_endpoints.append(ajax_url)
                            print(f"[SheepIt API] Found potential AJAX endpoint in JS: {ajax_url}")
            
            # Try to fetch the addproject.js file to find the real endpoint
            js_files_to_check = [
                '/media/ce152504/script/ajax/addproject.js',
                '/media/ce152504/script/ajax/getstarted.js',
            ]
            for js_path in js_files_to_check:
                try:
                    js_url = f"{config.SHEEPIT_API_BASE}{js_path}"
                    print(f"[SheepIt API] Attempting to fetch JavaScript file: {js_url}")
                    js_req = urllib.request.Request(js_url, headers=headers)
                    with urllib.request.urlopen(js_req, timeout=5) as js_response:
                        js_content = js_response.read().decode('utf-8', errors='ignore')
                        print(f"[SheepIt API] Loaded {js_path}, length: {len(js_content)} bytes")
                        # Print more of the JS for debugging, especially looking for FormData or .ajax calls
                        if js_path.endswith('addproject.js'):
                            # Search for the actual submission call - look for .ajax, .post, or FormData
                            submission_patterns = [
                                r'\.ajax\([^}]*url[^}]*\}',
                                r'\.post\([^)]+\)',
                                r'FormData[^}]+\.(?:send|submit|post)',
                                r'action["\']?\s*[:=]\s*["\']([^"\']+)["\']',
                            ]
                            print(f"[SheepIt API] Searching for submission patterns in addproject.js...")
                            for pattern in submission_patterns:
                                matches = list(re.finditer(pattern, js_content, re.IGNORECASE | re.DOTALL))
                                if matches:
                                    print(f"[SheepIt API] Found {len(matches)} matches for pattern: {pattern[:50]}...")
                                    for idx, match in enumerate(matches[:3]):  # Show first 3 matches
                                        start = max(0, match.start() - 100)
                                        end = min(len(js_content), match.end() + 300)
                                        print(f"[SheepIt API] Match {idx+1}: ...{js_content[start:end]}...")
                            
                            # Also print a section that likely contains the submission (search for "submit" or "add")
                            submit_section = re.search(r'submit[^}]{0,500}', js_content, re.IGNORECASE | re.DOTALL)
                            if submit_section:
                                print(f"[SheepIt API] Submission section found: ...{submit_section.group(0)[:800]}...")
                            
                            # Look for file upload patterns - the file might be uploaded separately first
                            # Also search for where the file input is used
                            file_upload_section = re.search(r'input.*type.*file[^}]{0,1000}', js_content, re.IGNORECASE | re.DOTALL)
                            if file_upload_section:
                                print(f"[SheepIt API] File input section: ...{file_upload_section.group(0)[:600]}...")
                            
                            # Search for FormData usage with file uploads
                            formdata_sections = list(re.finditer(r'FormData[^}]{0,800}', js_content, re.IGNORECASE | re.DOTALL))
                            if formdata_sections:
                                print(f"[SheepIt API] Found {len(formdata_sections)} FormData sections in addproject.js")
                                for idx, match in enumerate(formdata_sections[:3]):
                                    start = max(0, match.start() - 50)
                                    end = min(len(js_content), match.end() + 200)
                                    print(f"[SheepIt API] FormData section {idx+1}: ...{js_content[start:end]}...")
                            
                            # Print the full JavaScript to see the upload flow
                            print(f"[SheepIt API] Full addproject.js content (for analysis):")
                            print(js_content)
                        
                        # Search for endpoints in the JavaScript
                        # First pass: collect all endpoints
                        all_js_endpoints = []
                        for pattern in ajax_patterns:
                            for js_match in re.finditer(pattern, js_content, re.IGNORECASE | re.DOTALL):
                                js_url_found = js_match.group(1)
                                if js_url_found and not js_url_found.startswith('javascript') and js_url_found not in found_endpoints:
                                    if js_url_found.startswith('/'):
                                        all_js_endpoints.append(js_url_found)
                                    elif js_url_found.startswith('http'):
                                        found_endpoints.append(js_url_found)
                                        print(f"[SheepIt API] Found endpoint in {js_path}: {js_url_found}")
                        
                        # Second pass: prioritize endpoints (prefer "add" without "analyse")
                        # Also try endpoints on both www and client subdomains
                        for js_url_found in all_js_endpoints:
                            # Try on www first
                            potential_endpoint_www = f"{config.SHEEPIT_API_BASE}{js_url_found}"
                            # Try on client subdomain
                            potential_endpoint_client = f"{config.SHEEPIT_CLIENT_BASE}{js_url_found}"
                            
                            found_endpoints.append(js_url_found)
                            print(f"[SheepIt API] Found endpoint in {js_path}: {potential_endpoint_www} (also trying: {potential_endpoint_client})")
                            
                            # Prioritize: exact match "/project/add_internal" (the actual submission endpoint) > "/project/add" or "/add" > "add" without "analyse" > others
                            if js_url_found.lower() == '/project/add_internal':
                                # This is the actual submission endpoint!
                                # Try client subdomain first (where connection test works), then www
                                api_endpoint = potential_endpoint_client  # Try client subdomain first
                                print(f"[SheepIt API] Found submission endpoint '/project/add_internal' from {js_path}")
                                print(f"[SheepIt API] Trying client subdomain first: {api_endpoint}")
                                print(f"[SheepIt API] Will fallback to www if needed: {potential_endpoint_www}")
                                break
                            elif js_url_found.lower() in ['/project/add', '/add']:
                                # Try client subdomain first for project endpoints
                                if api_endpoint == getstarted_url:
                                    api_endpoint = potential_endpoint_client
                                    print(f"[SheepIt API] Using preferred endpoint from {js_path} (client subdomain): {api_endpoint}")
                            elif 'add' in js_url_found.lower() and 'analyse' not in js_url_found.lower() and 'internal' not in js_url_found.lower() and api_endpoint == getstarted_url:
                                # Try client subdomain for add endpoints (but not add_internal, which we already handled)
                                api_endpoint = potential_endpoint_client
                                print(f"[SheepIt API] Using endpoint from {js_path} (client subdomain): {api_endpoint}")
                        
                        # Also look for FormData usage which might reveal the endpoint
                        formdata_patterns = [
                            r'FormData[^}]*\.(?:post|send|submit)\(["\']([^"\']+)["\']',
                            r'new FormData\([^)]*\)[^}]*\.(?:post|send|submit)\(["\']([^"\']+)["\']',
                        ]
                        for pattern in formdata_patterns:
                            for fd_match in re.finditer(pattern, js_content, re.IGNORECASE | re.DOTALL):
                                fd_url = fd_match.group(1)
                                if fd_url and fd_url.startswith('/') and fd_url not in found_endpoints:
                                    potential_endpoint = f"{config.SHEEPIT_API_BASE}{fd_url}"
                                    found_endpoints.append(fd_url)
                                    print(f"[SheepIt API] Found FormData endpoint in {js_path}: {potential_endpoint}")
                                    if 'add' in fd_url.lower() or 'project' in fd_url.lower():
                                        api_endpoint = potential_endpoint
                                        print(f"[SheepIt API] Using FormData endpoint from {js_path}: {api_endpoint}")
                except Exception as js_e:
                    print(f"[SheepIt API] Could not fetch {js_path}: {type(js_e).__name__}: {str(js_e)}")
            
            # Also try common endpoint patterns as fallback
            # If we haven't found a good endpoint yet, try these
            if api_endpoint == getstarted_url or 'getstarted' in api_endpoint:
                # Try the actual submission endpoint first (from JavaScript analysis)
                # Try client subdomain first since connection test works there
                common_endpoints = [
                    (config.SHEEPIT_CLIENT_BASE, '/project/add_internal'),  # Try client first!
                    (config.SHEEPIT_API_BASE, '/project/add_internal'),  # Then www
                    (config.SHEEPIT_CLIENT_BASE, '/project/add'),
                    (config.SHEEPIT_API_BASE, '/project/add'),
                ]
                print(f"[SheepIt API] No valid endpoint found, will try common patterns")
                # Try the first common endpoint (add_internal on client subdomain)
                api_endpoint = f"{common_endpoints[0][0]}{common_endpoints[0][1]}"
                print(f"[SheepIt API] Trying common endpoint (client subdomain): {api_endpoint}")
            
    except Exception as e:
        print(f"[SheepIt API] WARNING: Could not fetch form page: {type(e).__name__}: {str(e)}")
        import traceback
        print(f"[SheepIt API] Exception traceback:")
        traceback.print_exc()
        print(f"[SheepIt API] Using default endpoint: {getstarted_url}")
        api_endpoint = getstarted_url
        file_field_name = 'file'
        csrf_token = None
        cookie_jar = None
        submit_opener = None
    
    # STEP 1: Upload file to /project/internal/upload
    # This is a two-step process:
    # 1. Upload file to /project/internal/upload (field: addproject_archive)
    # 2. Extract token from redirect URL
    # 3. Open browser to project configuration page: /project/add/{token}
    
    print(f"[SheepIt API] Step 1: Uploading file to /project/internal/upload...")
    
    # IMPORTANT: Use the same opener that successfully fetched getstarted page
    # This ensures the session is properly established
    # Verify we have auth_cookies (required for upload)
    if not auth_cookies:
        error_msg = "No authentication cookies available. Please authenticate via browser login."
        print(f"[SheepIt API] ERROR: {error_msg}")
        return False, error_msg
    
    # Use the SAME session_opener from warmup/getstarted - this maintains session state
    
    # Use www subdomain (matches what works manually in browser)
    # User confirmed manual submission works on www.sheepit-renderfarm.com/getstarted
    upload_url = f"{config.SHEEPIT_API_BASE}/project/internal/upload"
    print(f"[SheepIt API] Upload URL: {upload_url} (using www subdomain to match manual browser test)")
    
    try:
        # Create multipart form data for file upload
        boundary = '----WebKitFormBoundary' + os.urandom(16).hex()
        
        # Build form data parts
        body_parts = []
        crlf = b'\r\n'
        
        # File field name is 'addproject_archive' (from controller code)
        body_parts.append(f'--{boundary}'.encode('utf-8'))
        body_parts.append(crlf)
        body_parts.append(f'Content-Disposition: form-data; name="addproject_archive"; filename="{file_path.name}"'.encode('utf-8'))
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
        
        # Add UPLOAD_IDENTIFIER field (matches web form, used for upload progress tracking)
        import secrets
        upload_uid = secrets.token_hex(16)
        body_parts.append(f'--{boundary}'.encode('utf-8'))
        body_parts.append(crlf)
        body_parts.append(f'Content-Disposition: form-data; name="UPLOAD_IDENTIFIER"'.encode('utf-8'))
        body_parts.append(crlf)
        body_parts.append(crlf)
        body_parts.append(upload_uid.encode('utf-8'))
        body_parts.append(crlf)
        
        print(f"[SheepIt API] Added UPLOAD_IDENTIFIER: {upload_uid}")
        
        # NOTE: The upload endpoint expects:
        # - File field: addproject_archive
        # - Optional: UPLOAD_IDENTIFIER (for progress tracking)
        # All metadata will be sent in step 4 to /project/add_internal
        
        # Close boundary
        body_parts.append(f'--{boundary}--'.encode('utf-8'))
        body_parts.append(crlf)
        
        # Build request body
        request_body = b''.join(body_parts)
        
        # Set content type header
        upload_headers = headers.copy()
        upload_headers['Content-Type'] = f'multipart/form-data; boundary={boundary}'
        # Use client subdomain to match where test connection works
        upload_headers['Referer'] = f"{config.SHEEPIT_API_BASE}/getstarted"
        upload_headers['Origin'] = config.SHEEPIT_API_BASE
        # Add X-Requested-With header (some servers require this for AJAX-like requests)
        upload_headers['X-Requested-With'] = 'XMLHttpRequest'
        # Add Accept header to prefer HTML/JSON responses
        upload_headers['Accept'] = 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'
        
        # Use direct headers (like test connection) - no cookie jar/opener
        # The Cookie header should already be in upload_headers from headers.copy()
        print(f"[SheepIt API] Using direct headers (no cookie jar/opener)")
        if 'Cookie' not in upload_headers:
            # Add Cookie header from auth_cookies if missing
            if auth_cookies:
                cookie_header = '; '.join(f"{k}={v}" for k, v in auth_cookies.items())
                upload_headers['Cookie'] = cookie_header
                print(f"[SheepIt API] Added Cookie header from auth_cookies: {list(auth_cookies.keys())}")
            else:
                error_msg = "No authentication cookies available. Cannot proceed with upload."
                print(f"[SheepIt API] ERROR: {error_msg}")
                return False, error_msg
        else:
            print(f"[SheepIt API] Cookie header already present: {upload_headers['Cookie'][:50]}...")
        
        print(f"[SheepIt API] Upload request body size: {len(request_body) / (1024*1024):.2f} MB")
        print(f"[SheepIt API] Sending file upload POST request...")
        print(f"[SheepIt API] Upload request headers: {list(upload_headers.keys())}")
        if 'Cookie' in upload_headers:
            print(f"[SheepIt API] Upload Cookie header: {upload_headers['Cookie'][:50]}...")
        else:
            print(f"[SheepIt API] WARNING: No Cookie header in upload request!")
        
        # Update Cookie header from cookie jar BEFORE creating request (in case it was updated during warmup/getstarted)
        jar_cookies = {c.name: c.value for c in cookie_jar}
        if jar_cookies:
            cookie_str = '; '.join(f"{k}={v}" for k, v in jar_cookies.items())
            upload_headers['Cookie'] = cookie_str
            print(f"[SheepIt API] Updated Cookie header from jar: {cookie_str[:50]}...")
        
        # Debug: Print all headers that will be sent
        print(f"[SheepIt API] Full upload request headers:")
        for key in upload_headers:
            value = upload_headers[key]
            if len(value) > 80:
                print(f"[SheepIt API]   {key}: {value[:80]}...")
            else:
                print(f"[SheepIt API]   {key}: {value}")
        
        # Create upload request with updated headers
        upload_req = urllib.request.Request(upload_url, data=request_body, headers=upload_headers, method='POST')
        
        # Upload file - use the SAME session_opener that was used for warmup and getstarted
        # This maintains the full session state including any cookies updated during those requests
        try:
            print(f"[SheepIt API] Using session_opener (maintains cookie jar state from warmup/getstarted)...")
            upload_response = session_opener.open(upload_req, timeout=300)  # 5 minute timeout
            
            with upload_response:
                upload_final_url = upload_response.geturl()
                upload_status = upload_response.status
                upload_data = upload_response.read().decode('utf-8', errors='ignore')
                print(f"[SheepIt API] Upload response status: {upload_status}")
                print(f"[SheepIt API] Upload final URL: {upload_final_url}")
                
                # Debug: show response headers
                response_headers = dict(upload_response.headers)
                print(f"[SheepIt API] Upload response headers: {list(response_headers.keys())}")
                if 'Set-Cookie' in response_headers:
                    print(f"[SheepIt API] Upload Set-Cookie: {response_headers['Set-Cookie'][:100]}...")
                if 'Location' in response_headers:
                    print(f"[SheepIt API] Upload Location: {response_headers['Location']}")
                
                # Check response body for specific error messages FIRST
                # This is important because a 200 response with error page might still have redirect URL
                print(f"[SheepIt API] Response body preview (first 500 chars): {upload_data[:500]}")
                
                # Check for "File upload failed" or "Upload failed" - this means authentication worked but file field is missing
                # This is the same error you get when manually submitting without a file
                if 'File upload failed' in upload_data or 'Upload failed' in upload_data:
                    error_msg = "Upload failed: File field 'addproject_archive' not found in request. Check multipart form encoding."
                    print(f"[SheepIt API] ERROR: {error_msg}")
                    print(f"[SheepIt API] This suggests authentication worked but the file wasn't sent correctly.")
                    print(f"[SheepIt API] Response body: {upload_data[:1000]}")
                    return False, error_msg
                
                # Check if we got redirected to sign-in (authentication failed)
                # Only check this if we didn't get the "Upload failed" error (which means auth worked)
                if 'signin' in upload_final_url.lower() or 'login' in upload_final_url.lower():
                    # Also check if response body is the sign-in page HTML
                    if 'sign in' in upload_data.lower() and 'password' in upload_data.lower():
                        error_msg = "Upload failed: Authentication expired. Please re-authenticate via browser login in preferences."
                        print(f"[SheepIt API] ERROR: {error_msg}")
                        print(f"[SheepIt API] Redirected to: {upload_final_url}")
                        return False, error_msg
                
                # Check if response is an error page
                if upload_status != 200 and upload_status not in (301, 302, 303, 307, 308):
                    error_msg = f"Upload failed with status {upload_status}: {upload_data[:500]}"
                    print(f"[SheepIt API] ERROR: {error_msg}")
                    return False, error_msg
                
                # Extract token from redirect URL: /project/add/{token}
                import re
                token_match = re.search(r'/project/add/([a-zA-Z0-9]+)', upload_final_url)
                
                # If not found in URL, also check response body (token might be in HTML)
                if not token_match:
                    # Check response body for token in URL pattern
                    token_match = re.search(r'/project/add/([a-zA-Z0-9]+)', upload_data)
                    if not token_match:
                        # Check for token in hidden input fields (common in HTML forms)
                        token_match = re.search(r'<input[^>]*name=["\']token["\'][^>]*value=["\']([a-zA-Z0-9]+)["\']', upload_data, re.IGNORECASE)
                    if not token_match:
                        # Check for token in form action URLs
                        token_match = re.search(r'action=["\'][^"\']*/([a-zA-Z0-9]+)["\']', upload_data, re.IGNORECASE)
                    if not token_match:
                        # Check for token in JavaScript variables (like var token = "OszOg4")
                        token_match = re.search(r'(?:var|let|const)\s+token\s*=\s*["\']([a-zA-Z0-9]+)["\']', upload_data, re.IGNORECASE)
                    if not token_match:
                        # Check for token in data attributes or other HTML attributes
                        token_match = re.search(r'(?:data-)?token=["\']([a-zA-Z0-9]+)["\']', upload_data, re.IGNORECASE)
                
                if not token_match:
                    # Also check response body for other error messages
                    if 'error' in upload_data.lower() or 'failed' in upload_data.lower():
                        error_msg = f"Upload failed: {upload_data[:500]}"
                        print(f"[SheepIt API] ERROR: {error_msg}")
                        return False, error_msg
                    error_msg = f"Failed to extract token from upload response. URL: {upload_final_url}, Status: {upload_status}"
                    print(f"[SheepIt API] ERROR: {error_msg}")
                    print(f"[SheepIt API] Response preview: {upload_data[:500]}")
                    print(f"[SheepIt API] Searched for token in URL and response body")
                    return False, error_msg
                
                token = token_match.group(1)
                print(f"[SheepIt API] Extracted token: {token}")
                
                # Open browser to project configuration page
                project_url = f"{config.SHEEPIT_API_BASE}/project/add/{token}"
                print(f"[SheepIt API] Opening project configuration page: {project_url}")
                try:
                    webbrowser.open(project_url)
                    success_msg = f"File uploaded successfully! Opening project configuration page: {project_url}"
                    print(f"[SheepIt API] SUCCESS: {success_msg}")
                    return True, success_msg
                except Exception as e:
                    error_msg = f"File uploaded successfully, but failed to open browser: {str(e)}. Please visit: {project_url}"
                    print(f"[SheepIt API] WARNING: {error_msg}")
                    return True, error_msg
                
        except urllib.error.HTTPError as e:
            error_body = ""
            try:
                if hasattr(e, 'read'):
                    error_body = e.read().decode('utf-8', errors='ignore')
                else:
                    error_body = str(e)
            except Exception:
                error_body = str(e)
            
            print(f"[SheepIt API] HTTP Error {e.code}")
            print(f"[SheepIt API] Error URL: {e.url if hasattr(e, 'url') else 'unknown'}")
            print(f"[SheepIt API] Error body: {error_body[:1000]}...")
            
            error_msg = f"HTTP error {e.code}: {error_body[:500]}"
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
