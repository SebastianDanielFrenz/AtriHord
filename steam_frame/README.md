Install on the Frame (desktop mode terminal):
  cp media-dl-screenshot-sync.py ~/ ; mkdir -p ~/.config/systemd/user
  printf 'MEDIADL_URL=https://...\nMEDIADL_KEY=<admin api token>\n' > ~/.config/media-dl-screenshot-sync.env; chmod 600 ~/.config/media-dl-screenshot-sync.env
  cp media-dl-screenshot-sync.service ~/.config/systemd/user/ ; systemctl --user enable --now media-dl-screenshot-sync
Server: steam_ingest.py is registered in server.py; files land in <downloads>/Steam Frame/.
