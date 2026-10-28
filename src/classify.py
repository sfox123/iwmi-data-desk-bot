"""Classify a file into Category / Region / Dataset from metadata, filename and email context."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .config import Config, Region
from .emailparse import EmailContext
from .extract import FileMetadata
from .naming import ascii_clean, base_stem, clean_stem, dataset_key

# Evidence weights by where a word was found.
WEIGHTS = {"variable": 3.0, "attr": 2.0, "filename": 2.0, "subject": 1.5, "field": 1.0, "body": 1.0}
# Region confidence by the strongest evidence kind that named it.
REGION_CONF = {"variable": 1.0, "attr": 1.0, "filename": 1.0, "subject": 0.9, "field": 0.9, "body": 0.75,
               "bbox": 0.85, "global": 0.8}
UNKNOWN_SOURCE = "UnknownSource"
UNKNOWN_SOURCE_PENALTY = 0.5  # confidence multiplier; the file also always goes to needs-review
SHORT = 3  # keywords/aliases this short are not matched in email bodies ("et al", "pr", ISO codes)

YEAR_RE = re.compile(r"(?<!\d)((?:19[5-9]|20[0-4])\d)(?:(0[1-9]|1[0-2])([0-2]\d|3[01]))?(?!\d)")


# ---------------------------------------------------------------------------- tokens
def tokenize(text: str) -> list[str]:
    """Lower-case alphanumeric tokens; splits camelCase ('NileBasin' -> nile, basin)."""
    text = re.sub(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])|(?<=\d)(?=[A-Z][a-z])", " ", str(text))
    return re.findall(r"[a-z0-9]+", text.lower())


def token_bag(tokens: list[str]) -> set[str]:
    """Tokens + letter-only parts ('tmax2020' -> tmax) + joined bigrams ('land_cover')."""
    bag = set(tokens)
    for t in tokens:
        bag.update(p for p in re.findall(r"[a-z]+", t) if len(p) > 1)
    bag.update(f"{a}_{b}" for a, b in zip(tokens, tokens[1:]))
    return bag


def phrase_in(tokens: list[str], bag: set[str], phrase: str) -> bool:
    words = tokenize(phrase)
    if not words:
        return False
    if len(words) == 1:
        return words[0] in bag
    n = len(words)
    return any(tokens[i:i + n] == words for i in range(len(tokens) - n + 1))


@dataclass
class Evidence:
    kind: str
    text: str
    tokens: list[str] = field(init=False)
    bag: set[str] = field(init=False)

    def __post_init__(self):
        self.tokens = tokenize(self.text)
        self.bag = token_bag(self.tokens)

    @property
    def weight(self) -> float:
        return WEIGHTS[self.kind]


def build_evidence(meta: FileMetadata, filename: str, email: EmailContext | None) -> list[Evidence]:
    ev: list[Evidence] = []
    for v in meta.variables:
        ev.append(Evidence("variable", " ".join(filter(None, [v.name, v.standard_name]))))
        if v.long_name:
            ev.append(Evidence("attr", v.long_name))
    for key in ("title", "source", "institution"):
        if meta.attrs.get(key):
            ev.append(Evidence("attr", meta.attrs[key]))
    if meta.attrs.get("summary"):
        ev.append(Evidence("field", meta.attrs["summary"][:1000]))
    ev.append(Evidence("filename", base_stem(filename)))
    if meta.fields:
        ev.append(Evidence("field", " ".join(meta.fields[:200])))
    if email:
        if email.subject:
            ev.append(Evidence("subject", email.subject))
        if email.body:
            ev.append(Evidence("body", email.body))
    return [e for e in ev if e.tokens]


# ---------------------------------------------------------------------------- result
@dataclass
class Classification:
    status: str                     # filed | needs_review
    category: str | None
    region: str | None
    dataset: str | None
    source: str | None
    variable: str | None
    time_start: str | None
    time_end: str | None
    confidence: float
    reasons: list[str] = field(default_factory=list)
    scores: dict[str, float] = field(default_factory=dict)
    region_method: str | None = None


# ---------------------------------------------------------------------------- scoring
def _keyword_hits(keywords: list[str], ev: Evidence) -> list[str]:
    return [k for k in keywords
            if not (ev.kind == "body" and len(k) <= SHORT) and phrase_in(ev.tokens, ev.bag, k)]


def score_categories(evidence: list[Evidence], categories: dict[str, list[str]]) -> dict[str, float]:
    scores = {}
    for cat, keywords in categories.items():
        scores[cat] = sum(ev.weight * len(_keyword_hits(keywords, ev)) for ev in evidence)
    return scores


def match_source(evidence: list[Evidence], sources: dict[str, list[str]]) -> str | None:
    best, best_key = None, (0.0, 0)
    for name, aliases in sources.items():
        score, longest = 0.0, 0
        for ev in evidence:
            hits = [a for a in aliases + [name]
                    if not (ev.kind == "body" and len(a) <= SHORT) and phrase_in(ev.tokens, ev.bag, a)]
            if hits:
                score += ev.weight
                longest = max(longest, max(len(tokenize(h)) for h in hits))
        key = (score, longest)
        if score > 0 and key > best_key:
            best, best_key = name, key
    return best


def _is_code(alias: str) -> bool:
    return alias.isupper() and len(alias) <= SHORT


def match_region_by_name(evidence: list[Evidence], regions: list[Region]) -> tuple[str | None, str | None]:
    """Return (region, strongest evidence kind). Ties -> (None, None) so bbox can decide."""
    scores: dict[str, float] = {}
    kinds: dict[str, str] = {}
    for r in regions:
        for ev in evidence:
            names = [r.name] + r.aliases
            if any(phrase_in(ev.tokens, ev.bag, a) for a in names
                   if not (ev.kind == "body" and _is_code(a))):
                scores[r.name] = scores.get(r.name, 0) + ev.weight
                if REGION_CONF[ev.kind] > REGION_CONF.get(kinds.get(r.name, "body"), 0) or r.name not in kinds:
                    kinds[r.name] = ev.kind
    if not scores:
        return None, None
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
        return None, None
    return ranked[0][0], kinds[ranked[0][0]]


def _area(b) -> float:
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def match_region_by_bbox(bbox, regions: list[Region], min_overlap: float = 0.5) -> str | None:
    cx, cy = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
    data_area = _area(bbox)
    best, best_area = None, None
    for r in regions:
        if not r.bbox:
            continue
        w, s, e, n = r.bbox
        if not (w <= cx <= e and s <= cy <= n):
            continue
        inter = (max(w, bbox[0]), max(s, bbox[1]), min(e, bbox[2]), min(n, bbox[3]))
        overlap = _area(inter) / data_area if data_area > 0 else 1.0
        if overlap < min_overlap:
            continue
        area = _area(r.bbox)
        if best_area is None or area < best_area:
            best, best_area = r.name, area
    return best


def years_from_text(*texts: str) -> tuple[str | None, str | None]:
    found = []
    for t in texts:
        if t:
            for m in YEAR_RE.finditer(t):
                y, mo, d = m.group(1), m.group(2) or "01", m.group(3) or "01"
                found.append(f"{y}-{mo}-{d}")
    if not found:
        return None, None
    return min(found), max(found)


def _pick_variable(meta: FileMetadata, keywords: list[str], evidence: list[Evidence]) -> str | None:
    for v in meta.variables:
        ev = Evidence("variable", " ".join(filter(None, [v.name, v.standard_name, v.long_name])))
        if _keyword_hits(keywords, ev):
            return v.name
    if meta.kind == "netcdf" and meta.variables:
        return meta.variables[0].name
    for kind in ("filename", "attr", "subject", "field"):
        for ev in evidence:
            if ev.kind != kind:
                continue
            hits = _keyword_hits(keywords, ev)
            if hits:
                # keep the order in which keywords appear in the text
                return min(hits, key=lambda k: _position(ev.tokens, k))
    return None


def _position(tokens: list[str], keyword: str) -> int:
    first = tokenize(keyword)[0]
    for i, t in enumerate(tokens):
        if t == first or t.startswith(first):
            return i
    return len(tokens)


# ---------------------------------------------------------------------------- main
def classify(meta: FileMetadata, filename: str, email: EmailContext | None, cfg: Config) -> Classification:
    evidence = build_evidence(meta, filename, email)
    reasons: list[str] = []

    # Category
    scores = score_categories(evidence, cfg.categories)
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    top_cat, top = ranked[0] if ranked else (None, 0.0)
    second = ranked[1][1] if len(ranked) > 1 else 0.0
    total = sum(scores.values())
    category = top_cat if top > 0 else None
    if top == 0:
        reasons.append("no category keywords matched")
        cat_conf = 0.0
    elif top == second:
        tied = [c for c, s in ranked if s == top]
        reasons.append(f"category tie: {', '.join(tied)}")
        cat_conf = 0.0
    else:
        cat_conf = min(1.0, 0.5 + top / 6) * (top / total)

    # Region
    region, kind = match_region_by_name(evidence, cfg.regions)
    method = kind
    if region is None and meta.bbox:
        if meta.bbox[2] - meta.bbox[0] > cfg.global_lon_span:
            region, method = "Global", "global"
        else:
            region = match_region_by_bbox(meta.bbox, cfg.regions)
            method = "bbox" if region else None
    region_conf = REGION_CONF.get(method, 0.0) if region else 0.0
    if region is None:
        reasons.append("unknown region")

    # Source / dataset / variable / time
    # Sources are matched in file contents, filename and email subject/body alike, so a source
    # named only in the email still counts. Without one, the file goes to needs-review.
    source = match_source(evidence, cfg.sources)
    source_known = source is not None
    if not source_known:
        source = UNKNOWN_SOURCE
    dataset = "Documents" if meta.kind == "document" else source

    keywords = cfg.categories.get(category, []) if category else []
    variable = _pick_variable(meta, keywords, evidence) if category else None
    if not variable:
        variable = clean_stem(filename)

    # Dataset key = source + primary variable, so different variables from one source get
    # their own folder and versions only count re-uploads of the same variable.
    if meta.kind != "document":
        dataset = dataset_key(dataset, variable)

    time_start, time_end = meta.time_start, meta.time_end
    if not time_start and not time_end:
        time_start, time_end = years_from_text(base_stem(filename), email.subject if email else "")

    confidence = cat_conf * region_conf
    if not source_known:
        confidence *= UNKNOWN_SOURCE_PENALTY
        reasons.append("no recognised source (file, filename or email)")
    confidence = round(confidence, 2)
    if not reasons and confidence < cfg.min_confidence:
        reasons.append(f"low confidence ({confidence:.2f} < {cfg.min_confidence})")
    if meta.error:
        reasons.append(f"could not read file contents ({meta.error.split(':')[0]})")

    status = "filed" if source_known and category and region and region != "Unknown" \
        and confidence >= cfg.min_confidence and cat_conf > 0 else "needs_review"
    return Classification(
        status=status, category=category, region=region, dataset=dataset, source=source,
        variable=variable, time_start=time_start, time_end=time_end, confidence=confidence,
        reasons=reasons if status == "needs_review" else [r for r in reasons if r.startswith("could not")],
        scores={k: round(v, 2) for k, v in scores.items() if v}, region_method=method,
    )
