<p align="center">
  <img src="web/logo.svg" alt="Footage Harbor logo" width="72">
</p>

<h1 align="center">Footage Harbor</h1>

<p align="center">
  A little station for your SD cards.<br>
  Plug a card in, and your footage ends up where it belongs.
</p>

<p align="center">
  <a href="#install-it-on-a-pi-or-mini-pc">Install</a> ·
  <a href="docs/HOW_IT_WORKS.md">How it works</a> ·
  <a href="https://github.com/ProfessorEngineergit/footage-harbor/issues/new">Report a problem</a>
</p>

---

Hi! Footage Harbor is a vibe-coded tool I made to fix a problem I kept having: I lose SD cards.
Or I find them weeks later, full, and can't remember if I ever backed them up. Or I forget to
upload anything to the cloud at all, and then the card gets formatted for the next shoot.

So the idea is simple. A Raspberry Pi or a small mini PC sits on the shelf with a card reader
attached (ideally one of those hubs with lots of SD and microSD slots), and that's where the
cards live. You plug a card in, the station notices, copies everything new to an SSD and/or your
cloud, checks that every file actually arrived intact, and, only if you want it to, cleans the
card up afterwards. You manage all of it from a web page on your laptop or phone.

To be upfront: I built this with a lot of help from AI, quickly, because I needed it. I tested the
parts that matter most (copying, checking and deleting) and there are tests for them, but treat it
like what it is: a personal tool that might be useful to you too. If something looks off,
[please tell me](https://github.com/ProfessorEngineergit/footage-harbor/issues/new).
The interface is in German for now, because that's what I use every day.

## What it does

**Card dock.** Every reader slot shows up in the web interface. You see which card is in which
slot, what it's doing right now and how far along it is. Give your cards a name and a color
(the same color as the sticker on the card, then you find it immediately).

**Flows.** A small node editor where you decide what happens when a card is plugged in. Drag
steps onto the canvas and connect them: *card plugged in → only RAW and video → copy to the SSD →
upload to Nextcloud → clean up the card → eject*. You can branch, too: *if the label is `DJI*`
go this way, otherwise that way*. Each card can get its own flow, or the first matching one is
used. There is a dry run that shows you what a flow would do with the card that's plugged in
right now, without touching anything.

**Checked, not just copied.** "Upload finished" and "the file is really there and identical" are
two different things. Every copy is checked: local copies are read back and compared with
SHA-256, cloud copies are compared against the provider's checksum or, if there isn't a
suitable one, downloaded again and compared. Plug the same card in twice and nothing gets copied
twice.

**Careful cleanup.** Deleting is off unless you add a cleanup step, and even then it asks you
first by default. Only files whose copies were verified in that run are deleted, and each one is
hashed again right before. Filtered-out files, new files and changed files stay. Folders stay.
The card is never formatted. Pull the card out and everything stops.
[`docs/HOW_IT_WORKS.md`](docs/HOW_IT_WORKS.md) has the full rules.

**Sign in from your own computer.** This was important to me: you never type a cloud password
into the Pi. For Nextcloud you just click "grant access" in your normal browser, where you're
probably already logged in. For Google Drive, Dropbox, Box and pCloud you sign in in your browser
as well and paste one address back (the interface walks you through it). The station gets its own
access that you can revoke at any time. SFTP, SMB/NAS, WebDAV and S3 work with a small form, and
anything else rclone supports (OneDrive, iCloud Drive, …) can be added by pasting an rclone config.

**History.** Every import is listed with every file, where its copies are and how they were
checked. You can download it as a JSON report.

## Install it on a Pi or mini PC

You need a Debian-based Linux: Raspberry Pi OS **Lite**, Debian or Ubuntu Server. A Pi 4 or 5 is
plenty. Put your archive on an SSD, not on the Pi's own SD card.

```bash
curl -fsSL https://raw.githubusercontent.com/ProfessorEngineergit/footage-harbor/main/install.sh | sudo bash
```

That's it. The script installs the few things it needs (Python 3, rclone, exFAT/NTFS support),
sets up a service that starts on boot, and tells you the address to open. On the first visit you
pick a password and where the archive goes.

Running the same command again updates Footage Harbor and keeps your data. To remove it:

```bash
sudo bash /opt/footage-harbor/uninstall.sh
```

A few things worth knowing:

- Use a **Lite / server image**. Desktop images like to automount cards on their own, which gets
  in the way. Footage Harbor mounts cards itself, read-only, and only makes them writable for the
  moment it deletes something.
- Cheap multi-slot readers often only show one card at a time. If you want several cards in at
  once, a powered USB hub with several separate readers works best.
- The station runs as root, because mounting cards needs it. It only listens in your local
  network. Don't forward it to the internet.

## Try it on your own computer

You don't need a Pi to play with it. In development mode every folder inside a directory counts
as a plugged-in card, so you can drag folders in and out:

```bash
git clone https://github.com/ProfessorEngineergit/footage-harbor
cd footage-harbor
mkdir -p dev-cards dev-data
python3 -m footage_harbor --port 8765 --host 127.0.0.1 --data dev-data --cards-dir dev-cards
```

Open http://127.0.0.1:8765, set it up, then copy a folder with some photos into `dev-cards/`.
Rename it to start with a dot (`.MYCARD`) to "pull it out" again. There are no dependencies
besides Python 3.9+ (and rclone, if you want cloud destinations).

The tests cover copying, verification, re-inserting, versioning and every cleanup rule:

```bash
python3 -m unittest discover tests
```

## How it is built

On purpose, there's very little: a Python service using only the standard library, SQLite for the
card list, flows and checksums, and [rclone](https://rclone.org/) for everything cloud. The web
interface is plain HTML, CSS and JavaScript, no build step, no framework. That keeps the install
quick and means there's nothing to break on a small device.

```
footage_harbor/   the service: card detection, import engine, cloud sign-in, API
web/              the interface, including the flow editor
systemd/          the service file
tests/            tests for the import and cleanup rules
```

## Helping out

Ideas, bug reports and pull requests are all welcome. I'm especially curious which card readers
and hubs work well for people, since that's the part I can test the least.

## License

MIT, see [LICENSE](LICENSE). rclone and the other tools it installs keep their own licenses.
