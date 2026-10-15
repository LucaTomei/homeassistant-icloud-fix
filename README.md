# Apple iCloud Fix for Home Assistant

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/docs/faq/custom_repositories)
[![GitHub Release](https://img.shields.io/github/v/release/LucaTomei/homeassistant-icloud-fix)](https://github.com/LucaTomei/homeassistant-icloud-fix/releases)
[![CI](https://github.com/LucaTomei/homeassistant-icloud-fix/actions/workflows/ci.yml/badge.svg)](https://github.com/LucaTomei/homeassistant-icloud-fix/actions/workflows/ci.yml)
[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-2025.1%2B-18BCF2.svg)](https://www.home-assistant.io/)
[![License](https://img.shields.io/github/license/LucaTomei/homeassistant-icloud-fix)](LICENSE)

**Stay signed in to iCloud across Home Assistant restarts.**

A drop-in replacement for the Home Assistant **Apple iCloud** integration that
keeps your trusted session alive, survives network hiccups and lets you request
a new 2FA code (also by SMS) without starting over.

> [!NOTE]
> This integration uses the same `icloud` domain as the built-in one: while it
> is installed, it replaces Home Assistant's Apple iCloud integration. Your
> existing configuration is kept, no need to remove and re-add it.

## ✨ Highlights

- 🔐 **No re-authentication after a restart** while Apple still trusts the session
- 🌐 **Resilient to network errors** at boot and while polling
- 📲 **New 2FA code on demand**, on your Apple devices or by SMS
- 🚦 **No login storms** while a re-authentication is pending
- 🌍 Translated into **English, Italian, German and French**

## 🛠️ What this version fixes

| Problem | Before | Now |
|---|---|---|
| **Re-authentication after every restart** when iCloud is not reachable for a moment (DNS not ready at boot, Apple 5xx, timeouts) | pyicloud reports network errors as "login failed", so HA asked for the password again | Temporary errors are recognised: HA retries setup by itself (`SETUP_RETRY`) and never asks for the password |
| **Trusted session thrown away** at startup when pyicloud says "2FA required" although a valid trust token is saved | Immediate re-authentication with a new Apple code | The saved session/trust token is used to log in again silently; a code is requested only if Apple really revoked the trust |
| **Polling stops forever** after a network error during a refresh | Location updates silently stopped until the next restart | The error is logged and polling retries after 5 minutes |
| **Login storms** while a re-authentication is pending | The polling loop kept logging in from scratch, triggering Apple sign-in pop-ups | No new logins until the user completes the re-authentication |
| **Lost or expired 2FA code** | Only way out: restart the whole flow | **"I did not get the code: send a new one"** checkbox in the code form, optionally **by SMS** |
| **Second login after entering the code** | The flow logged in again from scratch after a valid code | The already verified and trusted session is reused, and trust is confirmed explicitly |
| **No code sent during re-authentication** when Apple still accepted the saved session token but no longer trusted it | "Failed to send verification code" every time | A password sign-in starts a real 2FA challenge, so Apple sends the code |
| **Find My not reachable at startup** | Setup failed until the next restart | Setup is retried automatically |
| **Old sessions left running** after a reload or a re-authentication | Background refreshes kept saving old tokens over the new trusted session | Old sessions are stopped, only the current one is saved |
| **Stale locations** with pyicloud 2.5 | Positions came from a cache, Apple was never asked to locate the devices | Every update asks Apple for fresh locations |
| **Empty code submitted** | Generic error | Clear message |

Also included:

- The code form shows where Apple sent the code (Apple devices or SMS).
- `pyicloud==2.5.0`, explicit `request_2fa_code()` before the code form.
- No new code requested on every wrong entry.
- Updated Apple terms are accepted automatically.

## 📦 Installation

> [!TIP]
> Create a full Home Assistant backup before installing.

### HACS (recommended)

[![Open your Home Assistant instance and open this repository in HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=LucaTomei&repository=homeassistant-icloud-fix&category=integration)

Or manually in HACS:

1. **HACS → ⋮ → Custom repositories**.
2. Add `https://github.com/LucaTomei/homeassistant-icloud-fix` with type **Integration**.
3. Search for **Apple iCloud Fix**, download it and **restart Home Assistant**.

### Manual

1. Download `icloud.zip` from the
   [latest release](https://github.com/LucaTomei/homeassistant-icloud-fix/releases/latest).
2. Extract it into `config/custom_components/icloud/`.
3. Restart Home Assistant.

## ⚙️ Configuration

If iCloud is not configured yet:

[![Open your Home Assistant instance and start setting up Apple iCloud.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=icloud)

Or go to **Settings → Devices & services → Add integration → Apple iCloud**.

### Getting a new code

In the **Apple Account code** step:

1. Tick **"I did not get the code: send a new one"** (and **"by SMS"** if you
   prefer a text message) and press **Submit** without entering a code.
2. Enter the new code and press **Submit**.

## 🐞 Troubleshooting

Enable debug logs in `configuration.yaml`, restart and reproduce the problem:

```yaml
logger:
  default: warning
  logs:
    custom_components.icloud: debug
    pyicloud: debug
```

Then open an [issue](https://github.com/LucaTomei/homeassistant-icloud-fix/issues)
with the relevant log lines (remove your Apple ID and any personal data).

## 🧪 Tests

The `tests` folder simulates Apple's behaviour: restart with a trusted session,
revoked trust, network errors at boot, in Find My and while polling, wrong
password, re-authentication with an untrusted saved session, reload and
unload, new code, SMS and session reuse.

```bash
pip install -r requirements_test.txt
pytest
```

## 🇮🇹 In italiano

Versione corretta dell'integrazione Apple iCloud di Home Assistant:

- niente più nuova autenticazione a ogni riavvio quando iCloud non è
  raggiungibile per un momento;
- ripristino automatico della sessione di fiducia;
- aggiornamenti che ripartono da soli dopo un errore di rete;
- pulsante **"Non ho ricevuto il codice: inviane uno nuovo"** (anche via SMS);
- traduzione italiana.

Installazione: HACS → ⋮ → **Custom repositories** → aggiungi
`https://github.com/LucaTomei/homeassistant-icloud-fix` come **Integrazione**,
scarica **Apple iCloud Fix** e riavvia Home Assistant.

## 📄 License

Released under the [Apache License 2.0](LICENSE).

Based on the Apple iCloud integration of
[Home Assistant Core](https://github.com/home-assistant/core) (Apache-2.0)
and on the fork by
[mdeuerlein](https://github.com/mdeuerlein/homeassistant-icloud-fix).
