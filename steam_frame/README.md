Getting a shell on the Frame (needed once, for the install below or for copying files over SSH):
  1. On the headset: Steam Settings > System > Enable Developer Mode (turn it on).
  2. Same menu, Developer section on the left, near the bottom: Set User Password.
  3. From a PC on the same network: ssh steamos@frame
     ("frame" is the name under Steam Settings > System > Hostname; use yours, or the headset's IP.)
  Developer Mode is what opens ssh (also adb and rdp); there is no separate SSH switch.
  Valve's docs: https://partner.steamgames.com/doc/steamframe/setup and .../steamframe/debugging
  Without SSH, run the commands below in a Desktop Mode terminal on the headset instead.

The VR viewer's SteamVR binding is NOT installed on the Frame: it goes on the PC that runs SteamVR
(vr-bindings/README.md). The Frame's own browser needs no binding.

Install on the Frame (desktop mode terminal):
  cp media-dl-screenshot-sync.py ~/ ; mkdir -p ~/.config/systemd/user
  printf 'MEDIADL_URL=https://...\nMEDIADL_KEY=<admin api token>\n' > ~/.config/media-dl-screenshot-sync.env; chmod 600 ~/.config/media-dl-screenshot-sync.env
  cp media-dl-screenshot-sync.service ~/.config/systemd/user/ ; systemctl --user enable --now media-dl-screenshot-sync
Server: steam_ingest.py is registered in server.py; files land in <downloads>/Steam Frame/.
