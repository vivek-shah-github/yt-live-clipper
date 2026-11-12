YT LIVE CLIPPER - WEB VERSION
=============================

Turns one of your computers into a password-protected clipper website.
Anyone with the link + password can use it in a browser on any phone or PC,
with nothing to install. The clipping work runs on your computer.

START
  Mac     : double-click start-web.command  (first time: right-click > Open)
  Windows : double-click start-web.bat
  Needs Python 3.9+ (python.org). Everything else installs automatically,
  including the free Cloudflare tunnel that creates the public link.

  First start asks you to choose a password.
  The window then shows your link, like  https://word-word-word.trycloudflare.com
  Share the link and the password. Keep the window open: closing it stops the site.
  The link is also saved in current-link.txt.

GOOD TO KNOW
  - The link changes every time you restart. For a fixed address
    (e.g. clipper.madebytwins.com) set up a named Cloudflare Tunnel on a domain
    that uses Cloudflare, pointing to http://127.0.0.1:8765
  - Change the password: run start-web with  --password NEWPASSWORD
    (everyone is signed out).
  - Each visitor gets their own clips list. Finished clips download to the
    visitor's device; the copies on this computer are deleted after 24 hours
    (folder: web/data/files).
  - At most 2 exports run at the same time, so the computer stays usable.
  - On a Mac the computer is kept awake while the site runs. Keep it plugged in
    and connected to the internet.
  - Live recordings run on this computer, so a visitor can close their browser
    and come back to download it later (same browser).

Use it on your own streams or content you have permission to reuse.
