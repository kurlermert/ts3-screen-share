import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot"))
import screenshare_bot as bot  # noqa: E402


class EscapeTests(unittest.TestCase):
    def test_roundtrip(self):
        s = "a b|c/d\\e\nf\tg"
        self.assertEqual(bot.unesc(bot.esc(s)), s)

    def test_escape_known_sequences(self):
        self.assertEqual(bot.esc("hello world|x/y"), "hello\\sworld\\px\\/y")

    def test_parse_items(self):
        items = bot.parse_items("clid=1 client_nickname=Ali\\sVeli|clid=2 client_nickname=Ayse")
        self.assertEqual(items, [{"clid": "1", "client_nickname": "Ali Veli"},
                                 {"clid": "2", "client_nickname": "Ayse"}])

    def test_parse_flag_without_value(self):
        self.assertEqual(bot.parse_items("a=1 flag"), [{"a": "1", "flag": ""}])


class StateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "state.json")
        self.state = bot.State(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_one_room_per_user(self):
        a, ra = self.state.room_for_user(1, "uid1", "Ali", "5")
        b, rb = self.state.room_for_user(1, "uid1", "Ali", "6")
        self.assertEqual(a, b)
        self.assertRegex(a, r"^[a-z0-9]{12}$")
        self.assertEqual(ra["token"], rb["token"])
        self.assertEqual(self.state.room(a)["cid"], "6")

    def test_rooms_are_per_server(self):
        a, _ = self.state.room_for_user(1, "uid1", "Ali", "5")
        b, _ = self.state.room_for_user(2, "uid1", "Ali", "5")
        self.assertNotEqual(a, b)

    def test_viewer_key_is_stable(self):
        k1 = self.state.viewer_key(1, "uid9", "Veli")
        k2 = self.state.viewer_key(1, "uid9", "Veli")
        self.assertEqual(k1, k2)
        self.assertNotEqual(k1, self.state.viewer_key(2, "uid9", "Veli"))

    def test_persistence_resets_live_flag(self):
        name, _ = self.state.room_for_user(1, "uid1", "Ali", "5")
        self.state.update_room(name, live=True)
        reloaded = bot.State(self.path)
        self.assertFalse(reloaded.room(name)["live"])

    def test_notify_once_per_session(self):
        name, _ = self.state.room_for_user(1, "uid1", "Ali", "5")
        self.assertTrue(self.state.mark_notified(name, "uidX"))
        self.assertFalse(self.state.mark_notified(name, "uidX"))

    def test_cleanup_keeps_live_rooms(self):
        old, _ = self.state.room_for_user(1, "u1", "A", "1")
        live, _ = self.state.room_for_user(1, "u2", "B", "1")
        self.state.rooms[old]["used"] = time.time() - 10 * 3600
        self.state.rooms[live]["used"] = time.time() - 10 * 3600
        self.state.rooms[live]["live"] = True
        removed = self.state.cleanup(room_ttl_hours=6)
        self.assertEqual(removed, [old])
        self.assertIsNotNone(self.state.room(live))


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        bot.STATE = bot.State(os.path.join(self.tmp.name, "s.json"))
        bot.CONF["ip_check"] = True
        self.room, r = bot.STATE.room_for_user(1, "sharer", "Ali", "5")
        self.token = r["token"]
        self.key = bot.STATE.viewer_key(1, "viewer", "Veli")
        self.online = {"viewer": {"client_unique_identifier": "viewer",
                                  "connection_client_ip": "198.51.100.7"}}
        self.find = lambda uid: self.online.get(uid)

    def tearDown(self):
        self.tmp.cleanup()

    def read(self, key, ip="198.51.100.7", room=None):
        return bot.authorize({"action": "read", "path": room or self.room, "ip": ip,
                              "token": key}, find_client=self.find)

    def test_publish_needs_room_token(self):
        ok = bot.authorize({"action": "publish", "path": self.room, "token": self.token})
        bad = bot.authorize({"action": "publish", "path": self.room, "token": "wrong"})
        self.assertTrue(ok)
        self.assertFalse(bad)

    def test_publish_token_via_query(self):
        self.assertTrue(bot.authorize({"action": "publish", "path": self.room, "query": "t=" + self.token}))

    def test_viewer_allowed(self):
        self.assertTrue(self.read(self.key))

    def test_viewer_key_via_query(self):
        self.assertTrue(bot.authorize({"action": "read", "path": self.room, "ip": "198.51.100.7",
                                       "query": "k=" + self.key}, find_client=self.find))

    def test_anonymous_or_fake_key_denied(self):
        self.assertFalse(self.read(""))
        self.assertFalse(self.read("fake"))

    def test_forwarded_link_from_other_ip_denied(self):
        self.assertFalse(self.read(self.key, ip="203.0.113.99"))
        self.assertEqual(bot.check_viewer(self.room, self.key, "203.0.113.99", self.find)[0], "ip")

    def test_ip_check_can_be_disabled(self):
        bot.CONF["ip_check"] = False
        self.assertTrue(self.read(self.key, ip="203.0.113.99"))

    def test_viewer_must_be_online(self):
        self.online.clear()
        self.assertEqual(bot.check_viewer(self.room, self.key, "198.51.100.7", self.find)[0], "ts_yok")

    def test_key_from_other_server_denied(self):
        other = bot.STATE.viewer_key(2, "viewer", "Veli")
        self.assertFalse(self.read(other))

    def test_unknown_room_denied(self):
        self.assertFalse(self.read(self.key, room="aaaaaaaaaaaa"))
        self.assertFalse(self.read(self.key, room="../etc/passwd"))

    def test_api_only_from_localhost(self):
        self.assertTrue(bot.authorize({"action": "api", "ip": "127.0.0.1"}))
        self.assertFalse(bot.authorize({"action": "api", "ip": "198.51.100.7"}))


if __name__ == "__main__":
    unittest.main()
