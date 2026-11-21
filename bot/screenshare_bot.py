#!/usr/bin/env python3
"""TeamSpeak 3 screen-share bot.

Adds browser-based screen sharing to an existing TeamSpeak 3 / TeaSpeak server
without installing anything on the clients:

* Listens on ServerQuery (one connection per virtual server) for chat commands:
  ``!ekran`` (get a private share link), ``!ekran dur`` (stop), ``!izle`` (watch links).
* When a stream goes live, the sharer gets a temporary "LIVE" server group
  (icon + ``[CANLI YAYIN]`` prefix in the channel tree) and everybody in the
  sharer's channel receives a *personal* watch link by private message.
* Watch links are bound to a TS identity: the owner must be online on the server
  and open the link from the same public IP as their TS connection.
* Acts as the HTTP auth backend and event hook target for mediamtx
  (WebRTC WHIP/WHEP media server) and serves a small status API for the web pages.

Standard library only.
"""
import json
import logging
import os
import queue
import re
import secrets
import socket
import string
import threading
import time
import urllib.request
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.environ.get("SCREENSHARE_CONFIG", os.path.join(HERE, "config.json"))

DEFAULT_CONF = {
    "query_host": "127.0.0.1",
    "query_port": 10011,
    "query_user": "serveradmin",
    "query_pass": "",
    "sids": [1],                     # list of virtual server ids, or "all"
    "nickname": "ScreenShareBot",
    "site": "https://screen.example.com",
    "http_host": "127.0.0.1",
    "http_port": 8890,
    "mtx_api": "http://127.0.0.1:9997",
    "state_file": os.path.join(HERE, "state.json"),
    "log_file": "",
    "room_ttl_hours": 6,
    "live_group_name": "CANLI YAYIN",
    "live_group_old_names": [],
    "live_icon": os.path.join(HERE, "live.png"),
    "live_name_in_tree": 1,          # 0 = icon only, 1 = name prefix, 2 = name suffix
    "ip_check": True,                # watch link must be opened from the TS client's IP
}

CONF = dict(DEFAULT_CONF)
log = logging.getLogger("screenshare")
ROOM_RE = re.compile(r"^[a-z0-9]{12}$")


def load_conf(path=CONFIG_PATH):
    conf = dict(DEFAULT_CONF)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            conf.update(json.load(f))
    return conf


# ---------------------------------------------------------------- ServerQuery escaping
_ESC = [("\\", "\\\\"), ("/", "\\/"), (" ", "\\s"), ("|", "\\p"), ("\a", "\\a"),
        ("\b", "\\b"), ("\f", "\\f"), ("\n", "\\n"), ("\r", "\\r"), ("\t", "\\t"), ("\v", "\\v")]
_UNESC = {"\\": "\\", "/": "/", "s": " ", "p": "|", "a": "\a", "b": "\b", "f": "\f",
          "n": "\n", "r": "\r", "t": "\t", "v": "\v"}


def esc(s):
    s = str(s)
    for a, b in _ESC:
        s = s.replace(a, b)
    return s


def unesc(s):
    out, i = [], 0
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s):
            out.append(_UNESC.get(s[i + 1], s[i + 1]))
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def parse_items(body):
    """'a=1 b=x|a=2' -> [{'a': '1', 'b': 'x'}, {'a': '2'}]"""
    items = []
    for part in body.split("|"):
        d = {}
        for kv in part.split(" "):
            if not kv:
                continue
            k, _, v = kv.partition("=")
            d[k] = unesc(v)
        items.append(d)
    return items


class QueryError(Exception):
    def __init__(self, msg, code=None):
        super().__init__(msg)
        self.code = code


def rand_name(n=12):
    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(n))


# ---------------------------------------------------------------- State (rooms + viewer keys)
class State:
    """Rooms (one per sharer) and personal viewer keys, persisted to a JSON file."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        self.rooms = {}     # room -> {sid, uid, nick, cid, token, created, used, live, cldbid, notified}
        self.viewers = {}   # key  -> {sid, uid, nick, created, used}
        if path and os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    d = json.load(f)
                if isinstance(d.get("rooms"), dict) and isinstance(d.get("viewers"), dict):
                    self.rooms, self.viewers = d["rooms"], d["viewers"]
            except Exception:
                log.exception("could not read state file")
        for r in self.rooms.values():
            r["live"] = False
            r["notified"] = []

    def save(self):
        if not self.path:
            return
        with self.lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"rooms": self.rooms, "viewers": self.viewers}, f)
            os.replace(tmp, self.path)

    def room_for_user(self, sid, uid, nick, cid):
        with self.lock:
            for name, r in self.rooms.items():
                if r["sid"] == sid and r["uid"] == uid:
                    r.update(nick=nick, cid=cid, used=time.time())
                    self.save()
                    return name, dict(r)
            name = rand_name()
            r = {"sid": sid, "uid": uid, "nick": nick, "cid": cid,
                 "token": secrets.token_urlsafe(24), "created": time.time(),
                 "used": time.time(), "live": False, "cldbid": None, "notified": []}
            self.rooms[name] = r
            self.save()
            return name, dict(r)

    def viewer_key(self, sid, uid, nick):
        with self.lock:
            for k, v in self.viewers.items():
                if v["sid"] == sid and v["uid"] == uid:
                    v["nick"] = nick
                    v["used"] = time.time()
                    return k
            k = secrets.token_urlsafe(18)
            self.viewers[k] = {"sid": sid, "uid": uid, "nick": nick,
                               "created": time.time(), "used": time.time()}
            self.save()
            return k

    def room(self, name):
        with self.lock:
            r = self.rooms.get(name)
            return dict(r) if r else None

    def viewer(self, key):
        with self.lock:
            v = self.viewers.get(key)
            return dict(v) if v else None

    def update_room(self, name, **kw):
        with self.lock:
            r = self.rooms.get(name)
            if r:
                r.update(kw)
                r["used"] = time.time()
                self.save()
            return dict(r) if r else None

    def mark_notified(self, name, uid):
        """True if ``uid`` has not been notified about this live session yet."""
        with self.lock:
            r = self.rooms.get(name)
            if not r or not uid or uid in r["notified"]:
                return False
            r["notified"].append(uid)
            return True

    def live_rooms(self, sid):
        with self.lock:
            return [(n, dict(r)) for n, r in self.rooms.items() if r["sid"] == sid and r.get("live")]

    def remove_user(self, sid, uid):
        with self.lock:
            names = [n for n, r in self.rooms.items() if r["sid"] == sid and r["uid"] == uid]
            removed = [(n, self.rooms.pop(n)) for n in names]
            self.save()
            return removed

    def cleanup(self, room_ttl_hours, viewer_ttl_days=7):
        now = time.time()
        with self.lock:
            old = [n for n, r in self.rooms.items()
                   if not r.get("live") and now - r["used"] > room_ttl_hours * 3600]
            for n in old:
                del self.rooms[n]
            oldv = [k for k, v in self.viewers.items()
                    if now - v.get("used", v["created"]) > viewer_ttl_days * 86400]
            for k in oldv:
                del self.viewers[k]
            if old or oldv:
                self.save()
        return old


STATE = State(None)


# ---------------------------------------------------------------- mediamtx API
def mtx_get(path):
    with urllib.request.urlopen(CONF["mtx_api"] + path, timeout=3) as r:
        return json.loads(r.read().decode())


def mtx_kick(room):
    try:
        for s in mtx_get("/v3/webrtcsessions/list").get("items", []):
            if s.get("path") == room:
                req = urllib.request.Request(CONF["mtx_api"] + "/v3/webrtcsessions/kick/" + s["id"], method="POST")
                urllib.request.urlopen(req, timeout=3).read()
    except Exception as e:
        log.warning("kick failed %s: %s", room, e)


def room_status(room):
    st = {"canli": False, "izleyici": 0}
    try:
        p = mtx_get("/v3/paths/get/" + room)
        st["canli"] = bool(p.get("ready") or p.get("available"))
        st["izleyici"] = len(p.get("readers") or [])
    except Exception:
        pass
    return st


def watch_link(room, key):
    return "%s/izle?oda=%s&k=%s" % (CONF["site"], room, key)


# ---------------------------------------------------------------- ServerQuery connection
class VServerConn(threading.Thread):
    """One ServerQuery connection bound to one virtual server."""

    def __init__(self, sid):
        super().__init__(name="sid%d" % sid, daemon=True)
        self.sid = sid
        self.sock = None
        self.resp_q = queue.Queue()
        self.send_lock = threading.Lock()
        self.alive = False
        self.stop_flag = False
        self.own_clid = None
        self.live_sgid = None
        self._cl_cache = (0, [])

    # -- low level: a reader thread splits notifications from command responses
    def _reader(self):
        buf = b""
        try:
            while self.alive:
                data = self.sock.recv(65536)
                if not data:
                    break
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.decode("utf-8", "replace").strip("\r")
                    if not line:
                        continue
                    if line.startswith("notify"):
                        threading.Thread(target=self._safe_notify, args=(line,), daemon=True).start()
                    else:
                        self.resp_q.put(line)
        except OSError:
            pass
        self.alive = False
        self.resp_q.put(None)

    def cmd(self, command, timeout=8, empty_ok=False):
        with self.send_lock:
            if not self.alive:
                raise QueryError("not connected")
            while not self.resp_q.empty():
                self.resp_q.get_nowait()
            self.sock.sendall((command + "\n").encode("utf-8"))
            lines = []
            deadline = time.time() + timeout
            while True:
                try:
                    line = self.resp_q.get(timeout=max(0.1, deadline - time.time()))
                except queue.Empty:
                    raise QueryError("timeout: " + command.split(" ")[0])
                if line is None:
                    raise QueryError("connection lost")
                if line.startswith("error "):
                    e = parse_items(line[6:])[0]
                    code = e.get("id")
                    if code == "1281" and empty_ok:  # empty result set
                        return []
                    if code != "0":
                        raise QueryError("%s: %s (%s)" % (command.split(" ")[0], e.get("msg"), code), code)
                    body = "\n".join(lines)
                    return parse_items(body) if body else []
                lines.append(line)

    def _open(self):
        self.sock = socket.create_connection((CONF["query_host"], CONF["query_port"]), timeout=10)
        self.sock.settimeout(None)
        time.sleep(0.4)  # swallow the greeting banner
        self.sock.setblocking(False)
        try:
            self.sock.recv(65536)
        except BlockingIOError:
            pass
        self.sock.setblocking(True)
        self.alive = True
        threading.Thread(target=self._reader, name="rd%d" % self.sid, daemon=True).start()
        self.cmd("login %s %s" % (esc(CONF["query_user"]), esc(CONF["query_pass"])))

    def connect(self):
        self._open()
        self.cmd("use sid=%d" % self.sid)
        try:
            self.cmd("clientupdate client_nickname=%s" % esc(CONF["nickname"]))
        except QueryError:
            self.cmd("clientupdate client_nickname=%s" % esc(CONF["nickname"] + str(self.sid)))
        self.own_clid = self.cmd("whoami")[0].get("client_id")
        # NOTE: query clients cannot join channels on TeaSpeak 1.4 (clientmove returns ok
        # but nothing happens), so channel chat is unusable -> server chat + private messages.
        self.cmd("servernotifyregister event=textserver")
        self.cmd("servernotifyregister event=textprivate")
        self.cmd("servernotifyregister event=server")
        self.cmd("servernotifyregister event=channel id=0")
        try:
            self.setup_live_group()
        except Exception as e:
            log.warning("sid %d: could not set up live group: %s", self.sid, e)
        log.info("sid %d connected (clid=%s, live_group=%s)", self.sid, self.own_clid, self.live_sgid)

    def close(self):
        self.alive = False
        try:
            self.sock.close()
        except Exception:
            pass

    def run(self):
        backoff = 5
        while not self.stop_flag:
            try:
                self.connect()
                backoff = 5
                last_ka = time.time()
                while self.alive and not self.stop_flag:
                    time.sleep(1)
                    if time.time() - last_ka > 180:
                        self.cmd("version")  # keepalive
                        last_ka = time.time()
            except Exception as e:
                log.warning("sid %d: %s", self.sid, e)
            self.close()
            if self.stop_flag:
                break
            time.sleep(backoff)
            backoff = min(backoff * 2, 120)

    # -- LIVE server group
    def _upload_icon(self, data):
        crc = zlib.crc32(data) & 0xFFFFFFFF
        r = self.cmd("ftinitupload clientftfid=%d name=%s cid=0 cpw= size=%d overwrite=1 resume=0"
                     % (secrets.randbelow(60000) + 1, esc("/icon_%d" % crc), len(data)))
        ft = r[0] if r else {}
        if not ft.get("ftkey"):
            raise QueryError("no ftkey: %s" % ft)
        s = socket.create_connection((CONF["query_host"], int(ft.get("port") or 30033)), timeout=10)
        s.sendall(ft["ftkey"].encode())
        time.sleep(0.05)
        s.sendall(data)
        time.sleep(0.2)
        s.close()
        return crc - 0x100000000 if crc >= 0x80000000 else crc  # icon ids are signed int32

    def setup_live_group(self):
        gname = CONF["live_group_name"]
        groups = [g for g in self.cmd("servergrouplist") if g.get("type") == "1"]
        sgid = next((g["sgid"] for g in groups if g.get("name") == gname), None)
        if not sgid:
            old = next((g for g in groups if g.get("name") in CONF["live_group_old_names"]), None)
            if old:
                sgid = old["sgid"]
                self.cmd("servergrouprename sgid=%s name=%s" % (sgid, esc(gname)))
            else:
                sgid = self.cmd("servergroupadd name=%s type=1" % esc(gname))[0]["sgid"]
                log.info("sid %d: live group created sgid=%s", self.sid, sgid)
        perms = [("i_group_show_name_in_tree", CONF["live_name_in_tree"]),
                 ("i_group_sort_id", 1),
                 ("i_group_needed_member_add_power", 100),
                 ("i_group_needed_member_remove_power", 100),
                 ("i_group_needed_modify_power", 100)]
        if CONF["live_icon"] and os.path.exists(CONF["live_icon"]):
            with open(CONF["live_icon"], "rb") as f:
                perms.append(("i_icon_id", self._upload_icon(f.read())))
        self.cmd("servergroupaddperm sgid=%s %s" % (sgid, "|".join(
            "permsid=%s permvalue=%s permnegated=0 permskip=0" % (k, v) for k, v in perms)))
        # remove leftovers from a previous run
        live_db = {str(r.get("cldbid")) for _, r in STATE.live_rooms(self.sid)}
        for m in self.cmd("servergroupclientlist sgid=%s" % sgid, empty_ok=True):
            if m.get("cldbid") and m["cldbid"] not in live_db:
                self.cmd("servergroupdelclient sgid=%s cldbid=%s" % (sgid, m["cldbid"]))
        self.live_sgid = sgid

    def set_live_badge(self, cldbid, on):
        if not self.live_sgid or not cldbid:
            return
        try:
            self.cmd("servergroup%sclient sgid=%s cldbid=%s" % ("add" if on else "del", self.live_sgid, cldbid))
        except QueryError as e:
            log.warning("sid %d: live badge %s failed: %s", self.sid, "add" if on else "remove", e)

    # -- clients
    def clients(self, max_age=0):
        t, cl = self._cl_cache
        if max_age and time.time() - t < max_age:
            return cl
        cl = [c for c in self.cmd("clientlist -uid -ip") if c.get("client_type") == "0"]
        self._cl_cache = (time.time(), cl)
        return cl

    def find_client(self, uid, max_age=0):
        return next((c for c in self.clients(max_age) if c.get("client_unique_identifier") == uid), None)

    def pm(self, clid, msg):
        self.cmd("sendtextmessage targetmode=1 target=%s msg=%s" % (clid, esc(msg)))

    def send_watch_link(self, room, r, client):
        uid = client.get("client_unique_identifier")
        if not uid or uid == r["uid"]:
            return False
        key = STATE.viewer_key(self.sid, uid, client.get("client_nickname", "?"))
        try:
            self.pm(client["clid"],
                    "[b][color=red]CANLI[/color] %s[/b] ekranini paylasiyor, izlemek icin tikla: [URL]%s[/URL]\n"
                    "(Bu link sana ozel, baskasinda calismaz.)" % (r["nick"], watch_link(room, key)))
            return True
        except QueryError as e:
            log.warning("sid %d: pm to %s failed: %s", self.sid, client.get("clid"), e)
            return False

    # -- notifications
    def _safe_notify(self, line):
        try:
            self.on_notify(line)
        except Exception:
            log.exception("sid %d: notification handler failed", self.sid)

    def on_notify(self, line):
        name, _, body = line.partition(" ")
        if name == "notifytextmessage":
            return self.on_text(parse_items(body)[0])
        if name in ("notifyclientmoved", "notifycliententerview"):
            lives = STATE.live_rooms(self.sid)
            if lives:
                for e in parse_items(body):
                    self.on_moved(e, lives)

    def on_moved(self, e, lives):
        """Someone joined/moved: send them the watch link if they entered a live channel."""
        ctid, clid = e.get("ctid"), e.get("clid")
        if not ctid or not clid or clid == self.own_clid:
            return
        time.sleep(0.3)
        cl = self.clients()
        me = next((c for c in cl if c.get("clid") == clid), None)
        if not me:
            return
        for room, r in lives:
            sharer = next((c for c in cl if c.get("client_unique_identifier") == r["uid"]), None)
            if not sharer:
                continue
            if me.get("client_unique_identifier") == r["uid"]:
                # the sharer moved: announce to the new channel
                for c in cl:
                    if c.get("cid") == sharer.get("cid") and STATE.mark_notified(room, c.get("client_unique_identifier")):
                        self.send_watch_link(room, r, c)
            elif sharer.get("cid") == ctid and STATE.mark_notified(room, me.get("client_unique_identifier")):
                self.send_watch_link(room, r, me)

    def on_text(self, d):
        if d.get("invokerid") == self.own_clid:
            return
        msg = d.get("msg", "").strip()
        low = msg.lower()
        clid = d.get("invokerid")
        nick = d.get("invokername", "?")
        uid = d.get("invokeruid", clid)

        if low == "!izle":
            lives = STATE.live_rooms(self.sid)
            if not lives:
                self.pm(clid, "Su an bu sunucuda acik ekran paylasimi yok.")
                return
            key = STATE.viewer_key(self.sid, uid, nick)
            lines = ["[b]Acik yayinlar[/b] (linkler sana ozel):"]
            lines += ["%s: [URL]%s[/URL]" % (r["nick"], watch_link(room, key)) for room, r in lives]
            self.pm(clid, "\n".join(lines))
            return

        if not (low == "!ekran" or low.startswith("!ekran ")):
            return
        arg = low[len("!ekran"):].strip()
        log.info("sid %d: command %r from %s", self.sid, msg, nick)

        if arg in ("dur", "kapat", "stop"):
            removed = STATE.remove_user(self.sid, uid)
            for n, r in removed:
                mtx_kick(n)
                if r.get("cldbid"):
                    self.set_live_badge(r["cldbid"], False)
            self.pm(clid, "Ekran paylasimin kapatildi." if removed else "Acik bir paylasimin yok.")
            return
        if arg in ("yardim", "help", "?"):
            self.pm(clid, "!ekran = ekranini paylas  |  !ekran dur = kapat  |  !izle = acik yayinlarin linki")
            return

        info = self.cmd("clientinfo clid=%s" % clid)[0]
        room, r = STATE.room_for_user(self.sid, uid, nick, info.get("cid"))
        share = "%s/paylas?oda=%s&t=%s" % (CONF["site"], room, r["token"])
        self.pm(clid,
                "[b]Ekran paylasimi[/b]\n"
                "Bu linki tarayicida ac ve 'Ekranimi Paylas'a bas (link SANA OZEL, kimseyle paylasma):\n"
                "[URL]%s[/URL]\n"
                "Yayin baslayinca isminin yaninda CANLI YAYIN yazar, kanalindakilere kisiye ozel izleme linki gider.\n"
                "Kapatmak icin: !ekran dur" % share)


class Manager(threading.Thread):
    """Keeps one VServerConn per wanted virtual server; housekeeping every 5 minutes."""

    def __init__(self):
        super().__init__(name="manager", daemon=True)
        self.conns = {}

    def wanted_sids(self):
        if CONF["sids"] != "all":
            return set(int(s) for s in CONF["sids"])
        c = VServerConn(0)
        try:
            c._open()
            return set(int(i["virtualserver_id"]) for i in c.cmd("serverlist")
                       if i.get("virtualserver_status") == "online")
        finally:
            c.close()

    def conn(self, sid):
        c = self.conns.get(sid)
        return c if c and c.alive else None

    def run(self):
        while True:
            try:
                want = self.wanted_sids()
                for sid in want - set(self.conns):
                    self.conns[sid] = VServerConn(sid)
                    self.conns[sid].start()
                for sid in set(self.conns) - want:
                    self.conns[sid].stop_flag = True
                    self.conns[sid].close()
                    del self.conns[sid]
                for n in STATE.cleanup(CONF["room_ttl_hours"]):
                    log.info("expired room removed %s", n)
            except Exception as e:
                log.warning("manager: %s", e)
            time.sleep(300)


MANAGER = Manager()


def on_live(room, live):
    """mediamtx hook: stream became (un)available."""
    r = STATE.room(room)
    if not r:
        return
    c = MANAGER.conn(r["sid"])
    if not live:
        STATE.update_room(room, live=False, notified=[])
        if c and r.get("cldbid"):
            c.set_live_badge(r["cldbid"], False)
        return
    STATE.update_room(room, live=True, notified=[])
    if not c:
        return
    try:
        cl = c.clients()
        sharer = next((x for x in cl if x.get("client_unique_identifier") == r["uid"]), None)
        cldbid = sharer.get("client_database_id") if sharer else None
        cid = sharer.get("cid") if sharer else r["cid"]
        STATE.update_room(room, cldbid=cldbid, cid=cid)
        c.set_live_badge(cldbid, True)
        n = 0
        for x in cl:
            if x.get("cid") == cid and STATE.mark_notified(room, x.get("client_unique_identifier")):
                n += 1 if c.send_watch_link(room, r, x) else 0
        log.info("live %s: badge=%s channel=%s links_sent=%d", room, cldbid, cid, n)
    except Exception as e:
        log.warning("live start failed %s: %s", room, e)


def check_viewer(room, key, ip, find_client=None):
    """Return None if the viewer may watch, otherwise (code, user-facing reason)."""
    r = STATE.room(room) if ROOM_RE.match(room or "") else None
    if not r:
        return ("oda_yok", "Bu yayin linki gecersiz ya da suresi dolmus.")
    v = STATE.viewer(key) if key else None
    if not v or v["sid"] != r["sid"]:
        return ("anahtar", "Bu link sana ait degil. TS'te sunucu sohbetine !izle yazip kendi linkini al.")
    if find_client is None:
        c = MANAGER.conn(r["sid"])
        if not c:
            return ("bot", "Sunucuya su an ulasilamiyor, biraz sonra tekrar dene.")
        find_client = lambda uid: c.find_client(uid, max_age=5)  # noqa: E731
    cl = find_client(v["uid"])
    if not cl:
        return ("ts_yok", "Izlemek icin TS sunucusuna bagli olman gerekiyor.")
    if CONF["ip_check"] and ip and cl.get("connection_client_ip") and cl["connection_client_ip"] != ip:
        return ("ip", "Bu link sana ozel: TS'e bagli oldugun cihaz/internet uzerinden acmalisin.")
    return None


def authorize(p, find_client=None):
    """mediamtx HTTP auth decision for one request payload -> True/False."""
    action, ip, room = p.get("action"), p.get("ip", ""), p.get("path", "")
    if action in ("api", "metrics", "pprof"):
        return ip in ("127.0.0.1", "::1")
    q = parse_qs(p.get("query") or "")
    tok = p.get("token") or ""
    if action == "read":
        err = check_viewer(room, tok or (q.get("k") or [""])[0], ip, find_client)
        if err:
            log.info("watch denied %s ip=%s: %s", room, ip, err[0])
        return err is None
    if action == "publish":
        r = STATE.room(room)
        tok = tok or (q.get("t") or [""])[0]
        if r and tok and secrets.compare_digest(tok, r["token"]):
            return True
        log.warning("unauthorized publish attempt %s ip=%s", room, ip)
    return False


# ---------------------------------------------------------------- HTTP (auth / hooks / status)
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, code, obj=None):
        body = json.dumps(obj or {}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        if self.path == "/auth":
            try:
                p = json.loads(raw.decode() or "{}")
            except Exception:
                return self._send(400)
            return self._send(200 if authorize(p) else 401)
        m = re.match(r"^/hook/(available|unavailable)/([a-z0-9]{12})$", self.path)
        if m:
            live = m.group(1) == "available"
            log.info("stream %s: %s", "started" if live else "ended", m.group(2))
            threading.Thread(target=on_live, args=(m.group(2), live), daemon=True).start()
            return self._send(200)
        self._send(404)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path != "/api/durum":
            return self._send(404)
        q = parse_qs(u.query)
        room = (q.get("oda") or [""])[0]
        r = STATE.room(room) if ROOM_RE.match(room) else None
        if not r:
            return self._send(404, {"var": False, "sebep": "Bu yayin linki gecersiz ya da suresi dolmus."})
        st = room_status(room)
        st.update(var=True, sahip=r["nick"])
        tok = (q.get("t") or [""])[0]
        if tok:  # share page
            st["sahip_mi"] = secrets.compare_digest(tok, r["token"])
            return self._send(200, st)
        ip = (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
        err = check_viewer(room, (q.get("k") or [""])[0], ip)
        st["izin"] = err is None
        if err:
            st["kod"], st["sebep"] = err
        return self._send(200, st)


def main():
    global STATE
    CONF.update(load_conf())
    handlers = [logging.StreamHandler()]
    if CONF["log_file"]:
        handlers.append(logging.FileHandler(CONF["log_file"]))
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(threadName)s %(message)s")
    if not CONF["query_pass"]:
        raise SystemExit("query_pass is empty - copy config.example.json to config.json and fill it in")
    STATE = State(CONF["state_file"])
    log.info("screen-share bot starting, sids=%s", CONF["sids"])
    MANAGER.start()
    srv = ThreadingHTTPServer((CONF["http_host"], CONF["http_port"]), Handler)
    srv.daemon_threads = True
    srv.serve_forever()


if __name__ == "__main__":
    main()
