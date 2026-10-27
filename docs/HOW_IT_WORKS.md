# How it works

This is the longer version of what happens between plugging a card in and pulling it out
again, written down mostly so you can check whether you trust it with your footage.

## When a card goes in

1. The station checks every couple of seconds which removable disks are connected (`lsblk`).
   Disks that hold the system or one of your local destinations are never treated as cards.
2. The card is mounted **read-only** (`ro,noexec,nodev,nosuid`). Nothing on the card is
   ever executed.
3. The card is recognised by its volume ID, label and size. If a camera formatted it (new
   volume ID, same label and size), it's still recognised as the same card.
4. The flow is picked: the card's own flow if it has one, otherwise the first flow in the list
   whose trigger matches (any card, unknown cards, a label pattern like `SONY*`, or one specific
   card).

## While a flow runs

The steps run in the order they're connected, left to right. A step only sees the files the
step before it passed on, so a filter in front of a backup step means only those files get backed up,
and only those can be cleaned up later.

**Backup to a local disk.** Each file is streamed into a temporary file, hashed (SHA-256, MD5 and
SHA-1 in one pass) and synced to disk, then renamed into place. Afterwards the copy is read back
completely and its SHA-256 compared with the original. If the file changed on the card while it
was being read, it's treated as an error.

**Backup to the cloud.** Uploads come from the verified local copy when there is one, otherwise
straight from the card. Afterwards the station asks the provider for a checksum it already has
(SHA-256, SHA-1 or MD5). If the provider doesn't offer one, the file is downloaded again and
hashed. A finished upload alone never counts.

**Already there?** Before copying, the station looks for a receipt of the same content at that
destination. If the file is still there with the right size, it's skipped. When the flow contains
a cleanup step, "still there" is checked by content instead of size.

**Same name, different content** never overwrites anything. The new file gets ` (2)` and so on.

## Cleanup rules

These can't be switched off by a flow:

- A file is only deleted if every required destination before the cleanup step holds a copy that
  was verified **in this run**.
- Right before deleting, the file on the card is hashed again. If it changed, it stays.
- Files that were filtered out, new files, and anything the flow didn't see stay on the card.
- Folders are never deleted and the card is never formatted (cameras need their folder structure).
- If the card is pulled out, everything stops immediately. The next time it's plugged in, a new
  run starts from scratch; old approvals are never reused.
- The card is only writable for the duration of the cleanup step and is set back to read-only
  right after.
- By default the station asks before deleting. You see how many files would go and approve or
  decline in the dock.

Recommended: two independent destinations (for example SSD + cloud) before a cleanup step.
Two folders on the same SSD are still just one copy.

## What it can't promise

A verified copy is verified at that moment. If the SSD dies later or someone deletes the cloud
folder, Footage Harbor can't know. And FAT/exFAT cards can still get corrupted if the power goes
out while something is being deleted. That's why deleting is opt-in and asks first.

## Signing in to cloud services

The station never asks for your cloud password.

- **Nextcloud** uses Nextcloud's own *Login Flow v2*: the station asks your Nextcloud for a login
  link, you approve it in your browser, and the station receives an app password that shows up
  under *Settings → Security → Devices* in Nextcloud, where you can revoke it.
- **Google Drive, Dropbox, Box, pCloud** use `rclone authorize` running on the station. The
  provider's login page opens in your browser. Afterwards the provider sends your browser to
  `127.0.0.1`, which is your own computer, so you see an error page. You paste that address back
  into the interface, and the station replays it against its own local rclone, which then
  receives the token.
- Everything else rclone can do can be added by pasting an rclone config section.

Tokens are stored in `/var/lib/footage-harbor/rclone.conf`, readable only by root.
