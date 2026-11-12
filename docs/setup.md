# Setting up FlightPlan (no coding needed)

This page walks you through one command. It takes about five minutes the first
time, mostly waiting for downloads.

## 1. Open a terminal

A terminal (also called "command line" or "shell") is a window where you type a
command and press Enter.

* **Windows 10/11:** press the Start button, type `PowerShell`, and open
  **Windows PowerShell**. (Not "PowerShell ISE".)
* **macOS:** press Command+Space, type `Terminal`, press Enter.
* **Linux:** open **Terminal** from your applications menu (often Ctrl+Alt+T).

## 2. Run one command

The fastest path needs no manual download:

**macOS / Linux**

```
curl -fsSL https://raw.githubusercontent.com/HenryGetz/FlightPlan/main/bootstrap.sh | sh
```

**Windows PowerShell**

```
irm https://raw.githubusercontent.com/HenryGetz/FlightPlan/main/bootstrap.ps1 | iex
```

The bootstrap clones the project to `${FLIGHTPLAN_HOME:-$HOME/.flightplan}`,
then runs the installer. It reads setup prompts from `/dev/tty` when one is
available. With no terminal (automation), it stops with an actionable message
instead of looping on EOF; use the non-interactive command below. If you already
downloaded/cloned the folder, use:

**macOS / Linux**

```
cd /path/to/FlightPlan
sh install.sh
```

**Windows**

```
powershell -ExecutionPolicy Bypass -File install.ps1
```

The second line only exists because Windows blocks downloaded scripts by default.
It allows this one script for this one run; it does not change your system
settings.

## 3. What the prompts are asking

The setup asks three things. Nothing is sent anywhere except your own computer
and the UNW servers.

1. **UNW email** — your full address, for example
   `you@students.unwsp.edu`.
2. **UNW password** — typing is hidden on purpose (the screen stays blank).
   That is normal; type it and press Enter.
3. **TOTP secret** — the long code that pairs your phone's authenticator app
   (Microsoft Authenticator, Google Authenticator, 1Password, etc.) with your
   account.

### Where the TOTP secret comes from

At <https://mysignins.microsoft.com/security-info>, choose **Add sign-in method →
Authenticator app**. Microsoft shows a QR code. If you cannot scan it, click
**"I want to use a different authenticator app"** (or **"Can't scan image?"**),
then **"Next"**. The next screen shows a **manual key** — a string of letters and
digits such as `JBSW Y3DP EHPK 3PXP`. That string is the TOTP secret. Type it
without spaces. If you paste the whole `otpauth://...` link, setup extracts the
key for you.

## 4. What success looks like

You will see a short checklist:

```
Doctor checks:
  ✓ Python version: 3.12.x
  ✓ Virtual environment: ...
  ✓ Project .venv: ...
  ✓ Package importable: flightplan
  ✓ Chromium (Playwright): headless browser
  ✓ Credentials file: .../.env
  ✓ Cached session: .../.state/unw_state.json
```

Then it asks **"Log in now?"**. Say yes. A browser window briefly opens and
closes by itself while it signs in (about 20 seconds). When it finishes you will
see **"Login complete."** and three suggested commands, starting with:

```
.venv/bin/flightplan advise --term 202620
```

On Windows use `.venv\Scripts\flightplan.exe`.

That produces the advising brief described in `docs/planner.md`.

## 5. If something goes wrong

**"Python 3.11 or newer is required" / "Python was not found".**
Install Python 3.12 from <https://www.python.org/downloads/>. On Windows, tick
**"Add Python to PATH"** on the first installer screen. On macOS you can also run
`brew install python@3.12`. Then close the terminal, open a new one, and run the
install command again.

**"Login failed" or the browser closes without signing in.**
Check the email and password by signing in at <https://login.microsoftonline.com>
in a normal browser. If the password works there, the TOTP secret is usually the
problem: re-copy the manual key from the security-info page (no spaces) and run
the setup again with `--force` to replace the saved credentials:

```
.venv/bin/python -m flightplan.cli setup --force
```

(On Windows, use `.venv\Scripts\python.exe` instead of `.venv/bin/python`.)

**"that looks like an email address, not a TOTP secret".**
You pasted your email into the third prompt. Run setup again with `--force` and
paste the manual key from the authenticator screen.

## Re-running setup

It is safe. Existing credentials are kept unless you pass `--force`; finished
steps (browser download, package install) are skipped. For automation, pipe the password and provide the user/TOTP values:

```
printf '%s\n' "$UNW_PASS" | .venv/bin/python -m flightplan.cli setup \
  --yes --no-login --password-stdin \
  --user "$UNW_USER" --totp-secret "$UNW_TOTP_SECRET"
```

`--no-login` still writes `.env`, so credentials are required. In a
non-interactive shell without `--password-stdin`, setup stops with an actionable
error instead of falling into `getpass`. After a no-login setup, run
`.venv/bin/flightplan login` before any live Banner tool.

Never share or commit the `.env` file: it holds your password and TOTP secret.
The setup restricts its permissions automatically.
