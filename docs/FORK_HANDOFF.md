# Fork handoff (magmu/tuya-ble-mesh)

State of this fork for anyone, human or agent, picking up work here. It was last updated at v0.42.13 (2026-10-06).

This fork of [11z4t/tuya-ble-mesh](https://github.com/11z4t/tuya-ble-mesh) adds SIG Mesh lights reached through an ESPHome Bluetooth proxy. It also carries fixes for Tuya white-label Telink lights. Keep crediting the original project in the README.

## How work lands

- Work goes through PRs to `main`. The maintainer merges; agents never merge or approve.
- Bumping `version` in `custom_components/tuya_ble_mesh/manifest.json` and merging to `main` publishes a release (`.github/workflows/release.yml`). HACS only offers proper updates through releases, so bump the version in any PR that changes the integration. Docs-only PRs don't need a bump.
- CI (`.github/workflows/ci.yml`) must be green before asking for a merge. It runs ruff (Markdown excluded), mypy on lib and integration, pytest on Python 3.13 using `requirements_test.txt`, hassfest, HACS validation, and icon-check.
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

### Tuya white-label Telink ceiling light (Smart Life "WC Bulb", product `bXun1QKL`, MAC prefix `BC:23:4C`)

Status: fixed in v0.42.13 from a user's report and local patch tested in Home Assistant. Nobody has confirmed it on hardware since.

The integration recognises the light from status packets that carry vendor bytes `02 01`. Its commands are:

- Power is `0xD0` sent with vendor `0x0102`. The light ignores DP 121.
- Brightness is DP 122, sent with vendor `0x1001`.
- White is DP 123, sent with vendor `0x1001`. Only the second-lowest byte is read: `0x0100` is full warm and `0xFF00` full cold (`0` is ignored).

How it behaves:

- **Status:** status comes as `0xDB` packets: byte 13 cold, byte 14 warm, byte 15 brightness %. There is no on/off field, and the lamp reports only every 30 s, so Home Assistant assumes the values it sent.
- **Pairing:** after SET_NAME and SET_PASS the light answers `0x06` until the long-term key is written. The key is sent only when the light asks for it.
- **Command spacing:** the lamp drops commands that arrive back to back, so the dispatcher spaces sends at least 0.4 s apart.
- **Notifications:** `start_notify` can kill the BlueZ link. After one failure the integration skips it for that address, even across reloads, and a link that dies during notification setup fails the connect.

`docs/PROTOCOL.md` section 8 has the details. The Smart Life app also uses `0xE2` commands with vendor `0x0102`; they are not used here because nobody has tested them from Home Assistant.

## Open items

- Get a test report (debug log of pairing and on/off) from an owner of the WC Bulb.
- Check whether HSL colour on SIG lights looks dim. It maps to 0–50 % lightness, a one-line change.
- Replace the deprecated `show_advanced_options` (5 call sites) before Home Assistant 2027.6.
- Issues in the original 11z4t repo have not been reviewed against this fork. Its documents in `docs/BRANDS_SUBMISSION.md`, `docs/HA_CORE_SUBMISSION.md` and `docs/HA_DOCS_DRAFT.md` still point to the original repo on purpose.
- The GitHub About text and the Website field are set by hand in the repository settings.
