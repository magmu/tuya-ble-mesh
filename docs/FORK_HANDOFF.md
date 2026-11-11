# Fork handoff (magmu/tuya-ble-mesh)

State of this fork for anyone, human or agent, picking up work here. It was last updated at v0.42.13 (2026-10-06).

This fork of [11z4t/tuya-ble-mesh](https://github.com/11z4t/tuya-ble-mesh) adds SIG Mesh lights reached through an ESPHome Bluetooth proxy. It also carries fixes for Tuya white-label Telink lights. Keep crediting the original project in the README.

## How work lands

- Work goes through PRs to `main`. The maintainer merges; agents never merge or approve.
- Bumping `version` in `custom_components/tuya_ble_mesh/manifest.json` and merging to `main` publishes a release (`.github/workflows/release.yml`). HACS only offers proper updates through releases, so bump the version in any PR that changes the integration. Docs-only PRs don't need a bump.
- CI (`.github/workflows/ci.yml`) must be green before asking for a merge. It runs ruff (Markdown excluded), mypy on lib and integration, pytest on Python 3.13 using `requirements_test.txt`, hassfest, HACS validation, icon-check, and a security job running bandit, pip-audit (manifest requirements) and detect-secrets (PR #22).
- To check locally: `ruff check` and `ruff format --check` on `custom_components tests`; `mypy custom_components/tuya_ble_mesh`; `pytest tests/unit tests/security tests/integration`. Don't use `pip install -e .`, because it fails on the flat layout.
- hassfest rejects manifest requirements that Home Assistant already ships (for example `cryptography`).
- Two PRs that both bump the version conflict on `manifest.json`. Merge `main` into the second one and keep the higher version.
- Public repo rules:
  - No links to private chat, project or session pages in PRs, comments or commits.
  - PR bodies end with a plain "Generated with Claude Code" line.
  - Never log or print secrets (see `CLAUDE.md`).

## Tests compared with the original repo

At v0.42.13 the fork contains every commit of the original repo up to its v0.38.1 release, and none of its test files or test functions were removed. CI runs `tests/unit`, `tests/security` and `tests/integration`: 2322 tests, against 2196 on the original repo's `main`. The original CI ran a narrower set.

Two suites exist but are not run in CI, here or in the original repo:
- `tests/e2e`: Playwright tests that need a live Home Assistant instance (see `tests/e2e/SETUP-PRODUCTION-HA.md`).
- `tests/hardware`: tests that need a real adapter and devices.

## Device findings

### Classy Caps Lumineer solar post cap (SIG Mesh, CID `07D0`, PID `0300`)

Device type is `sig_light`. Status: working.

- **Light state:**
  - The cap answers Light CTL Get but drops CTL Set. White works as Lightness Set plus CTL Temperature Set on the CTL Temperature element, across its full 800–20000 K range.
  - State is read back after every connect.
  - Sequence numbers are saved after every command, so restarts don't replay them.
- **Solar sleep:** the cap is solar powered and stops advertising in daylight. The per-device **Solar powered** switch silences warnings and repairs from civil dawn to dusk, reconnects pause while the cap isn't advertising, and a cap still missing after dark raises a device-not-found repair.
- **Tuya vendor model:**
  - The cap ignores the Tuya "report all" query and sends no data points.
  - It asks for the time (cmd `0x02`, data `06`) after a power cycle only. The reply uses calendar format 4 first: year (2 B, BE), month, day, hour, minute, second, weekday (0 = Sunday), then the timezone in hundredths of an hour (2 B, BE, signed). It looked accepted, but this is not confirmed.
- **Benign log lines and connection drops:**
  - "Upper transport decryption failed (akf=0)" right after a segmented send is the cap's segment ack, and is harmless.
  - The connection drops are not caused by time sync.
  - No battery data is available over mesh.

### Tuya `0x0102` lights: Smart Life "WC Bulb" (`bXun1QKL`, MAC prefix `BC:23:4C`) and GE imagine Paragon downlights (`9lcrglpq`, MAC prefix `DC:23:4D`)

Status: the WC bulb was fixed in v0.42.13 from robin1333k's report and patch (11z4t/tuya-ble-mesh#2). robin1333k's later corrections are in v0.42.14. The GE downlights were added by oscarmorrison in PR #23 and tested on his lights. Nobody has run v0.42.14 on a WC bulb yet.

The integration recognises these lights from status packets that carry vendor bytes `02 01` or `05 01`, or from vendor `0x0102` set in setup. Their commands are:

- Power is `0xD0` sent with vendor `0x0102`. The lights ignore DP 121.
- Brightness, white and RGB are one `0xE2` packet with vendor `0x0102`, which is what the Smart Life app sends. The lights ignore compact DP writes.

How they behave:

- **Status:** `0xDB` (answer to a `0xDA` query) carries colour temperature and set brightness. `0xDC`, sent on every change and after a `0x01` write to `…1911`, carries the current brightness, with 0 meaning off. On/off comes from `0xDC`, and until the first one arrives HA shows what it sent.
- **Confirmation:** a status query goes out 0.6 s after each command. 3.5 s after the command, HA compares the target with the light's report and resends up to twice. It only resends when a report has arrived since the command.
- **Sequence:** the light drops packets whose sequence number is not above the last it saw, so the counter starts from the clock (PR #23). Before that, commands were ignored after every reload.
- **Pairing:** after SET_NAME and SET_PASS the light answers `0x06` until the long-term key is written. The key is sent only when the light asks for it. A light already on its own credentials is logged in to without rewriting them, so it no longer flashes on connect.
- **Command spacing:** the light drops packets that arrive back to back, so every write is spaced at least 0.4 s apart. This is done in `BLEConnection`.
- **Notifications:** the WC bulb never answers the CCCD write. BlueZ (5.85 too) times out after 30 s and drops the link, so `start_notify` is skipped for that address after one failure. Without a subscription BlueZ delivers no notifications, so status then comes only from the queries above. robin1333k got reliable status with a raw L2CAP ATT client instead of bleak. That client is Linux-only, does not work through an ESPHome proxy, and is not included here.

`docs/PROTOCOL.md` section 8 has the byte layouts.

## Open items

- Get a v0.42.14 debug log (pairing, on/off, a reload) from a WC bulb owner, and check whether status arrives in poll-only mode on BlueZ. If it doesn't, consider robin1333k's L2CAP ATT client as an opt-in for local adapters.
- The `0xDB` white channel order differs between the WC bulb (cold first) and the GE downlights (warm first). Decoding assumes cold first.
- Check whether HSL colour on SIG lights looks dim. It maps to 0–50 % lightness, a one-line change.
- Replace the deprecated `show_advanced_options` (5 call sites) before Home Assistant 2027.6.
- Issues in the original 11z4t repo have not been reviewed against this fork. Its documents in `docs/BRANDS_SUBMISSION.md`, `docs/HA_CORE_SUBMISSION.md` and `docs/HA_DOCS_DRAFT.md` still point to the original repo on purpose.
- The GitHub About text and the Website field are set by hand in the repository settings.
