# AICA Desktop Release 1.0.1

**Status:** Canonical Windows install established

| Field | Value |
|-------|--------|
| Product | AICA — Financial Intelligence |
| Version | **1.0.1** |
| Installer | `AICA_Setup_1.0.1.exe` |
| Canonical install | `%LOCALAPPDATA%\AICA\` |
| Launch | Desktop shortcut `AICA.lnk` |

## Paths

- Installer (user-facing): `%USERPROFILE%\Desktop\AICA\AICA_Setup_1.0.1.exe` (also on OneDrive Desktop `\AICA\` when Desktop is redirected)
- Executable: `%LOCALAPPDATA%\AICA\AICA.exe`
- Engine: `%LOCALAPPDATA%\AICA\AICA.Engine.exe`
- User config/logs: `%APPDATA%\AICA\`
- WebView2 profile: `%LOCALAPPDATA%\AICA\webview\`

## Upgrade policy

Same Inno `AppId` — future `AICA_Setup_x.y.z.exe` upgrades this installation in place (no parallel copies).

## Not in this release

Wake-word (“Hey Ira”) and speech-command accuracy improvements are deferred to the next task.
