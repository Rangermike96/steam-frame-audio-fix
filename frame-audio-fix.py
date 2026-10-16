#!/usr/bin/env python3
"""
frame-audio-fix  -  workaround for Steam Frame audio going silent on
external outputs (USB-C adapters, USB DACs, Bluetooth) when the mic opens.

Upstream bug: https://github.com/ValveSoftware/SteamOS/issues/2896

When an app opens the mic (loading into a VRChat world, joining a Discord
call...), SteamOS turns on an echo-cancel "smart filter" aimed at your
external output. WirePlumber then leaves app audio streams disconnected,
so you hear nothing until the mic closes. This tool watches for exactly
that situation and reconnects the streams to your output.

Run it on the Frame in desktop mode (e.g. right-click > Run In Konsole in
Dolphin) and leave the window open while you play.
Run with --help for options. Python 3 standard library only.

License: MIT
"""

import argparse
import datetime
import fcntl
import json
import math
import os
import platform
import select
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import time
import wave

VERSION = "1.0.0"
ISSUE_URL = "https://github.com/ValveSoftware/SteamOS/issues/2896"
# The audio stack this was built and tested against. On anything else,
# Valve may have changed or fixed things, so we tell the user to check.
TESTED_PIPEWIRE = "1.6.8"
TESTED_WIREPLUMBER = "0.5.18"

HOME = os.path.expanduser("~")
LOG_PATH = os.path.join(HOME, "frame-audio-fix.log")
REPORT_PATH = os.path.join(HOME, "frame-audio-fix-report.md")
LOG_MAX_BYTES = 1024 * 1024
FIGHT_MAX = 6          # relinks of one stream within 60 s before backing off
FIGHT_PAUSE = 60       # seconds to leave a "fighting" stream alone
HEARTBEAT_EVERY = 60   # seconds of quiet before a "still watching" line
INTERVALS = [0.5, 1.0, 2.0, 3.0, 5.0]
STEAM_LINK_NODE = "vrlink"   # the Frame's Steam Link playback stream

# Channel fallbacks when the output device lacks a stream's channel.
CHANNEL_FALLBACK = {
    "FC": ["FL", "FR"], "RL": ["FL"], "SL": ["FL"],
    "RR": ["FR"], "SR": ["FR"], "LFE": [],
}


# =============================================================================
#  Output
# =============================================================================
class Style:
    def __init__(self, plain):
        color = (not plain and sys.stdout.isatty() and not os.environ.get("NO_COLOR")
                 and os.environ.get("TERM") != "dumb")
        c = (lambda code: "\033[%sm" % code) if color else (lambda code: "")
        self.red, self.grn, self.yel = c("31"), c("32"), c("33")
        self.cyn, self.mag = c("36"), c("35")
        self.bld, self.dim, self.rst = c("1"), c("2"), c("0")
        if plain:
            self.i = dict(mic="[MIC]", fix="[FIXED]", ok="[OK]", warn="[!]", err="[X]",
                          bell="[BEEP]", out="[OUT]", undo="[UNDO]", rs="[RESTART]",
                          spd="[SPEED]", mode="[MODE]", bye="[BYE]", on="[ON]", off="[OFF]",
                          yes="+", no="x", dot="-", arrow="->", diag="[TEST]")
        else:
            self.i = dict(mic="🎤", fix="🔧", ok="✅", warn="⚠", err="✖", bell="🔔",
                          out="🎧", undo="↩", rs="♻", spd="⏱", mode="🎛", bye="👋",
                          on="▶", off="⏸", yes="✔", no="✖", dot="•", arrow="→", diag="🔬")


S = Style(plain=True)   # replaced in main()


def _rotate_log():
    try:
        if os.path.getsize(LOG_PATH) > LOG_MAX_BYTES:
            os.replace(LOG_PATH, LOG_PATH + ".1")
    except OSError:
        pass


def log(msg, color=""):
    now = datetime.datetime.now()
    print("%s%s%s %s%s%s" % (S.dim, now.strftime("%H:%M:%S"), S.rst, color, msg, S.rst), flush=True)
    try:
        with open(LOG_PATH, "a") as f:
            f.write("%s %s\n" % (now.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except OSError:
        pass


def say(msg=""):
    print(msg, flush=True)


# =============================================================================
#  PipeWire graph snapshot (from pw-dump)
# =============================================================================
def _truthy(v, default=True):
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() not in ("false", "0", "no")


def _json_value(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


class Graph:
    """One snapshot of the PipeWire graph."""

    def __init__(self, objects):
        self.nodes = {}       # node id -> props
        self.ports = {}       # port id -> dict
        self.links = []       # dicts
        self.metadata = {}    # metadata name -> list of entries
        self.core_version = None
        self.wp_version = None
        for o in objects:
            t = o.get("type", "")
            info = o.get("info") or {}
            if t.endswith(":Node"):
                self.nodes[o["id"]] = info.get("props") or {}
            elif t.endswith(":Port"):
                p = info.get("props") or {}
                try:
                    nid = int(p.get("node.id"))
                except (TypeError, ValueError):
                    continue
                name = p.get("port.name", "")
                ch = p.get("audio.channel") or name.rsplit("_", 1)[-1]
                self.ports[o["id"]] = dict(
                    node=nid, name=name, ch=ch, dir=info.get("direction"),
                    monitor=_truthy(p.get("port.monitor"), False) or name.startswith("monitor_"))
            elif t.endswith(":Link"):
                self.links.append(dict(
                    out_node=info.get("output-node-id"), out_port=info.get("output-port-id"),
                    in_node=info.get("input-node-id"), in_port=info.get("input-port-id")))
            elif t.endswith(":Metadata"):
                name = (o.get("props") or {}).get("metadata.name")
                self.metadata[name] = o.get("metadata") or []
            elif t.endswith(":Core"):
                self.core_version = info.get("version")
            elif t.endswith(":Client"):
                p = info.get("props") or {}
                if "wireplumber" in str(p.get("application.name", "")).lower() and not self.wp_version:
                    self.wp_version = p.get("application.version")

        self.out_links = {}   # out port id -> [in port id]
        self.in_links = {}    # in node id -> [out node id]
        for l in self.links:
            self.out_links.setdefault(l["out_port"], []).append(l["in_port"])
            self.in_links.setdefault(l["in_node"], []).append(l["out_node"])

    # ---- loading -----------------------------------------------------------
    @classmethod
    def capture(cls, dump_file=None):
        if dump_file:
            with open(dump_file) as f:
                return cls(json.load(f))
        r = subprocess.run(["pw-dump"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=10)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.decode(errors="replace").strip() or "pw-dump failed")
        return cls(json.loads(r.stdout.decode(errors="replace")))

    # ---- node helpers ------------------------------------------------------
    def prop(self, nid, key, default=None):
        return self.nodes.get(nid, {}).get(key, default)

    def name(self, nid):
        return self.prop(nid, "node.name", str(nid))

    def label(self, nid):
        p = self.nodes.get(nid, {})
        if p.get("node.name") == STEAM_LINK_NODE:
            return "Steam Link (PC audio)"
        return (p.get("application.name") or p.get("node.description")
                or p.get("media.name") or p.get("node.name") or str(nid))

    def desc(self, nid):
        p = self.nodes.get(nid, {})
        return p.get("node.description") or p.get("node.nick") or p.get("node.name") or str(nid)

    def is_filter(self, nid):
        # Filter-chain, echo-cancel and loopback nodes all carry a link group.
        return "node.link-group" in self.nodes.get(nid, {})

    def is_echo_filter(self, nid):
        p = self.nodes.get(nid, {})
        return self.is_filter(nid) and (
            "echo" in str(p.get("node.link-group", "")).lower()
            or "echo" in str(p.get("node.name", "")).lower())

    def is_real_sink(self, nid):
        return self.prop(nid, "media.class") == "Audio/Sink" and not self.is_filter(nid)

    def real_sinks(self):
        return sorted(n for n in self.nodes if self.is_real_sink(n))

    def default_sink(self):
        name = None
        for e in self.metadata.get("default", []):
            if e.get("key") == "default.audio.sink":
                v = _json_value(e.get("value"))
                name = v.get("name") if isinstance(v, dict) else v
        if not name:
            return None
        for nid, p in self.nodes.items():
            if p.get("node.name") == name and p.get("media.class") == "Audio/Sink":
                return nid
        return None

    def is_builtin(self, nid):
        p = self.nodes.get(nid, {})
        return p.get("device.form-factor") == "internal" or "platform-sound" in str(p.get("node.name", ""))

    # ---- port helpers ------------------------------------------------------
    def out_ports(self, nid):
        return sorted((pid, p["ch"]) for pid, p in self.ports.items()
                      if p["node"] == nid and p["dir"] == "output" and not p["monitor"])

    def in_ports(self, nid):
        return sorted((pid, p["ch"]) for pid, p in self.ports.items()
                      if p["node"] == nid and p["dir"] == "input")

    def port_targets(self, pid):
        return [self.ports[i]["node"] for i in self.out_links.get(pid, []) if i in self.ports]

    # ---- the bug -----------------------------------------------------------
    def bug_active(self, sink):
        """An echo-cancel filter is feeding the output device."""
        return sink is not None and any(self.is_echo_filter(n) for n in self.in_links.get(sink, []))

    def smart_filter_entries(self):
        out = []
        for e in self.metadata.get("filters", []):
            subj = e.get("subject")
            out.append((subj, self.name(subj), e.get("key"), _json_value(e.get("value"))))
        return out

    def app_streams(self, sink, steam_link_only=False):
        """App playback streams we're allowed to touch."""
        sink_name = self.name(sink) if sink is not None else None
        sink_serial = str(self.prop(sink, "object.serial", "")) if sink is not None else ""
        res = []
        for nid, p in self.nodes.items():
            if p.get("media.class") != "Stream/Output/Audio" or self.is_filter(nid):
                continue
            if steam_link_only and p.get("node.name") != STEAM_LINK_NODE:
                continue
            if not _truthy(p.get("node.autoconnect"), True):
                continue   # the app manages its own connections
            target = p.get("target.object") or p.get("node.target")
            if target and str(target) not in (sink_name, sink_serial, str(sink), "-1"):
                continue   # deliberately routed somewhere else
            res.append(nid)
        return sorted(res)

    def stream_state(self, nid):
        """'ok' (reaches a real output), 'unlinked', or 'wrong' (only filters)."""
        targets = set()
        for pid, _ in self.out_ports(nid):
            targets.update(self.port_targets(pid))
        if not targets:
            return "unlinked"
        for t in targets:
            if self.is_real_sink(t) or (self.is_filter(t) and not self.is_echo_filter(t)):
                return "ok"
        return "wrong"


def map_channels(out_ports, sink_ports):
    """Pair a stream's output ports with the output device's input ports."""
    by_ch = {}
    for pid, ch in sink_ports:
        by_ch.setdefault(ch, pid)
    pairs = []
    for opid, ch in out_ports:
        if ch in by_ch:
            pairs.append((opid, by_ch[ch]))
        elif ch == "MONO":
            pairs.extend((opid, s) for s, _ in sink_ports)
        elif "MONO" in by_ch:
            pairs.append((opid, by_ch["MONO"]))
        else:
            for f in CHANNEL_FALLBACK.get(ch, []):
                if f in by_ch:
                    pairs.append((opid, by_ch[f]))
    return pairs


# =============================================================================
#  The fixer
# =============================================================================
class Fixer:
    def __init__(self, args):
        self.args = args
        self.dump_file = args.dump_file
        self.dry = args.dry_run or bool(args.dump_file)
        self.auto = not args.no_auto
        self.steam_link_only = args.steam_link_only
        self.interval = args.interval
        self.grace = args.grace
        self.always = args.always
        self.total_fixes = 0
        self.last_active = None
        self.bug_ended_at = None
        self.made = []          # (out_port_id, in_port_id) we created
        self.recent = {}        # stream node name -> [timestamps]
        self.blocked = {}       # stream node name -> unblock time
        self.dry_seen = set()
        self.pw_error_shown = False

    def snapshot(self):
        try:
            g = Graph.capture(self.dump_file)
            if self.pw_error_shown:
                log("%s PipeWire is reachable again." % S.i["ok"], S.grn)
                self.pw_error_shown = False
            return g
        except Exception as e:  # noqa: BLE001 - keep the watcher alive
            if not self.pw_error_shown:
                log("%s Can't read the audio graph right now (%s). Retrying..." % (S.i["warn"], e), S.yel)
                self.pw_error_shown = True
            return None

    # ---- safeguards --------------------------------------------------------
    def fight_ok(self, key, label):
        now = time.time()
        until = self.blocked.get(key)
        if until:
            if now < until:
                return False
            del self.blocked[key]
            log("%s Trying %s again." % (S.i["on"], label), S.cyn)
        hist = [t for t in self.recent.get(key, []) if now - t < 60] + [now]
        if len(hist) > FIGHT_MAX:
            self.blocked[key] = now + FIGHT_PAUSE
            self.recent[key] = []
            log("%s Something keeps disconnecting %s. Leaving it alone for %ds so we don't fight."
                % (S.i["warn"], label, FIGHT_PAUSE), S.red + S.bld)
            log("   If audio stays dead, restart SteamVR on the headset.", S.red)
            return False
        self.recent[key] = hist
        return True

    # ---- linking -----------------------------------------------------------
    def link(self, g, out_pid, in_pid):
        desc = "%s:%s %s %s:%s" % (g.name(g.ports[out_pid]["node"]), g.ports[out_pid]["name"],
                                    S.i["arrow"], g.name(g.ports[in_pid]["node"]), g.ports[in_pid]["name"])
        if self.dry:
            if (out_pid, in_pid) not in self.dry_seen:
                self.dry_seen.add((out_pid, in_pid))
                log("  [dry run] would connect %s" % desc, S.yel)
            return True
        r = subprocess.run(["pw-link", str(out_pid), str(in_pid)],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
        err = (r.stdout + r.stderr).decode(errors="replace").strip()
        if r.returncode == 0:
            self.made.append((out_pid, in_pid))
            return True
        if "exist" in err.lower():
            return True
        log("  %s Couldn't connect %s: %s" % (S.i["err"], desc, err), S.red)
        return False

    def run_once(self, manual=False):
        g = self.snapshot()
        if g is None:
            return
        sink = g.default_sink()
        if sink is None:
            if manual:
                log("%s No output device found. Is your adapter plugged in?" % S.i["warn"], S.yel)
            return

        active = g.bug_active(sink)
        now = time.time()
        if active != self.last_active:
            if active:
                log("%s Mic turned on %s audio bug is ACTIVE on %s (normal in a VRChat world / call)"
                    % (S.i["mic"], S.i["arrow"], g.desc(sink)), S.yel + S.bld)
            elif self.last_active:
                log("%s Mic turned off %s bug cleared" % (S.i["mic"], S.i["arrow"]), S.grn)
                self.bug_ended_at = now
            self.last_active = active

        in_grace = self.bug_ended_at is not None and now - self.bug_ended_at < self.grace
        if not (active or in_grace or self.always):
            if manual:
                log("%s The bug isn't active right now, so there's nothing to fix." % S.i["ok"], S.grn)
            return

        sink_ports = g.in_ports(sink)
        fixed_labels = []
        for nid in g.app_streams(sink, self.steam_link_only):
            state = g.stream_state(nid)
            if state == "ok":
                continue
            if not active and state != "unlinked":
                continue   # outside the bug, only rescue fully disconnected streams
            pairs = map_channels(g.out_ports(nid), sink_ports)
            if not pairs:
                continue
            key, label = g.name(nid), g.label(nid)
            if not self.fight_ok(key, label):
                continue
            if all([self.link(g, o, i) for o, i in pairs]):
                fixed_labels.append(label)

        if fixed_labels and not self.dry:
            self.total_fixes += len(fixed_labels)
            log("%s Reconnected %s %s %s" % (S.i["fix"], ", ".join(sorted(set(fixed_labels))),
                                             S.i["arrow"], g.desc(sink)), S.grn + S.bld)
        elif manual and not fixed_labels:
            log("%s Nothing to fix. Everything playing is connected." % S.i["ok"], S.grn)

    # ---- other actions -----------------------------------------------------
    def undo(self):
        if not self.made:
            log("Nothing to undo.", S.cyn)
            return
        n = 0
        for o, i in self.made:
            r = subprocess.run(["pw-link", "-d", str(o), str(i)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            n += r.returncode == 0
        self.made = []
        log("%s Removed %d link(s) this tool made. (If the bug is active, audio goes silent again.)"
            % (S.i["undo"], n), S.yel)

    def status(self):
        g = self.snapshot()
        if g is None:
            return
        sink = g.default_sink()
        say()
        say("%s  STATUS%s" % (S.bld, S.rst))
        if sink is None:
            say("  Output device : %snone found%s" % (S.red, S.rst))
            return
        kind = "built-in" if g.is_builtin(sink) else "external"
        say("  Output device : %s%s%s (%s)" % (S.bld, g.desc(sink), S.rst, kind))
        if g.bug_active(sink):
            say("  Bug status    : %sACTIVE (mic is on)%s" % (S.yel, S.rst))
        else:
            say("  Bug status    : %snot active%s" % (S.grn, S.rst))
        say("  Auto-fix      : %s  (%s, every %ss)" % (
            (S.grn + "ON" if self.auto else S.red + "OFF") + S.rst, self.mode_name(), _fmt(self.interval)))
        say("  Fixes so far  : %d   Links made by this tool: %d" % (self.total_fixes, len(self.made)))
        if self.blocked:
            say("  %sBacked off    : %s%s" % (S.red, ", ".join(self.blocked), S.rst))
        say("  Streams:")
        streams = g.app_streams(sink, self.steam_link_only)
        if not streams:
            say("    %s(nothing playing right now)%s" % (S.dim, S.rst))
        for nid in streams:
            st = g.stream_state(nid)
            mark = {"ok": S.grn + S.i["yes"], "unlinked": S.red + S.i["no"],
                    "wrong": S.yel + S.i["dot"]}[st] + S.rst
            note = {"ok": "connected", "unlinked": S.red + "NOT connected" + S.rst,
                    "wrong": S.yel + "only reaches the echo-cancel filter" + S.rst}[st]
            say("    %s %s  (%s)" % (mark, g.label(nid), note))

    def mode_name(self):
        return "Steam Link only" if self.steam_link_only else "all streams"


def _fmt(x):
    return ("%g" % x)


# =============================================================================
#  Tones, diagnose and report
# =============================================================================
def make_tone(path, seconds, freq=660, amp=2500):
    r = 48000
    with wave.open(path, "w") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(r)
        frames = bytearray()
        for i in range(int(r * seconds)):
            v = int(amp * math.sin(2 * math.pi * freq * i / r))
            frames += struct.pack("<hh", v, v)
        w.writeframes(bytes(frames))


def play_beep():
    path = os.path.join(tempfile.gettempdir(), "frame-audio-fix-beep.wav")
    if not os.path.exists(path):
        make_tone(path, 1.5)
    subprocess.Popen(["pw-play", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    log("%s Playing a 1.5s beep." % S.i["bell"], S.cyn)


def os_release():
    info = {}
    try:
        with open("/etc/os-release") as f:
            for line in f:
                if "=" in line:
                    k, v = line.rstrip("\n").split("=", 1)
                    info[k] = v.strip('"')
    except OSError:
        pass
    return info


def find_node_by_media_name(g, media_name):
    for nid, p in g.nodes.items():
        if p.get("media.name") == media_name or p.get("media.filename") == media_name:
            return nid
    return None


def stream_reaches(g, nid, sink):
    """Does this stream have a link straight into the output device?"""
    if nid is None:
        return False
    return any(sink in g.port_targets(pid) for pid, _ in g.out_ports(nid))


def run_active_test(dump_file=None):
    """Play a quiet tone, open the mic briefly, and watch the routing."""
    tmp = tempfile.mkdtemp(prefix="frame-audio-fix-")
    tone = os.path.join(tmp, "diagnose-tone.wav")
    rec = os.path.join(tmp, "diagnose-record.wav")
    make_tone(tone, 6.0, freq=520, amp=2000)
    result = {}
    player = recorder = None
    try:
        player = subprocess.Popen(["pw-play", tone], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1.2)
        g = Graph.capture(dump_file)
        sink = g.default_sink()
        tnode = find_node_by_media_name(g, tone)
        result["sink"] = g.desc(sink) if sink is not None else None
        result["before"] = stream_reaches(g, tnode, sink)
        result["bug_before"] = g.bug_active(sink)

        recorder = subprocess.Popen(["pw-record", rec], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1.8)
        g = Graph.capture(dump_file)
        tnode = find_node_by_media_name(g, tone)
        result["during"] = stream_reaches(g, tnode, sink)
        result["bug_during"] = g.bug_active(sink)
        result["during_state"] = g.stream_state(tnode) if tnode is not None else "missing"
        result["smart_filters"] = g.smart_filter_entries()

        recorder.terminate()
        recorder.wait(timeout=5)
        recorder = None
        time.sleep(1.2)
        g = Graph.capture(dump_file)
        tnode = find_node_by_media_name(g, tone)
        result["after"] = stream_reaches(g, tnode, sink)
        result["bug_after"] = g.bug_active(sink)
    finally:
        for p in (recorder, player):
            if p is not None and p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    p.kill()
        shutil.rmtree(tmp, ignore_errors=True)
    return result


def verdict(r):
    if r.get("sink") is None:
        return "UNKNOWN", "No output device was found."
    if not r.get("before"):
        return "UNKNOWN", "The test tone never reached the output, even before the mic opened."
    if r.get("bug_during") and not r.get("during"):
        if r.get("after"):
            return "AFFECTED", ("Opening the mic disconnected audio from the output, and it came back "
                                "when the mic closed. This tool will help.")
        return "AFFECTED", ("Opening the mic disconnected audio from the output, and it did NOT come "
                            "back on its own afterwards. This tool will help.")
    if r.get("during"):
        return "NOT AFFECTED", "Audio stayed connected while the mic was open. You probably don't need this tool."
    return "UNKNOWN", "Audio dropped, but not in the way this tool recognizes."


def build_report(g, test):
    osr = os_release()
    sink = g.default_sink() if g else None
    lines = []
    lines.append("### frame-audio-fix diagnose report")
    lines.append("")
    lines.append("- Tool version: %s (tested with PipeWire %s / WirePlumber %s)"
                 % (VERSION, TESTED_PIPEWIRE, TESTED_WIREPLUMBER))
    lines.append("- Date: %s" % datetime.datetime.now().strftime("%Y-%m-%d %H:%M"))
    lines.append("- OS: %s (version %s, build %s)" % (osr.get("PRETTY_NAME", "?"), osr.get("VERSION_ID", "?"),
                                                    osr.get("BUILD_ID", "?")))
    lines.append("- Kernel: %s" % platform.release())
    if g:
        lines.append("- PipeWire: %s / WirePlumber: %s" % (g.core_version or "?", g.wp_version or "?"))
    if sink is not None:
        p = g.nodes[sink]
        lines.append("- Output device: %s (`%s`)" % (g.desc(sink), g.name(sink)))
        lines.append("  - api: %s, bus: %s, form factor: %s, channels: %s" % (
            p.get("device.api", "?"), p.get("device.bus", "?"), p.get("device.form-factor", "?"),
            p.get("audio.position", p.get("audio.channels", "?"))))
    else:
        lines.append("- Output device: none found")
    if g and sink is not None:
        streams = g.app_streams(sink)
        lines.append("- Right now: echo-cancel feeding output: %s; app streams: %s" % (
            "yes" if g.bug_active(sink) else "no",
            ", ".join("%s (%s)" % (g.label(n), g.stream_state(n)) for n in streams) or "none"))
        sf = g.smart_filter_entries()
        if sf and test is None:
            lines.append("")
            lines.append("Smart-filter metadata:")
            lines.append("```")
            for subj, name, key, val in sf:
                lines.append("%s (%s) %s = %s" % (subj, name, key, json.dumps(val)))
            lines.append("```")
    if test is not None:
        v, why = verdict(test)
        lines.append("")
        lines.append("**Result: %s.** %s" % (v, why))
        lines.append("")
        lines.append("| Step | Tone reaches output | Echo-cancel feeding output |")
        lines.append("|---|---|---|")
        for step, a, b in (("Before mic", "before", "bug_before"), ("Mic open", "during", "bug_during"),
                           ("After mic", "after", "bug_after")):
            lines.append("| %s | %s | %s |" % (step, "yes" if test.get(a) else "no",
                                                "yes" if test.get(b) else "no"))
        sf = test.get("smart_filters") or []
        if sf:
            lines.append("")
            lines.append("Smart-filter metadata while the mic was open:")
            lines.append("```")
            for subj, name, key, val in sf:
                lines.append("%s (%s) %s = %s" % (subj, name, key, json.dumps(val)))
            lines.append("```")
    lines.append("")
    lines.append("Upstream issue: %s" % ISSUE_URL)
    return "\n".join(lines)


def diagnose(args, ask=True, fixer=None):
    say()
    say("%s%s  DIAGNOSE%s" % (S.bld, S.i["diag"], S.rst))
    g = None
    try:
        g = Graph.capture(args.dump_file)
    except Exception as e:  # noqa: BLE001
        say("  %sCan't read the audio graph: %s%s" % (S.red, e, S.rst))
        return
    sink = g.default_sink()
    if sink is None:
        say("  %sNo output device found. Plug in your adapter/headphones first.%s" % (S.red, S.rst))
        return
    say("  Output device: %s%s%s" % (S.bld, g.desc(sink), S.rst))

    test = None
    if args.dump_file:
        say("  (analyzing a saved dump, so skipping the live test)")
    else:
        if not (shutil.which("pw-play") and shutil.which("pw-record")):
            say("  %spw-play / pw-record not found, can't run the live test.%s" % (S.red, S.rst))
        else:
            go = True
            if ask:
                say("  The test plays a %squiet tone for about 5 seconds%s and briefly opens the mic." % (S.bld, S.rst))
                say("  Wear your headphones (or keep them nearby). Nothing is changed.")
                go = prompt_line("  Run it now? (y/N) ").strip().lower() in ("y", "yes")
            if go:
                was_auto = fixer.auto if fixer else None
                if fixer:
                    fixer.auto = False   # don't let auto-fix hide the result
                try:
                    say("  Testing...")
                    test = run_active_test()
                finally:
                    if fixer:
                        fixer.auto = was_auto
                v, why = verdict(test)
                color = {"AFFECTED": S.yel, "NOT AFFECTED": S.grn}.get(v, S.red)
                say("  Result: %s%s%s%s. %s" % (color, S.bld, v, S.rst, why))

    report = build_report(g, test)
    try:
        with open(REPORT_PATH, "w") as f:
            f.write(report + "\n")
        say("  Report saved to %s%s%s" % (S.bld, REPORT_PATH, S.rst))
        say("  (paste it into %s if you want to help Valve fix this)" % ISSUE_URL)
    except OSError as e:
        say("  Couldn't save report (%s). Here it is:" % e)
        say(report)


# =============================================================================
#  Terminal handling
# =============================================================================
class Keys:
    """Single-key input without Enter; restores the terminal on exit."""

    def __init__(self, enabled):
        self.enabled = enabled
        self.fd = sys.stdin.fileno() if enabled else None
        self.saved = None

    def __enter__(self):
        if self.enabled:
            import termios
            import tty
            self.saved = termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd)
        return self

    def __exit__(self, *exc):
        self.restore()

    def restore(self):
        if self.saved is not None:
            import termios
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)

    def resume(self):
        if self.enabled:
            import tty
            tty.setcbreak(self.fd)

    def wait(self, timeout):
        if not self.enabled:
            time.sleep(timeout)
            return None
        r, _, _ = select.select([sys.stdin], [], [], timeout)
        if r:
            ch = os.read(self.fd, 1).decode(errors="ignore")
            return ch
        return None


KEYS = None


def prompt_line(text):
    """Read a full line even while single-key mode is on."""
    if KEYS:
        KEYS.restore()
    try:
        return input(text)
    except EOFError:
        return ""
    finally:
        if KEYS:
            KEYS.resume()


# =============================================================================
#  Interactive UI
# =============================================================================
def banner(fixer):
    say()
    say("%s%s  ╔══════════════════════════════════════════════╗%s" % (S.mag, S.bld, S.rst))
    say("%s%s  ║   Steam Frame Audio Fix  v%-19s ║%s" % (S.mag, S.bld, VERSION, S.rst))
    say("%s%s  ╚══════════════════════════════════════════════╝%s" % (S.mag, S.bld, S.rst))
    say("  %sUnofficial tool, not made by Valve. Use at your own risk.%s" % (S.dim, S.rst))
    if fixer.dry:
        say("  %s%sDRY RUN: nothing will be changed, I'll only say what I'd do.%s" % (S.yel, S.bld, S.rst))


def menu(fixer):
    a = (S.grn + "ON" if fixer.auto else S.red + "OFF") + S.rst
    say("%s  ─────────────────────────────────────────────────%s" % (S.dim, S.rst))
    say("  Auto-fix: %s   Mode: %s%s%s   Check every: %s%ss%s" % (
        a, S.bld, fixer.mode_name(), S.rst, S.bld, _fmt(fixer.interval), S.rst))
    b, r = S.bld, S.rst
    say("  %s[a]%s auto on/off   %s[f]%s fix now        %s[s]%s status" % (b, r, b, r, b, r))
    say("  %s[m]%s mode          %s[t]%s test beep      %s[o]%s change output" % (b, r, b, r, b, r))
    say("  %s[d]%s diagnose      %s[c]%s undo my links  %s[+]%s/%s[-]%s speed" % (b, r, b, r, b, r, b, r))
    say("  %s[p]%s restart PipeWire %s(last resort)%s" % (b, r, S.red, r))
    say("  %s[l]%s recent log    %s[h]%s help           %s[q]%s quit" % (b, r, b, r, b, r))
    say("%s  ─────────────────────────────────────────────────%s" % (S.dim, S.rst))


def help_text():
    b, r = S.bld, S.rst
    say("""
%(b)sWhat's going on%(r)s
  When a game opens the mic (loading into a VRChat world, joining a Discord
  call), SteamOS turns on an echo-cancel filter for your headphone output and
  your audio gets disconnected. This tool notices and reconnects it.

%(b)sWhat you'll see%(r)s
  %(mic)s Mic turned on    the bug kicked in (normal in a VRChat world)
  %(fix)s Reconnected      fixed, audio should be back
  %(mic)s Mic turned off   bug cleared

%(b)sKeys%(r)s
  a  auto-fix on/off             f  fix right now
  s  status: output, bug, every stream
  m  fix ALL streams, or only Steam Link (PC/game audio)
  t  short quiet beep            o  change output device
  d  diagnose: test whether your setup has the bug, save a report
  c  undo the links this tool made
  +/-  check faster / slower     l  last 20 log lines
  p  restart PipeWire. LAST RESORT: all audio stays dead until you
     restart SteamVR on the headset. Asks first.
  q  quit (Ctrl+C works too)

%(b)sSafety%(r)s
  - Only acts while the bug is active (plus %(g)ds after, to catch apps that
    stay disconnected). It doesn't touch your audio otherwise.
  - Skips streams that an app routed somewhere on purpose.
  - Backs off if something keeps undoing a fix, instead of fighting it.
  - Only one copy runs at a time. Links vanish on their own when apps close.

%(b)sKnown limits%(r)s
  - Frame UI click sounds are too short to catch. Game/PC audio is what
    this saves.
  - If UI sounds stay dead after a session, restart SteamVR on the headset.
  - Closing this window stops the fix.

  Log: %(log)s
""" % dict(b=b, r=r, mic=S.i["mic"], fix=S.i["fix"], g=ARGS.grace, log=LOG_PATH))


def change_output(fixer):
    g = fixer.snapshot()
    if g is None:
        return
    sinks = g.real_sinks()
    if not sinks:
        log("No output devices found.", S.yel)
        return
    cur = g.default_sink()
    say()
    say("%s  Outputs:%s" % (S.bld, S.rst))
    for nid in sinks:
        say("   %s %4d  %s" % ("*" if nid == cur else " ", nid, g.desc(nid)))
    choice = prompt_line("  Type the number of the output to use (Enter = cancel): ").strip()
    if not choice:
        log("Cancelled.", S.dim)
        return
    if not choice.isdigit() or int(choice) not in sinks:
        log("%s '%s' isn't one of the outputs listed." % (S.i["err"], choice), S.red)
        return
    r = subprocess.run(["wpctl", "set-default", choice], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if r.returncode == 0:
        log("%s Output switched to %s" % (S.i["out"], g.desc(int(choice))), S.grn)
        fixer.last_active = None
    else:
        log("%s Couldn't switch output." % S.i["err"], S.red)


def restart_pipewire(fixer):
    say("  %s%s%s Heads up:%s after this, %sALL audio stays dead until you restart SteamVR%s"
        % (S.red, S.bld, S.i["warn"], S.rst, S.bld, S.rst))
    say("  %son the headset.%s Only use it if restarting SteamVR alone didn't help." % (S.red, S.rst))
    if prompt_line("  Type YES to restart PipeWire (anything else cancels): ").strip() != "YES":
        log("Cancelled.", S.dim)
        return
    log("%s Restarting audio services..." % S.i["rs"], S.yel)
    r = subprocess.run(["systemctl", "--user", "restart", "wireplumber", "pipewire", "pipewire-pulse"],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    for line in r.stdout.decode(errors="replace").splitlines():
        log("  " + line, S.red)
    time.sleep(3)
    fixer.made, fixer.recent, fixer.blocked, fixer.last_active = [], {}, {}, None
    log("%s Done. %s Now restart SteamVR on the headset to get audio back." % (S.i["rs"], S.i["arrow"]),
        S.yel + S.bld)


def show_log():
    say()
    try:
        with open(LOG_PATH) as f:
            for line in f.readlines()[-20:]:
                say("  " + line.rstrip())
    except OSError:
        say("  (no log yet)")
    say()


def handle_key(k, fixer):
    k = k.lower()
    if k == "a":
        fixer.auto = not fixer.auto
        log(("%s Auto-fix ON" % S.i["on"]) if fixer.auto else ("%s Auto-fix OFF (press f to fix manually)" % S.i["off"]),
            S.grn if fixer.auto else S.yel)
    elif k == "f":
        fixer.run_once(manual=True)
    elif k == "s":
        fixer.status()
        menu(fixer)
    elif k == "m":
        fixer.steam_link_only = not fixer.steam_link_only
        log("%s Mode: %s" % (S.i["mode"], fixer.mode_name()), S.cyn)
    elif k == "t":
        play_beep()
    elif k == "o":
        change_output(fixer)
        menu(fixer)
    elif k == "d":
        diagnose(ARGS, ask=True, fixer=fixer)
        menu(fixer)
    elif k in "+=" and k:
        i = min(range(len(INTERVALS)), key=lambda j: abs(INTERVALS[j] - fixer.interval))
        fixer.interval = INTERVALS[min(i + 1, len(INTERVALS) - 1)]
        log("%s Checking every %ss" % (S.i["spd"], _fmt(fixer.interval)), S.cyn)
    elif k in "-_" and k:
        i = min(range(len(INTERVALS)), key=lambda j: abs(INTERVALS[j] - fixer.interval))
        fixer.interval = INTERVALS[max(i - 1, 0)]
        log("%s Checking every %ss" % (S.i["spd"], _fmt(fixer.interval)), S.cyn)
    elif k == "c":
        fixer.undo()
    elif k == "p":
        restart_pipewire(fixer)
    elif k == "l":
        show_log()
    elif k in ("h", "?"):
        help_text()
        menu(fixer)
    elif k == "q":
        raise KeyboardInterrupt
    elif k in ("\n", "\r", " "):
        pass
    else:
        say("  %sUnknown key '%s'. Press h for help.%s" % (S.dim, k.strip() or repr(k), S.rst))


# =============================================================================
#  Startup checks
# =============================================================================
def preflight(args):
    if sys.version_info < (3, 7):
        sys.exit("Python 3.7 or newer is needed.")
    if os.geteuid() == 0 and not args.allow_root:
        sys.exit("Don't run this as root or with sudo: it would look at the wrong audio session.\n"
                 "Run it as your normal user (on the Frame that's 'steamos').")
    os.environ.setdefault("XDG_RUNTIME_DIR", "/run/user/%d" % os.getuid())
    if args.dump_file:
        return
    missing = [c for c in ("pw-dump", "pw-link", "wpctl") if not shutil.which(c)]
    if missing:
        sys.exit("Missing %s. Run this on the Steam Frame (SteamOS with PipeWire)." % ", ".join(missing))
    r = subprocess.run(["wpctl", "status"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if r.returncode != 0:
        sys.exit("Can't talk to PipeWire. Run it as the Frame's normal user, not with sudo.")
    osr = os_release()
    if osr.get("ID") != "steamos":
        print("Note: this doesn't look like SteamOS (%s). It may still work, but it was built for "
              "the Steam Frame." % osr.get("PRETTY_NAME", "unknown OS"))


def take_lock(allow_shared=False):
    d = os.environ.get("XDG_RUNTIME_DIR", "")
    if not (d and os.path.isdir(d) and os.access(d, os.W_OK)):
        d = tempfile.gettempdir()
    path = os.path.join(d, "frame-audio-fix.lock")
    f = open(path, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit("frame-audio-fix is already running (maybe in another Konsole window).\n"
                 "Close that one first%s." % (", or press d inside it to diagnose" if allow_shared else ""))
    return f   # keep the file object alive to hold the lock


def check_versions(g):
    """Warn when the audio stack differs from what this tool was tested on."""
    have = (g.core_version or "?", g.wp_version or "?")
    if have == (TESTED_PIPEWIRE, TESTED_WIREPLUMBER):
        return
    log("%s Your audio stack (PipeWire %s / WirePlumber %s) differs from what this was tested on "
        "(PipeWire %s / WirePlumber %s)." % ((S.i["warn"],) + have + (TESTED_PIPEWIRE, TESTED_WIREPLUMBER)),
        S.yel + S.bld)
    log("   A SteamOS update may have fixed or changed the bug. Press d to check before relying on this.",
        S.yel)


def parse_args():
    p = argparse.ArgumentParser(
        description="Keep Steam Frame audio working on external outputs when the mic opens (SteamOS #2896).")
    p.add_argument("--once", action="store_true", help="run one fix pass and exit")
    p.add_argument("--diagnose", action="store_true",
                   help="test whether this Frame has the bug and save a report, then exit")
    p.add_argument("--yes", action="store_true", help="with --diagnose: don't ask before the live test")
    p.add_argument("--steam-link-only", action="store_true", help="only fix the Steam Link (PC audio) stream")
    p.add_argument("--interval", type=float, default=1.0, help="seconds between checks (default 1)")
    p.add_argument("--grace", type=int, default=20,
                   help="seconds to keep rescuing disconnected streams after the bug clears (default 20)")
    p.add_argument("--always", action="store_true",
                   help="also reconnect disconnected streams when the bug isn't active (not recommended)")
    p.add_argument("--no-auto", action="store_true", help="start with auto-fix off")
    p.add_argument("--dry-run", action="store_true", help="say what would be fixed, change nothing")
    p.add_argument("--plain", action="store_true", help="no colors or emoji")
    p.add_argument("--allow-root", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--dump-file", help=argparse.SUPPRESS)   # analyze a saved pw-dump (implies dry run)
    p.add_argument("--version", action="version", version="frame-audio-fix " + VERSION)
    a = p.parse_args()
    if not 0.2 <= a.interval <= 30:
        p.error("--interval must be between 0.2 and 30 seconds")
    if not 0 <= a.grace <= 600:
        p.error("--grace must be between 0 and 600 seconds")
    return a


ARGS = None


def main():
    global S, ARGS, KEYS
    ARGS = args = parse_args()
    S = Style(args.plain)
    preflight(args)
    _rotate_log()

    if args.diagnose:
        lock = None if args.dump_file else take_lock(allow_shared=True)
        diagnose(args, ask=not args.yes)
        del lock
        return

    lock = None if args.dump_file else take_lock()
    fixer = Fixer(args)

    if args.once:
        fixer.run_once(manual=True)
        return

    interactive = sys.stdin.isatty() and sys.stdout.isatty()

    def on_signal(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGHUP, on_signal)

    banner(fixer)
    log("Started v%s. Watching for the mic/echo-cancel bug..." % VERSION, S.cyn)
    if not interactive:
        log("%s No keyboard attached (not a terminal). Running without the menu." % S.i["warn"], S.yel)
    g = fixer.snapshot()
    if g is not None:
        check_versions(g)
        sink = g.default_sink()
        if sink is None:
            log("%s No output device right now. Plug in your adapter, I'll keep watching." % S.i["warn"], S.yel)
        elif g.is_builtin(sink):
            log("%s Output is the built-in speakers. Plug in your headphones/adapter (or press o)."
                % S.i["warn"], S.yel)
        else:
            log("%s Output: %s" % (S.i["out"], g.desc(sink)), S.grn)
    if interactive:
        menu(fixer)

    last_hb = time.time()
    KEYS = Keys(interactive)
    try:
        with KEYS:
            while True:
                if fixer.auto:
                    fixer.run_once()
                k = KEYS.wait(fixer.interval)
                if k:
                    handle_key(k, fixer)
                    last_hb = time.time()
                if time.time() - last_hb >= HEARTBEAT_EVERY:
                    st = (S.yel + "bug active, fixing") if fixer.last_active else (S.grn + "all quiet")
                    say("%s%s%s %s%s still watching (%s%s) %s %d fixes %s press h for help%s" % (
                        S.dim, datetime.datetime.now().strftime("%H:%M:%S"), S.rst, S.dim, S.i["dot"],
                        st, S.dim, S.i["dot"], fixer.total_fixes, S.i["dot"], S.rst))
                    _rotate_log()
                    last_hb = time.time()
    except KeyboardInterrupt:
        say()
        log("%s Bye! %d fix(es) this session. Log: %s" % (S.i["bye"], fixer.total_fixes, LOG_PATH), S.cyn)
    finally:
        del lock


if __name__ == "__main__":
    main()
