# Steam Frame Audio Fix

[![Support on Ko-fi](https://img.shields.io/badge/Support%20on-Ko--fi-FF5E5B?logo=ko-fi&logoColor=white)](https://ko-fi.com/theinkedtail)

**Fixes Steam Frame audio going silent on USB-C headphone adapters, USB DACs and Bluetooth headphones whenever an app uses the microphone**, like loading into a VRChat world or joining a Discord call while streaming from your PC.

When an app opens the mic, SteamOS switches on an echo-cancel filter aimed at your external audio device. The audio system then leaves your games and apps **disconnected from your headphones**, so you hear nothing (no game audio, no SteamVR sounds) until the mic closes. Your adapter is fine: the audio just isn't being sent to it.

This script runs on the Frame in a Konsole window while you play. It watches for that exact situation and plugs your audio back into your headphones within about a second. It only reconnects audio: it doesn't install anything, edit settings files, or touch your system, and everything it does goes away when apps close or you reboot. When the bug isn't happening, it just watches.

It also has a **diagnose mode** that tests whether your Frame has the bug and writes a report you can add to Valve's bug thread: [ValveSoftware/SteamOS #2896](https://github.com/ValveSoftware/SteamOS/issues/2896).

> [!IMPORTANT]
> **If your audio stops working and the script doesn't bring it back, restart SteamVR on the headset.**
> That resets the Frame's audio connections and is what brought audio back in testing. Restarting PipeWire (the script's `p` option) won't. It leaves audio dead until SteamVR is restarted anyway.

> [!WARNING]
> **Unofficial, use at your own risk.** This is a community workaround, not made or endorsed by Valve. It was tested on one Steam Frame, and SteamOS updates may change how audio works. See the full [Disclaimer](#disclaimer).

---

## Do I have this bug?

You probably do if **all** of these are true:

- You use something other than the Frame's built-in speakers: a USB-C headphone adapter, USB DAC, or Bluetooth headphones.
- Audio works on loading screens and menus, but goes **completely silent** (game audio *and* SteamVR UI sounds) once something uses your mic.
- In VRChat, switching the in-game mic away from "Steam Streaming Microphone" makes the problem go away.
- Unplugging the adapter instantly brings sound back on the built-in speakers.

## Check that the bug still exists before using this

Valve may fix this in a SteamOS update, and then you won't need the script. Before relying on it:

1. **Check the [Valve issue](https://github.com/ValveSoftware/SteamOS/issues/2896).** If it's closed as fixed and you're on the latest SteamOS, try without the script first.
2. **Try it without the script.** Plug in your headphones, load into a VRChat world (or join a voice call). If your audio keeps working, you don't need this.
3. **Run the diagnose test.** Start the script and press **`d`** (see [Diagnose](#diagnose)). It tells you **AFFECTED** or **NOT AFFECTED**.

The script also checks your audio system version on startup. If it differs from what the script was tested on (PipeWire 1.6.8 / WirePlumber 0.5.18), it shows a warning and asks you to run diagnose before relying on it.

---

## Setup (one time)

Everything happens on the Frame, in desktop mode. Nothing to install: the script only uses what SteamOS already ships with (Python 3 and the PipeWire tools).

1. Switch the Frame to **desktop mode**.
2. Download **`frame-audio-fix.py`** from this repo (open the file on GitHub, then use the download button) and save it somewhere easy to find, like your home folder.
3. Make it runnable. In **Dolphin**, right-click the file → **Properties** → **Permissions** → tick **Is executable** → **OK**.

## Running it

1. In **Dolphin**, right-click `frame-audio-fix.py` → **Run In Konsole**.
2. **Leave the Konsole window open** while you play. Closing it stops the fix.

When the bug kicks in you'll see something like:

```
21:04:12 🎤 Mic turned on → audio bug is ACTIVE on UGREEN CM721 USB Audio (normal in a VRChat world / call)
21:04:12 🔧 Reconnected Steam Link (PC audio) → UGREEN CM721 USB Audio
```

When you're done, press `q` or close the window.

### Keys

Click the Konsole window first so it receives your key presses.

| Key | What it does |
|---|---|
| `s` | Status: output device, whether the bug is active, every audio stream |
| `a` | Turn auto-fix on/off |
| `f` | Fix right now |
| `m` | Fix all audio, or only Steam Link (PC/game audio) |
| `t` | Play a short quiet test beep |
| `o` | Switch output device (e.g. to the built-in speakers) |
| `d` | Diagnose (see below) |
| `c` | Undo the connections this tool made |
| `+` / `-` | Check more or less often |
| `l` | Show the last 20 log lines |
| `p` | Restart PipeWire. **Last resort:** all audio stays dead until you restart SteamVR on the headset |
| `h` | Help |
| `q` | Quit (Ctrl+C also works) |

### Options (for terminal users)

If you'd rather start it from a terminal, run `./frame-audio-fix.py` from its folder. These options are available:

```
--steam-link-only   only fix Steam Link (PC/game) audio
--interval N        seconds between checks (default 1)
--grace N           seconds to keep rescuing streams after the bug clears (default 20)
--dry-run           show what would be fixed without changing anything
--no-auto           start with auto-fix off (fix manually with f)
--plain             no colors or emoji
--once              do one fix pass and exit
--diagnose          test for the bug, save a report, and exit
```

---

## Diagnose

With the script running, press **`d`**.

It plays a **quiet tone for about 5 seconds**, briefly opens the mic, and watches whether your audio stays connected. It tells you **AFFECTED** or **NOT AFFECTED** and saves a report as `frame-audio-fix-report.md` in your home folder. Nothing is changed.

If you're affected, adding your report to [the Valve issue](https://github.com/ValveSoftware/SteamOS/issues/2896) helps them fix it. Open the report in Kate (or any text editor), copy everything, and paste it into a comment there.

---

## Safety

What the tool does:

- **Only acts while the bug is active**, plus a short grace period (20 s) after it clears, to catch apps that stay disconnected. The rest of the time it only watches.
- **Only creates audio connections.** It never edits config files, and its connections disappear on their own when apps close or you reboot.
- **Skips streams that an app deliberately routed elsewhere**, or that manage their own connections.
- **Backs off** if something keeps undoing a fix, instead of fighting it, and tells you.
- **Runs one copy at a time** and refuses to run as root.
- Keeps a log at `~/frame-audio-fix.log`, capped at 1 MB.

The only two actions that change anything beyond audio connections are ones you trigger yourself: `o` (switch default output) and `p` (restart PipeWire, which asks first).

## Known limits

- **Frame UI click sounds** are too short to catch, so they may stay silent while the bug is active. Game and PC audio is what this saves.
- If UI sounds stay dead **after** a session, **restart SteamVR on the headset**. Restarting PipeWire alone doesn't bring them back.
- Voice chat keeps working while the fix is active.
- Some Bluetooth headsets switch into a lower-quality "headset/call" mode when the mic opens. That's separate from this bug and this tool doesn't change it.

## Troubleshooting

- **"Run In Konsole" isn't in the menu**: the file isn't executable yet. Redo step 3 of Setup.
- **The window flashes and closes**: open Konsole yourself, `cd` to the folder with the script, and run `./frame-audio-fix.py` to see the error message.
- **"already running"**: another Konsole window is running it. Use that one, or close it first.
- **Keys don't do anything**: click the Konsole window so it has focus.
- **Nothing happens and audio is still silent**: press `s`. If the bug status says "not active", your problem is something else. Press `d` and open an issue here with the report.
- **Weird characters instead of emoji**: run it from a terminal with `--plain`.

## Uninstall

Delete `frame-audio-fix.py`, and if you like, `frame-audio-fix.log` and `frame-audio-fix-report.md` from your home folder.

## Disclaimer

This is an unofficial, community-made workaround. It is **not made, supported or endorsed by Valve**. "Steam", "Steam Frame", "SteamVR" and "SteamOS" are trademarks of Valve Corporation.

**You use this software entirely at your own risk.** It is provided "as is", without warranty of any kind, as stated in the [MIT License](LICENSE). It was developed and tested on a single Steam Frame with one audio setup. Future SteamOS, SteamVR, PipeWire or WirePlumber updates may change how audio works, and the script may then behave differently, stop working, or no longer be needed. Always check that the bug still exists before using it (see [above](#check-that-the-bug-still-exists-before-using-this)).

The author(s) are not responsible for any problems, data loss, damage, or other issues that may result from using it. If anything seems wrong, close the script and restart SteamVR on the headset, or reboot the Frame. Nothing the script does survives a reboot.

## Support

If this saved your VR sessions and you'd like to say thanks, you can buy me a coffee on **[Ko-fi](https://ko-fi.com/theinkedtail)**. Totally optional, and much appreciated.

Found a problem or have a different adapter that behaves oddly? Open an issue here with your diagnose report.

## License

MIT. See [LICENSE](LICENSE).
