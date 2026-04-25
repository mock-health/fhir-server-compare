// Port of loadtest/update_templates.py — six pure functions that each
// mutate a copy of a Patient resource in exactly one way. `do_update` in
// crud.js picks one uniformly at random per op so p99 can be sliced
// per-template.
//
// Three templates touch search indexes (name_given / address_city /
// active_toggle); three don't (meta_tag / meta_security / telecom_phone).
// The split surfaces servers that reindex on field edits vs those that
// only dirty a version row for meta-only edits.

export const GIVEN_NAMES = [
  'Avery', 'Bailey', 'Cameron', 'Dakota', 'Elliot', 'Finley', 'Gray',
  'Harper', 'Indigo', 'Jordan', 'Kai', 'Logan', 'Morgan', 'Nova', 'Oakley',
  'Parker', 'Quinn', 'Reese', 'Sage', 'Tatum', 'Umi', 'Val', 'Wren', 'Xio',
  'Yuki', 'Zion', 'Ash', 'Blair', 'Casey', 'Drew', 'Emerson', 'Frankie',
  'Gale', 'Hollis', 'Ira', 'Jules', 'Kit', 'Lane', 'Max', 'Noel', 'Onyx',
  'Pax', 'Rio', 'Sky', 'Toby', 'Uli', 'Vesper', 'West', 'Yael', 'Zen',
];

export const CITIES = [
  'Boston', 'Worcester', 'Springfield', 'Lowell', 'Cambridge', 'Brockton',
  'Quincy', 'Lynn', 'Newton', 'Somerville', 'Lawrence', 'Fall River',
  'Haverhill', 'Waltham', 'Malden', 'Brookline', 'Plymouth', 'Medford',
  'Taunton', 'Chicopee', 'Weymouth', 'Revere', 'Peabody', 'Methuen',
  'Barnstable', 'Pittsfield', 'Attleboro', 'Arlington', 'Everett', 'Salem',
  'Westfield', 'Leominster', 'Fitchburg', 'Beverly', 'Holyoke', 'Marlborough',
  'Woburn', 'Amherst', 'Chelsea', 'Braintree', 'Natick', 'Randolph',
  'Watertown', 'Franklin', 'Northampton', 'Gloucester', 'Milford', 'Needham',
  'Melrose', 'Dedham',
];

function metaTag(patient) {
  patient.meta = patient.meta || {};
  patient.meta.tag = patient.meta.tag || [];
  patient.meta.tag.push({
    system: 'urn:loadtest',
    code: `u-${Date.now()}-${Math.floor(Math.random() * 1_000_000)}`,
  });
  return patient;
}

function metaSecurity(patient) {
  const levels = ['N', 'L', 'M', 'R', 'U'];
  const level = levels[Math.floor(Math.random() * levels.length)];
  patient.meta = patient.meta || {};
  patient.meta.security = [{
    system: 'http://terminology.hl7.org/CodeSystem/v3-Confidentiality',
    code: level,
  }];
  return patient;
}

function nameGiven(patient) {
  const newGiven = GIVEN_NAMES[Math.floor(Math.random() * GIVEN_NAMES.length)];
  patient.name = patient.name || [{}];
  if (!patient.name.length) patient.name.push({});
  const n0 = patient.name[0];
  const given = n0.given || [''];
  given[0] = newGiven;
  n0.given = given;
  return patient;
}

function telecomPhone(patient) {
  const area = 200 + Math.floor(Math.random() * 800);
  const mid = 100 + Math.floor(Math.random() * 900);
  const last = Math.floor(Math.random() * 10_000);
  const value = `555-${String(area).padStart(3, '0')}-${String(mid).padStart(3, '0')}${String(last).padStart(4, '0')}`;
  patient.telecom = patient.telecom || [];
  for (const entry of patient.telecom) {
    if (entry.system === 'phone') {
      entry.value = value;
      return patient;
    }
  }
  patient.telecom.push({ system: 'phone', value, use: 'home' });
  return patient;
}

function addressCity(patient) {
  const newCity = CITIES[Math.floor(Math.random() * CITIES.length)];
  patient.address = patient.address || [{}];
  if (!patient.address.length) patient.address.push({});
  patient.address[0].city = newCity;
  return patient;
}

function activeToggle(patient) {
  patient.active = !(patient.active ?? true);
  return patient;
}

export const TEMPLATES = {
  meta_tag: metaTag,
  meta_security: metaSecurity,
  name_given: nameGiven,
  telecom_phone: telecomPhone,
  address_city: addressCity,
  active_toggle: activeToggle,
};

export const TEMPLATE_IDS = Object.keys(TEMPLATES);

export function pickTemplate() {
  const tid = TEMPLATE_IDS[Math.floor(Math.random() * TEMPLATE_IDS.length)];
  return { tid, fn: TEMPLATES[tid] };
}
