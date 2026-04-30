# MikroTik Bulk Provisioner

Windows desktop GUI tool for bulk provisioning MikroTik hEX S routers in an
ISP staging lab.  Written in Python (PyQt6 + paramiko).

---

## Lab Setup Assumed

| Component | Detail |
|-----------|--------|
| Laptop Ethernet IP | `192.168.88.5/24` |
| DHCP server | Tftpd64 on the laptop |
| Router DHCP pool | `192.168.88.100 – 192.168.88.254` |
| HTTP file server | HFS on port 8000 |
| Config URL | `http://192.168.88.5:8000/mikrotik-provision/full-config.rsc` |
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

```
python mikrotik_provisioner.py
```

---

## Usage Walkthrough

### 1 – Configure Connection Settings (top of the window)

| Field | Default | Notes |
|-------|---------|-------|
| IP Range Start | `192.168.88.100` | First DHCP lease |
| IP Range End | `192.168.88.254` | Last DHCP lease |
| SSH User | `admin` | Temporary admin account set by Netinstall |
| SSH Password | *(blank)* | Leave blank if Netinstall leaves no password |
| Max Parallel | `6` | Simultaneous provisioning sessions |
| Config URL | `http://192.168.88.5:8000/…` | Full URL served by HFS |

### 2 – Scan for Routers

Click **Scan for Routers**.

The tool probes every IP in the range for an open SSH port (TCP 22).  
When a port responds it logs in and retrieves:

- Router identity (`/system identity get name`)
- RouterOS version (`/system resource get version`)
- ether1 MAC address

Each discovered router appears as a row in the table, pre-checked.

> **Tip:** Click **Stop Scan** at any time; already-found routers remain in
> the table.

### 3 – Select Routers

Use the checkboxes in the **✓** column, or the **Select All / Deselect All**
buttons.

### 4 – Provision Selected Routers

Click **Provision Selected**.  A confirmation dialog shows the URL and
concurrency limit.  After confirming, the tool:

1. Connects to each selected router via SSH.
2. Runs `/tool fetch url="<CONFIG_URL>" mode=http dst-path=full-config.rsc`
3. Runs `/import file-name=full-config.rsc`

The **Status** column updates in real time:

| Status | Meaning |
|--------|---------|
| Discovered | Found during scan, not yet provisioned |
| Queued | Waiting for a concurrency slot |
| Connecting | Opening SSH session |
| Fetching config | Running `/tool fetch` |
| Importing config | Running `/import` |
| **Success** | Config applied successfully |
| **Failed: …** | Error; hover over the cell for the full message |

> **Note:** The SSH session will be dropped when `/import` restarts services
> or resets the network stack.  This is expected and does **not** indicate
> failure; the tool reports **Success** if the import command was dispatched.

### 5 – Logs

All activity is shown in the **Activity Log** panel at the bottom and saved to
`mikrotik_provisioner.log` in the same folder as the script.

---

## Provisioning Commands Sent

```routeros
/tool fetch url="http://192.168.88.5:8000/mikrotik-provision/full-config.rsc" mode=http dst-path=full-config.rsc
/import file-name=full-config.rsc
```

---

## Concurrency & Timeouts

| Parameter | Value | File constant |
|-----------|-------|---------------|
| Scan TCP timeout | 1.5 s | `SCAN_TIMEOUT` |
| Scan parallel threads | 60 | `SCAN_THREADS` |
| SSH connect timeout | 10 s | `SSH_TIMEOUT` |
| SSH connect retries | 2 | `SSH_RETRIES` |
| SSH command timeout | 30 s | `CMD_TIMEOUT` |
| Fetch / import timeout | 120 s | `FETCH_TIMEOUT` |
| Default provisioning concurrency | 6 | `DEFAULT_CONCURRENCY` |

All values can be adjusted at the top of `mikrotik_provisioner.py`.

---

## Troubleshooting

| Symptom | Likely cause / fix |
|---------|--------------------|
| No routers discovered | Check that routers obtained DHCP IPs; verify SSH is enabled in the Netinstall minimal config |
| `auth failed` in identity column | Wrong SSH username or password; routers may still have blank password – leave the Password field empty |
| Fetch fails with timeout | HFS is not running or the URL is wrong; check `http://192.168.88.5:8000/` from a browser on the laptop |
| Import succeeds but router is not configured | The `full-config.rsc` script may have syntax errors; test it manually on one router first |
| GUI freezes | Should not happen – all network operations run in background threads; if it does, increase `SCAN_TIMEOUT` |

---

## File Structure

```
mikrotik_provisioner.py   ← main application (single file)
requirements.txt          ← Python package list
README.md                 ← this file
mikrotik_provisioner.log  ← created at runtime
```
