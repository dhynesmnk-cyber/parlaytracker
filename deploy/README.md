# Running ParlayTracker on the laptop

The app runs on one dedicated laptop at home and is reachable only over Tailscale, from your
phones (SPEC.md section 12). Nothing is exposed to the internet.

**You need:** a 64-bit laptop with 4 GB of RAM or more (8 GB is comfortable), about 20 GB of free
disk, a USB stick for the Ubuntu installer, and about an hour.

## 1. Install Ubuntu Server on the laptop

1. Download **Ubuntu Server 24.04 LTS** from ubuntu.com and write it to the USB stick (for example
   with balenaEtcher).
2. Boot the laptop from the stick and install. Use the whole disk, and tick **Install OpenSSH
   server** so you can work on it from another computer.
3. Leave it plugged in permanently. A wired network connection is more reliable than Wi-Fi.

## 2. Set up Tailscale (free)

1. Create an account at tailscale.com. You sign in with Google, Microsoft, Apple or GitHub.
2. In the Tailscale admin console, under **DNS**, turn on **MagicDNS** and **HTTPS
   certificates**. The app's address needs both.
3. Install the Tailscale app on both phones and sign in.
4. Invite the second person from **Users → Invite**. They join with their own account.
5. Note each person's **login name** from the **Users** page (for example `alice@gmail.com` or
   `alice@github`). These go into `ALLOWED_LOGINS`.

## 3. Run the setup script

On the laptop (or over SSH):

```bash
curl -fsSL https://raw.githubusercontent.com/dhynesmnk-cyber/parlaytracker/main/deploy/setup.sh -o setup.sh
sudo bash setup.sh
```

The laptop follows `main`: whatever is merged there is deployed automatically. To try a branch
before it's merged, run `sudo BRANCH=<branch> bash setup.sh`, then run `sudo bash setup.sh`
again afterwards to go back to `main`.

The script:
- installs Docker and Tailscale, and asks you to sign the laptop in to Tailscale;
- stops the laptop sleeping when the lid is closed;
- asks for your display time zone, the allowed logins and your API keys (both keys can wait);
- starts the app and turns on automatic updates and nightly backups;
- prints the app's address (`https://<laptop-name>.<tailnet>.ts.net`).

Open that address on a phone with Tailscale switched on, and add it to your home screen.

It's safe to run the script again at any time.

**Letting Claude do it:** run `claude remote-control` in a terminal on the laptop (in
`/opt/parlaytracker` once it exists). The session appears in the Claude Code app, and Claude
can run the setup for you there.

## 4. Copy backups off the laptop

A backup that only exists on the laptop is lost with the laptop.

1. `sudo rclone config`, then create a remote, for example `gdrive` for Google Drive. The laptop
   has no browser, so rclone prints a command to run on another computer to authorise it.
2. In `/opt/parlaytracker/.env`, set `BACKUP_RCLONE_REMOTE=gdrive:parlaytracker-backups`.
3. Check it works: `sudo /opt/parlaytracker/deploy/backup.sh`.

## 5. Prove a restore works (do this once)

```bash
sudo /opt/parlaytracker/deploy/backup.sh
ls -t /var/backups/parlaytracker/        # the newest file is first
sudo /opt/parlaytracker/deploy/restore.sh /var/backups/parlaytracker/<newest>.dump
```

Then open the app and check your slips are all there.

## Day to day

| Task | How |
|---|---|
| Get new versions | Automatic: anything merged into `main` is deployed within about 5 minutes |
| Check everything | `sudo /opt/parlaytracker/deploy/status.sh` |
| Read the logs | `sudo /opt/parlaytracker/deploy/compose.sh logs -f web worker` |
| Update history | `journalctl -u parlaytracker-update` |
| Change a setting | Edit `/opt/parlaytracker/.env`, then `sudo /opt/parlaytracker/deploy/compose.sh up -d` |
| Add a user | Invite them to the tailnet, add their login to `ALLOWED_LOGINS`, then `compose.sh up -d` |
| Settle old games once (after the first install, or a long outage) | `compose.sh stop worker`, then `compose.sh run --rm worker python -m parlaytracker.cli backfill`, then `compose.sh start worker` |
| Save a raw ESPN/Odds response as a test fixture | `compose.sh run --rm worker python -m parlaytracker.cli export-sample <id> /tmp/sample.json` |

**If the laptop is off or offline during games:**
- closing lines for those games are lost for good;
- live tracking pauses;
- results still settle once it's back (the worker asks ESPN for finished games every 15 minutes, and NFL results are also checked against nflverse each morning).

The app shows a banner when the background worker has stopped.

## Troubleshooting

| You see | Do this |
|---|---|
| "Open ParlayTracker through Tailscale" | You used the laptop's IP address or `localhost`. Use the `https://…ts.net` address |
| "This Tailscale account isn't allowed" | Add that login to `ALLOWED_LOGINS` in `.env`, then run `compose.sh up -d` |
| `tailscale serve` complains about HTTPS | Turn on HTTPS certificates in the Tailscale admin console (step 2) |
| "The background worker was last seen …" | Run `status.sh`, then `compose.sh logs worker` |
| The page won't load at all | Check Tailscale is on (phone and laptop), then run `status.sh` |

## Security

- Never use Tailscale **Funnel**, and never forward port 8501 on your router. The app trusts
  Tailscale to say who you are, which only holds while Tailscale Serve is the only way in.
- `/opt/parlaytracker/.env` holds your API keys. It stays readable by root only and is never
  committed.
- The laptop only ever pulls code from GitHub. Nothing can push code onto it.
