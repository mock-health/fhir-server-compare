"""Update templates for CRUD-U diversity.

Each template is a pure function `apply(patient, rng) -> patient'` that
mutates a copy of the resource in exactly one way. `do_update` picks one
uniformly at random per op, so p99 can be sliced per-template to surface
servers that reindex on specific field edits (name_given / address_city /
active_toggle) vs those that only dirty a version row for meta-only edits
(meta_tag / meta_security / telecom_phone).

The sample pools are baked in rather than sourced from Synthea — 50 given
names and 50 cities is enough to avoid any realistic server-side uniqueness
hot-set, and keeps this module free of I/O at import time.
"""
from __future__ import annotations

import random
import time
from typing import Callable

GIVEN_NAMES: list[str] = [
    "Avery", "Bailey", "Cameron", "Dakota", "Elliot", "Finley", "Gray",
    "Harper", "Indigo", "Jordan", "Kai", "Logan", "Morgan", "Nova", "Oakley",
    "Parker", "Quinn", "Reese", "Sage", "Tatum", "Umi", "Val", "Wren", "Xio",
    "Yuki", "Zion", "Ash", "Blair", "Casey", "Drew", "Emerson", "Frankie",
    "Gale", "Hollis", "Ira", "Jules", "Kit", "Lane", "Max", "Noel", "Onyx",
    "Pax", "Rio", "Sky", "Toby", "Uli", "Vesper", "West", "Yael", "Zen",
]

CITIES: list[str] = [
    "Boston", "Worcester", "Springfield", "Lowell", "Cambridge", "Brockton",
    "Quincy", "Lynn", "Newton", "Somerville", "Lawrence", "Fall River",
    "Haverhill", "Waltham", "Malden", "Brookline", "Plymouth", "Medford",
    "Taunton", "Chicopee", "Weymouth", "Revere", "Peabody", "Methuen",
    "Barnstable", "Pittsfield", "Attleboro", "Arlington", "Everett", "Salem",
    "Westfield", "Leominster", "Fitchburg", "Beverly", "Holyoke", "Marlborough",
    "Woburn", "Amherst", "Chelsea", "Braintree", "Natick", "Randolph",
    "Watertown", "Franklin", "Northampton", "Gloucester", "Milford", "Needham",
    "Melrose", "Dedham",
]


def _meta_tag(patient: dict, rng: random.Random) -> dict:
    """Append a monotonic-ish tag; bounded (no index impact)."""
    patient.setdefault("meta", {}).setdefault("tag", []).append({
        "system": "urn:loadtest",
        "code": f"u-{int(time.time() * 1000)}-{rng.randrange(1_000_000)}",
    })
    return patient


def _meta_security(patient: dict, rng: random.Random) -> dict:
    """Idempotent replace of meta.security (bounded size)."""
    level = rng.choice(["N", "L", "M", "R", "U"])
    patient.setdefault("meta", {})["security"] = [{
        "system": "http://terminology.hl7.org/CodeSystem/v3-Confidentiality",
        "code": level,
    }]
    return patient


def _name_given(patient: dict, rng: random.Random) -> dict:
    """Rotate name[0].given[0] — touches the `given` search index."""
    new_given = rng.choice(GIVEN_NAMES)
    names = patient.setdefault("name", [{}])
    if not names:
        names.append({})
    n0 = names[0]
    given = n0.get("given") or [""]
    given[0] = new_given
    n0["given"] = given
    return patient


def _telecom_phone(patient: dict, rng: random.Random) -> dict:
    """Set/overwrite the first phone telecom entry. No search-index churn."""
    area = rng.randrange(200, 1000)
    mid = rng.randrange(100, 1000)
    last = rng.randrange(0, 10_000)
    value = f"555-{area:03d}-{mid:03d}{last:04d}"
    telecom = patient.setdefault("telecom", [])
    for entry in telecom:
        if entry.get("system") == "phone":
            entry["value"] = value
            return patient
    telecom.append({"system": "phone", "value": value, "use": "home"})
    return patient


def _address_city(patient: dict, rng: random.Random) -> dict:
    """Rotate address[0].city — touches the `address-city` search index."""
    new_city = rng.choice(CITIES)
    addrs = patient.setdefault("address", [{}])
    if not addrs:
        addrs.append({})
    addrs[0]["city"] = new_city
    return patient


def _active_toggle(patient: dict, rng: random.Random) -> dict:
    """Flip `active` — touches the `active` search index."""
    patient["active"] = not bool(patient.get("active", True))
    return patient


TemplateFn = Callable[[dict, random.Random], dict]

TEMPLATES: dict[str, TemplateFn] = {
    "meta_tag": _meta_tag,
    "meta_security": _meta_security,
    "name_given": _name_given,
    "telecom_phone": _telecom_phone,
    "address_city": _address_city,
    "active_toggle": _active_toggle,
}

TEMPLATE_IDS: list[str] = list(TEMPLATES.keys())


def pick_template(rng: random.Random) -> tuple[str, TemplateFn]:
    tid = rng.choice(TEMPLATE_IDS)
    return tid, TEMPLATES[tid]
