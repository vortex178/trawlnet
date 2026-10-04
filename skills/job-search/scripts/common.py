"""Shared helpers: paths, config + country pack, IO, normalization, dates, salary, HTML.

Two roots:
  PLUGIN_ROOT  engine files (this repo / installed plugin dir): packs/, seeds/, templates/
  HOME         the user's data folder (config.yaml, CLAUDE.md, data/, .secrets/) — from $JOB_SEARCH_HOME,
               else the nearest folder at/above the working directory that has config.yaml + data/.
"""
from __future__ import annotations

import datetime as dt
import email.utils
import hashlib
import html
import json
import os
import re
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")  # LibreSSL / EOL noise from google-auth, urllib3

import yaml  # noqa: E402

SCRIPTS_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPTS_DIR.parent
PLUGIN_ROOT = SKILL_DIR.parents[1]
PACKS_DIR = PLUGIN_ROOT / "packs"
SEEDS_DIR = PLUGIN_ROOT / "seeds"
TEMPLATES_DIR = PLUGIN_ROOT / "templates"


def _find_home() -> Path:
    env = os.environ.get("JOB_SEARCH_HOME")
    if env:
        return Path(env).expanduser().resolve()
    cwd = Path.cwd().resolve()
    for d in [cwd, *cwd.parents]:
        if (d / "config.yaml").exists() and (d / "data").is_dir():
            return d
    return cwd


HOME = _find_home()
DATA = HOME / "data"
PROFILES_DIR = DATA / "profiles"
RUNS_DIR = DATA / "runs"
COMPANIES_PATH = DATA / "companies.json"
PENDING_ROWS_PATH = DATA / "pending_tracker_rows.jsonl"
UA = "Mozilla/5.0 (personal job-search script; trawlnet)"


def rel(path: Path) -> str:
    """Path for display: relative to the data folder when inside it."""
    try:
        return str(Path(path).resolve().relative_to(HOME))
    except ValueError:
        return str(path)


# ---------- config / country pack / profiles ----------

class ConfigPathError(ValueError):
    """A config.yaml path that leaves the data folder."""


def served(path: Path, name: str = "file") -> Path:
    """`path` itself, refused unless it lies in the data folder with no symlink anywhere below it: the MCP server
    serves files of a folder that may be a clone, where a link (to the file or to a directory above it) could point
    at any file of the user's, including the folder's own .secrets/ and config.yaml."""
    lexical = Path(os.path.abspath(path))
    if not lexical.is_relative_to(HOME):
        raise ConfigPathError(f"{name} is outside the data folder; refusing to read it")
    for part in (lexical, *lexical.parents):
        if part == HOME:
            break
        if part.is_symlink():
            raise ConfigPathError(f"{name} is reached through a symlink; refusing to read it")
    return lexical


def confined(configured, default: str, name: str, home: Path | None = None) -> Path:
    """A config path inside the data folder: a copied folder's config.yaml is untrusted, so it must not point the
    engine at other files (key files are sent to APIs). Symlinks are followed, so one pointing out is refused too."""
    home = home or HOME
    path = (home / (configured or default)).resolve()
    if not path.is_relative_to(home.resolve()):
        raise ConfigPathError(f"{name} must be inside the data folder ({home}), got {configured}; move the file there "
                         "(a symlink that resolves outside is refused too)")
    return path


def load_pack(country: str) -> dict:
    """Country pack: the data folder's packs/<cc>.yaml (user override) else the plugin's."""
    cc = country.lower()
    if not re.fullmatch(r"[a-z0-9_-]+", cc):
        sys.exit(f"country {country!r} is not a country code")
    for d in (HOME / "packs", PACKS_DIR):
        p = d / f"{cc}.yaml"
        if p.exists():
            return yaml.safe_load(p.read_text(encoding="utf-8"))
    sys.exit(f"No country pack '{cc}.yaml' in {PACKS_DIR}. Available: "
             + ", ".join(sorted(x.stem for x in PACKS_DIR.glob('*.yaml'))))


def load_config() -> dict:
    path = HOME / "config.yaml"
    if not path.exists():
        sys.exit(f"No config.yaml in {HOME}. Run setup (see the job-search skill) or set JOB_SEARCH_HOME.")
    cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    pack = load_pack(cfg.get("country", "IN"))
    pack.update(cfg.get("pack_overrides") or {})
    cfg["pack"] = pack
    cfg.setdefault("currency", pack["currency"])
    cfg["fx_to_local"] = {**pack.get("fx_to_local", {}), **(cfg.get("fx_to_local") or {})}
    cfg.setdefault("indeed_country_code", pack.get("indeed_country", cfg.get("country", "IN")))
    cfg.setdefault("accept_cities", {})
    cfg.setdefault("sources", {})
    if cfg["sources"].get("ziprecruiter") and cfg.get("country") not in ("US", "CA"):
        cfg["sources"]["ziprecruiter"] = False  # connector only covers US/Canada
    return cfg


def save_config_value(dotted_key: str, value) -> None:
    """Update one scalar in config.yaml in place, preserving comments."""
    path = HOME / "config.yaml"
    key = dotted_key.split(".")[-1]
    text = path.read_text(encoding="utf-8")
    new, n = re.subn(rf"(?m)^(\s*{re.escape(key)}:)\s*[^#\n]*", rf"\g<1> {json.dumps(value)} ", text, count=1)
    if n != 1:
        raise KeyError(dotted_key)
    path.write_text(new, encoding="utf-8")


def load_profiles() -> dict:
    """Role profiles keyed by id (excludes master.yaml / preferences.yaml)."""
    out = {}
    for p in sorted(PROFILES_DIR.glob("*.yaml")):
        if p.stem in ("master", "preferences"):
            continue
        prof = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        if prof.get("active", True):
            out[prof["id"]] = prof
    return out


def load_preferences() -> dict:
    p = PROFILES_DIR / "preferences.yaml"
    return (yaml.safe_load(p.read_text(encoding="utf-8")) or {}) if p.exists() else {}


def salary_floor(prefs: dict, profile_id: str, currency: str):
    """Annual floor in local currency: `min_annual`, or `min_lpa`/`min_inr_lpa` (lakhs per annum, INR)."""
    sal = (prefs.get("salary") or {})
    entry = sal.get(profile_id) or sal.get("default") or {}
    if entry.get("min_annual"):
        return float(entry["min_annual"])
    lpa = entry.get("min_lpa") or entry.get("min_inr_lpa")
    return lpa * 1e5 if lpa and currency == "INR" else None


# ---------- run dirs / jsonl ----------

def today() -> str:
    return dt.date.today().isoformat()


def run_dir(date: str | None = None) -> Path:
    d = RUNS_DIR / (date or today())
    (d / "jd").mkdir(parents=True, exist_ok=True)
    return d


def read_jsonl(path: Path) -> list:
    if not path.exists():
        gz = path.with_name(path.name + ".gz")  # raw files are gzipped after publish (retention rule)
        if not gz.exists():
            return []
        import gzip
        text = gzip.decompress(gz.read_bytes()).decode()
    else:
        text = path.read_text(encoding="utf-8")
    out = []
    for i, line in enumerate(text.split("\n"), 1):  # not splitlines(): JDs contain U+2028/2029
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            print(f"WARN {path.name}:{i} invalid JSON, skipped")
    return out


def write_jsonl(path: Path, rows) -> None:
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def append_jsonl(path: Path, rows) -> None:
    with path.open("a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


# ---------- normalization ----------

_NON_ALNUM = re.compile(r"[^a-z0-9+#]+")
_CO_SUFFIX = re.compile(
    r"\b(inc|llc|ltd|limited|pvt|private|corp|corporation|co|gmbh|plc|technologies|technology|"
    r"solutions|software|labs|com|india)\b"
)


def norm(text: str | None) -> str:
    """Lowercase, punctuation -> spaces, padded for ' phrase ' matching."""
    t = (text or "").lower().replace("sr.", "senior ").replace("jr.", "junior ")
    return " " + " ".join(_NON_ALNUM.sub(" ", t).split()) + " "


def has_phrase(normed: str, phrase: str) -> bool:
    return norm(phrase) in normed


def norm_company(name: str | None) -> str:
    t = norm(name).replace(".com", " ")
    t = _CO_SUFFIX.sub(" ", t)
    return " ".join(t.split())


def job_key(company: str, title: str, loc_bucket: str) -> str:
    t = norm(re.sub(r"\([^)]*\)", " ", title or ""))  # drop "(IND, Hybrid)"-style notes
    raw = f"{norm_company(company)}|{' '.join(t.split())}|{loc_bucket}"
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


# ---------- dates ----------

def parse_date(value) -> str | None:
    """Return ISO date (YYYY-MM-DD) or None. Accepts ISO, RFC822, 'September 11, 2026', epoch ms."""
    if value in (None, "", "N/A"):
        return None
    if isinstance(value, (int, float)):
        secs = value / 1000 if value > 1e11 else value
        return dt.datetime.fromtimestamp(secs, dt.timezone.utc).date().isoformat()
    s = str(value).strip()
    m = re.match(r"(\d{4}-\d{2}-\d{2})", s)
    if m:
        return m.group(1)
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%d %b %Y", "%d %B %Y"):
        try:
            return dt.datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    try:
        return email.utils.parsedate_to_datetime(s).date().isoformat()
    except (TypeError, ValueError):
        return None


def age_days(iso: str | None):
    if not iso:
        return None
    return (dt.date.today() - dt.date.fromisoformat(iso)).days


# ---------- salary ----------

_CUR = [("INR", r"₹|\binr\b|\brs\.?|\blpa\b|\blakhs?\b|\blacs?\b|\bcrores?\b"),
        ("CAD", r"\bcad\b|c\$"), ("USD", r"\$|\busd\b"), ("EUR", r"€|\beur\b"), ("GBP", r"£|\bgbp\b")]
_NUM = re.compile(r"(\d+(?:[.,]\d+)*)\s*(k|l|lakhs?|lacs?|lpa|cr|crores?|m|mn)?\b", re.I)
_MULT = {"k": 1e3, "l": 1e5, "lakh": 1e5, "lakhs": 1e5, "lac": 1e5, "lacs": 1e5, "lpa": 1e5,
         "cr": 1e7, "crore": 1e7, "crores": 1e7, "m": 1e6, "mn": 1e6}
_PERIOD = [(r"\b(hour|hr|hourly)\b", 2080), (r"\b(day|daily)\b", 260), (r"\b(week|weekly)\b", 52),
           (r"\b(month|monthly|mo|pm|p\.m\.)\b", 12)]


def parse_salary(text: str | None, fx_to_local: dict, local: str = "INR"):
    """'₹12,00,000 - ₹18,00,000 a year' / '12-18 LPA' / '$257K – $335K' -> annual (min, max) in the local
    currency, or None. `fx_to_local` maps other currencies to the local one."""
    if not text or str(text).strip().lower() in ("n/a", "none", "not disclosed"):
        return None
    s = str(text).lower()
    cur = next((c for c, rx in _CUR if re.search(rx, s)), None)
    if not cur:
        return None
    lpa = bool(re.search(r"\blpa\b|\blakhs?\b|\blacs?\b", s))
    vals = []
    for num, suf in _NUM.findall(s):
        v = float(num.replace(",", ""))
        suf = (suf or "").lower()
        if suf:
            v *= _MULT[suf]
        elif lpa and v < 1000:
            v *= 1e5
        vals.append(v)
    mult = next((m for rx, m in _PERIOD if re.search(rx, s)), 1)
    vals = [v for v in vals if v >= (1 if mult > 1 else 100)]  # drop stray small numbers
    if not vals:
        return None
    rate = 1.0 if cur == local else float(fx_to_local.get(cur, 0) or 0)
    if not rate:
        return None
    lo, hi = min(vals[:2]), max(vals[:2])
    return round(lo * mult * rate), round(hi * mult * rate)


# ---------- html ----------

def html_to_text(raw: str | None, limit: int = 9000) -> str:
    if not raw:
        return ""
    t = html.unescape(raw)
    t = re.sub(r"(?i)<\s*(br|/p|/li|/h\d|/div|/tr)\s*/?>", "\n", t)
    t = re.sub(r"(?i)<li[^>]*>", "\n- ", t)
    t = re.sub(r"<[^>]+>", " ", t)
    t = html.unescape(t)
    t = re.sub(r"[ \t\xa0]+", " ", t)
    t = re.sub(r" ?\n ?", "\n", t)
    t = re.sub(r"\n\s*\n+", "\n\n", t).strip()
    return t[:limit]
