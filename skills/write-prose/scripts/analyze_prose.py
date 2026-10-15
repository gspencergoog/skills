#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "spacy>=3.7,<4.0",
#   "wordfreq>=3.0,<4.0",
# ]
# ///
"""Statistical readability, anti-AI-ism, and cognitive complexity analyzer for prose."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import statistics
import sys
from dataclasses import dataclass, field
from typing import Any

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

import prose_lexicon

BANNED_WORDS = [
    "delve",
    "leverage",
    "foster",
    "cultivate",
    "maximize",
    "democratize",
    "resonate",
    "encompass",
    "bridge",
    "underscore",
    "utilize",
    "seamless",
    "seamlessly",
    "extensively",
    "robust",
    "pivotal",
    "crucial",
    "vibrant",
    "intricate",
    "nuanced",
    "unwavering",
    "indelible",
    "uncharted",
    "transformative",
    "breathtaking",
    "nestled",
    "dynamic",
    "comprehensive",
    "intuitive",
    "holistic",
    "frictionless",
    "scalable",
    "synergistic",
    "tapestry",
    "landscape",
    "realm",
    "testament",
    "interplay",
    "synergy",
    "cornerstone",
    "boasts",
    "premier",
    "spearhead",
    "multifaceted",
    "overarching",
    "streamline",
    "empower",
    "unleash",
    "harness",
]

BANNED_PHRASES = [
    ("serves as", r"\bserves as\b"),
    ("stands as", r"\bstands as\b"),
    ("acts as", r"\bacts as\b"),
    ("testament to", r"\btestament to\b"),
    ("indelible mark", r"\bindelible mark\b"),
    ("pivotal moment", r"\bpivotal moment\b"),
    ("shaping the landscape", r"\bshaping the landscape\b"),
    ("in order to", r"\bin order to\b"),
    ("at this point in time", r"\bat this point in time\b"),
    ("rapidly evolving", r"\brapidly evolving\b"),
    ("state-of-the-art", r"\bstate-of-the-art\b"),
    ("not only ... but also", r"\bnot only\b[^.?!]+\bbut also\b"),
    (
        "not just about ... it is about",
        r"\bnot just about\b[^.?!]+\bit(?:'s| is) about\b",
    ),
    (
        "dangling -ing participle commentary",
        r",\s+(?:highlighting|emphasizing|underscoring|showcasing|demonstrating|fostering|symbolizing)\b",
    ),
]

INSTALL_HINT_MESSAGE = (
    "Optional NLP packages not found; running stdlib lexicon and syntactic "
    "heuristics. For 50k+ word Zipf frequency lookup and Universal "
    "Dependencies parse trees, install with: uv pip install wordfreq spacy "
    "&& python3 -m spacy download en_core_web_sm"
)

_WORD_RE = re.compile(r"\b[a-zA-Z0-9_'-]+\b")
_ALPHA_WORD_RE = re.compile(r"\b[a-zA-Z][a-zA-Z'-]*\b")
_BULLET_RE = re.compile(r"^\s*[-*+]\s+(.*)")
_NUMBERED_LIST_RE = re.compile(r"^\s*\d+\.\s+(.*)")
_INLINE_HEADER_RE = re.compile(r"^\*\*[^*]+\*\*(\s*[:\u2014\u2013-]|\s+)")
_EM_DASH_RE = re.compile(r"\u2014|(?<!-)(?:(?<=\S)--|--(?=\s|$))(?!-)")
_SMART_QUOTE_RE = re.compile(r"[\u2018\u2019\u201c\u201d]")
_HORIZONTAL_RULE_RE = re.compile(r"^[-_*]{3,}$")
_YAML_KEY_RE = re.compile(r"^[a-zA-Z0-9_-]+\s*:")

_NEGATION_RE = re.compile(
    r"\b(?:not|never|no|neither|nor|unless|without|except|cannot|"
    r"can't|won't|don't|doesn't|didn't|fails?\s+to|failed\s+to|omits?|omitted)\b",
    re.IGNORECASE,
)

_SUBORDINATOR_RE = re.compile(
    r"\b(?:if|unless|although|though|even\s+though|while|whereas|because|"
    r"since|until|provided\s+that|assuming\s+that|so\s+that|inasmuch\s+as|"
    r"whereby|wherein|which|who|whom|whose)\b",
    re.IGNORECASE,
)

_INTRO_OPENER_RE = re.compile(
    r"^\s*(?:if|when|whenever|while|although|though|because|since|unless|"
    r"until|before|after|once|whereas|provided|assuming|given|in|on|during|"
    r"for|under|without|with|by|to|upon|despite|notwithstanding)\b",
    re.IGNORECASE,
)

_PASSIVE_RE = re.compile(
    r"\b(?:am|is|are|was|were|be|been|being|gets|got|gotten)\s+"
    r"(?:not\s+|already\s+|also\s+|still\s+|often\s+|always\s+|never\s+)?"
    r"([a-z]+ed|built|caught|chosen|done|drawn|driven|eaten|fallen|fed|felt|"
    r"found|forbidden|forgotten|frozen|given|gone|grown|heard|held|hidden|"
    r"hit|kept|known|laid|led|left|lost|made|meant|met|paid|put|read|ridden|"
    r"risen|run|said|seen|sent|set|shaken|shown|shut|spoken|spent|spread|"
    r"stood|stolen|struck|sung|sunk|swept|sworn|taken|taught|torn|told|"
    r"thought|thrown|understood|woken|worn|won|written)\b(\s+by\b)?",
    re.IGNORECASE,
)

_STATIVE_PARTICIPLES = frozenset(
    {
        "allowed",
        "based",
        "complicated",
        "concerned",
        "configured",
        "connected",
        "dedicated",
        "detailed",
        "disabled",
        "distributed",
        "enabled",
        "expected",
        "focused",
        "formatted",
        "interested",
        "isolated",
        "limited",
        "located",
        "needed",
        "organized",
        "oriented",
        "pleased",
        "prepared",
        "qualified",
        "related",
        "required",
        "satisfied",
        "scheduled",
        "specialized",
        "structured",
        "supposed",
        "tired",
        "unhandled",
        "unsupported",
        "unused",
        "used",
        "worried",
    }
)

_FINITE_VERBS_SET = frozenset(
    {
        "accept",
        "accepts",
        "ACCEPTED",
        "add",
        "adds",
        "allow",
        "allows",
        "am",
        "apply",
        "applies",
        "are",
        "aren't",
        "be",
        "become",
        "becomes",
        "begin",
        "begins",
        "block",
        "blocks",
        "build",
        "builds",
        "call",
        "calls",
        "can",
        "cancel",
        "cancels",
        "Cannot",
        "cannot",
        "catch",
        "catches",
        "cause",
        "causes",
        "change",
        "changes",
        "check",
        "checks",
        "clear",
        "clears",
        "close",
        "closes",
        "collect",
        "collects",
        "combine",
        "combines",
        "come",
        "comes",
        "compare",
        "compares",
        "complete",
        "completes",
        "compute",
        "computes",
        "confirm",
        "confirms",
        "connect",
        "connects",
        "contain",
        "contains",
        "continue",
        "continues",
        "convert",
        "converts",
        "copy",
        "copies",
        "could",
        "count",
        "counts",
        "create",
        "creates",
        "define",
        "defines",
        "delete",
        "deletes",
        "depend",
        "depends",
        "describe",
        "describes",
        "detect",
        "detects",
        "determine",
        "determines",
        "did",
        "didn't",
        "disable",
        "disables",
        "display",
        "displays",
        "do",
        "does",
        "doesn't",
        "don't",
        "drop",
        "drops",
        "emit",
        "emits",
        "enable",
        "enables",
        "enforce",
        "enforces",
        "ensure",
        "ensures",
        "evaluate",
        "evaluates",
        "exceed",
        "exceeds",
        "execute",
        "executes",
        "exist",
        "exists",
        "expect",
        "expects",
        "explain",
        "explains",
        "expose",
        "exposes",
        "extract",
        "extracts",
        "fail",
        "failed",
        "fails",
        "fetch",
        "fetches",
        "filter",
        "filters",
        "find",
        "finds",
        "finish",
        "finishes",
        "fix",
        "fixes",
        "flag",
        "flags",
        "follow",
        "follows",
        "forbid",
        "forbids",
        "force",
        "forces",
        "format",
        "formats",
        "generate",
        "generates",
        "get",
        "gets",
        "give",
        "gives",
        "go",
        "goes",
        "govern",
        "governs",
        "had",
        "handle",
        "handles",
        "happen",
        "happens",
        "has",
        "have",
        "hold",
        "holds",
        "ignore",
        "ignores",
        "implement",
        "implements",
        "imply",
        "implies",
        "include",
        "includes",
        "increase",
        "increases",
        "indicate",
        "indicates",
        "initialize",
        "initializes",
        "insert",
        "inserts",
        "inspect",
        "inspects",
        "install",
        "installs",
        "introduce",
        "introduces",
        "invoke",
        "invokes",
        "is",
        "isn't",
        "issue",
        "issues",
        "keep",
        "keeps",
        "kill",
        "kills",
        "know",
        "knows",
        "launch",
        "launches",
        "lead",
        "leads",
        "leave",
        "leaves",
        "let",
        "lets",
        "limit",
        "limits",
        "link",
        "links",
        "list",
        "lists",
        "load",
        "loads",
        "lock",
        "locks",
        "log",
        "logs",
        "look",
        "looks",
        "maintain",
        "maintains",
        "make",
        "makes",
        "manage",
        "manages",
        "map",
        "maps",
        "mark",
        "marks",
        "match",
        "matches",
        "may",
        "mean",
        "means",
        "measure",
        "measures",
        "meet",
        "meets",
        "merge",
        "merges",
        "might",
        "modify",
        "modifies",
        "monitor",
        "monitors",
        "move",
        "moves",
        "must",
        "need",
        "needs",
        "notify",
        "notifies",
        "occur",
        "occurs",
        "offer",
        "offers",
        "omit",
        "omits",
        "open",
        "opens",
        "operate",
        "operates",
        "parse",
        "parses",
        "pass",
        "passes",
        "perform",
        "performs",
        "permit",
        "permits",
        "persist",
        "persists",
        "place",
        "places",
        "populate",
        "populates",
        "prefer",
        "prefers",
        "preserve",
        "preserves",
        "prevent",
        "prevents",
        "print",
        "prints",
        "process",
        "processes",
        "produce",
        "produces",
        "protect",
        "protects",
        "provide",
        "provides",
        "publish",
        "publishes",
        "pull",
        "pulls",
        "push",
        "pushes",
        "put",
        "puts",
        "query",
        "queries",
        "raise",
        "raises",
        "reach",
        "reaches",
        "read",
        "reads",
        "receive",
        "receives",
        "Keeps",
        "record",
        "records",
        "reduce",
        "reduces",
        "refer",
        "refers",
        "reflect",
        "reflects",
        "reject",
        "rejects",
        "rely",
        "relies",
        "remain",
        "remains",
        "remove",
        "removes",
        "render",
        "renders",
        "replace",
        "replaces",
        "reply",
        "replies",
        "report",
        "reports",
        "represent",
        "represents",
        "require",
        "requires",
        "reset",
        "resets",
        "resolve",
        "resolves",
        "respond",
        "responds",
        "restore",
        "restores",
        "restrict",
        "restricts",
        "retain",
        "retains",
        "retrieve",
        "retrieves",
        "retry",
        "retries",
        "return",
        "returns",
        "reuse",
        "reuses",
        "reveal",
        "reveals",
        "revert",
        "reverts",
        "review",
        "reviews",
        "rewrite",
        "rewrites",
        "run",
        "runs",
        "save",
        "saves",
        "scan",
        "scans",
        "schedule",
        "schedules",
        "score",
        "scores",
        "search",
        "searches",
        "select",
        "selects",
        "send",
        "sends",
        "separate",
        "separates",
        "serve",
        "serves",
        "set",
        "sets",
        "shall",
        "share",
        "shares",
        "ship",
        "ships",
        "should",
        "shouldn't",
        "show",
        "shows",
        "simplify",
        "simplifies",
        "skip",
        "skips",
        "solve",
        "solves",
        "sort",
        "sorts",
        "spawn",
        "spawns",
        "specify",
        "specifies",
        "split",
        "splits",
        "start",
        "starts",
        "state",
        "states",
        "stay",
        "stays",
        "stop",
        "stops",
        "store",
        "stores",
        "strip",
        "strips",
        "submit",
        "submits",
        "succeed",
        "succeeds",
        "suggest",
        "suggests",
        "support",
        "supports",
        "switch",
        "switches",
        "sync",
        "syncs",
        "synthesize",
        "synthesizes",
        "take",
        "takes",
        "terminate",
        "terminates",
        "test",
        "tests",
        "throw",
        "throws",
        "time",
        "times",
        "track",
        "tracks",
        "transfer",
        "transfers",
        "transform",
        "transforms",
        "translate",
        "translates",
        "treat",
        "treats",
        "trigger",
        "triggers",
        "trim",
        "trims",
        "try",
        "tries",
        "turn",
        "turns",
        "unify",
        "unifies",
        "update",
        "updates",
        "upgrade",
        "upgrades",
        "upload",
        "uploads",
        "use",
        "uses",
        "validate",
        "validates",
        "verify",
        "verifies",
        "violate",
        "violates",
        "wait",
        "waits",
        "walk",
        "walks",
        "want",
        "wants",
        "warn",
        "warns",
        "was",
        "wasn't",
        "watch",
        "watches",
        "were",
        "weren't",
        "will",
        "won't",
        "work",
        "works",
        "would",
        "wouldn't",
        "wrap",
        "wraps",
        "write",
        "writes",
        "yield",
        "yields",
    }
)

_NON_NOUN_CLUSTER_WORDS = _FINITE_VERBS_SET | frozenset(
    {
        "a",
        "about",
        "above",
        "across",
        "after",
        "against",
        "all",
        "almost",
        "alone",
        "along",
        "already",
        "also",
        "although",
        "always",
        "among",
        "an",
        "and",
        "another",
        "any",
        "anyone",
        "anything",
        "anywhere",
        "around",
        "as",
        "at",
        "away",
        "back",
        "bad",
        "because",
        "been",
        "before",
        "behind",
        "being",
        "below",
        "beneath",
        "beside",
        "besides",
        "best",
        "better",
        "between",
        "beyond",
        "big",
        "both",
        "broad",
        "broken",
        "but",
        "by",
        "certain",
        "clean",
        "clear",
        "close",
        "cold",
        "common",
        "complete",
        "complex",
        "concrete",
        "consistent",
        "correct",
        "current",
        "custom",
        "dark",
        "dead",
        "deep",
        "dense",
        "dependent",
        "different",
        "difficult",
        "direct",
        "distinct",
        "done",
        "double",
        "down",
        "dry",
        "dual",
        "due",
        "during",
        "each",
        "early",
        "easier",
        "easy",
        "effective",
        "efficient",
        "eight",
        "either",
        "empty",
        "enough",
        "entire",
        "equal",
        "equivalent",
        "essential",
        "even",
        "every",
        "everyday",
        "everyone",
        "everything",
        "exact",
        "except",
        "existing",
        "explicit",
        "external",
        "extra",
        "false",
        "familiar",
        "far",
        "fast",
        "faster",
        "fatal",
        "few",
        "fewer",
        "final",
        "fine",
        "finite",
        "firm",
        "first",
        "five",
        "fixed",
        "flat",
        "following",
        "for",
        "foreign",
        "formal",
        "former",
        "four",
        "fourth",
        "fragile",
        "free",
        "frequent",
        "fresh",
        "friendly",
        "from",
        "full",
        "further",
        "future",
        "general",
        "genuine",
        "given",
        "global",
        "good",
        "great",
        "greater",
        "green",
        "half",
        "hard",
        "harder",
        "healthy",
        "heavy",
        "helpful",
        "her",
        "here",
        "hers",
        "herself",
        "high",
        "higher",
        "highest",
        "him",
        "himself",
        "his",
        "hot",
        "how",
        "however",
        "huge",
        "human",
        "hundred",
        "i",
        "identical",
        "if",
        "immediate",
        "important",
        "impossible",
        "in",
        "inactive",
        "incomplete",
        "incorrect",
        "independent",
        "indirect",
        "individual",
        "informal",
        "initial",
        "inner",
        "inside",
        "instead",
        "insufficient",
        "intact",
        "interactive",
        "interesting",
        "intermediate",
        "internal",
        "international",
        "into",
        "invalid",
        "irrelevant",
        "isolated",
        "it",
        "its",
        "itself",
        "joint",
        "just",
        "known",
        "large",
        "larger",
        "largest",
        "last",
        "late",
        "later",
        "latest",
        "latter",
        "leading",
        "least",
        "left",
        "legal",
        "less",
        "light",
        "lighter",
        "like",
        "likely",
        "limited",
        "linear",
        "little",
        "live",
        "local",
        "logical",
        "long",
        "longer",
        "longest",
        "loose",
        "lost",
        "low",
        "lower",
        "lowest",
        "main",
        "major",
        "mandatory",
        "many",
        "manual",
        "maximum",
        "me",
        "Minimal",
        "minimal",
        "minimum",
        "minor",
        "missing",
        "mixed",
        "modern",
        "more",
        "most",
        "much",
        "multiple",
        "mutual",
        "my",
        "myself",
        "narrow",
        "native",
        "natural",
        "near",
        "necessary",
        "negative",
        "neither",
        "never",
        "new",
        "next",
        "nice",
        "nine",
        "no",
        "nobody",
        "none",
        "nor",
        "normal",
        "not",
        "nothing",
        "novel",
        "now",
        "nowhere",
        "null",
        "numerous",
        "obvious",
        "odd",
        "of",
        "off",
        "often",
        "ok",
        "okay",
        "old",
        "older",
        "on",
        "once",
        "one",
        "ongoing",
        "online",
        "only",
        "onto",
        "opaque",
        "open",
        "optional",
        "or",
        "ordinary",
        "original",
        "other",
        "others",
        "otherwise",
        "ought",
        "our",
        "ours",
        "ourselves",
        "out",
        "outer",
        "outside",
        "over",
        "overall",
        "own",
        "parallel",
        "partial",
        "particular",
        "past",
        "pending",
        "per",
        "perfect",
        "perhaps",
        "permanent",
        "permissive",
        "personal",
        "physical",
        "plain",
        "plainer",
        "plainest",
        "plus",
        "poor",
        "popular",
        "portable",
        "positive",
        "possible",
        "potential",
        "powerful",
        "practical",
        "precise",
        "preferable",
        "previous",
        "primary",
        "principal",
        "prior",
        "private",
        "probable",
        "proper",
        "public",
        "pure",
        "quick",
        "quiet",
        "quite",
        "random",
        "rapid",
        "rare",
        "rather",
        "raw",
        "ready",
        "real",
        "reasonable",
        "recent",
        "red",
        "redundant",
        "regular",
        "relative",
        "relevant",
        "reliable",
        "remote",
        "repeated",
        "respective",
        "responsible",
        "rich",
        "right",
        "rigorous",
        "risky",
        "rough",
        "round",
        "routine",
        "safe",
        "safer",
        "same",
        "second",
        "secondary",
        "separate",
        "sequential",
        "serial",
        "serious",
        "seven",
        "several",
        "severe",
        "shared",
        "sharp",
        "she",
        "short",
        "shorter",
        "shortest",
        "silent",
        "similar",
        "simple",
        "simpler",
        "simplest",
        "since",
        "single",
        "singular",
        "six",
        "slow",
        "slower",
        "small",
        "smaller",
        "smallest",
        "smart",
        "smooth",
        "so",
        "social",
        "soft",
        "some",
        "somebody",
        "someone",
        "something",
        "sometimes",
        "somewhat",
        "somewhere",
        "soon",
        "sound",
        "special",
        "specific",
        "stable",
        "stale",
        "standalone",
        "standard",
        "static",
        "steep",
        "still",
        "straight",
        "straightforward",
        "strange",
        "strict",
        "strong",
        "stronger",
        "strongest",
        "subtle",
        "such",
        "sudden",
        "sufficient",
        "suitable",
        "sure",
        "Symmetric",
        "symmetric",
        "synchronous",
        "tall",
        "technical",
        "temporary",
        "ten",
        "than",
        "that",
        "the",
        "their",
        "theirs",
        "them",
        "themselves",
        "then",
        "there",
        "therefore",
        "these",
        "they",
        "thick",
        "thin",
        "third",
        "thirty",
        "this",
        "thorough",
        "those",
        "though",
        "three",
        "through",
        "throughout",
        "thus",
        "tight",
        "tiny",
        "to",
        "today",
        "together",
        "tomorrow",
        "too",
        "top",
        "total",
        "tough",
        "toward",
        "towards",
        "traditional",
        "true",
        "twelve",
        "twenty",
        "twice",
        "two",
        "typical",
        "ultimate",
        "unable",
        "unambiguous",
        "uncertain",
        "unchanged",
        "unclear",
        "uncommon",
        "under",
        "underlying",
        "unexpected",
        "unfamiliar",
        "unfinished",
        "uniform",
        "unique",
        "universal",
        "unknown",
        "unless",
        "unlike",
        "unlikely",
        "unlimited",
        "unnecessary",
        "until",
        "unusual",
        "up",
        "upon",
        "upper",
        "usable",
        "useful",
        "usual",
        "vague",
        "valid",
        "valuable",
        "various",
        "vast",
        "very",
        "via",
        "virtual",
        "visible",
        "visual",
        "vital",
        "void",
        "volatile",
        "warm",
        "we",
        "weak",
        "well",
        "were",
        "what",
        "whatever",
        "when",
        "whenever",
        "where",
        "whereas",
        "wherever",
        "whether",
        "which",
        "while",
        "white",
        "who",
        "whole",
        "whom",
        "whose",
        "why",
        "wide",
        "wider",
        "willing",
        "with",
        "within",
        "without",
        "worse",
        "worst",
        "worth",
        "wrong",
        "yellow",
        "yes",
        "yesterday",
        "yet",
        "you",
        "young",
        "your",
        "yours",
        "yourself",
        "zero",
    }
)

_ADJECTIVE_OR_ADVERB_SUFFIXES = (
    "ly",
    "ous",
    "ive",
    "able",
    "ible",
    "ful",
    "less",
    "ic",
    "ish",
    "ary",
    "ory",
    "ent",
    "ant",
    "ed",
    "ing",
)

_EXCEPT_NOUNS_WITH_ADJ_SUFFIX = frozenset(
    {
        "agent",
        "argument",
        "arguments",
        "arrangement",
        "assignment",
        "attachment",
        "binary",
        "building",
        "client",
        "clients",
        "comment",
        "comments",
        "component",
        "components",
        "content",
        "contents",
        "current",
        "department",
        "deployment",
        "deployments",
        "development",
        "directory",
        "directories",
        "document",
        "documents",
        "element",
        "elements",
        "encoding",
        "environment",
        "environments",
        "equipment",
        "event",
        "events",
        "experiment",
        "experiments",
        "factory",
        "factories",
        "fragment",
        "fragments",
        "gradient",
        "handling",
        "heading",
        "headings",
        "history",
        "histories",
        "implement",
        "incident",
        "incidents",
        "increment",
        "increments",
        "ingredient",
        "instrument",
        "intent",
        "interface",
        "inventory",
        "investment",
        "judgment",
        "library",
        "libraries",
        "listing",
        "listings",
        "logging",
        "management",
        "mapping",
        "mappings",
        "measurement",
        "measurements",
        "memory",
        "memories",
        "metric",
        "metrics",
        "moment",
        "moments",
        "movement",
        "movements",
        "nesting",
        "ordering",
        "padding",
        "parent",
        "parents",
        "Patient",
        "payment",
        "payments",
        "percent",
        "placement",
        "polling",
        "president",
        "processing",
        "recipient",
        "recipients",
        "recording",
        "refinement",
        "refinements",
        "registry",
        "registries",
        "replacement",
        "replacements",
        "repository",
        "repositories",
        "requirement",
        "requirements",
        "resident",
        "routing",
        "scheduling",
        "segment",
        "segments",
        "setting",
        "settings",
        "sharding",
        "sibling",
        "siblings",
        "spacing",
        "statement",
        "statements",
        "string",
        "strings",
        "student",
        "students",
        "summary",
        "summaries",
        "supplement",
        "supplements",
        "talent",
        "tenant",
        "tenants",
        "testing",
        "timing",
        "token",
        "tooling",
        "tracing",
        "training",
        "treatment",
        "warning",
        "warnings",
        "weighting",
        "wiring",
        "wording",
        "writing",
    }
)


@dataclass
class RuntimeCapabilities:
    """Describes which NLP libraries were discovered at runtime."""

    engine_used: str
    wordfreq_available: bool
    spacy_available: bool
    spacy_model: str | None
    missing_packages: list[str] = field(default_factory=list)
    install_hint: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "engine_used": self.engine_used,
            "wordfreq_available": self.wordfreq_available,
            "spacy_available": self.spacy_available,
            "spacy_model": self.spacy_model,
            "missing_packages": list(self.missing_packages),
            "install_hint": self.install_hint,
        }


@dataclass
class SentenceComplexityResult:
    """Per-sentence Prose Cognitive Complexity (PCC) score and breakdown."""

    index: int
    line: int
    word_count: int
    cognitive_complexity: int
    classification: str
    breakdown: list[str] = field(default_factory=list)
    text: str = ""

    def to_dict(self) -> dict[str, Any]:
        snippet = self.text[:140] + ("..." if len(self.text) > 140 else "")
        return {
            "index": self.index,
            "line": self.line,
            "word_count": self.word_count,
            "cognitive_complexity": self.cognitive_complexity,
            "classification": self.classification,
            "breakdown": list(self.breakdown),
            "text": snippet,
        }


def _try_import_optional(module_name: str) -> Any:
    """Attempts to import `module_name`, returning `None` if unavailable."""
    try:
        return importlib.import_module(module_name)
    except (ImportError, OSError, RuntimeError, ValueError):
        return None


def _load_spacy_model(
    spacy_mod: Any, model_name: str = "en_core_web_sm"
) -> tuple[Any, str | None]:
    """Loads `model_name` from `spacy_mod` when present."""
    if spacy_mod is None:
        return None, None
    try:
        nlp = spacy_mod.load(model_name, disable=["ner"])
        return nlp, model_name
    except (ImportError, OSError, RuntimeError, ValueError, AttributeError):
        return None, None


def _probe_nlp_backends(
    engine: str = "auto",
) -> tuple[Any, Any, RuntimeCapabilities]:
    """Probes optional `wordfreq` and `spacy` backends with graceful fallback."""
    if engine == "stdlib":
        missing = ["wordfreq", "spacy", "en_core_web_sm"]
        caps = RuntimeCapabilities(
            engine_used="stdlib",
            wordfreq_available=False,
            spacy_available=False,
            spacy_model=None,
            missing_packages=missing,
            install_hint=INSTALL_HINT_MESSAGE,
        )
        return None, None, caps

    wordfreq_mod = _try_import_optional("wordfreq")
    spacy_mod = _try_import_optional("spacy")
    spacy_nlp, model_name = _load_spacy_model(spacy_mod)

    missing: list[str] = []
    if wordfreq_mod is None:
        missing.append("wordfreq")
    if spacy_mod is None:
        missing.append("spacy")
    if spacy_nlp is None:
        missing.append("en_core_web_sm")

    engine_used = "spacy" if spacy_nlp is not None else "stdlib"
    hint = INSTALL_HINT_MESSAGE if missing else None
    caps = RuntimeCapabilities(
        engine_used=engine_used,
        wordfreq_available=wordfreq_mod is not None,
        spacy_available=spacy_nlp is not None,
        spacy_model=model_name,
        missing_packages=missing,
        install_hint=hint,
    )
    return wordfreq_mod, spacy_nlp, caps


def split_into_sentences(text: str) -> list[str]:
    """Splits text into sentences while masking inline code spans."""
    masked = re.sub(r"`[^`]*`", "CODE_IDENT", text)
    raw_sentences = re.split(r"(?<=[.!?])\s+", masked)
    cleaned: list[str] = []
    for s in raw_sentences:
        s_strip = s.strip()
        if (
            s_strip
            and not s_strip.startswith("#")
            and not s_strip.startswith("```")
            and not s_strip.startswith("|")
            and not _HORIZONTAL_RULE_RE.match(s_strip)
        ):
            cleaned.append(s_strip)
    return cleaned


def _detect_frontmatter_end(lines: list[str]) -> int:
    """Returns the line index immediately after YAML frontmatter, or 0."""
    if not lines or lines[0].strip() != "---":
        return 0
    saw_yaml_key = False
    limit = min(len(lines), 40)
    for idx in range(1, limit):
        stripped = lines[idx].strip()
        if stripped == "---":
            return (idx + 1) if saw_yaml_key else 0
        if _YAML_KEY_RE.match(stripped):
            saw_yaml_key = True
    return 0


def _clean_line_for_scanning(line: str) -> str:
    """Replaces inline code, math, and HTML comments, and unwraps markdown links."""
    without_code = re.sub(r"`[^`]*`", " CODE_IDENT ", line)
    without_math = re.sub(r"\$[^$]+\$", " ", without_code)
    without_comments = re.sub(r"<!--.*?-->", " ", without_math)
    return re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", without_comments)


def _is_Fence_toggle(stripped: str) -> bool:
    """Returns whether `stripped` opens or closes a fenced code block."""
    return stripped.startswith(("```", "~~~"))


def _is_non_prose_structural_line(stripped: str) -> bool:
    """Returns whether `stripped` is empty, a heading, a table row, or a rule."""
    return (
        not stripped
        or stripped.startswith(("#", "|", "\u274c", "- \u274c"))
        or bool(_HORIZONTAL_RULE_RE.match(stripped))
    )


def _extract_paragraphs_and_sentences(
    lines: list[str], start_idx: int
) -> tuple[list[str], list[tuple[str, int]]]:
    """Extracts prose paragraphs and `(sentence_text, line_number)` pairs."""
    in_code_block = False
    paragraphs: list[str] = []
    current_para: list[str] = []
    current_para_lines: list[tuple[str, int]] = []
    sentences_with_lines: list[tuple[str, int]] = []

    for idx in range(start_idx, len(lines)):
        line_num = idx + 1
        stripped = lines[idx].strip()
        if _is_Fence_toggle(stripped):
            in_code_block = not in_code_block
            continue
        if in_code_block:
            continue
        if _is_non_prose_structural_line(stripped):
            _flush_paragraph(
                current_para,
                current_para_lines,
                paragraphs,
                sentences_with_lines,
            )
            current_para = []
            current_para_lines = []
            continue
        if _BULLET_RE.match(stripped) or _NUMBERED_LIST_RE.match(stripped):
            _flush_paragraph(
                current_para,
                current_para_lines,
                paragraphs,
                sentences_with_lines,
            )
            current_para = [stripped]
            current_para_lines = [(stripped, line_num)]
            continue
        current_para.append(stripped)
        current_para_lines.append((stripped, line_num))

    _flush_paragraph(current_para, current_para_lines, paragraphs, sentences_with_lines)
    return paragraphs, sentences_with_lines


def _normalize_paragraph_line(raw_line: str) -> str:
    """Strips blockquote prefixes, callout headers, and list markers from a line."""
    cleaned = _clean_line_for_scanning(raw_line).strip()
    cleaned = re.sub(r"^\s*>\s*", "", cleaned)
    if cleaned.startswith("[!") or re.match(r"^\*\*[A-Z0-9 &_-]+\*\*$", cleaned):
        return ""
    return re.sub(r"^\s*(?:[-*+]|\d+\.)\s+", "", cleaned).strip()


def _flush_paragraph(
    current_para: list[str],
    current_para_lines: list[tuple[str, int]],
    paragraphs: list[str],
    sentences_with_lines: list[tuple[str, int]],
) -> None:
    """Flushes accumulated paragraph lines into `paragraphs` and `sentences_with_lines`."""
    if not current_para:
        return
    paragraphs.append("\n".join(current_para))
    buffer_text = ""
    buffer_line = current_para_lines[0][1]
    for raw_line, line_num in current_para_lines:
        cleaned = _normalize_paragraph_line(raw_line)
        if not cleaned:
            continue
        if not buffer_text:
            buffer_line = line_num
            buffer_text = cleaned
        else:
            buffer_text = f"{buffer_text} {cleaned}"
        parts = re.split(r"(?<=[.!?])\s+", buffer_text)
        for part in parts[:-1]:
            if part.strip():
                sentences_with_lines.append((part.strip(), buffer_line))
        buffer_text = parts[-1]
        buffer_line = line_num
    if buffer_text.strip():
        sentences_with_lines.append((buffer_text.strip(), buffer_line))


def _is_likely_noun_token(word: str) -> bool:
    """Heuristically determines whether `word` can act as a noun in a noun cluster."""
    if prose_lexicon.is_code_or_identifier(word):
        return False
    low = word.lower().strip("'-")
    if len(low) <= 2 or low in _NON_NOUN_CLUSTER_WORDS:
        return False
    if low in _EXCEPT_NOUNS_WITH_ADJ_SUFFIX:
        return True
    if low in prose_lexicon.CONCRETE_TECHNICAL_ACTORS:
        return True
    if low.endswith(_ADJECTIVE_OR_ADVERB_SUFFIXES):
        return False
    lemma = prose_lexicon.normalize_lemma(low)
    return not (
        lemma in _FINITE_VERBS_SET
        and lemma not in prose_lexicon.CONCRETE_TECHNICAL_ACTORS
    )


def _find_stdlib_noun_clusters(cleaned_line: str) -> list[list[str]]:
    """Finds runs of 4 or more consecutive noun/noun-adjunct tokens."""
    clusters: list[list[str]] = []
    segments = re.split(r"[,.;:!?()\[\]{}\"/\\&+=|<>]+", cleaned_line)
    for seg in segments:
        run: list[str] = []
        for token in re.findall(r"\b[a-zA-Z0-9_'-]+\b", seg):
            if _is_likely_noun_token(token):
                run.append(token)
            else:
                if len(run) >= 4:
                    clusters.append(run)
                run = []
        if len(run) >= 4:
            clusters.append(run)
    return clusters


def _find_spacy_noun_clusters(cleaned_line: str, nlp: Any) -> list[list[str]]:
    """Uses a spaCy parse to find runs of 4 or more consecutive `NOUN`/`PROPN` tokens."""
    doc = nlp(cleaned_line)
    clusters: list[list[str]] = []
    run: list[str] = []
    for tok in doc:
        if (
            tok.pos_ in ("NOUN", "PROPN")
            and not prose_lexicon.is_code_or_identifier(tok.text)
            and tok.text != "CODE_IDENT"
        ):
            run.append(tok.text)
        else:
            if len(run) >= 4:
                clusters.append(run)
            run = []
    if len(run) >= 4:
        clusters.append(run)
    return clusters


def _scan_noun_clusters(
    cleaned_line: str,
    line_idx: int,
    snippet: str,
    nlp: Any,
    out: list[dict[str, Any]],
) -> None:
    """Appends 4+ consecutive noun clusters on `cleaned_line` to `out`."""
    clusters = (
        _find_spacy_noun_clusters(cleaned_line, nlp)
        if nlp is not None
        else _find_stdlib_noun_clusters(cleaned_line)
    )
    for cluster in clusters:
        phrase = " ".join(cluster)
        out.append(
            {
                "line": line_idx,
                "cluster": phrase,
                "length": len(cluster),
                "suggestion": (
                    f"Break up the {len(cluster)}-noun stack {phrase!r} "
                    "using prepositions or hyphens (max 3 consecutive nouns)."
                ),
                "line_snippet": snippet,
            }
        )


def _extract_tokens_with_pos(
    cleaned_line: str, nlp: Any
) -> list[tuple[str, str | None]]:
    """Tokenizes `cleaned_line`, attaching Universal POS tags when `nlp` is active."""
    if nlp is not None:
        doc = nlp(cleaned_line)
        return [(t.text, t.pos_) for t in doc if t.is_alpha]
    return [(w, None) for w in _ALPHA_WORD_RE.findall(cleaned_line)]


def _format_obscure_record(
    entry: prose_lexicon.ObscureWordEntry, line_idx: int, snippet: str
) -> dict[str, Any]:
    """Formats an `ObscureWordEntry` into a violation dictionary."""
    record: dict[str, Any] = {
        "line": line_idx,
        "word": entry.word,
        "pos": entry.pos,
        "replacement": entry.replacement,
        "line_snippet": snippet,
    }
    if entry.zipf is not None:
        record["zipf"] = entry.zipf
    return record


def _format_advisory_record(
    word: str, zipf_val: float | None, line_idx: int, snippet: str
) -> dict[str, Any]:
    """Formats a rare domain noun advisory entry."""
    adv_record: dict[str, Any] = {
        "line": line_idx,
        "word": word,
        "note": (
            "Rare or specialized domain noun; verify target audience "
            "familiarity or define on first use."
        ),
        "line_snippet": snippet,
    }
    if zipf_val is not None:
        adv_record["zipf"] = zipf_val
    return adv_record


def _scan_obscure_and_advisory_words(
    cleaned_line: str,
    line_idx: int,
    snippet: str,
    wordfreq_mod: Any,
    nlp: Any,
    obscure_out: list[dict[str, Any]],
    advisory_out: list[dict[str, Any]],
    seen_advisory: set[str],
) -> None:
    """Scans a line for obscure non-noun words and rare domain nouns."""
    for tok, pos_hint in _extract_tokens_with_pos(cleaned_line, nlp):
        entry = prose_lexicon.lookup_obscure_word(
            tok, pos_hint=pos_hint, wordfreq_mod=wordfreq_mod
        )
        if entry is not None:
            obscure_out.append(_format_obscure_record(entry, line_idx, snippet))
            continue

        is_noun = pos_hint in ("NOUN", "PROPN") if pos_hint else False
        is_rare, zipf_val = prose_lexicon.is_rare_domain_noun(
            tok, is_noun_hint=is_noun, wordfreq_mod=wordfreq_mod
        )
        low = tok.lower()
        if is_rare and low not in seen_advisory:
            seen_advisory.add(low)
            advisory_out.append(
                _format_advisory_record(low, zipf_val, line_idx, snippet)
            )


def _scan_nominalizations(
    cleaned_line: str,
    line_idx: int,
    snippet: str,
    out: list[dict[str, Any]],
) -> None:
    """Scans `cleaned_line` for light-verb nominalization constructions."""
    for pattern, template in prose_lexicon.LIGHT_VERB_NOMINALIZATIONS:
        for match in pattern.finditer(cleaned_line):
            noun = match.group(1).lower()
            out.append(
                {
                    "line": line_idx,
                    "phrase": match.group(0).strip(),
                    "nominalization": noun,
                    "suggestion": template.format(noun=noun),
                    "line_snippet": snippet,
                }
            )


def _scan_phrasal_verbs(
    cleaned_line: str,
    line_idx: int,
    snippet: str,
    out: list[dict[str, Any]],
) -> None:
    """Scans `cleaned_line` for colloquial phrasal verbs."""
    for pattern, canonical, replacement in prose_lexicon.PHRASAL_VERBS:
        for match in pattern.finditer(cleaned_line):
            out.append(
                {
                    "line": line_idx,
                    "phrasal_verb": canonical,
                    "matched_text": match.group(0),
                    "replacement": replacement,
                    "line_snippet": snippet,
                }
            )


def _record_synonym_candidates(
    cleaned_line: str, line_idx: int, word_lines: dict[str, list[int]]
) -> None:
    """Records appearances of STE100 synonym-group base words on `cleaned_line`."""
    for tok in _ALPHA_WORD_RE.findall(cleaned_line):
        if prose_lexicon.is_code_or_identifier(tok):
            continue
        low = tok.lower()
        word_lines.setdefault(low, []).append(line_idx)


def _scan_synonym_rotations(
    word_lines: dict[str, list[int]],
) -> list[dict[str, Any]]:
    """Checks document-wide `word_lines` for rotated STE100 action synonyms."""
    rotations: list[dict[str, Any]] = []
    for group in prose_lexicon.SYNONYM_GROUPS:
        present = [w for w in group if w in word_lines]
        if len(present) >= 2:
            rotations.append(
                {
                    "synonym_group": list(group),
                    "words_used": [
                        {"word": w, "lines": sorted(set(word_lines[w]))[:10]}
                        for w in present
                    ],
                    "suggestion": (
                        f"Pick one term ({present[0]!r}) and use it consistently "
                        f"instead of rotating between {', '.join(repr(w) for w in present)}."
                    ),
                }
            )
    return rotations


def _classify_pcc_score(score: int) -> str:
    """Maps a numeric PCC score to a qualitative band."""
    if score <= 2:
        return "simple"
    if score <= 5:
        return "moderate"
    if score <= 8:
        return "elevated"
    return "high"


def _score_stacked_negations(sentence: str) -> tuple[int, str | None]:
    """Returns a `+2` increment when `sentence` contains 2+ negative operators."""
    matches = [m.group(0).lower() for m in _NEGATION_RE.finditer(sentence)]
    if len(matches) < 2:
        return 0, None
    preview = " ... ".join(matches[:3])
    return (
        2,
        f"+2: stacked negation ({len(matches)} negative operators: {preview!r})",
    )


def _score_abstractness_cluster(words: list[str]) -> tuple[int, str | None]:
    """Returns a `+1` increment when 3+ abstract words lack any concrete actor."""
    abstract_count = 0
    concrete_count = 0
    for w in words:
        if w == "CODE_IDENT":
            concrete_count += 1
            continue
        category = prose_lexicon.classify_concreteness(w)
        if category == "abstract":
            abstract_count += 1
        elif category == "concrete":
            concrete_count += 1
    if abstract_count >= 3 and concrete_count == 0:
        return (
            1,
            f"+1: abstractness cluster ({abstract_count} abstract terms without a concrete technical actor)",
        )
    return 0, None


def _score_passive_voice(sentence: str, has_subordinate: bool) -> tuple[int, list[str]]:
    """Scores passive voice constructions while exempting epistemic modal hedges."""
    masked = prose_lexicon.EPISTEMIC_HEDGE_RE.sub(" MODAL_HEDGE ", sentence)
    total_inc = 0
    reasons: list[str] = []
    for match in _PASSIVE_RE.finditer(masked):
        participle = match.group(1).lower()
        by_agent = match.group(2)
        if participle in _STATIVE_PARTICIPLES and not by_agent:
            continue
        phrase = " ".join(match.group(0).split())
        if by_agent:
            total_inc += 2
            reasons.append(f"+2: passive voice with explicit agent ({phrase!r})")
        elif has_subordinate:
            total_inc += 2
            reasons.append(f"+2: nested passive voice ({phrase!r})")
        else:
            total_inc += 1
            reasons.append(f"+1: passive voice ({phrase!r})")
    return total_inc, reasons


def _score_pre_verbal_delay_stdlib(sentence: str) -> tuple[int, str | None]:
    """Scores front-loaded introductory clauses or phrases before the main clause."""
    if not _INTRO_OPENER_RE.match(sentence) or "," not in sentence:
        return 0, None
    comma_parts = sentence.split(",")
    best_delay = 0
    for idx in range(1, len(comma_parts)):
        prefix = ",".join(comma_parts[:idx])
        remainder = comma_parts[idx].strip()
        rem_words = _ALPHA_WORD_RE.findall(remainder)
        if len(rem_words) >= 2 and rem_words[0].lower() in (
            "the",
            "a",
            "an",
            "this",
            "that",
            "these",
            "those",
            "it",
            "we",
            "you",
            "they",
            "clients",
            "client",
            "servers",
            "server",
            "users",
            "user",
            "requests",
            "request",
            "callers",
            "caller",
            "workers",
            "worker",
            "system",
            "service",
            "module",
            "function",
            "deployment",
            "pipeline",
            "parser",
            "controller",
        ):
            best_delay = len(_WORD_RE.findall(prefix))
            break
    if best_delay == 0:
        best_delay = len(_WORD_RE.findall(comma_parts[0]))
    if best_delay >= 15:
        return (
            2,
            f"+2: front-loaded pre-verbal delay ({best_delay} words before main subject-verb)",
        )
    if best_delay >= 9:
        return (
            1,
            f"+1: front-loaded pre-verbal delay ({best_delay} words before main subject-verb)",
        )
    return 0, None


def _score_center_embedding_and_dd_stdlib(
    sentence: str,
) -> tuple[int, list[str]]:
    """Detects center-embedded clauses and long subject-verb distance in stdlib mode."""
    words = _ALPHA_WORD_RE.findall(sentence)
    if len(words) < 8:
        return 0, []
    lower_words = [w.lower() for w in words]
    subj_idx, subj_word = _find_clause_subject_stdlib(lower_words, words)
    if subj_idx is None:
        return 0, []
    verb_idx, verb_word = _find_governing_verb_stdlib(lower_words, words, subj_idx)
    if verb_idx is None or verb_idx - subj_idx < 5:
        return 0, []

    dd_sv = verb_idx - subj_idx
    intervening = lower_words[subj_idx + 1 : verb_idx]
    clause_markers = {
        "which",
        "who",
        "whom",
        "whose",
        "that",
        "when",
        "where",
        "if",
        "unless",
        "although",
        "though",
        "because",
        "since",
        "while",
    }
    if any(m in clause_markers for m in intervening):
        return (
            2,
            [
                (
                    f"+2: center-embedded clause interrupts subject {subj_word!r} "
                    f"and verb {verb_word!r} (DD_SV={dd_sv})"
                )
            ],
        )
    if dd_sv >= 12:
        return (
            2,
            [
                (
                    f"+2: long subject-verb distance ({dd_sv} words between "
                    f"{subj_word!r} and {verb_word!r})"
                )
            ],
        )
    if dd_sv >= 7:
        return (
            1,
            [
                (
                    f"+1: long subject-verb distance ({dd_sv} words between "
                    f"{subj_word!r} and {verb_word!r})"
                )
            ],
        )
    return 0, []


def _find_clause_subject_stdlib(
    lower_words: list[str], words: list[str]
) -> tuple[int | None, str]:
    """Locates the head noun of the initial subject noun phrase."""
    determiners = {
        "the",
        "a",
        "an",
        "this",
        "that",
        "these",
        "those",
        "each",
        "every",
        "any",
        "all",
        "our",
        "your",
        "their",
        "its",
    }
    start = 0
    if lower_words[0] in (
        "if",
        "when",
        "while",
        "although",
        "because",
        "since",
        "unless",
    ):
        start = 1
    if start >= len(lower_words):
        return None, ""
    if lower_words[start] in determiners:
        idx = start + 1
        while idx < min(len(lower_words), start + 5):
            if _is_likely_noun_token(words[idx]) and (
                idx + 1 == len(lower_words) or not _is_likely_noun_token(words[idx + 1])
            ):
                return idx, words[idx]
            idx += 1
        if start + 1 < len(words):
            return start + 1, words[start + 1]
    if _is_likely_noun_token(words[start]):
        return start, words[start]
    return None, ""


def _find_governing_verb_stdlib(
    lower_words: list[str], words: list[str], subj_idx: int
) -> tuple[int | None, str]:
    """Finds the governing verb for `subj_idx`, skipping an embedded relative clause."""
    clause_openers = {"which", "who", "whom", "whose", "that", "when", "where"}
    in_embedded = False
    embedded_verb_seen = False
    for idx in range(subj_idx + 1, len(lower_words)):
        lw = lower_words[idx]
        if lw in clause_openers and not in_embedded and idx <= subj_idx + 3:
            in_embedded = True
            continue
        is_verb = lw in _FINITE_VERBS_SET
        if not is_verb:
            continue
        if in_embedded and not embedded_verb_seen:
            embedded_verb_seen = True
            continue
        return idx, words[idx]
    return None, ""


def _score_subordinate_clauses_stdlib(
    sentence: str,
) -> tuple[int, list[str]]:
    """Counts subordinate and relative clauses with nesting increments."""
    markers: list[str] = []
    for match in _SUBORDINATOR_RE.finditer(sentence):
        markers.append(match.group(0).lower())
    for match in re.finditer(r"\b([a-zA-Z]+)\s+that\b", sentence):
        prev_word = match.group(1).lower()
        if prev_word not in (
            "in",
            "to",
            "of",
            "for",
            "on",
            "with",
            "at",
            "from",
            "by",
            "so",
            "provided",
            "assuming",
            "such",
            "after",
            "before",
        ):
            markers.append("that")

    total_inc = 0
    reasons: list[str] = []
    for nesting, marker in enumerate(markers):
        inc = 1 + nesting
        total_inc += inc
        if nesting == 0:
            reasons.append(f"+{inc}: subordinate clause ({marker + ' ...'!r})")
        else:
            reasons.append(
                f"+{inc}: nested clause ({marker + ' ...'!r}, nesting={nesting})"
            )
    return total_inc, reasons


def _score_sentence_stdlib(
    sentence: str, line_num: int, sentence_idx: int
) -> SentenceComplexityResult:
    """Computes Prose Cognitive Complexity (PCC) using stdlib heuristics."""
    words = _WORD_RE.findall(sentence)
    breakdown: list[str] = []
    total = 0

    clause_inc, clause_reasons = _score_subordinate_clauses_stdlib(sentence)
    total += clause_inc
    breakdown.extend(clause_reasons)

    ce_inc, ce_reasons = _score_center_embedding_and_dd_stdlib(sentence)
    total += ce_inc
    breakdown.extend(ce_reasons)

    delay_inc, delay_reason = _score_pre_verbal_delay_stdlib(sentence)
    if delay_reason:
        total += delay_inc
        breakdown.append(delay_reason)

    pass_inc, pass_reasons = _score_passive_voice(
        sentence, has_subordinate=(clause_inc > 0)
    )
    total += pass_inc
    breakdown.extend(pass_reasons)

    neg_inc, neg_reason = _score_stacked_negations(sentence)
    if neg_reason:
        total += neg_inc
        breakdown.append(neg_reason)

    abs_inc, abs_reason = _score_abstractness_cluster(words)
    if abs_reason:
        total += abs_inc
        breakdown.append(abs_reason)

    return SentenceComplexityResult(
        index=sentence_idx,
        line=line_num,
        word_count=len(words),
        cognitive_complexity=total,
        classification=_classify_pcc_score(total),
        breakdown=breakdown,
        text=sentence,
    )


def _score_clauses_spacy(doc: Any) -> tuple[int, list[str]]:
    """Scores subordinate and relative clauses from a spaCy dependency tree."""
    clause_deps = {"advcl", "relcl", "ccomp"}
    total_inc = 0
    reasons: list[str] = []
    for tok in doc:
        if tok.dep_ not in clause_deps:
            continue
        nesting = sum(1 for anc in tok.ancestors if anc.dep_ in clause_deps)
        inc = 1 + nesting
        total_inc += inc
        marker = tok.text.lower()
        for child in tok.lefts:
            if child.dep_ in ("mark", "ref", "nsubj", "advmod"):
                marker = child.text.lower()
                break
        if nesting == 0:
            reasons.append(f"+{inc}: subordinate clause ({marker + ' ...'!r})")
        else:
            reasons.append(
                f"+{inc}: nested clause ({marker + ' ...'!r}, nesting={nesting})"
            )
    return total_inc, reasons


def _score_center_embedding_and_dd_spacy(doc: Any) -> tuple[int, list[str]]:
    """Scores center-embedded clauses and subject-verb distance from a spaCy parse."""
    total_inc = 0
    reasons: list[str] = []
    for tok in doc:
        if tok.dep_ not in ("nsubj", "nsubjpass", "csubj"):
            continue
        head = tok.head
        if head.pos_ not in ("VERB", "AUX") or tok.i >= head.i:
            continue
        dd_sv = head.i - tok.i
        if dd_sv < 5:
            continue
        span = doc[tok.i + 1 : head.i]
        has_embedded = any(t.dep_ in ("relcl", "advcl", "acl", "ccomp") for t in span)
        if has_embedded:
            total_inc += 2
            reasons.append(
                f"+2: center-embedded clause interrupts subject {tok.text!r} "
                f"and verb {head.text!r} (DD_SV={dd_sv})"
            )
        elif dd_sv >= 12:
            total_inc += 2
            reasons.append(
                f"+2: long subject-verb distance ({dd_sv} words between "
                f"{tok.text!r} and {head.text!r})"
            )
        elif dd_sv >= 7:
            total_inc += 1
            reasons.append(
                f"+1: long subject-verb distance ({dd_sv} words between "
                f"{tok.text!r} and {head.text!r})"
            )
    return total_inc, reasons


def _score_pre_verbal_delay_spacy(doc: Any) -> tuple[int, str | None]:
    """Scores pre-verbal delay before the ROOT verb and its subject in spaCy."""
    roots = [t for t in doc if t.dep_ == "ROOT"]
    if not roots:
        return 0, None
    root = roots[0]
    anchor_i = root.i
    for child in root.lefts:
        if child.dep_ in ("nsubj", "nsubjpass", "csubj"):
            anchor_i = min(anchor_i, child.i)
            break
    delay_words = sum(1 for t in doc[:anchor_i] if not t.is_punct and not t.is_space)
    if delay_words >= 15:
        return (
            2,
            f"+2: front-loaded pre-verbal delay ({delay_words} words before main subject-verb)",
        )
    if delay_words >= 9:
        return (
            1,
            f"+1: front-loaded pre-verbal delay ({delay_words} words before main subject-verb)",
        )
    return 0, None


def score_sentence_complexity(
    sentence: str,
    line_num: int,
    sentence_idx: int,
    nlp: Any = None,
) -> SentenceComplexityResult:
    """Scores the syntactic and lexical cognitive complexity of `sentence`."""
    stdlib_res = _score_sentence_stdlib(sentence, line_num, sentence_idx)
    if nlp is None:
        return stdlib_res

    doc = nlp(sentence)
    words = _WORD_RE.findall(sentence)
    breakdown: list[str] = []
    total = 0

    clause_inc, clause_reasons = _score_clauses_spacy(doc)
    total += clause_inc
    breakdown.extend(clause_reasons)

    ce_inc, ce_reasons = _score_center_embedding_and_dd_spacy(doc)
    total += ce_inc
    breakdown.extend(ce_reasons)

    delay_inc, delay_reason = _score_pre_verbal_delay_spacy(doc)
    if delay_reason:
        total += delay_inc
        breakdown.append(delay_reason)

    pass_inc, pass_reasons = _score_passive_voice(
        sentence, has_subordinate=(clause_inc > 0)
    )
    total += pass_inc
    breakdown.extend(pass_reasons)

    neg_inc, neg_reason = _score_stacked_negations(sentence)
    if neg_reason:
        total += neg_inc
        breakdown.append(neg_reason)

    abs_inc, abs_reason = _score_abstractness_cluster(words)
    if abs_reason:
        total += abs_inc
        breakdown.append(abs_reason)

    if stdlib_res.cognitive_complexity > total:
        return stdlib_res

    return SentenceComplexityResult(
        index=sentence_idx,
        line=line_num,
        word_count=len(words),
        cognitive_complexity=total,
        classification=_classify_pcc_score(total),
        breakdown=breakdown,
        text=sentence,
    )


def _scan_line_classic_violations(
    line: str,
    cleaned_line: str,
    stripped: str,
    line_idx: int,
    compiled_phrases: list[tuple[str, re.Pattern[str]]],
    banned_matches: list[dict[str, Any]],
    banned_phrase_matches: list[dict[str, Any]],
    em_dash_matches: list[dict[str, Any]],
    smart_quote_matches: list[dict[str, Any]],
    inline_header_bullets: list[dict[str, Any]],
) -> int:
    """Scans a line for classic banned words, phrases, punctuation, and bullet headers."""
    snippet = stripped[:90]
    line_words = _WORD_RE.findall(cleaned_line.lower())
    for bw in BANNED_WORDS:
        if bw in line_words:
            banned_matches.append(
                {
                    "line": line_idx,
                    "banned_word": bw,
                    "line_snippet": snippet,
                }
            )

    for label, pattern_re in compiled_phrases:
        if pattern_re.search(cleaned_line):
            banned_phrase_matches.append(
                {
                    "line": line_idx,
                    "banned_phrase": label,
                    "line_snippet": snippet,
                }
            )

    if _EM_DASH_RE.search(cleaned_line):
        em_dash_matches.append({"line": line_idx, "line_snippet": snippet})

    if _SMART_QUOTE_RE.search(cleaned_line):
        smart_quote_matches.append({"line": line_idx, "line_snippet": snippet})

    bullet_match = _BULLET_RE.match(line) or _NUMBERED_LIST_RE.match(line)
    if not bullet_match:
        return 0
    bullet_body = bullet_match.group(1).strip()
    if _INLINE_HEADER_RE.match(bullet_body):
        inline_header_bullets.append(
            {
                "line": line_idx,
                "line_snippet": snippet,
            }
        )
    return 1


def _compute_paragraph_stats(
    paragraphs: list[str],
) -> tuple[list[int], list[int], list[dict[str, Any]]]:
    """Computes sentence and word counts per paragraph and flags paragraphs > 4 sentences."""
    para_sentence_counts: list[int] = []
    para_word_counts: list[int] = []
    long_paragraphs: list[dict[str, Any]] = []

    for idx, p in enumerate(paragraphs, 1):
        p_sentences = split_into_sentences(p)
        p_words = _WORD_RE.findall(p)
        num_s = len(p_sentences)
        num_w = len(p_words)
        para_sentence_counts.append(num_s)
        para_word_counts.append(num_w)
        if num_s > 4:
            long_paragraphs.append(
                {
                    "paragraph_index": idx,
                    "sentence_count": num_s,
                    "snippet": p[:80] + ("..." if len(p) > 80 else ""),
                }
            )
    return para_sentence_counts, para_word_counts, long_paragraphs


def _safe_median(values: list[int]) -> float:
    """Returns the median of `values` rounded to 1 decimal place, or 0.0 if empty."""
    if not values:
        return 0.0
    return round(float(statistics.median(values)), 1)


def _evaluate_sentences(
    sentences_with_lines: list[tuple[str, int]],
    complexity_threshold: int,
    spacy_nlp: Any,
) -> tuple[list[int], list[dict[str, Any]], list[int], list[dict[str, Any]]]:
    """Computes word counts and PCC scores for every sentence in the document."""
    sentence_word_counts: list[int] = []
    long_sentences: list[dict[str, Any]] = []
    pcc_scores: list[int] = []
    complex_sentences: list[dict[str, Any]] = []

    for idx, (s_text, s_line) in enumerate(sentences_with_lines, 1):
        w_count = len(_WORD_RE.findall(s_text))
        if w_count > 0:
            sentence_word_counts.append(w_count)
        if w_count > 25:
            long_sentences.append(
                {
                    "index": idx,
                    "line": s_line,
                    "word_count": w_count,
                    "text": s_text[:100] + ("..." if len(s_text) > 100 else ""),
                }
            )
        pcc_res = score_sentence_complexity(s_text, s_line, idx, nlp=spacy_nlp)
        pcc_scores.append(pcc_res.cognitive_complexity)
        if pcc_res.cognitive_complexity > complexity_threshold:
            complex_sentences.append(pcc_res.to_dict())

    return sentence_word_counts, long_sentences, pcc_scores, complex_sentences


def _is_skippable_scan_line(stripped: str, in_code_block: bool) -> bool:
    """Returns whether a line should be skipped during line-by-line scanning."""
    return (
        in_code_block
        or stripped.startswith(("|", "\u274c", "- \u274c"))
        or bool(re.match(r"^[-_*]{2,}$", stripped))
    )


def _scan_document_lines(
    lines: list[str],
    start_idx: int,
    wordfreq_mod: Any,
    spacy_nlp: Any,
) -> dict[str, Any]:
    """Runs all line-level and document-wide lexical and formatting scanners."""
    banned_matches: list[dict[str, Any]] = []
    banned_phrase_matches: list[dict[str, Any]] = []
    em_dash_matches: list[dict[str, Any]] = []
    smart_quote_matches: list[dict[str, Any]] = []
    inline_header_bullets: list[dict[str, Any]] = []
    obscure_matches: list[dict[str, Any]] = []
    advisory_nouns: list[dict[str, Any]] = []
    seen_advisory: set[str] = set()
    nominalizations: list[dict[str, Any]] = []
    noun_clusters: list[dict[str, Any]] = []
    phrasal_verbs: list[dict[str, Any]] = []
    word_lines: dict[str, list[int]] = {}
    total_bullets = 0
    in_code_block = False

    compiled_phrases = [
        (label, re.compile(pattern, re.IGNORECASE)) for label, pattern in BANNED_PHRASES
    ]

    for idx in range(start_idx, len(lines)):
        line_idx = idx + 1
        line = lines[idx]
        stripped = line.strip()
        if _is_Fence_toggle(stripped):
            in_code_block = not in_code_block
            continue
        if _is_skippable_scan_line(stripped, in_code_block):
            continue

        cleaned_line = _clean_line_for_scanning(line)
        snippet = stripped[:90]
        total_bullets += _scan_line_classic_violations(
            line,
            cleaned_line,
            stripped,
            line_idx,
            compiled_phrases,
            banned_matches,
            banned_phrase_matches,
            em_dash_matches,
            smart_quote_matches,
            inline_header_bullets,
        )
        _scan_obscure_and_advisory_words(
            cleaned_line,
            line_idx,
            snippet,
            wordfreq_mod,
            spacy_nlp,
            obscure_matches,
            advisory_nouns,
            seen_advisory,
        )
        _scan_nominalizations(cleaned_line, line_idx, snippet, nominalizations)
        _scan_phrasal_verbs(cleaned_line, line_idx, snippet, phrasal_verbs)
        _scan_noun_clusters(cleaned_line, line_idx, snippet, spacy_nlp, noun_clusters)
        _record_synonym_candidates(cleaned_line, line_idx, word_lines)

    inline_header_ratio = (
        (len(inline_header_bullets) / total_bullets) if total_bullets else 0.0
    )
    return {
        "total_bullets": total_bullets,
        "inline_header_bullets": inline_header_bullets,
        "inline_header_ratio": round(inline_header_ratio, 2),
        "inline_header_overuse": total_bullets >= 3 and inline_header_ratio > 0.5,
        "banned_matches": banned_matches,
        "banned_phrase_matches": banned_phrase_matches,
        "obscure_matches": obscure_matches,
        "advisory_nouns": advisory_nouns,
        "nominalizations": nominalizations,
        "noun_clusters": noun_clusters,
        "phrasal_verbs": phrasal_verbs,
        "synonym_rotations": _scan_synonym_rotations(word_lines),
        "em_dash_matches": em_dash_matches,
        "smart_quote_matches": smart_quote_matches,
    }


def _read_input_lines(
    file_path: str, text: str | None = None
) -> tuple[list[str] | None, str | None]:
    """Reads input lines from `text`, `sys.stdin` (when `file_path == '-'`), or disk."""
    if text is not None:
        return text.splitlines(keepends=True), None
    if file_path == "-":
        return sys.stdin.readlines(), None
    if not os.path.exists(file_path):
        return None, f"File not found: {file_path}"
    with open(file_path, "r", encoding="utf-8") as f:
        return f.readlines(), None


def analyze_prose(
    file_path: str,
    complexity_threshold: int = 5,
    engine: str = "auto",
    text: str | None = None,
) -> dict[str, Any]:
    """Analyzes a Markdown file, text string, or stdin (`'-'`) for prose quality."""
    lines, err = _read_input_lines(file_path, text)
    if err is not None or lines is None:
        return {"error": err or f"File not found: {file_path}"}

    wordfreq_mod, spacy_nlp, capabilities = _probe_nlp_backends(engine)
    reported_path = "<stdin>" if file_path == "-" else file_path

    start_idx = _detect_frontmatter_end(lines)
    body_text = "".join(lines[start_idx:])
    paragraphs, sentences_with_lines = _extract_paragraphs_and_sentences(
        lines, start_idx
    )

    words = _WORD_RE.findall(body_text)
    word_lengths = [len(w) for w in words]
    s_word_counts, long_sentences, pcc_scores, complex_sentences = _evaluate_sentences(
        sentences_with_lines, complexity_threshold, spacy_nlp
    )
    para_s_counts, para_w_counts, long_paragraphs = _compute_paragraph_stats(paragraphs)
    scan = _scan_document_lines(lines, start_idx, wordfreq_mod, spacy_nlp)

    return {
        "file_path": reported_path,
        "summary": {
            "capabilities": capabilities.to_dict(),
            "total_words": len(words),
            "total_sentences": len(sentences_with_lines),
            "total_paragraphs": len(paragraphs),
            "median_word_length_chars": _safe_median(word_lengths),
            "median_words_per_sentence": _safe_median(s_word_counts),
            "median_sentences_per_paragraph": _safe_median(para_s_counts),
            "median_words_per_paragraph": _safe_median(para_w_counts),
            "median_sentence_complexity": _safe_median(pcc_scores),
            "max_sentence_complexity": max(pcc_scores) if pcc_scores else 0,
            "total_bullets": scan["total_bullets"],
            "inline_header_bullets": len(scan["inline_header_bullets"]),
            "inline_header_bullet_ratio": scan["inline_header_ratio"],
        },
        "violations": {
            "banned_ai_words_found": scan["banned_matches"],
            "banned_ai_phrases_found": scan["banned_phrase_matches"],
            "obscure_words_found": scan["obscure_matches"],
            "rare_domain_nouns_advisory": scan["advisory_nouns"],
            "nominalizations_found": scan["nominalizations"],
            "noun_clusters_found": scan["noun_clusters"],
            "phrasal_verbs_found": scan["phrasal_verbs"],
            "synonym_rotations_found": scan["synonym_rotations"],
            "complex_sentences_found": complex_sentences,
            "em_dashes_found": scan["em_dash_matches"],
            "smart_quotes_found": scan["smart_quote_matches"],
            "inline_header_list_overuse": {
                "flagged": scan["inline_header_overuse"],
                "inline_header_count": len(scan["inline_header_bullets"]),
                "total_bullets": scan["total_bullets"],
                "ratio": scan["inline_header_ratio"],
                "examples": scan["inline_header_bullets"][:10],
            },
            "sentences_exceeding_25_words": long_sentences,
            "paragraphs_exceeding_4_sentences": long_paragraphs,
        },
    }


def _parse_cli_args(argv: list[str]) -> argparse.Namespace:
    """Parses command-line arguments for `analyze_prose.py`."""
    parser = argparse.ArgumentParser(
        description="Analyze prose for readability, anti-AI-isms, and cognitive complexity."
    )
    parser.add_argument(
        "file_path",
        nargs="?",
        help="Path to markdown/text file, or '-' to read from stdin.",
    )
    parser.add_argument(
        "--threshold",
        type=int,
        default=5,
        help="Prose Cognitive Complexity (PCC) threshold per sentence (default: 5).",
    )
    parser.add_argument(
        "--engine",
        choices=("auto", "stdlib", "spacy"),
        default="auto",
        help="NLP parser engine to use (default: auto).",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    if len(sys.argv) < 2 and sys.stdin.isatty():
        print(json.dumps({"error": "Usage: analyze_prose.py <path-to-file | ->"}))
        sys.exit(1)

    args = _parse_cli_args(sys.argv[1:])
    target_path = args.file_path or ("-" if not sys.stdin.isatty() else None)
    if not target_path:
        print(json.dumps({"error": "Usage: analyze_prose.py <path-to-file | ->"}))
        sys.exit(1)

    result = analyze_prose(
        target_path,
        complexity_threshold=args.threshold,
        engine=args.engine,
    )
    print(json.dumps(result, indent=2))
