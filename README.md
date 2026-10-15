# Apple iCloud Fix for Home Assistant

Patched version of the built-in Home Assistant **Apple iCloud** integration
(`icloud` domain). It is based on
[mdeuerlein/homeassistant-icloud-fix](https://github.com/mdeuerlein/homeassistant-icloud-fix)
(2FA code request fix), which is in turn based on the Home Assistant Core
integration (Apache-2.0).

> While installed, this replaces Home Assistant's built-in Apple iCloud
> integration (same `icloud` domain).

## What this version fixes

| Problem | Before | Now |
|---|---|---|
| **Re-authentication after every restart** when iCloud is not reachable for a moment (DNS not ready at boot, Apple 5xx, timeouts) | pyicloud reports network errors as "login failed", so HA asked for the password again | Temporary errors are recognised: HA retries setup by itself (`SETUP_RETRY`) and never asks for the password |
| **Trusted session thrown away** at startup when pyicloud says "2FA required" although a valid trust token is saved | Immediate re-authentication with a new Apple code | The saved session/trust token is used to log in again silently; a code is requested only if Apple really revoked the trust |
| **Polling stops forever** after a network error during a refresh | Location updates silently stopped until the next restart | The error is logged and polling retries after 5 minutes |
| **Login storms** while a re-authentication is pending | The polling loop kept logging in from scratch, triggering Apple sign-in pop-ups | No new logins until the user completes the re-authentication |
| **Lost or expired 2FA code** | Only way out: restart the whole flow | **"I did not get the code: send a new one"** checkbox in the code form, optionally **by SMS** |
| **Second login after entering the code** | The flow logged in again from scratch after a valid code | The already verified and trusted session is reused, and trust is confirmed explicitly |
| Empty code submitted | Generic error | Clear message |

The code form also shows where Apple sent the code (Apple devices or SMS).
Translations: English, Italian, German, French.

Kept from the upstream fix: `pyicloud==2.5.0`, explicit `request_2fa_code()`
before the code form, no new code requested on every wrong entry, automatic
acceptance of updated Apple terms.

## Installation

### HACS (custom repository)

1. Create a full Home Assistant backup.
2. HACS → ⋮ → **Custom repositories** → add
   `https://github.com/LucaTomei/homeassistant-icloud-fix`, type **Integration**.
3. Search **Apple iCloud Fix**, download it and restart Home Assistant.

### Manual

Copy `custom_components/icloud` into your Home Assistant `custom_components`
folder and restart.

Your existing iCloud configuration is kept: no need to remove and re-add it.

## Getting a new code

In the "Apple Account code" step:

1. Tick **"I did not get the code: send a new one"** (and **"by SMS"** if you
   prefer a text message) and press **Submit** without entering a code.
2. Enter the new code and press **Submit**.

## Tests

The `tests` folder simulates Apple's behaviour (restart with a trusted
session, revoked trust, network errors at boot and while polling, wrong
password, new code, SMS, session reuse).

```bash
pip install -r requirements_test.txt
pytest
```

## In italiano

Versione corretta dell'integrazione Apple iCloud di Home Assistant:
niente più nuova autenticazione a ogni riavvio quando iCloud non è
raggiungibile per un momento, ripristino automatico della sessione di
fiducia, aggiornamenti che ripartono da soli dopo un errore di rete,
pulsante **"Non ho ricevuto il codice: inviane uno nuovo"** (anche via SMS)
e traduzione italiana.
