# Dependency and License Risk

Load when a change adds, removes, or re-constrains a dependency, or changes a
license, attribution, or third-party asset. Any newly introduced item
propagates legal obligations and resolution constraints to every downstream
consumer, so this is a mandatory check whenever the condition is met.

Language-agnostic by contract: specific manifest file names, package names,
registry names, and tool commands belong to the language pack and the reviewed
repository, not here.

## 1. New third-party dependency

| Check | Point |
| --- | --- |
| License type | Permissive licenses are generally acceptable; reciprocal licenses require confirming the usage and distribution mode; strong copyleft → Critical, escalate to an owner decision |
| Usage restrictions | Non-commercial, research-only, attribution-required, or custom terms; treat a restricted asset as unacceptable as a default |
| Transitive dependencies | A permissive direct dependency can still pull a restrictive one; inspect the whole dependency tree, not just the top-level entry |
| Placement | A dependency with license risk or a very large footprint belongs in an optional group, not the mandatory core set |
| Attribution | Copied third-party code needs its source and license identifier recorded; missing attribution → Minor |
| Exact-version pin | Requires a documented reason. Prefer no constraint, then a lower bound, then an upper bound, then an exact pin. "It works today" is not a reason |
| Core dependency constraints | Extra constraints on a dependency at the center of the dependency tree are architecture decisions and require an explicit explanation |
| Constraint conflicts | A new requirement incompatible with an existing pin is Critical |
| Unpin condition | Every constraint states why it exists and under what condition it can be relaxed; an unexplained bare lock needs a comment |
| Constraint scope | A version requirement affecting only one optional feature stays in that feature's dependency group, not the core set |
| Supply chain | Maintenance activity, pre-1.0 compatibility risk, and known vulnerabilities |
| Size and platform | Large packages must be optional; verify that platform, runtime, and language-version requirements are compatible with the project's support policy |

## 2. Third-party non-code assets

A bundled non-code artifact carries its own license; verify it and its
attribution requirements before accepting it. An artifact with no license
declaration is unauthorized — treat as high risk.

## 3. Removal and upgrades

- Removing a dependency must also remove its references, configuration, optional
  group entries, documentation, and CI steps. A removed declaration with
  surviving references is a defect.
- Upgrading a major version requires checking documented breaking changes
  against the call sites in the diff.
- Downgrading or relaxing a constraint needs a stated reason and a check that
  the reason for the original constraint no longer applies.

## 4. Severity and output

| Level | Situation |
| --- | --- |
| Critical | Strong copyleft, non-commercial restriction, missing license declaration, or a constraint conflict |
| Major | Weak reciprocal terms needing confirmation, or a new unexplained exact pin |
| Minor | Missing attribution or license identifier |

Report a four-column table — item → license → risk → recommendation — plus a
dependency-change table (constraint form, central-dependency impact, rationale
present, conflicts, recommendation). Never assert a license that could not be
verified; record "unverified" and treat it as a finding.
