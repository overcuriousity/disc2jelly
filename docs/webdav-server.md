# Setting up a WebDAV destination

Disc2Jelly uploads finished files over WebDAV with HTTP Basic auth. Two kinds of
server work, and the client picks its upload strategy from the URL:

| Server | URL shape | Uploads |
|---|---|---|
| Nextcloud / ownCloud | `…/dav/files/<user>/…` | chunking v2 (resumable-style, 64 MiB parts) for files ≥ 256 MiB |
| Any plain WebDAV (rclone, nginx `dav_methods`, Apache `mod_dav`) | anything else | one streamed PUT, any size |

Chunking v2 is a Nextcloud-specific protocol, so a plain server gets single
PUTs. That is fine for DVD rips (1–2 GB) as long as the server and any reverse
proxy in front of it do not cap the request body — see below.

If you already run Nextcloud, just point Disc2Jelly at
`https://cloud.example/remote.php/dav/files/<user>/<folder>` with an app
password and skip the rest of this page.

## Plain WebDAV with rclone behind nginx

This is the light option: no PHP, no database, and the daemon runs as whatever
user already owns the media, so file ownership stays correct for Jellyfin.

Assumes an existing nginx TLS vhost (for example the one already proxying
Jellyfin) and media in `/mnt/media`.

### 1. Credentials

```bash
sudo apt install rclone apache2-utils          # Fedora: dnf install rclone httpd-tools
sudo htpasswd -B -c /etc/rclone-dav.htpasswd disc2jelly   # -c only the first time
sudo chown mediauser: /etc/rclone-dav.htpasswd
sudo chmod 600 /etc/rclone-dav.htpasswd
```

Use a long random password. It is a write credential for your media library.

### 2. The daemon

`/etc/systemd/system/rclone-dav.service` — replace `mediauser` with the account
that owns `/mnt/media`:

```ini
[Unit]
Description=WebDAV for /mnt/media
After=network.target

[Service]
User=mediauser
Group=mediauser
UMask=0002
ExecStart=/usr/bin/rclone serve webdav /mnt/media \
  --addr 127.0.0.1:8095 \
  --baseurl /dav \
  --htpasswd /etc/rclone-dav.htpasswd \
  --dir-perms 0775 --file-perms 0664
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now rclone-dav
```

It listens on loopback only; nginx terminates TLS in front of it.

### 3. Reverse proxy

Add to the TLS `server` block, **above** any `location /`:

```nginx
location /dav/ {
    proxy_pass http://127.0.0.1:8095;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;

    client_max_body_size 0;        # a rip is multi-GB; the default 1m rejects it
    proxy_request_buffering off;   # stream straight through, no disk spool
    proxy_buffering off;
    proxy_read_timeout 3600;
    proxy_send_timeout 3600;

    # Recommended if the app runs on your LAN or over VPN:
    # allow 192.168.0.0/16; allow 10.0.0.0/8; deny all;
}
```

`--baseurl /dav` makes rclone emit `/dav/…` hrefs in PROPFIND replies, so the
prefix must match the `location` and `proxy_pass` must have **no** trailing
path (which would strip it).

```bash
sudo nginx -t && sudo systemctl reload nginx
```

### 4. Verify before touching the app

```bash
curl -u disc2jelly:PASSWORD -X PROPFIND -H 'Depth: 0' https://media.example/dav/
```

`207 Multi-Status` with a `<D:href>/dav/</D:href>` means the whole chain works.
`401` = credentials, `404` = baseurl/location mismatch, `413` = missing
`client_max_body_size 0`.

## Point Disc2Jelly at it

Settings (gear icon) → Destination → **WebDAV**:

- URL: `https://media.example/dav`
- User / password: the htpasswd pair

By default Disc2Jelly creates `Movies/` and `Shows/` under that URL. Both are
configurable in Settings → Advanced (*Films folder* / *Series folder*), and the
URL should point at the directory those roots belong in — if your library lives
under `streaming-server/`, the URL is `https://media.example/dav/streaming-server`,
not `https://media.example/dav`.

## Jellyfin libraries

With the defaults, uploads land in `/mnt/media/Movies/…` and `/mnt/media/Shows/…`.
If your library folders are named differently (`movies`, `series`, …), note that
Linux paths are case-sensitive — `Movies` and `movies` are separate directories.
Set *Films folder* and *Series folder* to match what you already have, rather
than adding a second pair of folders to the Jellyfin library.

If your existing files are named `<Series> (<Year>) - S01E05.mkv` without a
`[tmdbid-…]` tag, also set *File naming* to **Plain style**. Otherwise new rips
get their own `<Series> (<Year>) [tmdbid-…]/` folder next to the one you have,
splitting the show across two directories on disk.
