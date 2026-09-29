"""Every country pack: required keys, internal consistency, and basic location routing."""
import _home  # noqa: F401  (must be first: sets JOB_SEARCH_HOME)

import unittest
from zoneinfo import ZoneInfo

import yaml

from common import PACKS_DIR, SEEDS_DIR, load_pack
from jobsearch import location_check

PACKS = sorted(p.stem for p in PACKS_DIR.glob("*.yaml"))
ADZUNA = {"at", "au", "be", "br", "ca", "ch", "de", "es", "fr", "gb", "in", "it", "mx", "nl", "nz", "pl", "sg", "us", "za"}
REQUIRED = ("country", "name", "currency", "fx_to_local", "timezone_default", "cities", "country_places",
            "remote_positive", "remote_reject", "indeed_country", "default_seed")


def seeds_of(pack):
    s = pack["default_seed"]
    return [s] if isinstance(s, str) else s


def rec(location, remote=None):
    return {"source": "greenhouse", "title": "Backend Engineer", "location": location, "remote": remote,
            "region_text": "", "eligible_countries": "", "description": ""}


class PackTest(unittest.TestCase):
    def test_every_pack_is_consistent(self):
        self.assertGreaterEqual(len(PACKS), 2)
        for cc in PACKS:
            with self.subTest(pack=cc):
                p = load_pack(cc)
                self.assertTrue(all(k in p for k in REQUIRED), [k for k in REQUIRED if k not in p])
                self.assertEqual(p["country"], cc.upper())
                ZoneInfo(p["timezone_default"])
                self.assertEqual(len(p["currency"]), 3)
                self.assertNotIn(p["currency"], p["fx_to_local"])
                self.assertTrue(all(isinstance(v, list) for v in p["cities"].values()))
                own = {p["name"].lower(), *p.get("country_aliases", [])}
                self.assertFalse(own & set(p["remote_reject"]), "own country names must not be rejected")
                self.assertFalse(set(p["remote_positive"]) & set(p["remote_reject"]), "positive/reject overlap")
                if "adzuna" in p:
                    self.assertIn(p["adzuna"]["country"], ADZUNA)
                    self.assertTrue(p["adzuna"]["details_domain"].startswith("www.adzuna."))
                for seed in seeds_of(p):
                    self.assertTrue(any((SEEDS_DIR / f"{seed}{x}").exists() for x in (".yaml", ".json")), seed)

    def test_default_seeds_parse(self):
        for cc in PACKS:
            for seed in seeds_of(load_pack(cc)):
                path = SEEDS_DIR / f"{seed}.yaml"
                if path.exists():
                    rows = yaml.safe_load(path.read_text())
                    self.assertTrue(all(len(e) == 2 for xs in rows.values() for e in xs), seed)

    def test_routing_basics(self):
        for cc in PACKS:
            p = load_pack(cc)
            city, aliases = next(iter(p["cities"].items()))
            cfg = {"pack": p, "accept_cities": {city: aliases}, "remote_scope": "country_eligible",
                   "accept_any_country_location": True}
            with self.subTest(pack=cc):
                self.assertEqual(location_check(rec(f"{city.title()}, {p['name']}"), cfg)[0], "ok")
                self.assertEqual(location_check(rec(f"Remote - {p['name']}", remote=True), cfg)[0], "ok")
                self.assertEqual(location_check(rec("Remote (Japan)", remote=True), cfg)[0], "reject")


if __name__ == "__main__":
    unittest.main()
