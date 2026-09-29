import _home  # noqa: F401  (must be first: sets JOB_SEARCH_HOME)

import unittest

from common import load_pack, load_profiles
from jobsearch import location_check, route, seniority


def rec(location="", title="Backend Engineer", source="greenhouse", remote=None, description="", **kw):
    return {"source": source, "title": title, "location": location, "remote": remote, "region_text": "",
            "eligible_countries": "", "description": description, **kw}


def cfg_for(country, cities, **kw):
    return {"pack": load_pack(country), "accept_cities": cities, "remote_scope": "country_eligible",
            "accept_any_country_location": True, **kw}


IN = cfg_for("in", {"delhi ncr": ["gurugram", "noida"], "bengaluru": ["bangalore"]})
US = cfg_for("us", {"austin": [], "new york": ["nyc"]})


class IndiaLocationTest(unittest.TestCase):
    def test_accepted_city_and_alias(self):
        self.assertEqual(location_check(rec("Gurugram, Haryana"), IN), ("ok", "delhi ncr", ""))
        self.assertEqual(location_check(rec("Bangalore"), IN), ("ok", "bengaluru", ""))

    def test_other_city_rejected(self):
        self.assertEqual(location_check(rec("Pune, India"), IN), ("reject", "", "location"))

    def test_country_only_accepted_unless_other_city(self):
        self.assertEqual(location_check(rec("India"), IN), ("ok", "india", ""))
        self.assertEqual(location_check(rec("Chennai, India"), IN)[0], "reject")
        strict = {**IN, "accept_any_country_location": False}
        self.assertEqual(location_check(rec("India"), strict)[0], "reject")

    def test_remote_eligibility(self):
        self.assertEqual(location_check(rec("Remote - India", remote=True), IN), ("ok", "remote", ""))
        self.assertEqual(location_check(rec("Remote (US only)", remote=True), IN), ("reject", "", "remote-region"))
        self.assertEqual(location_check(rec("Remote", title="Engineer (APAC)", remote=True), IN)[0], "ok")
        self.assertEqual(location_check(rec("Remote", remote=True), IN), ("verify", "remote", "remote-unverified"))
        self.assertEqual(location_check(rec("Anywhere", remote=True), IN), ("ok", "remote", ""))

    def test_remote_restricted_by_jd(self):
        us = rec("Remote", remote=True, description="Great team. You must be based in the United States.")
        self.assertEqual(location_check(us, IN), ("reject", "", "remote-restricted-jd"))
        ok = rec("Remote", remote=True, description="Candidates must be located in India or anywhere in APAC.")
        self.assertEqual(location_check(ok, IN), ("ok", "remote", ""))
        self.assertEqual(location_check(rec("Remote", remote=True, description="We are a US-based company."), IN)[0],
                         "reject")

    def test_eligible_country_list(self):
        self.assertEqual(location_check(rec("Remote", remote=True, eligible_countries="India, Nepal"), IN)[0], "ok")
        self.assertEqual(location_check(rec("Remote", remote=True, eligible_countries="Germany"), IN),
                         ("reject", "", "remote-country-list"))

    def test_indeed_and_global_scope(self):
        self.assertEqual(location_check(rec("Remote", source="indeed", remote=True), IN), ("ok", "remote", ""))
        self.assertEqual(location_check(rec("Remote (US only)", remote=True), {**IN, "remote_scope": "global_ok"})[0], "ok")

    def test_custom_source_unverified(self):
        self.assertEqual(location_check(rec("", source="custom"), IN), ("verify", "unknown", "location-unverified"))
        self.assertEqual(location_check(rec("Engineering", source="custom"), IN)[0], "verify")
        self.assertEqual(location_check(rec("Hyderabad", source="custom"), IN)[0], "reject")


class USLocationTest(unittest.TestCase):
    def test_us_pack(self):
        self.assertEqual(location_check(rec("New York, NY"), US), ("ok", "new york", ""))
        self.assertEqual(location_check(rec("Remote - USA", remote=True), US), ("ok", "remote", ""))
        self.assertEqual(location_check(rec("Remote (India)", remote=True), US), ("reject", "", "remote-region"))
        self.assertEqual(location_check(rec("Seattle, WA"), US)[0], "reject")


class RoutingTest(unittest.TestCase):
    profiles = load_profiles()  # examples/data-folder: backend-sde, appsec

    def test_seniority(self):
        for title, lvl in [("Senior Backend Engineer", "senior"), ("Sr. Java Developer", "senior"),
                           ("Software Engineer II", "mid"), ("SDE I", "junior"), ("Staff Engineer", "staff"),
                           ("Member of Technical Staff", "mid"), ("Engineering Manager", "manager"),
                           ("Backend Intern", "intern"), ("Backend Engineer", "mid")]:
            self.assertEqual(seniority(title), lvl, title)

    def test_routes(self):
        self.assertEqual(route("Senior Backend Engineer", self.profiles)[0], [("backend-sde", 3)])
        self.assertEqual(route("Platform Engineer", self.profiles)[:2], ([("backend-sde", 2)], ["appsec"]))
        self.assertEqual(route("Application Security Engineer", self.profiles)[:2],
                         ([("appsec", 3)], ["backend-sde"]))
        self.assertEqual(route("Integration Engineer", self.profiles)[:2], ([], ["appsec", "backend-sde"]))

    def test_rejections(self):
        self.assertEqual(route("Frontend Engineer", self.profiles), ([], [], "title-exclude"))
        self.assertEqual(route("Software Engineer I", self.profiles)[2], "seniority-junior")
        self.assertEqual(route("Account Executive", self.profiles)[:2], ([], []))


if __name__ == "__main__":
    unittest.main()
