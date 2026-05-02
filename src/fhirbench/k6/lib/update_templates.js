// Per-type update-template registry — picks a mutator function for each
// of the five resource types CRUD now exercises (Patient, Observation,
// Condition, Encounter, MedicationRequest).
//
// History: v1 of this file was Patient-only — Marat Surmashev pointed out
// our CRUD workload tested Observation creates + Patient updates and didn't
// surface per-resource-type latency deviation. v2 keeps the original 6
// Patient mutators verbatim and adds a handful per type so the U verb
// exercises a comparable mix of indexed-field edits vs meta-only edits
// across all five types. The per-mutator template id (`tid`) is preserved
// in the metric tag so a future report can slice p99 by mutator class
// (e.g. all `meta_tag` updates across types) if the deviation pattern
// turns out to be index-driven rather than type-driven.
//
// Per-type mutator count is deliberately uneven — Patient has 6 because
// existing baselines depend on that distribution; the new types have 3
// each, all targeting fields supported by the base FHIR R4 definitions
// without IG-specific validation that could fail on Aidbox.

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

// ----------------------------------------------------------------------
// Shared mutator: meta.tag is universally writeable on every resource
// type and isn't search-indexed by default — a "cheap edit" baseline that
// lets us compare per-type update latency on a workload servers should
// all handle identically.
// ----------------------------------------------------------------------
function metaTagBump(resource) {
  resource.meta = resource.meta || {};
  resource.meta.tag = resource.meta.tag || [];
  resource.meta.tag.push({
    system: 'urn:loadtest',
    code: `u-${Date.now()}-${Math.floor(Math.random() * 1_000_000)}`,
  });
  return resource;
}

// ----------------------------------------------------------------------
// Patient mutators (preserved verbatim from v1 — existing baselines
// across published runs depend on this exact distribution).
// ----------------------------------------------------------------------

function metaTag(patient) {
  // v1 implementation; identical effect to metaTagBump but kept for stable id.
  return metaTagBump(patient);
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

// ----------------------------------------------------------------------
// Observation mutators. Synthea writes both numeric (vital sign) and
// coded observations; the mutators below are safe for either shape.
// ----------------------------------------------------------------------

function obsValueJitter(obs) {
  // Only perturb if a numeric valueQuantity is present (vital signs +
  // labs); otherwise fall back to the meta_tag baseline so the mutator
  // never throws on a coded-only Observation.
  if (obs.valueQuantity && typeof obs.valueQuantity.value === 'number') {
    const v = obs.valueQuantity.value;
    // ±2% jitter, two decimal places — keeps the value in a clinically
    // plausible range so any value-driven validators don't reject.
    const delta = v * (Math.random() * 0.04 - 0.02);
    obs.valueQuantity.value = Math.round((v + delta) * 100) / 100;
    return obs;
  }
  return metaTagBump(obs);
}

function obsStatusFlip(obs) {
  // FHIR R4 ObservationStatus FSM allows preliminary/final/amended — flip
  // between final and amended (the realistic post-publish edit) without
  // tripping the entered-in-error trap state.
  obs.status = obs.status === 'amended' ? 'final' : 'amended';
  return obs;
}

// ----------------------------------------------------------------------
// Condition mutators.
// ----------------------------------------------------------------------

function condClinicalStatusFlip(cond) {
  // Toggle active/inactive — the two clinical-status values that matter
  // for problem-list rendering. Avoid 'resolved' since some servers
  // gate that with a state-machine check that would reject the flip.
  //
  // FHIR R4 invariant con-4: "If condition is abated, then clinicalStatus
  // must be either inactive, resolved, or remission." Synthea writes
  // abatementDateTime/abatementBoolean on Conditions that have ended, so
  // any Condition with an abatement* field MUST be in a non-active state.
  // The first version of this mutator ignored that — flipping an abated
  // Condition to "active" produced 400 OperationOutcome on Medplum (which
  // enforces con-4 strictly) and ~12% U-Condition err in the 1K shadow.
  // Now: if abated, lock the flip to (inactive ↔ remission); otherwise
  // toggle (active ↔ inactive) as before. Both branches still produce a
  // real edit (the mutator's purpose).
  const cur = (cond.clinicalStatus
    && cond.clinicalStatus.coding
    && cond.clinicalStatus.coding[0]
    && cond.clinicalStatus.coding[0].code) || 'active';
  const isAbated = cond.abatementDateTime != null
    || cond.abatementAge != null
    || cond.abatementPeriod != null
    || cond.abatementRange != null
    || cond.abatementString != null
    || cond.abatementBoolean === true;
  let next;
  if (isAbated) {
    next = cur === 'remission' ? 'inactive' : 'remission';
  } else {
    next = cur === 'active' ? 'inactive' : 'active';
  }
  cond.clinicalStatus = {
    coding: [{
      system: 'http://terminology.hl7.org/CodeSystem/condition-clinical',
      code: next,
    }],
  };
  return cond;
}

function condVerificationStatusSet(cond) {
  // Cycle through the verification-status vocabulary. Confirmed is the
  // most common clinical state, so weight it.
  const states = ['confirmed', 'confirmed', 'provisional', 'differential'];
  const next = states[Math.floor(Math.random() * states.length)];
  cond.verificationStatus = {
    coding: [{
      system: 'http://terminology.hl7.org/CodeSystem/condition-ver-status',
      code: next,
    }],
  };
  return cond;
}

// ----------------------------------------------------------------------
// Encounter mutators.
// ----------------------------------------------------------------------

function encStatusFlip(enc) {
  // Avoid 'planned' / 'cancelled' — those are state-machine transitions
  // some servers gate with extra validation. Flip between 'in-progress'
  // and 'finished' as a clinically realistic edit.
  enc.status = enc.status === 'finished' ? 'in-progress' : 'finished';
  return enc;
}

function encPeriodExtend(enc) {
  // Push period.end forward by 5 minutes — the "discharge time corrected"
  // edit. If period.end isn't set, set it to now.
  enc.period = enc.period || {};
  const cur = enc.period.end ? new Date(enc.period.end) : new Date();
  cur.setMinutes(cur.getMinutes() + 5);
  enc.period.end = cur.toISOString();
  return enc;
}

// ----------------------------------------------------------------------
// MedicationRequest mutators.
// ----------------------------------------------------------------------

function mrStatusFlip(mr) {
  // 'active' ↔ 'completed' — the most common transition (script filled).
  // Avoid 'cancelled' / 'stopped' which trigger e-prescribing workflows
  // on some servers.
  mr.status = mr.status === 'completed' ? 'active' : 'completed';
  return mr;
}

function mrIntentSet(mr) {
  // Intent is required and immutable in some clinical workflows but most
  // servers permit the field-level edit. Cycle through reasonable values.
  const intents = ['order', 'order', 'plan', 'instance-order'];
  mr.intent = intents[Math.floor(Math.random() * intents.length)];
  return mr;
}

// ----------------------------------------------------------------------
// Per-type registry. Keys: tid (template id, used in metric tags).
// ----------------------------------------------------------------------

export const TEMPLATES_BY_TYPE = {
  Patient: {
    meta_tag:      metaTag,
    meta_security: metaSecurity,
    name_given:    nameGiven,
    telecom_phone: telecomPhone,
    address_city:  addressCity,
    active_toggle: activeToggle,
  },
  Observation: {
    obs_meta_tag:    metaTagBump,
    obs_value_jitter: obsValueJitter,
    obs_status_flip:  obsStatusFlip,
  },
  Condition: {
    cond_meta_tag:                metaTagBump,
    cond_clinical_status_flip:    condClinicalStatusFlip,
    cond_verification_status_set: condVerificationStatusSet,
  },
  Encounter: {
    enc_meta_tag:        metaTagBump,
    enc_status_flip:     encStatusFlip,
    enc_period_extend:   encPeriodExtend,
  },
  MedicationRequest: {
    mr_meta_tag:    metaTagBump,
    mr_status_flip: mrStatusFlip,
    mr_intent_set:  mrIntentSet,
  },
};

// pickTemplate(resourceType) — returns { tid, fn } for the given type.
// Throws if the type isn't registered so a typo in crud.js fails loud
// rather than silently degrading to no-op updates.
export function pickTemplate(resourceType) {
  const group = TEMPLATES_BY_TYPE[resourceType];
  if (!group) {
    throw new Error(`pickTemplate: no mutators registered for ${resourceType}`);
  }
  const ids = Object.keys(group);
  const tid = ids[Math.floor(Math.random() * ids.length)];
  return { tid, fn: group[tid] };
}
