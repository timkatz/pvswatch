"""Unit tests for the server-side display math (/api/v1/now).

Run inside the dev container by `./pvswatch.sh test`:
    python -m unittest discover -s tests -p 'test_*.py'
"""
import time
import unittest

import proxy


def snapshot(solar=0.0, home=0.0, grid=None, battery_kw=None, soc=0.5, inverters=(), legs=None):
    meter_c = {"TYPE": "PVS5-METER-C", "p_3phsum_kw": home}
    if legs:
        meter_c.update(p1_kw=legs[0], p2_kw=legs[1])
    data = {"devices": [{"TYPE": "PVS5-METER-P", "p_3phsum_kw": solar, "CURTIME": "2026,11,14,18,54,54"},
                        meter_c, *inverters]}
    if grid is not None:
        data["grid"] = {"p_kw": grid}
    if battery_kw is not None:
        data["battery"] = {"p_kw": battery_kw, "soc": soc}
    return data


def inverter(state="Working", kw=0.3, op="", vac=240.0):
    return {"TYPE": "SOLARBRIDGE", "SERIAL": f"E{kw}{state}", "STATEDESCR": state,
            "OPERATION": op, "p_3phsum_kw": kw, "vln_3phavg_v": vac}


class NowTest(unittest.TestCase):
    def setUp(self):
        proxy._last_battery.update(block=None, ts=None)

    def test_charge_source(self):
        src = lambda s, h, b: proxy.build_now(snapshot(s, h, 0, b))["battery"]["charge_source"]
        self.assertEqual(src(5, 1, -2), "solar")
        self.assertEqual(src(0.5, 1.2, -0.8), "grid")
        self.assertEqual(src(1.5, 1.0, -0.8), "solar+grid")
        self.assertIsNone(src(0, 1, 1))

    def test_battery_state_thresholds(self):
        state = lambda b: proxy.build_now(snapshot(1, 1, 0, b))["battery"]["state"]
        self.assertEqual(state(-0.3), "charging")
        self.assertEqual(state(0.3), "discharging")
        self.assertEqual(state(0.02), "idle")

    def test_battery_kept_when_block_missing_then_expires(self):
        proxy.build_now(snapshot(1, 1, 0, -1, soc=0.8), fetched_at=time.time())
        b = proxy.build_now(snapshot(1, 1, 0))["battery"]
        self.assertEqual(b["soc"], 0.8)
        self.assertTrue(b["stale"])
        proxy._last_battery["ts"] = time.time() - proxy.API_V1_BATTERY_STALE_S - 1
        self.assertIsNone(proxy.build_now(snapshot(1, 1, 0))["battery"])

    def test_grid_direction_and_derivation(self):
        self.assertEqual(proxy.build_now(snapshot(0, 2, 2))["grid"]["direction"], "importing")
        self.assertEqual(proxy.build_now(snapshot(4, 1, -3))["grid"]["direction"], "exporting")
        self.assertEqual(proxy.build_now(snapshot(1, 1, 0.03))["grid"]["direction"], "balanced")
        g = proxy.build_now(snapshot(1, 3))["grid"]  # no meter block: home - solar - battery
        self.assertEqual((g["kw"], g["source"]), (2, "derived"))

    def test_flow_split(self):
        f = proxy.build_now(snapshot(6.7, 1.9, 1.8, -6.6))["flow"]
        p = f["paths"]
        self.assertAlmostEqual(p["solar_home"], 1.9)
        self.assertAlmostEqual(p["solar_battery"], 4.8)
        self.assertAlmostEqual(p["grid_battery"], 1.8)
        self.assertIn("Grid is charging the battery", f["headline"])
        proxy._last_battery.update(block=None, ts=None)
        self.assertEqual(proxy.build_now(snapshot())["flow"]["headline"], "System idle")

    def test_house_sides(self):
        s = proxy.build_now(snapshot(2, 3, 1, 0, legs=(-0.5, 1.5)))["house_sides"]
        self.assertEqual((s["l1_kw"], s["l2_kw"]), (0.5, 2.5))
        self.assertFalse(s["balanced"])
        self.assertAlmostEqual(s["battery_cover_kw"], 0.9)
        self.assertIsNone(proxy.build_now(snapshot(1, 1))["house_sides"])

    def test_panels_night_vs_comms_error_vs_producing(self):
        night = [inverter("Error", 0, "noop", 0) for _ in range(3)]
        p = proxy.build_now(snapshot(0.01, 1, inverters=night))["panels"]
        self.assertEqual(p["mode"], "night")
        self.assertTrue(all(i["status"] == "idle" for i in p["items"]))
        self.assertEqual(p["errors"], 0)

        p = proxy.build_now(snapshot(2, 1, inverters=night))["panels"]
        self.assertEqual(p["mode"], "comms_error")
        self.assertEqual(p["errors"], 3)

        mixed = [inverter(kw=0.3), inverter(kw=0.2), inverter("Error", 0.0)]
        p = proxy.build_now(snapshot(0.5, 1, inverters=mixed))["panels"]
        self.assertEqual(p["mode"], "producing")
        self.assertEqual([i["status"] for i in p["items"]], ["ok", "ok", "error"])
        self.assertEqual([i["low"] for i in p["items"]], [False, True, False])

        dim = [inverter(kw=0.058), inverter(kw=0.040)]  # best under 100 W: nobody is "low"
        p = proxy.build_now(snapshot(0.1, 1, inverters=dim))["panels"]
        self.assertEqual([i["low"] for i in p["items"]], [False, False])

    def test_curtime_formats(self):
        a = proxy._curtime_epoch("2026,11,14,18,55,01")
        self.assertEqual(a, proxy._curtime_epoch("2026-11-14T18:55:01Z"))
        self.assertIsNone(proxy._curtime_epoch("garbage"))
        self.assertIsNone(proxy._curtime_epoch(""))


if __name__ == "__main__":
    unittest.main()
