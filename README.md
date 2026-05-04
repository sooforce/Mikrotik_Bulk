# MikroTik Bulk Provisioner

Windows desktop GUI tool for bulk provisioning MikroTik hEX S routers in an
ISP staging lab.  Written in Python (PyQt6 + paramiko).

---

## Lab Setup Assumed

| Component | Detail |
|-----------|--------|
| Laptop Ethernet IP | `192.168.88.5/24` |
| DHCP server | Built-in DHCP server **or** tftpd64 (not both at once) |
| Router DHCP pool | `192.168.88.100 – 192.168.88.200` (configurable) |
| HTTP file server | HFS on port 80 |
| Config URL | `http://192.168.88.5:80/mikrotik-provision/full-config.rsc` |
| Switch | MikroTik PoE switch between laptop and routers |

Routers are first flashed via **MikroTik Netinstall** with a minimal config
that enables DHCP client on ether1, enables SSH, and sets a temporary admin
password.  This tool takes over after the router has booted and obtained a
DHCP IP.

---

## Requirements

- Windows 10/11
- Python 3.10 or newer  
  Download: <https://www.python.org/downloads/>

---

## Installation

```
# 1. Open a terminal (cmd or PowerShell) in this folder

# 2. (Recommended) Create a virtual environment
python -m venv .venv
.venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt
```

---

## Running the Tool

> **The application must be run as Administrator** when using the built-in
> DHCP server (port 67 is a privileged port on Windows).

```
# Right-click PowerShell → "Run as Administrator", then:
python mikrotik_provisioner.py
```

---

## Usage Walkthrough

### 1 – Configure Connection Settings (top of the window)

| Field | Default | Notes |
|-------|---------|-------|
| IP Range Start | `192.168.88.100` | Used by the manual scan only |
| IP Range End | `192.168.88.254` | Used by the manual scan only |
| SSH User | `admin` | Temporary admin account set by Netinstall |
| SSH Password | *(blank)* | Leave blank if Netinstall leaves no password |
| Max Parallel | `6` | Simultaneous provisioning sessions |
| Config URL | `http://192.168.88.5:80/…` | Full URL served by HFS |

---

### 2 – Built-in DHCP Server *(recommended — fastest discovery)*

The tool includes a pure-Python RFC 2131 DHCP server that replaces tftpd64's
DHCP component.  Because the server **knows exactly which IP it just leased**,
it immediately starts an SSH probe on that IP the moment the DHCP ACK is sent.
This eliminates the 60–90 s polling delay of the manual scan.

#### Setup

1. In **tftpd64 → Settings → DHCP**, uncheck **"Activate DHCP"** and click OK.
   tftpd64's TFTP server (port 69, used by Netinstall) continues to work normally.
2. Fill in the **Built-in DHCP Server** group box:

| Field | Default | Notes |
|-------|---------|-------|
| Server IP | `192.168.88.1` | IP of this laptop's Ethernet interface |
| Pool Start | `192.168.88.100` | First address handed out |
| Pool End | `192.168.88.200` | Last address handed out |
| Subnet Mask | `255.255.255.0` | Sent to clients |
| Gateway | `192.168.88.1` | Sent as default route |
| Lease | `3600` s | IP lease duration |

3. Click **Start DHCP Server**.  The status label changes to **Running**.

#### Discovery flow

```
Router boots
  └─► DHCP DISCOVER broadcast
        └─► Built-in server: OFFER (< 50 ms)
              └─► Router: REQUEST
                    └─► Built-in server: ACK  ──► device_leased signal
                                                     └─► SSH probe starts immediately
                                                           └─► Router appears in table
```

The whole process from boot-complete to table row takes only as long as
RouterOS needs to finish booting (~30–40 s) — no extra waiting.

> **Note:** If you prefer to keep using tftpd64 as the DHCP server, leave the
> built-in server stopped and use **Scan for Routers** as before.

---

### 3 – Scan for Routers *(alternative / legacy method)*

Click **Scan for Routers**.

The tool probes every IP in the configured range for an open SSH port (TCP 22).
When a port responds it logs in and retrieves:

- Router identity (`/system identity get name`)
- RouterOS version (`/system resource get version`)
- ether1 MAC address

Each discovered router appears as a row in the table, pre-checked.

> **Tip:** Click **Stop Scan** at any time; already-found routers remain in
> the table.

---

### 4 – Select Routers

Use the checkboxes in the **✓** column, or the **Select All / Deselect All**
buttons.

### 5 – Provision Selected Routers

Click **Provision Selected**.  A confirmation dialog shows the URL and
concurrency limit.  After confirming, the tool:

1. Connects to each selected router via SSH.
2. Runs `/tool fetch url="<CONFIG_URL>" mode=http dst-path=full-config.rsc`
3. Runs `/import file-name=full-config.rsc`

The **Status** column updates in real time:

| Status | Meaning |
|--------|---------|
| Discovered | Found during scan/DHCP, not yet provisioned |
| Queued | Waiting for a concurrency slot |
| Connecting | Opening SSH session |
| Fetching config | Running `/tool fetch` |
| Importing config | Running `/import` |
| **Success** | Config applied successfully |
| **Failed: …** | Error; hover over the cell for the full message |

> **Note:** The SSH session will be dropped when `/import` restarts services
> or resets the network stack.  This is expected and does **not** indicate
> failure; the tool reports **Success** if the import command was dispatched.

### 6 – Logs

All activity is shown in the **Activity Log** panel at the bottom and saved to
`mikrotik_provisioner.log` in the same folder as the script.

---

## Provisioning Commands Sent

```routeros
/tool fetch url="http://192.168.88.5:80/mikrotik-provision/full-config.rsc" mode=http dst-path=full-config.rsc
/import file-name=full-config.rsc
```

---

## Concurrency & Timeouts

| Parameter | Value | File constant |
|-----------|-------|---------------|
| Scan TCP timeout | 1.5 s | `SCAN_TIMEOUT` |
| Scan parallel threads | 60 | `SCAN_THREADS` |
| SSH connect timeout | 10 s | `SSH_TIMEOUT` |
| SSH connect retries | 3 | `SSH_RETRIES` |
| SSH command timeout | 30 s | `CMD_TIMEOUT` |
| Fetch / import timeout | 120 s | `FETCH_TIMEOUT` |
| Default provisioning concurrency | 6 | `DEFAULT_CONCURRENCY` |
| DHCP probe SSH retry interval | 3 s | `SingleProbeWorker._WAIT_INTERVAL` |
| DHCP probe max wait | 180 s | `SingleProbeWorker._WAIT_MAX` |

All values can be adjusted at the top of `mikrotik_provisioner.py`.

---

## Troubleshooting

| Symptom | Likely cause / fix |
|---------|--------------------|
| DHCP server won't start (`Cannot bind to port 67`) | Application is not running as Administrator; restart with elevated privileges |
| Routers not getting IPs from built-in DHCP | tftpd64 DHCP is still active; disable it in tftpd64 → Settings → DHCP |
| No routers discovered (manual scan) | Check that routers obtained DHCP IPs; verify SSH is enabled in the Netinstall minimal config |
| `auth failed` in identity column | Wrong SSH username or password; routers may still have blank password — leave the Password field empty |
| Fetch fails with timeout | HFS is not running or the URL is wrong; check `http://192.168.88.5:80/` from a browser on the laptop |
| Import succeeds but router is not configured | The `full-config.rsc` script may have syntax errors; test it manually on one router first |
| GUI freezes | Should not happen — all network operations run in background threads; if it does, increase `SCAN_TIMEOUT` |

---

## Building a Standalone EXE  *(no Python needed on target PC)*

The project includes a build script that uses
[PyInstaller](https://pyinstaller.org/) to compile everything into a single
`MikroProv.exe` that runs on any Windows 10/11 machine with a double-click.

### Prerequisites

- Python 3.10+ with `pip` (only needed on the **build** machine)
- `assets\logo.png` present in the project folder (already included)

### Steps

```
# Open PowerShell in the project folder, then:
.\build.bat
```

The script will automatically:

1. Install `pyinstaller` and `pillow` (if not already present)
2. Convert `assets\logo.png` → `assets\logo.ico` (multi-resolution icon)
3. Clean any previous build output
4. Run PyInstaller with `--onefile --windowed --uac-admin`
5. Output `dist\MikroProv.exe` (~40 MB)

### Deploying to another PC

Just copy `dist\MikroProv.exe` — no Python, no libraries, no installation
required.  Double-clicking it will:

- Trigger a **UAC prompt** for Administrator rights  
  *(required for the built-in DHCP server on port 67)*
- Open the app with the **MikroProv logo icon**

> **Rebuild after code changes:** run `.\build.bat` again.  
> The `dist\` and `build\` folders are git-ignored; only source files are tracked.

---

## File Structure

```
mikrotik_provisioner.py   ← main application (single file)
make_icon.py              ← PNG→ICO converter called by build.bat
build.bat                 ← one-click PyInstaller build script
requirements.txt          ← Python package list (for running from source)
assets/
  logo.png                ← application icon source
README.md                 ← this file
mikrotik_provisioner.log  ← created at runtime (git-ignored)
dist/
  MikroProv.exe           ← compiled output (git-ignored)
```
