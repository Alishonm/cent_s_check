"""Offline tests. Run:  python -m unittest discover -s tests -v"""
import sys, tempfile, unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import monitor as m  # noqa: E402
import requests  # noqa: E402

TODAY = date(2026, 11, 4)

HEAD = ("<tr><th>FORMAT</th><th>UNIVERSITY</th><th>REGION / FOREIGN COUNTRY</th><th>CITY</th>"
        "<th>BOOKINGS DEADLINE</th><th>SEATS</th><th>STATE</th><th>DATE</th></tr>")


def row(fmt="CENT@HOME", uni="Sapienza Università di Roma", reg="LAZIO", city="ROMA",
        dl="20/11/2026", seats="---", state="NOT LONGER AVAILABLE", d="26/11/2026"):
    cells = [fmt, uni, reg, city, dl, seats, state, d]
    return "<tr>" + "".join(f"<td>\n {c} </td>" for c in cells) + "</tr>"


def page(*rows, head=HEAD):
    return ("<html><body><h3>AVAILABLE DATES</h3><ul><li><a href='x'>TOLC-I</a></li></ul>"
            f"<table class='tabella'><thead>{head}</thead><tbody>{''.join(rows)}</tbody></table>"
            "<p>For any question contact our Help Desk</p></body></html>")


def recs(*rows, **kw):
    return m.parse_calendar(page(*rows, **kw))


class FakeTG:
    def __init__(self):
        self.sent, self.fail = [], False

    def send(self, text, sleep=None):
        if self.fail:
            return False, False
        self.sent.append(text)
        return True, False


def make_monitor(tmp, fetch=None, tg=None, **cfgkw):
    cfg = m.Config(token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ", chat_id="1", **cfgkw)
    return m.Monitor(cfg, tg or FakeTG(), fetch or (lambda: []), Path(tmp) / "state.json", sleep=lambda s: None)


class ParserTests(unittest.TestCase):
    def test_parse_live_like_row(self):
        r = recs(row())
        self.assertEqual(len(r), 1)
        self.assertEqual(r[0], {"format": "CENT@HOME", "university": "Sapienza Università di Roma",
                                "region": "LAZIO", "city": "ROMA", "deadline": "20/11/2026",
                                "seats": "---", "state": "NOT LONGER AVAILABLE", "date": "26/11/2026"})

    def test_no_table_raises(self):
        with self.assertRaises(m.ParseError):
            m.parse_calendar("<html><body>Maintenance</body></html>")

    def test_header_only_is_valid_empty_calendar(self):
        self.assertEqual(recs(), [])

    def test_column_order_does_not_matter(self):
        head = "<tr><th>DATE</th><th>UNIVERSITY</th><th>BOOKINGS DEADLINE</th></tr>"
        r = m.parse_calendar(page("<tr><td>1/2/2027</td><td>Uni X</td><td>1/1/2027</td></tr>", head=head))
        self.assertEqual((r[0]["university"], r[0]["date"], r[0]["deadline"]), ("Uni X", "1/2/2027", "1/1/2027"))

    def test_unmatched_rows_raise(self):
        with self.assertRaises(m.ParseError):
            m.parse_calendar(page("<tr><td>a</td><td>b</td><td>c</td></tr>"))

    def test_id_ignores_deadline_seats_state(self):
        a, b = recs(row())[0], recs(row(dl="01/01/2027", seats="9", state="AVAILABLE"))[0]
        self.assertEqual(m.session_id(a), m.session_id(b))
        self.assertNotEqual(m.session_id(a), m.session_id(recs(row(d="27/11/2026"))[0]))


class DetectionTests(unittest.TestCase):
    def fresh(self):
        return m.default_state()

    def test_first_run_is_baseline_no_alerts(self):
        st = self.fresh()
        status, msgs = m.apply_check(st, recs(row(), row(uni="Bologna", city="BOLOGNA")), TODAY)
        self.assertEqual((status, msgs, st["initialized"], len(st["sessions"])), ("baseline", [], True, 2))

    def test_new_session_detected(self):
        st = self.fresh()
        m.apply_check(st, recs(row()), TODAY)
        status, msgs = m.apply_check(st, recs(row(), row(uni="University of Bologna", city="Bologna",
                                                         dl="10/12/2026", seats="25", state="AVAILABLE",
                                                         d="15/12/2026")), TODAY)
        self.assertEqual(len(msgs), 1)
        self.assertIn("🆕 تاریخ آزمون جدید CEnT-S پیدا شد!", msgs[0])
        for s in ("University: University of Bologna", "Exam Date: 15/12/2026", "Booking Deadline: 10/12/2026",
                  "Available Seats: 25", "Status: AVAILABLE", m.CALENDAR_URL):
            self.assertIn(s, msgs[0])

    def test_deadline_change(self):
        st = self.fresh()
        m.apply_check(st, recs(row(uni="University of Bologna", dl="05/12/2026", d="15/12/2026")), TODAY)
        _, msgs = m.apply_check(st, recs(row(uni="University of Bologna", dl="10/12/2026", d="15/12/2026")), TODAY)
        self.assertEqual(len(msgs), 1)
        for s in ("⚠️ مهلت ثبت‌نام تغییر کرد!", "Previous deadline: 05/12/2026", "New deadline: 10/12/2026",
                  "Exam Date: 15/12/2026", m.CALENDAR_URL):
            self.assertIn(s, msgs[0])
        self.assertEqual(len(st["sessions"]), 1)  # same session, not a new one

    def test_seats_and_reopen(self):
        st = self.fresh()
        m.apply_check(st, recs(row()), TODAY)
        _, msgs = m.apply_check(st, recs(row(seats="12", state="AVAILABLE")), TODAY)
        self.assertEqual(len(msgs), 1)
        self.assertIn("🟢 ثبت‌نام برای این آزمون باز شد!", msgs[0])
        self.assertIn("Previous seats: ---", msgs[0])
        _, msgs = m.apply_check(st, recs(row(seats="3", state="AVAILABLE")), TODAY)
        self.assertIn("Previous seats: 12", msgs[0])
        self.assertIn("New seats: 3", msgs[0])

    def test_no_duplicate_notifications(self):
        st = self.fresh()
        m.apply_check(st, recs(row()), TODAY)
        changed = recs(row(dl="19/11/2026"))
        self.assertEqual(len(m.apply_check(st, changed, TODAY)[1]), 1)
        for _ in range(3):
            self.assertEqual(m.apply_check(st, changed, TODAY)[1], [])

    def test_empty_page_protection(self):
        st = self.fresh()
        m.apply_check(st, recs(row()), TODAY)
        for i in range(m.EMPTY_CONFIRM - 1):
            status, msgs = m.apply_check(st, [], TODAY)
            self.assertEqual((status, msgs, len(st["sessions"])), ("suspicious", [], 1))
        status, msgs = m.apply_check(st, [], TODAY)  # confirmed repeatedly -> accepted
        self.assertEqual(status, "ok")
        self.assertEqual(len(st["sessions"]), 0)

    def test_suspicious_counter_resets(self):
        st = self.fresh()
        m.apply_check(st, recs(row()), TODAY)
        m.apply_check(st, [], TODAY)
        m.apply_check(st, recs(row()), TODAY)
        self.assertEqual(st["pending_empty"], 0)

    def test_removed_only_if_future(self):
        st = self.fresh()
        m.apply_check(st, recs(row(d="01/10/2026"), row(uni="B", d="20/12/2026")), TODAY)
        st["pending_empty"] = m.EMPTY_CONFIRM
        _, msgs = m.apply_check(st, recs(row(uni="C", d="21/12/2026"), row(uni="D", d="22/12/2026"),
                                         row(uni="E", d="23/12/2026"), row(uni="F", d="24/12/2026"),
                                         row(uni="G", d="25/12/2026"), row(uni="H", d="26/12/2026")), TODAY)
        removed = [x for x in msgs if x.startswith("🗑")]
        self.assertEqual(len(removed), 1)  # only the future session "B"
        self.assertIn("University: B", removed[0])

    def test_many_new_are_compacted(self):
        st = self.fresh()
        m.apply_check(st, recs(row()), TODAY)
        many = [row(uni=f"Uni {i}", city=f"C{i}", d="01/12/2026") for i in range(15)]
        _, msgs = m.apply_check(st, recs(row(), *many), TODAY)
        self.assertEqual(len(msgs), 1)
        self.assertIn("15", msgs[0])


class MonitorFlowTests(unittest.TestCase):
    def test_first_run_startup_and_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            tg = FakeTG()
            mon = make_monitor(tmp, lambda: recs(row()), tg)
            self.assertTrue(mon.run_cycle(startup=True))
            self.assertEqual(len(tg.sent), 1)
            self.assertIn("اولین اجرا", tg.sent[0])
            self.assertIn("Sapienza", tg.sent[0])
            mon.run_cycle()
            self.assertEqual(len(tg.sent), 1)  # nothing new -> silent

    def test_restart_keeps_state_no_false_alerts(self):
        with tempfile.TemporaryDirectory() as tmp:
            tg = FakeTG()
            make_monitor(tmp, lambda: recs(row()), tg).run_cycle(startup=True)
            tg2 = FakeTG()
            mon2 = make_monitor(tmp, lambda: recs(row()), tg2)
            mon2.run_cycle(startup=True)
            self.assertEqual(len(tg2.sent), 1)
            self.assertIn("بازیابی", tg2.sent[0])

    def test_telegram_failure_is_retried_without_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            tg = FakeTG()
            data = {"r": recs(row())}
            mon = make_monitor(tmp, lambda: data["r"], tg)
            mon.run_cycle(startup=True)
            tg.sent.clear()
            tg.fail = True
            data["r"] = recs(row(dl="19/11/2026"))
            mon.run_cycle()
            self.assertEqual(tg.sent, [])
            self.assertEqual(len(mon.state["outbox"]), 1)
            tg.fail = False
            data["r"] = recs(row(dl="19/11/2026"))
            mon.run_cycle()
            self.assertEqual(len(tg.sent), 1)
            mon.run_cycle()
            self.assertEqual(len(tg.sent), 1)
            # also survives a program restart
            self.assertEqual(make_monitor(tmp).state["outbox"], [])

    def test_failure_keeps_data_and_error_alert_is_throttled(self):
        with tempfile.TemporaryDirectory() as tmp:
            tg = FakeTG()
            state = {"ok": True}

            def fetch():
                if state["ok"]:
                    return recs(row())
                raise RuntimeError("boom")
            mon = make_monitor(tmp, fetch, tg, error_after_failures=2)
            mon.run_cycle(startup=True)
            tg.sent.clear()
            state["ok"] = False
            mon.run_cycle()
            self.assertEqual(tg.sent, [])           # 1st failure: quiet
            mon.run_cycle()
            self.assertEqual(len(tg.sent), 1)       # 2nd: one error message
            for _ in range(5):
                mon.run_cycle()
            self.assertEqual(len(tg.sent), 1)       # no spam
            self.assertEqual(len(mon.state["sessions"]), 1)  # data kept
            state["ok"] = True
            mon.run_cycle()
            self.assertEqual(len(tg.sent), 2)
            self.assertIn("دوباره برقرار", tg.sent[1])

    def test_error_repeats_after_interval(self):
        with tempfile.TemporaryDirectory() as tmp:
            tg = FakeTG()

            def fetch():
                raise RuntimeError("down")
            mon = make_monitor(tmp, fetch, tg, error_after_failures=1, error_repeat_hours=6)
            mon.run_cycle()
            mon.run_cycle()
            self.assertEqual(len(tg.sent), 1)
            mon.state["last_error_notice"] = (datetime.now().astimezone() - timedelta(hours=7)).isoformat()
            mon.run_cycle()
            self.assertEqual(len(tg.sent), 2)

    def test_token_not_in_error_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            tg = FakeTG()
            tok = "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ"

            def fetch():
                raise RuntimeError(f"https://api.telegram.org/bot{tok}/x failed")
            mon = make_monitor(tmp, fetch, tg, error_after_failures=1)
            mon.run_cycle()
            self.assertNotIn(tok, tg.sent[0])

    def test_baseline_not_created_by_parse_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            def fetch():
                return m.parse_calendar("<html>error page</html>")
            mon = make_monitor(tmp, fetch)
            self.assertFalse(mon.run_cycle(startup=True))
            self.assertFalse(mon.state["initialized"])


class InfraTests(unittest.TestCase):
    def test_url_exact(self):
        self.assertEqual(m.CALENDAR_URL, "https://testcisia.it/calendario.php?tolc=cents&l=gb&lingua=inglese")

    def test_fetch_uses_exact_url_and_retries(self):
        calls = []

        class Resp:
            status_code, url, content, headers = 200, m.CALENDAR_URL, b"ok", {}

            def raise_for_status(self): pass

        class Sess:
            def get(self, url, **kw):
                calls.append((url, kw))
                if len(calls) < 3:
                    raise requests.ConnectionError("down")
                return Resp()
        out = m.fetch_html(Sess(), sleep=lambda s: None)
        self.assertEqual(out, b"ok")
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(c[0] == m.CALENDAR_URL for c in calls))
        self.assertTrue(all("timeout" in c[1] for c in calls))

    def test_fetch_gives_up(self):
        class Sess:
            def get(self, url, **kw):
                raise requests.Timeout("slow")
        with self.assertRaises(RuntimeError):
            m.fetch_html(Sess(), sleep=lambda s: None)

    def test_state_roundtrip_atomic_and_backup_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "state.json"
            st = m.default_state()
            st["sessions"] = {"a": {"university": "Ünï"}}
            m.save_state(st, p)
            st["next_id"] = 7
            m.save_state(st, p)
            self.assertFalse(Path(str(p) + ".tmp").exists())
            self.assertEqual(m.load_state(p)["sessions"]["a"]["university"], "Ünï")
            p.write_text("{broken", encoding="utf-8")  # simulate corruption
            self.assertEqual(m.load_state(p)["sessions"]["a"]["university"], "Ünï")

    def test_config_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "config.ini"
            p.write_text("[telegram]\nbot_token = YOUR_BOT_TOKEN\nchat_id = YOUR_CHAT_ID\n", encoding="utf-8")
            with self.assertRaises(m.ConfigError):
                m.load_config(p)
            p.write_text("\ufeff[telegram]\nbot_token = 123456789:AAHabcdefghijklmnopqrstuvwxyz12345\nchat_id = -100123\n",
                         encoding="utf-8")
            cfg = m.load_config(p)
            self.assertEqual((cfg.chat_id, cfg.interval_minutes), ("-100123", 15))

    def test_redact(self):
        self.assertNotIn("987654321:AAAAAAAAAAAAAAAAAAAAAA", m.redact("x bot987654321:AAAAAAAAAAAAAAAAAAAAAA y"))


if __name__ == "__main__":
    unittest.main()
