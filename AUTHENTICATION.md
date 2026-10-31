# Authentication Guide

This document explains how the authentication system works in the Paper Reader Tree QA application.

## Overview

The authentication system provides optional JWT-based session management. You can configure multiple users via environment variables, and all API endpoints are protected when authentication is enabled.

**Key features:**
- ✓ Multiple users support via `AUTH_USERS` environment variable
- ✓ JWT token-based sessions
- ✓ HttpOnly secure cookies
- ✓ Professional login/logout UI
- ✓ Fully optional (disable by not setting env variables)
- ✓ No database schema changes
- ✓ Zero impact on existing code when disabled

## Quick Start

### 1. Enable Authentication

Set the `AUTH_USERS` environment variable:

```bash
export AUTH_USERS='alice@example.com:password1,bob@example.com:password2'
```

Then run the app:

```bash
uv run python app.py
```

Visit http://localhost:8080 and log in with any configured user.

### 2. Disable Authentication

Simply unset all `AUTH_*` environment variables:

```bash
unset AUTH_USERS
unset AUTH_SECRET_KEY
unset AUTH_SESSION_TIMEOUT_SECONDS
```

Or don't set them in the first place. The app will work without authentication.

## Configuration

### Environment Variables

#### `AUTH_USERS` (Required to enable auth)

Comma-separated list of email:password pairs.

```bash
# Single user
AUTH_USERS='admin@example.com:password123'

# Multiple users
AUTH_USERS='alice@example.com:pass1,bob@example.com:pass2,charlie@example.com:pass3'

# With special characters (in password)
AUTH_USERS='user@example.com:my!@#$%pass'
```

**Format details:**
- Emails are **case-insensitive** (normalized to lowercase)
- Passwords are **case-sensitive**
- Each pair is separated by a comma
- Each email and password are separated by a colon
- Whitespace is trimmed around emails and passwords
- If not set, authentication is disabled

#### `AUTH_SECRET_KEY` (Optional)

Secret key for JWT token signing.

```bash
# Use a secure random string in production
AUTH_SECRET_KEY='your-super-secret-key-minimum-32-chars-recommended'
```

**Default:** If not set and `AUTH_USERS` is configured, uses `default-dev-secret-key-change-in-production` (for development only)

**Important:** Change this in production to a strong random string.

#### `AUTH_SESSION_TIMEOUT_SECONDS` (Optional)

How long a session lasts before the user must log in again.

```bash
# 24 hours (default)
AUTH_SESSION_TIMEOUT_SECONDS=86400

# 1 hour
AUTH_SESSION_TIMEOUT_SECONDS=3600

# 7 days
AUTH_SESSION_TIMEOUT_SECONDS=604800
```

**Default:** 86400 seconds (24 hours)

## Using in Docker Compose

### 1. Create `.env` file in the project root

Copy `.env.example` as a starting point:

```bash
cp .env.example .env
```

### 2. Edit `.env` with your configuration

```bash
# .env
AUTH_USERS=alice@company.com:secure_password1,bob@company.com:secure_password2
AUTH_SECRET_KEY=your-production-secret-key-here
AUTH_SESSION_TIMEOUT_SECONDS=86400

ANTHROPIC_API_KEY=sk-ant-...
```

### 3. Run with docker-compose

The `docker-compose.yml` already loads from `.env`:

```bash
docker-compose up
```

The environment variables from `.env` are automatically loaded into the container.

**Never hardcode secrets in `docker-compose.yml`** — always use `env_file: - .env`

## Deployment with Cloudflare

If your app is behind Cloudflare (or another reverse proxy), HTTPS/TLS is handled by Cloudflare, not your app. This is secure and requires no changes:

### How it works:

1. **User → Cloudflare:** HTTPS/TLS connection (encrypted by Cloudflare)
2. **Cloudflare → Your App:** HTTP connection (internal, trusted)
3. **Cookies are secure:** Even though the app runs on HTTP, Cloudflare's HTTPS ensures cookies are encrypted end-to-end

### Setup:

1. Configure your `.env` as normal (see Docker Compose section above)
2. No code changes needed
3. No HTTPS certificate needed in your app
4. Cookies work perfectly with Cloudflare

### Additional security with Cloudflare:

```
✓ HTTPS/TLS termination
✓ DDoS protection
✓ WAF (Web Application Firewall)
✓ Rate limiting (can protect against brute force)
✓ Geographic routing
```

**You can optionally enable Cloudflare's WAF rules to add rate limiting on `/api/auth/login`**

## How It Works

### Login Flow

1. User visits http://localhost:8080
2. Frontend checks if authentication is enabled (`GET /api/auth/status`)
3. If auth is enabled and user is not logged in, show **login screen**
4. User enters email and password
5. Frontend sends credentials to `POST /api/auth/login`
6. Backend verifies credentials against `AUTH_USERS`
7. If valid, backend creates JWT token and sets it in an **HttpOnly cookie**
8. Frontend hides login screen and loads the main app
9. All subsequent API requests automatically include the cookie

### Session Verification

For each request:
1. Frontend sends the auth cookie with each API call
2. Backend verifies the JWT token signature and expiration
3. If invalid or expired, returns 401 Unauthorized
4. Frontend catches 401, shows login screen
5. User logs in again

### Logout Flow

1. User clicks **Logout** button
2. Frontend calls `POST /api/auth/logout`
3. Backend clears the auth cookie
4. Frontend deletes the local auth state
5. Frontend shows login screen

## API Endpoints

### Public Endpoints (no auth required)

```
GET    /                       # Home page
GET    /favicon.ico            # Favicon
GET    /api/auth/status        # Check if auth is enabled
POST   /api/auth/login         # Login with email/password
POST   /api/auth/logout        # Logout
GET    /api/auth/verify        # Verify current session
```

### Protected Endpoints (auth required when enabled)

All other `/api/*` endpoints require valid authentication when `AUTH_ENABLED=True`:

```
/api/workspaces/*
/api/documents/*
/api/highlights/*
/api/nodes/*
/api/file-list/*
... (33+ endpoints)
```

**Response when auth is required but not provided:**
```json
HTTP 401 Unauthorized
{"error": "Authentication required"}
```

## Token Details

### JWT Token Structure

The JWT token contains:

```json
{
  "email": "user@example.com",
  "type": "session",
  "iat": 1698765432,    // Token issued at (unix timestamp)
  "exp": 1698851832     // Token expires at (unix timestamp)
}
```

### Token Storage

- Stored in an **HttpOnly cookie** named `auth_token`
- Cannot be accessed by JavaScript (secure against XSS)
- Automatically sent with every request
- Expires after `AUTH_SESSION_TIMEOUT_SECONDS`
- Uses **SameSite=Lax** for CSRF protection

### Token Verification

Backend verifies:
1. ✓ Token signature (using `AUTH_SECRET_KEY`)
2. ✓ Token has not expired
3. ✓ Token type is "session"
4. ✓ Email is present in token

If any check fails, the token is invalid and user must log in again.

## Security Considerations

### ✓ What's Secure

- **HttpOnly cookies:** JavaScript cannot access the token (prevents XSS leaks)
- **JWT signature verification:** Tokens cannot be forged without the secret key
- **Session expiration:** Tokens automatically expire
- **SameSite=Lax:** Provides CSRF protection
- **Email case-insensitivity:** Prevents user confusion (alice@example.com == ALICE@EXAMPLE.COM)
- **Stateless design:** No server-side session storage required

### ⚠️ What's NOT Secure (Development Only)

- **Plain-text passwords:** Passwords are stored as-is in environment variables
- **Default secret key:** If you use the default secret key, tokens are not secure
- **No HTTPS (local only):** In development, the cookie `Secure` flag is False
  - **If behind Cloudflare/reverse proxy:** HTTPS is handled by Cloudflare, not your app. This is fine.
  - **If accessing directly over HTTP:** Enable HTTPS or use a reverse proxy
- **No rate limiting:** Brute force attacks on login are not prevented

### Production Checklist

Before deploying to production:

- [ ] **Set a strong `AUTH_SECRET_KEY`** (minimum 32 characters, random)
  ```bash
  # Generate a secure key
  python3 -c "import secrets; print(secrets.token_hex(32))"
  ```

- [ ] **Verify HTTPS is in place**
  - If behind Cloudflare/reverse proxy: ✓ HTTPS is handled there (no change needed)
  - If accessing directly: Enable HTTPS or use a reverse proxy

- [ ] **Store passwords securely** (consider using bcrypt or similar for sensitive data)

- [ ] **Configure `AUTH_SESSION_TIMEOUT_SECONDS`** appropriately (shorter for sensitive apps)

- [ ] **Implement rate limiting** on `/api/auth/login` endpoint (or use Cloudflare WAF)

- [ ] **Use secure random `AUTH_SECRET_KEY`**, not a predictable string

- [ ] **Never commit `.env`** to git (it should be in `.gitignore`)

- [ ] **Rotate `AUTH_SECRET_KEY` periodically** (logs out all users when changed)

- [ ] **Monitor for suspicious login attempts** (consider adding logging)

## Troubleshooting

### Users can't log in

**Problem:** Login form shows error "Invalid credentials"

**Solutions:**
1. Verify the email format matches exactly (case-insensitive for email part)
2. Check that `AUTH_USERS` is set: `echo $AUTH_USERS`
3. Verify the format: `user@example.com:password,another@example.com:pass`
4. Ensure there are no trailing spaces: `export AUTH_USERS='test@example.com:pass'` (not `test@example.com:pass `)

### Logging in works, but API calls return 401

**Problem:** User is logged in (sees logout button) but API calls fail

**Solutions:**
1. Check browser console for JavaScript errors
2. Verify the cookie is being sent: Open DevTools → Application → Cookies → look for `auth_token`
3. Restart the app (tokens become invalid when `AUTH_SECRET_KEY` changes)
4. Clear browser cookies and log in again

### Can't log in at all (no login form shown)

**Problem:** App loads without login form, but auth should be enabled

**Solutions:**
1. Check if `AUTH_USERS` is actually set: `echo $AUTH_USERS`
2. Verify `/api/auth/status` returns `{"enabled": true}`
   ```bash
   curl http://localhost:8080/api/auth/status
   ```
3. Check app logs for errors during startup

### Session expires too quickly / too slowly

**Problem:** Users are logged out sooner or later than expected

**Solutions:**
1. Adjust `AUTH_SESSION_TIMEOUT_SECONDS`:
   ```bash
   export AUTH_SESSION_TIMEOUT_SECONDS=3600  # 1 hour
   ```
2. Restart the app
3. Note: Changing this value only affects NEW logins, not existing sessions

### Forgot password

**Problem:** User forgot their password

**Solution:** There is no self-service password reset. Admin must update `AUTH_USERS` and restart the app.

To reset a user's password:
1. Update `AUTH_USERS` in `.env` or environment
2. Restart the app: `docker-compose restart` or `Ctrl+C` then `uv run python app.py`
3. User can log in with new password

## API Examples

### Check if authentication is enabled

```bash
curl http://localhost:8080/api/auth/status
```

Response:
```json
{
  "enabled": true
}
```

Or if disabled:
```json
{
  "enabled": false
}
```

### Log in

```bash
curl -X POST http://localhost:8080/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email": "user@example.com", "password": "password123"}' \
  -c cookies.txt
```

On success:
```json
{
  "success": true,
  "message": "Login successful"
}
```

On failure:
```json
HTTP 401 Unauthorized
{
  "error": "Invalid credentials"
}
```

### Verify current session

```bash
curl http://localhost:8080/api/auth/verify \
  -b cookies.txt
```

On success:
```json
{
  "authenticated": true,
  "email": "user@example.com"
}
```

On failure:
```json
HTTP 401 Unauthorized
{
  "error": "Not authenticated"
}
```

### Log out

```bash
curl -X POST http://localhost:8080/api/auth/logout \
  -b cookies.txt
```

Response:
```json
{
  "success": true,
  "message": "Logged out successfully"
}
```

### Use authenticated request

```bash
curl http://localhost:8080/api/workspaces \
  -b cookies.txt
```

If authenticated:
```json
{
  "workspaces": [...]
}
```

If not authenticated:
```json
HTTP 401 Unauthorized
{
  "error": "Authentication required"
}
```

## Disabling Authentication

To disable authentication completely:

1. **Remove all `AUTH_*` environment variables** from `.env`
2. **Restart the app**
3. **The login screen will not appear**, and all endpoints will be publicly accessible

Example:
```bash
# Before (with auth)
AUTH_USERS='user@example.com:password'

# After (no auth)
# (delete the AUTH_USERS line)
```

Then:
```bash
docker-compose restart
```

## Files Modified for Authentication

- **`app/auth.py`** — JWT and credential verification functions
- **`app/config.py`** — Configuration parsing from environment variables
- **`app/routes.py`** — Authentication middleware and protected endpoints
- **`templates/index.html`** — Login screen HTML
- **`static/app.js`** — Frontend authentication logic
- **`static/styles.css`** — Login screen styling
- **`pyproject.toml`** — Added PyJWT dependency

## Frequently Asked Questions

### Q: How many users can I configure?
**A:** Unlimited! Just add more comma-separated pairs to `AUTH_USERS`.

### Q: Can I change users without restarting?
**A:** No, users must be configured before the app starts. Update `AUTH_USERS` and restart the app.

### Q: What if I set an invalid `AUTH_USERS` format?
**A:** Invalid pairs are silently skipped. The app starts with only valid pairs configured.

### Q: What if a user's password contains a comma or colon?
**A:** The password is everything after the first colon, so special characters are fine:
```bash
AUTH_USERS='user@example.com:pass:word:with:colons'
# Result: email=user@example.com, password=pass:word:with:colons
```

### Q: Can I use this without HTTPS?
**A:** For development, yes. For production, it depends:
- **If behind Cloudflare/reverse proxy:** ✓ Yes! HTTPS is handled by Cloudflare, not your app. This is secure.
- **If accessing directly:** No. Always use HTTPS because cookies are transmitted in plain text over HTTP.

**Recommended:** Use a reverse proxy like Cloudflare, Nginx, or Apache to handle HTTPS termination.

### Q: Are passwords hashed?
**A:** No, they are stored and compared as plain-text. For development this is fine. For sensitive production data, consider implementing bcrypt or similar.

### Q: What happens if two users have the same email?
**A:** The last one wins. Example:
```bash
AUTH_USERS='user@example.com:pass1,user@example.com:pass2'
# Only the second password (pass2) will work
```

### Q: Can I use uppercase in the email?
**A:** Yes, it's normalized to lowercase. `ADMIN@EXAMPLE.COM` and `admin@example.com` are the same user.

### Q: How do I generate a secure secret key?
**A:** Use Python:
```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

Then set it:
```bash
export AUTH_SECRET_KEY='<generated_key_here>'
```

## Migration Guide

If you were using an older version without authentication:

1. No changes needed! Authentication is completely optional.
2. If you want to enable it, just set `AUTH_USERS` and restart.
3. No database changes or data loss.
4. Existing workspaces and documents are unaffected.

## Support

For issues or questions, check:
1. This document's **Troubleshooting** section
2. The `.env.example` file for configuration examples
3. Application logs for error messages
