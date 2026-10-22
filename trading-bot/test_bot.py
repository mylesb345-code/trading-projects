"""Offline tests for bot.py. Run: python test_bot.py"""
import csv
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bot  # noqa: E402

DAY = bot.DAY
T0 = 1_700_006_400  # a UTC midnight

# Real Binance BTCUSDT daily candles, 2026-09-01 .. 2026-10-20 (open, high, low, close), with the EMA50 and
# EMA200 that TextToQuant's chart computed for the same days. Pulled 2026-10-21.
REAL_START = 1788220800
REAL_OHLC = [
    (78581.3, 79220.61, 76420, 77439), (77439.01, 77792, 76264, 77340.01), (77340.01, 82300, 76968, 81270.37),
    (81270.37, 81427.75, 78660, 79660.77), (79660.77, 80200, 79442, 79831.75), (79831.75, 80559.99, 79233, 80341.83),
    (80341.83, 80443.99, 78680, 79112.01), (79112, 79485, 77620.01, 78455.8), (78455.8, 79760, 77770, 78306.43),
    (78306.43, 78564.39, 76464, 76568.72), (76568.73, 79890, 76046.58, 77225.7), (77225.71, 77505.67, 77059.75, 77278.73),
    (77278.73, 77450, 76500, 76842.01), (76842.01, 79600, 76388.72, 78189.21), (78189.2, 78250.46, 74967.97, 75644.48),
    (75644.48, 76560.76, 75064.82, 76206.01), (76206, 77179.47, 76000, 76417.01), (76417.01, 81400, 76296, 80883.87),
    (80883.86, 81951, 80844.58, 81249.99), (81249.99, 81497.33, 80126.04, 81178), (81178.01, 87395.67, 80850.22, 86620),
    (86620.01, 86717.6, 85114, 86208.56), (86208.57, 87278.54, 83500.01, 84397.6), (84397.6, 84942.45, 82874.93, 84410.24),
    (84410.24, 85255, 83183, 84099.99), (84100, 84473.58, 83798, 84433.1), (84433.11, 85159.03, 84132, 84472),
    (84472.01, 84999, 82563, 83500.01), (83500.01, 84563.99, 82775.94, 83663.66), (83663.66, 85649.95, 82956.11, 83623.6),
    (83623.59, 85273.65, 83186, 84880.05), (84880.05, 87220, 83888, 84518.01), (84518, 85037.63, 84456.02, 84753.56),
    (84753.57, 86800, 84719.99, 86530), (86530, 86999.11, 84972.01, 85766.87), (85766.87, 86698.99, 85136.11, 85549.93),
    (85549.94, 85598.22, 82787.25, 83321.81), (83321.81, 83521.15, 80393.56, 81754.45), (81754.46, 83528.98, 81603.52, 82635.55),
    (82635.56, 83173.29, 82545.82, 82988.36), (82988.35, 84199, 82804.65, 83773.12), (83773.12, 84352, 81750.09, 81850),
    (81850.01, 84168, 81033.66, 83169), (83169.01, 84146.72, 81444.42, 81682.01), (81682.02, 82634.3, 79800, 82245.93),
    (82245.93, 82940.81, 80809.25, 81615.64), (81615.91, 81770.01, 80819.66, 81491.04), (81491.04, 82295.47, 80684.36, 82116),
    (82116, 83158, 81748.85, 81938.4), (81938.39, 83439, 81200, 83028.15),
]
REAL_EMA50 = [
    69623.4, 69926, 70370.9, 70735.2, 71091.9, 71454.7, 71755, 72017.7, 72264.4, 72433.2, 72621.1, 72803.8, 72962.1,
    73167.1, 73264.3, 73379.6, 73498.7, 73788.3, 74081, 74359.3, 74840.1, 75285.9, 75643.2, 75987, 76305.2, 76623.9,
    76931.7, 77189.3, 77443.2, 77685.5, 77967.7, 78224.6, 78480.6, 78796.3, 79069.6, 79323.7, 79480.5, 79569.7, 79689.9,
    79819.3, 79974.3, 80047.9, 80170.3, 80229.6, 80308.6, 80359.9, 80404.3, 80471.4, 80528.9, 80626.9,
]
REAL_EMA200 = [
    71543.2, 71600.9, 71697.1, 71776.3, 71856.5, 71940.9, 72012.3, 72076.4, 72138.4, 72182.5, 72232.6, 72282.8, 72328.2,
    72386.5, 72418.9, 72456.6, 72496, 72579.5, 72665.8, 72750.5, 72888.5, 73021, 73134.2, 73246.4, 73354.4, 73464.6,
    73574.2, 73672.9, 73772.3, 73870.4, 73979.9, 74084.8, 74190.9, 74313.7, 74427.7, 74538.3, 74625.7, 74696.7, 74775.7,
    74857.4, 74946.1, 75014.8, 75095.9, 75161.5, 75232, 75295.5, 75357.1, 75424.4, 75489.2, 75564.2,
]
REAL = [(REAL_START + i * DAY,) + c for i, c in enumerate(REAL_OHLC)]


def candles_from(closes, start=T0):
    return [(start + i * DAY, c, c * 1.01, c * 0.99, c) for i, c in enumerate(closes)]


def small_cfg(**over):
    cfg = dict(bot.DEFAULTS, ema_fast=3, ema_slow=6, symbols={"BTC-USD": "tested", "ETH-USD": "untested"})
    cfg.update(over)
    return cfg


class Recorder:
    def __init__(self):
        self.sent = []

    def __call__(self, title, body, priority="default"):
        self.sent.append((title, body, priority))


class IndicatorTests(unittest.TestCase):
    def test_ema_seeds_with_average_then_smooths(self):
        self.assertEqual(bot.ema([1, 2, 3, 4, 5], 3), [None, None, 2, 3, 4])

    def test_ema_step_matches_texttoquant_chart(self):
        closes = [c[4] for c in REAL]
        for line, period in ((REAL_EMA50, 50), (REAL_EMA200, 200)):
            k = 2 / (period + 1)
            for i in range(1, len(closes)):
                self.assertAlmostEqual(line[i], closes[i] * k + line[i - 1] * (1 - k), delta=0.15)

    def test_real_btc_is_long_and_still_open(self):
        # The backtest has an open long from 2026-09-12; on the chart's EMAs the conditions first all hold on
        # 2026-09-09, so the bot dates the signal 3 days earlier. No daily close fell below EMA50 since.
        position, entry, latest = bot.replay_rule(REAL, REAL_EMA50, REAL_EMA200)
        self.assertEqual(position, "LONG")
        self.assertEqual(bot.day_str(entry[0]), "2026-09-09")
        self.assertEqual(entry[1], 78306.43)
        self.assertEqual(bot.day_str(latest["time"]), "2026-10-20")
        self.assertEqual(latest["close"], 83028.15)

    def test_replay_long_exit_and_short(self):
        rising = list(range(100, 120))
        position, _, _ = bot.replay_rule(candles_from(rising), bot.ema(rising, 3), bot.ema(rising, 6))
        self.assertEqual(position, "LONG")
        dip = rising + [115]
        position, _, _ = bot.replay_rule(candles_from(dip), bot.ema(dip, 3), bot.ema(dip, 6))
        self.assertIsNone(position)
        falling = rising + list(range(118, 80, -2))
        position, _, _ = bot.replay_rule(candles_from(falling), bot.ema(falling, 3), bot.ema(falling, 6))
        self.assertEqual(position, "SHORT")


class PaperTests(unittest.TestCase):
    def test_win_pays_fee(self):
        result, after, wiped = bot.paper_result("LONG", 100, 110, 99, 1, 0.08)
        self.assertAlmostEqual(result, 10.0)
        self.assertAlmostEqual(after, 9.2)
        self.assertFalse(wiped)

    def test_loss_has_no_fee(self):
        self.assertEqual(bot.paper_result("LONG", 100, 95, 94, 1, 0.08)[:2], (-5.000000000000004, -5.000000000000004))

    def test_short_win(self):
        result, after, _ = bot.paper_result("SHORT", 100, 90, 101, 1, 0.08)
        self.assertAlmostEqual(result, 10.0)
        self.assertAlmostEqual(after, 9.2)

    def test_multiplier_wipes_out(self):
        self.assertEqual(bot.paper_result("LONG", 100, 120, 89, 10, 0.08), (-100.0, -100.0, True))
        self.assertEqual(bot.paper_result("SHORT", 100, 80, 111, 10, 0.08), (-100.0, -100.0, True))

    def test_worst_price_window(self):
        candles = candles_from([100, 90, 80, 95])
        self.assertAlmostEqual(bot.worst_price(candles, "LONG", T0, T0 + 3 * DAY), 79.2)
        self.assertAlmostEqual(bot.worst_price(candles, "SHORT", T0, T0 + 3 * DAY), 95.95)


class DataTests(unittest.TestCase):
    def test_coinbase_columns_and_paging(self):
        urls = []
        first = [[T0 + i * DAY, 1, 4, 2, 3, 9] for i in range(300)]
        second = [[T0 - DAY, 10, 40, 20, 30, 9]]

        def get(url):
            urls.append(url)
            return first if len(urls) == 1 else second

        with mock.patch.object(bot.time, "sleep"):
            rows = bot.fetch_coinbase("BTC-USD", 1000, T0 + 400 * DAY, get=get)
        self.assertEqual(len(urls), 2)
        self.assertEqual(rows[T0], (2.0, 4.0, 1.0, 3.0))
        self.assertEqual(rows[T0 - DAY], (20.0, 40.0, 10.0, 30.0))
        self.assertIn("granularity=86400", urls[0])

    def test_kraken_columns(self):
        data = {"error": [], "result": {"XXBTZUSD": [[T0, "2", "4", "1", "3", "2.5", "9", 5]], "last": T0}}
        self.assertEqual(bot.fetch_kraken("BTC-USD", 720, T0, get=lambda url: data), {T0: (2.0, 4.0, 1.0, 3.0)})

    def test_get_candles_falls_back_and_drops_unfinished_day(self):
        def broken(*a):
            raise OSError("blocked")

        def ok(symbol, days, now):
            return {T0: (1, 2, 0.5, 1.5), T0 + DAY: (1.5, 2, 1, 1.8)}

        candles, source = bot.get_candles("BTC-USD", 10, T0 + DAY + 60, 1, [("A", broken), ("B", ok)])
        self.assertEqual(source, "B")
        self.assertEqual(candles, [(T0, 1, 2, 0.5, 1.5)])

    def test_get_candles_reports_every_source(self):
        def broken(*a):
            raise OSError("blocked")

        with self.assertRaisesRegex(RuntimeError, "A: blocked; B: blocked"):
            bot.get_candles("BTC-USD", 10, T0, 1, [("A", broken), ("B", broken)])

    def test_get_candles_needs_enough_history(self):
        with self.assertRaisesRegex(RuntimeError, "need 400"):
            bot.get_candles("BTC-USD", 10, T0 + 5 * DAY, 400, [("A", lambda *a: {T0: (1, 1, 1, 1)})])


class RunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.journal = os.path.join(self.tmp.name, "journal.csv")
        self.closes = list(range(100, 120))

    def tearDown(self):
        self.tmp.cleanup()

    def fetch(self, symbol, days, now, min_needed):
        return bot.get_candles(symbol, days, now, min_needed,
                               [("Fake", lambda s, d, n: {c[0]: c[1:] for c in candles_from(self.closes)})])

    def run_at(self, state, day_index, notify):
        bot.run_once(small_cfg(), state, T0 + day_index * DAY + 3600, notify, fetch=self.fetch,
                     journal_path=self.journal)

    def test_start_repeat_then_exit(self):
        state, sent = {}, Recorder()
        self.run_at(state, 20, sent)
        self.assertEqual([s[0] for s in sent.sent], ["Bot is on"])
        self.assertIn("BTC (tested): LONG", sent.sent[0][1])
        self.assertEqual(state["coins"]["BTC-USD"]["paper"]["entry_price"], 119)

        sent.sent.clear()
        self.run_at(state, 20, sent)  # same candle an hour later: nothing new to say
        self.assertEqual(sent.sent, [])

        self.closes.append(115)
        self.run_at(state, 21, sent)
        titles = [s[0] for s in sent.sent]
        self.assertEqual(titles, ["BTC EXIT", f"Daily check {bot.day_str(T0 + 20 * DAY)}"])
        alert = sent.sent[0]
        self.assertEqual(alert[2], "high")
        self.assertIn("EXIT your LONG", alert[1])
        self.assertIn("Trench: close your BTC Up position.", alert[1])
        self.assertIn("-3.36%", alert[1])
        self.assertIn("ETH paper test changed today", sent.sent[1][1])
        self.assertIsNone(state["coins"]["BTC-USD"]["position"])

        with open(self.journal) as f:
            rows = list(csv.DictReader(f))
        self.assertEqual([(r["coin"], r["tested"], r["side"], r["after_fee_pct"]) for r in rows],
                         [("BTC", "yes", "LONG", "-3.36"), ("ETH", "no", "LONG", "-3.36")])

    def test_flip_opens_new_paper_trade(self):
        state, sent = {}, Recorder()
        self.run_at(state, 20, sent)
        self.closes.append(100)
        self.run_at(state, 21, sent)
        alert = next(s for s in sent.sent if s[2] == "high")
        self.assertEqual(alert[0], "BTC GO SHORT")
        self.assertIn("FLIP: exit your LONG, then go SHORT", alert[1])
        self.assertIn("Trench: open BTC Down.", alert[1])
        self.assertEqual(state["coins"]["BTC-USD"]["paper"], {"side": "SHORT", "entry_time": T0 + 20 * DAY,
                                                              "entry_price": 100})

    def test_failure_alert_once_then_recovery(self):
        state, sent = {}, Recorder()

        def broken(*a):
            raise RuntimeError("no data")

        for _ in range(4):
            bot.run_once(small_cfg(), state, T0, sent, fetch=broken, journal_path=self.journal)
        self.assertEqual([s[0] for s in sent.sent], ["Bot can't get prices"])
        self.run_at(state, 20, sent)
        self.assertIn("Bot is back", [s[0] for s in sent.sent])
        self.assertEqual(state["failed_runs"], 0)


class NotifyTests(unittest.TestCase):
    def test_posts_to_ntfy(self):
        captured = []
        with mock.patch.dict(os.environ, {}, clear=False), \
                mock.patch.object(bot.urllib.request, "urlopen", side_effect=lambda req, timeout: captured.append(req)
                                  or mock.MagicMock()):
            os.environ.pop("NTFY_TOPIC", None)
            bot.make_notifier(dict(bot.DEFAULTS, ntfy_topic="abc"))("BTC GO LONG", "body text", "high")
        req = captured[0]
        self.assertEqual(req.full_url, "https://ntfy.sh/abc")
        self.assertEqual(req.data, b"body text")
        self.assertEqual(req.get_header("Title"), "BTC GO LONG")
        self.assertEqual(req.get_header("Priority"), "high")

    def test_ensure_topic_creates_and_saves_once(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NTFY_TOPIC", None)
            path = os.path.join(d, "config.json")
            cfg = dict(bot.DEFAULTS)
            bot.ensure_topic(cfg, path)
            self.assertTrue(cfg["ntfy_topic"].startswith("swing-bot-"))
            self.assertEqual(bot.load_json(path, {})["ntfy_topic"], cfg["ntfy_topic"])
            first = cfg["ntfy_topic"]
            bot.ensure_topic(cfg, path)
            self.assertEqual(cfg["ntfy_topic"], first)


if __name__ == "__main__":
    unittest.main()
