# SteamVR binding for the VR viewer on a PC

`chrome_frame_controller_dpad.json` is a SteamVR controller binding named **media-dl d-pad**. It is for
watching the media-dl VR viewer in **Chrome on a Windows PC through SteamVR, with Steam Frame controllers**.

You do **not** need it in the Steam Frame's own browser (the Chromium build that runs on the headset). That
browser reports the Frame controllers to the page directly.

## Install

The binding goes on the **Windows PC that runs SteamVR**, not on the headset. In PowerShell on that PC:

```powershell
irm https://raw.githubusercontent.com/SebastianDanielFrenz/AtriHord/main/vr-bindings/install-windows.ps1 | iex
```

Then start, or restart, SteamVR and open the VR page in Chrome. SteamVR picks the binding up on its own.

`install-windows.ps1` downloads `chrome_frame_controller_dpad.json` from this folder of the repository and
registers it for the installed Chrome version. SteamVR names Chrome's entry after its major version
(`google chrome 156.chrome.exe`), and a binding for another version is ignored. The script then writes the
file to `Documents\steamvr\input\imports\`, following a Documents folder that Windows moved to OneDrive.

- **Update** the binding, or follow a **Chrome update**: run the same command again.
- **By hand:** copy `chrome_frame_controller_dpad.json` into that folder, and change `app_key` at the end of it
  if your Chrome's major version is not 156.
- **Still the old buttons** (see the table): pick the binding once in SteamVR → Settings → Controllers →
  Manage Controller Bindings → Chrome → **media-dl d-pad**.

## Buttons

| Controller | Button | Wall | Viewer |
|---|---|---|---|
| Left | d-pad up | Forward | Next item |
| Left | d-pad down | Back (hold 3 s: exit) | Back (hold 3 s: exit) |
| Left | d-pad left / right | - | Previous / next item |
| Right | A | Recenter | Recenter |
| Right | B | Back (hold 3 s: exit) | Back (hold 3 s: exit) |
| Right | X (hold) | Tilt: the UI follows your head up and down | same |
| Right | Y | - | Toggle favourite |
| Either | Trigger | Select | Play / pause video |
| Either | Stick click | - | Next item |

Without the binding, SteamVR's default sends the left d-pad's up, left and right as one button, so the page
cannot tell them apart, and the right X and Y act as Back.

## How it works

Chrome on Windows only knows Quest-style controllers: on each hand, two face buttons and a thumbrest. The
Frame has a d-pad on the left and four face buttons on the right, so the binding encodes the extra buttons
as touch combinations that one thumb cannot produce:

- d-pad left = left thumbrest touched + left X touched, without a click; d-pad right likewise with left Y.
- Right X held = right thumbrest touched + right A touched, without a click; right Y likewise with right B.

The page (`web_files/js/vr/host.js`) decodes these. The headset's own browser gets the Frame controller
as-is (profile `valve-frame`) and takes the other branch in the same file.
