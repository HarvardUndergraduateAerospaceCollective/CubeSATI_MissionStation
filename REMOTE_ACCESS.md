# Accessing the Mission Dashboard Remotely

The dashboard runs on the ground-station Pi (`hucsat-dashboard.service`,
port 5000) and is reachable from any browser — you don't need to be at
the Pi.

## Same network as the Pi (lab / same Wi-Fi)

Browse to:

    http://<pi-lan-ip>:5000

To find the Pi's LAN IP, run `hostname -I` on the Pi (first address).

## From anywhere (Tailscale — recommended)

The Pi is on our Tailscale network as `100.117.105.40`.

1. Install Tailscale on your laptop/phone: <https://tailscale.com/download>
2. Sign in and get invited to the tailnet (ask Jack to add you from the
   Tailscale admin console).
3. Browse to:

       http://100.117.105.40:5000

That's it — works from dorms, home, anywhere with internet. Tailscale
encrypts the connection end-to-end; nothing is exposed to the public
internet.

## Troubleshooting

- **Page won't load over Tailscale** — check Tailscale is connected
  (icon in your system tray / `tailscale status`), then confirm the
  dashboard is up: `sudo systemctl status hucsat-dashboard` on the Pi.
- **Stale layout after an update** — hard-refresh (`Ctrl+Shift+R`).

## Don't

- Don't port-forward 5000 on the router or otherwise expose the
  dashboard to the public internet — it has no authentication.
