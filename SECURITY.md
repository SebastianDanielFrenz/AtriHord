# Security

Report vulnerabilities privately to the maintainer (a private security advisory on the repository, or e-mail), not
in a public issue.

## Trust model

* **No login of its own.** AtriHord believes the `Remote-User` / `Remote-Groups` headers, but only from the
  addresses in `MEDIA_PROXY_IPS` (default: this machine). Whatever can reach the app from such an address can
  claim any identity: keep the app on `127.0.0.1` or a private network, put the proxy in front, list only the proxy.
* Two roles: **admin** (downloads, settings, library changes, upload, trash) and **guest** (browse and watch).
* Public share links (`/share/<token>`, `/api/public/...`) need no login; a link only reaches the files its
  share covers.
* `MEDIA_SINGLE_USER` turns the login off; the server refuses to start with it unless bound to localhost.
* API clients use `X-API-Key` (per-user token).
* The vision worker runs models on whatever it is sent: it binds to localhost; exposing it requires a token.

## Deployment checklist

1. Bind to localhost (the default) and put a reverse proxy with an identity provider in front
   (`deploy/Caddyfile.example`, `deploy/authelia-access-control.example.yml`).
2. List only the proxy in `MEDIA_PROXY_IPS`; make the proxy remove any `Remote-User` / `Remote-Groups` /
   `X-Forwarded-Host` sent by visitors and set its own (the example does).
3. Set a token on the vision worker if it is not on the same machine, and keep it on a private network.
4. Keep the app folder (`users.json`, `shares.json`, cookie files) readable by the service account only.
5. Public instance: turn off the public-share GPU features (upscale, 2D to 3D, smooth motion) in settings
   unless you want anonymous visitors to spend your GPU time.
6. Pin a revision for any model loaded with remote code (Florence-2).
