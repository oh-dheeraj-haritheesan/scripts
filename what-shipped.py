#!/usr/bin/env python3
"""
what-shipped.py — list the Jira tickets that went into a blueprint release.

What it does
------------
Given a dmm-tools blueprint name (e.g. `WRN_2_17_Android`), prints the set of
Jira tickets that landed in each app between this blueprint and the previous
one in the same product family. Useful for "what's in this release?" / release-
notes / customer-comms work, without manually diffing 4+ app repos by hand.

How it works
------------
1. Reads the target's pinned app versions from `dmm-tools/dependencies/<bp>.json`
   (and `archive/dependencies/` as a fallback).
2. Picks a baseline blueprint — by default the numerically previous one in the
   same family (WRN_2_17 -> WRN_2_16); override with `--baseline`.
3. For each app whose version differs between baseline and target:
   a. Looks up the artifact in Artifactory to get the exact commit SHA
      (Jenkins-era artifacts via `vcs.revision`; GHA-era via the build info's
      `GITHUB_SHA`).
   b. Falls back to `release/<major>.<minor>` branch tip if Artifactory can't
      resolve the SHA, and warns that patch differences may be misreported.
   c. Runs `git log <baseline-sha>..<target-sha>` in the sibling app repo and
      extracts ticket keys (TFM-1234, EXR-567, PAS-89, ...) from commit subjects.
4. Auth for Artifactory is read from `~/.gradle/gradle.properties`
   (`artifactory_user` + `artifactory_password`); override with $ARTIFACTORY_TOKEN.
   No auth -> still works, falls back to branch tips throughout.
5. App repos are discovered by their GitHub origin remote, so directory names
   on disk don't have to match the canonical repo name.

Setup
-----
- Edit DMM_TOOLS_PATH below to point at your local dmm-tools clone.
- Sibling app clones (android-system-ops, wrn2, ipr2, ...) are expected to live
  next to dmm-tools. Override with --projects-root if your layout differs.

Usage
-----
  what-shipped.py WRN_2_17_Android
  what-shipped.py WRN_2_17_Android --baseline WRN_2_15_Android
  what-shipped.py WRN_2_17_Android --verbose        # show commit subjects
  what-shipped.py WRN_2_17_Android --no-fetch       # skip the auto `git fetch`
  what-shipped.py WRN_2_17_Android --no-links       # disable terminal hyperlinks
"""

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

# Dependency key -> canonical GitHub slug ("owner/repo"). None means "external / not tracked here".
# Local clones are discovered by matching this slug against each candidate repo's origin remote,
# so directory naming on disk doesn't matter.
REPO_MAP = {
    "system-ops": "contextmedia/android-system-ops",
    "zygote": "contextmedia/android-system-ops",
    "wrn2": "contextmedia/wrn2",
    "ipr": "contextmedia/ipr2",
    "ipr_video_conference": "contextmedia/ipr2",
    "ixr7": "contextmedia/ixr7_android",
    "dmm": "contextmedia/dmm-agent",
    "webview": None,
    "zoom": None,
    "gecko": None,
    "installer": None,
    "mediaPlayer_r3": None,
    "pwr2": "contextmedia/wrn2",
    "ixr6": None,
    "force_dca_priv_app": None,
    "fieldDevice": None,
}

# Dependency key -> Artifactory path under MAVEN_REPO (excludes /<version>/<file>).
# Used to resolve a published version to the exact commit SHA via Artifactory build info.
# Add entries as you verify each app's published path; missing keys fall back to branch-tip.
ARTIFACT_PATH_MAP = {
    "system-ops": "com/patientpoint/system/ops/system-ops",
    "zygote": "com/patientpoint/system/ops/zygote",
    "wrn2": "com/patientpoint/passiveapps/wrn2",
    "pwr2": "com/patientpoint/passiveapps/pwr2",
    "ipr": "com/patientpoint/ipr/app",
    "ixr7": "com/patientpoint/ixr7/app",
    "ipr_video_conference": "com/patientpoint/ipr_video_conference",
    # dmm publishes one artifact per channel (production/staging/development/linux/...).
    # The source commit is the same across channels for a given version, so we resolve via the
    # production channel. Add a separate key (e.g. `dmm-linux`) only if a Linux DMM blueprint needs it.
    "dmm": "com/patientpoint/dmm/production/dmm-pr",
}

# Maps library version keys (from libs.versions.toml or gradle.properties) to the
# GitHub repo and Artifactory artifact needed to resolve exact commit SHAs.
# Keys are all variant names a repo might use for the same library.
LIBRARY_VERSION_REPO_MAP = {
    "pp-core":      {"slug": "contextmedia/android-core", "artifact_path": "com/patientpoint/core/core-code", "artifact_ext": "aar"},
    "coreVersion":  {"slug": "contextmedia/android-core", "artifact_path": "com/patientpoint/core/core-code", "artifact_ext": "aar"},
    "core_version": {"slug": "contextmedia/android-core", "artifact_path": "com/patientpoint/core/core-code", "artifact_ext": "aar"},
}

ARTIFACTORY_BASE = os.environ.get("ARTIFACTORY_URL", "https://outcomehealth.jfrog.io/artifactory").rstrip("/")
MAVEN_REPO = "tfm-maven"
ARTIFACT_FILE_EXT = "zip"

# >>> EDIT THIS <<<
# Absolute path to your local dmm-tools clone (the repo containing `dependencies/`).
# Example: "/Users/yourname/PP_Projects/dmm-tools"
# App repos (android-system-ops, wrn2, ipr2, ...) are assumed to be siblings of this path.
# Leave as None to fall back to this script's parent directory (works only if this file
# lives inside dmm-tools/scripts/).
DMM_TOOLS_PATH = "/Users/Dheerajkumar.Haritheesan/PP_Projects/dmm-tools"

# Match either git@github.com:owner/repo(.git) or https://github.com/owner/repo(.git)
REMOTE_SLUG_RE = re.compile(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?/?$")

TICKET_RE = re.compile(r"\b([A-Z]{2,}-\d+)\b")
BLUEPRINT_RE = re.compile(r"^(.+)_(\d+)_([A-Za-z]+)$")
SEMVER_RE = re.compile(r"^\d+(\.\d+)*$")
JIRA_BASE_URL = "https://jirapp.atlassian.net/browse/"


def hyperlink(text: str, url: str, enabled: bool) -> str:
    if not enabled:
        return text
    return f"\033]8;;{url}\033\\{text}\033]8;;\033\\"


def parse_version(v):
    if not SEMVER_RE.match(v):
        return None
    parts = [int(x) for x in v.split(".")]
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def resolve_dep_file(dep_dirs, name: str):
    """Return the first existing <name>.json across the given dirs, or None."""
    for d in dep_dirs:
        f = d / f"{name}.json"
        if f.exists():
            return f
    return None


def find_previous_blueprint(dep_dirs, name: str):
    m = BLUEPRINT_RE.match(name)
    if not m:
        return None
    family, num, suffix = m.group(1), int(m.group(2)), m.group(3)
    candidates = {}
    for d in dep_dirs:
        for f in d.glob(f"{family}_*_{suffix}.json"):
            mm = re.match(rf"^{re.escape(family)}_(\d+)_{re.escape(suffix)}\.json$", f.name)
            if mm:
                n = int(mm.group(1))
                if n < num:
                    candidates.setdefault(n, f.stem)
    if not candidates:
        return None
    return candidates[max(candidates)]


def git(repo: Path, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


_HTTP_CACHE = {}


def parse_gradle_properties(path: Path):
    """Parse a Java .properties file. Returns dict; {} if file missing."""
    if not path.exists():
        return {}
    props = {}
    for line in path.read_text().splitlines():
        s = line.strip()
        if not s or s.startswith(("#", "!")) or "=" not in s:
            continue
        k, _, v = s.partition("=")
        props[k.strip()] = v.strip()
    return props


def load_artifactory_auth():
    """Resolve Artifactory auth header. Returns (header_value, source_label) or (None, None).

    Order: $ARTIFACTORY_TOKEN (Bearer) -> ~/.gradle/gradle.properties (Basic).
    """
    tok = os.environ.get("ARTIFACTORY_TOKEN")
    if tok:
        return f"Bearer {tok}", "$ARTIFACTORY_TOKEN"
    props = parse_gradle_properties(Path.home() / ".gradle" / "gradle.properties")
    user = props.get("artifactory_user")
    pw = props.get("artifactory_password")
    if user and pw:
        encoded = base64.b64encode(f"{user}:{pw}".encode()).decode()
        return f"Basic {encoded}", "~/.gradle/gradle.properties"
    return None, None


def http_get_json(url: str, auth_header: str):
    """GET a URL with the given Authorization header value and decode JSON. Returns (data, error)."""
    if url in _HTTP_CACHE:
        return _HTTP_CACHE[url]
    req = urllib.request.Request(url, headers={"Authorization": auth_header})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
            _HTTP_CACHE[url] = (data, None)
            return data, None
    except urllib.error.HTTPError as e:
        result = (None, f"HTTP {e.code} from {url}")
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        result = (None, f"{type(e).__name__}: {e}")
    _HTTP_CACHE[url] = result
    return result


def resolve_sha_via_artifactory(dep_key: str, version: str, auth_header: str,
                                artifact_path: str = None, artifact_ext: str = None):
    """Return (sha, warning) for a published artifact version via Artifactory."""
    artifact_path = artifact_path or ARTIFACT_PATH_MAP.get(dep_key)
    if not artifact_path:
        return None, "no artifact path configured for this app"
    artifact_ext = artifact_ext or ARTIFACT_FILE_EXT
    artifact_id = artifact_path.rsplit("/", 1)[-1]

    props_url = (
        f"{ARTIFACTORY_BASE}/api/storage/{MAVEN_REPO}/{artifact_path}/"
        f"{version}/{artifact_id}-{version}.{artifact_ext}?properties"
    )
    data, err = http_get_json(props_url, auth_header)
    if err:
        return None, f"artifact properties lookup failed: {err}"
    props = data.get("properties", {}) or {}

    # Jenkins-era builds stamp the SHA directly on artifact properties.
    sha = (props.get("vcs.revision") or [None])[0]
    if sha:
        return sha, None

    # GitHub Actions builds don't set vcs.revision; chase the build info for GITHUB_SHA.
    build_name = (props.get("build.name") or [None])[0]
    build_number = (props.get("build.number") or [None])[0]
    if not build_name or not build_number:
        return None, "no vcs.revision and no build.name/build.number on artifact"

    build_url = f"{ARTIFACTORY_BASE}/api/build/{build_name}/{build_number}"
    data, err = http_get_json(build_url, auth_header)
    if err:
        return None, f"build info lookup failed: {err}"
    bi_props = (data.get("buildInfo", {}) or {}).get("properties", {}) or {}
    sha = bi_props.get("buildInfo.env.GITHUB_SHA") or bi_props.get("buildInfo.env.GITHUB_WORKFLOW_SHA")
    if not sha:
        return None, "GITHUB_SHA not present in build info"
    return sha, None


def origin_slug(repo: Path):
    """Return 'owner/repo' from the origin remote of a git repo, or None."""
    r = git(repo, "remote", "get-url", "origin")
    if r.returncode != 0:
        return None
    m = REMOTE_SLUG_RE.search(r.stdout.strip())
    return m.group(1).lower() if m else None


def discover_repos(root: Path):
    """Scan immediate children of root for git repos; return {slug: Path}."""
    found = {}
    if not root.exists():
        return found
    for child in root.iterdir():
        if not child.is_dir() or not (child / ".git").exists():
            continue
        slug = origin_slug(child)
        if slug:
            found.setdefault(slug, child)
    return found


def fetch_repo(repo: Path):
    """Fetch origin without touching working tree. Returns (ok, message)."""
    env = {"GIT_TERMINAL_PROMPT": "0"}
    r = subprocess.run(
        ["git", "-C", str(repo), "fetch", "--quiet", "--tags", "--prune", "origin"],
        capture_output=True, text=True, env={**__import__("os").environ, **env},
    )
    if r.returncode == 0:
        return True, None
    return False, (r.stderr.strip() or "fetch failed").splitlines()[0]


def resolve_ref(repo: Path, version: str, dep_key: str = None, auth_header: str = None,
               artifact_path: str = None, artifact_ext: str = None):
    """Return (ref, warning) for a version string.

    Resolution order:
      1. Artifactory build info (exact SHA per published build) — when auth + dep_key supplied.
      2. Local git tag matching the version.
      3. Tip of `release/<major>.<minor>` (may misreport patch version differences).
    """
    if auth_header and dep_key:
        sha, err = resolve_sha_via_artifactory(dep_key, version, auth_header,
                                               artifact_path=artifact_path, artifact_ext=artifact_ext)
        if sha:
            if git(repo, "rev-parse", "--verify", sha).returncode == 0:
                return sha, None
            return None, f"got SHA {sha[:12]} from Artifactory but it's not in the local repo (try `git fetch` in {repo.name})"
        artifactory_note = f"Artifactory lookup unavailable ({err})"
    else:
        artifactory_note = None

    for tag in (version, f"v{version}"):
        if git(repo, "rev-parse", "--verify", f"refs/tags/{tag}").returncode == 0:
            return tag, None
    pv = parse_version(version)
    if pv:
        for br in (f"origin/release/{pv[0]}.{pv[1]}", f"release/{pv[0]}.{pv[1]}"):
            if git(repo, "rev-parse", "--verify", br).returncode == 0:
                msg = f"no tag '{version}'; using tip of {br} (may misreport patch differences)"
                if artifactory_note:
                    msg = f"{artifactory_note}; {msg}"
                return br, msg
    return None, f"could not resolve version '{version}'"


def commits_between(repo: Path, base_ref: str, head_ref: str):
    r = git(repo, "log", "--no-merges", "--pretty=%H%x09%s", f"{base_ref}..{head_ref}")
    if r.returncode != 0:
        return None, r.stderr.strip()
    out = []
    for line in r.stdout.splitlines():
        if "\t" in line:
            sha, subject = line.split("\t", 1)
            out.append((sha, subject))
    return out, None


# Matches version-like values: at least one dot with digits on both sides,
# optional pre-release suffix (e.g. "1.3-SNAPSHOT", "2.0.0-beta01").
_VERSION_VALUE_RE = re.compile(r"^\d+\.\d[\w.\-]*$")


def _parse_toml_versions(content: str) -> dict:
    """Extract {key: version} from the [versions] section of a libs.versions.toml."""
    versions = {}
    in_versions = False
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            in_versions = stripped == "[versions]"
            continue
        if not in_versions or not stripped or stripped.startswith("#"):
            continue
        # Strip inline comment before parsing
        stripped = re.sub(r"\s*#.*$", "", stripped)
        if "=" not in stripped:
            continue
        k, _, v = stripped.partition("=")
        v = v.strip().strip('"')
        if v:
            versions[k.strip()] = v
    return versions


def _parse_gradle_prop_versions(content: str) -> dict:
    """Extract version-like entries from a gradle.properties file."""
    versions = {}
    for line in content.splitlines():
        s = line.strip()
        if not s or s.startswith(("#", "!")) or "=" not in s:
            continue
        k, _, v = s.partition("=")
        k, v = k.strip(), v.strip()
        # Skip known non-version gradle/android settings
        if "." in k:
            continue
        if _VERSION_VALUE_RE.match(v):
            versions[k] = v
    return versions


def read_library_versions_at_ref(repo: Path, ref: str):
    """Return ({key: version}, source_label) at the given git ref.

    Tries gradle/libs.versions.toml first, then gradle.properties.
    Returns ({}, None) if neither file is present at that ref.
    """
    for path, parser, label in (
        ("gradle/libs.versions.toml", _parse_toml_versions, "libs.versions.toml"),
        ("gradle.properties", _parse_gradle_prop_versions, "gradle.properties"),
    ):
        r = git(repo, "show", f"{ref}:{path}")
        if r.returncode == 0 and r.stdout.strip():
            return parser(r.stdout), label
    return {}, None


def diff_library_versions(base_versions: dict, head_versions: dict):
    """Return list of (key, old_ver, new_ver) for entries that changed or were added."""
    changes = []
    all_keys = sorted(set(base_versions) | set(head_versions))
    for k in all_keys:
        old = base_versions.get(k)
        new = head_versions.get(k)
        if old != new:
            changes.append((k, old, new))
    return changes


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("blueprint", help="Target blueprint name (e.g. WRN_2_17_Android)")
    p.add_argument("--baseline", help="Baseline blueprint (default: previous in family)")
    p.add_argument("--projects-root", help="Directory containing app repos (default: parent of dmm-tools)")
    p.add_argument("--verbose", "-v", action="store_true", help="Show commit subjects per ticket")
    p.add_argument("--no-links", action="store_true", help="Disable terminal hyperlinks on ticket keys")
    p.add_argument("--no-fetch", action="store_true", help="Skip `git fetch` in app repos (use local refs as-is)")
    args = p.parse_args()

    links_enabled = sys.stdout.isatty() and not args.no_links

    if DMM_TOOLS_PATH:
        dmm_tools = Path(DMM_TOOLS_PATH).expanduser().resolve()
        if not (dmm_tools / "dependencies").exists():
            sys.exit(
                f"error: DMM_TOOLS_PATH points to {dmm_tools}, which is not a valid dmm-tools clone\n"
                f"       (no `dependencies/` directory found there).\n"
                f"\n"
                f"Open {Path(__file__).resolve()} and update DMM_TOOLS_PATH near the top to\n"
                f"the absolute path of your local dmm-tools clone — e.g.\n"
                f'       DMM_TOOLS_PATH = "/Users/yourname/PP_Projects/dmm-tools"'
            )
    else:
        dmm_tools = Path(__file__).resolve().parent.parent
        if not (dmm_tools / "dependencies").exists():
            sys.exit(
                "error: DMM_TOOLS_PATH is not set and this script is not inside dmm-tools/scripts/.\n"
                "\n"
                f"Open {Path(__file__).resolve()} and set DMM_TOOLS_PATH near the top to\n"
                "the absolute path of your local dmm-tools clone — e.g.\n"
                '       DMM_TOOLS_PATH = "/Users/yourname/PP_Projects/dmm-tools"'
            )

    dep_dirs = [dmm_tools / "dependencies", dmm_tools / "archive" / "dependencies"]
    projects_root = Path(args.projects_root) if args.projects_root else dmm_tools.parent

    target_file = resolve_dep_file(dep_dirs, args.blueprint)
    if not target_file:
        sys.exit(f"error: dependency file '{args.blueprint}.json' not found in {[str(d) for d in dep_dirs]}")

    baseline_name = args.baseline or find_previous_blueprint(dep_dirs, args.blueprint)
    if not baseline_name:
        sys.exit(f"error: could not infer baseline for {args.blueprint}; pass --baseline")
    baseline_file = resolve_dep_file(dep_dirs, baseline_name)
    if not baseline_file:
        sys.exit(f"error: baseline dependency file '{baseline_name}.json' not found in {[str(d) for d in dep_dirs]}")

    target_deps = json.loads(target_file.read_text())
    baseline_deps = json.loads(baseline_file.read_text())

    artifactory_auth, auth_source = load_artifactory_auth()

    print(f"Target:   {args.blueprint}")
    print(f"Baseline: {baseline_name}")
    if artifactory_auth:
        print(f"Artifactory: ON — resolving exact SHA per published build (auth: {auth_source})")
    else:
        print("Artifactory: OFF — set artifactory_user/artifactory_password in ~/.gradle/gradle.properties "
              "(or export $ARTIFACTORY_TOKEN) for exact SHA resolution")
    print()

    slug_to_repo = {k.lower(): v for k, v in discover_repos(projects_root).items()}

    if not args.no_fetch:
        needed_slugs = sorted({
            REPO_MAP[k].lower() for k in target_deps
            if k in REPO_MAP and REPO_MAP[k] and REPO_MAP[k].lower() in slug_to_repo
        })
        if needed_slugs:
            print(f"Fetching {len(needed_slugs)} repo(s)... (use --no-fetch to skip)")
            for slug in needed_slugs:
                repo = slug_to_repo[slug]
                ok, err = fetch_repo(repo)
                status = "ok" if ok else f"FAILED: {err}"
                print(f"  {slug} ({repo.name}): {status}")
            print()

    # Group changed deps by (slug, baseline_version, target_version) so that keys sharing
    # the same repo and version transition (e.g. system-ops + zygote) print as one section.
    from collections import defaultdict
    groups = defaultdict(list)   # (slug, baseline_ver, target_ver) -> [key, ...]
    ungrouped = []               # (key, baseline_ver, target_ver) for keys with no repo

    for key, target_version in target_deps.items():
        baseline_version = baseline_deps.get(key)
        if baseline_version == target_version:
            continue
        slug = REPO_MAP.get(key) if key in REPO_MAP else "__unknown__"
        if slug and slug != "__unknown__":
            groups[(slug, baseline_version or "", target_version)].append(key)
        else:
            ungrouped.append((key, baseline_version, target_version, slug))

    all_tickets = set()

    for (slug, baseline_version, target_version), keys in groups.items():
        baseline_version = baseline_version or None
        label = " + ".join(keys)
        header = f"## {label}: {baseline_version or '(new)'} -> {target_version}"
        print(header)

        if baseline_version is None:
            print("  app is new in target; skipping ticket diff")
            print()
            continue

        slug_resolved = slug  # already a real slug here
        if slug_resolved is None:
            print("  external dependency; no local repo")
            print()
            continue

        repo = slug_to_repo.get(slug_resolved.lower())
        if repo is None:
            print(f"  WARN: no local clone of {slug_resolved} found under {projects_root}")
            print()
            continue

        key = keys[0]  # use first key for Artifactory lookup
        base_ref, base_warn = resolve_ref(repo, baseline_version, dep_key=key, auth_header=artifactory_auth)
        head_ref, head_warn = resolve_ref(repo, target_version, dep_key=key, auth_header=artifactory_auth)
        for w in (base_warn, head_warn):
            if w:
                print(f"  WARN: {w}")

        if not base_ref or not head_ref:
            print("  could not resolve both refs; skipping")
            print()
            continue

        commits, err = commits_between(repo, base_ref, head_ref)
        if err:
            print(f"  git error: {err}")
            print()
            continue

        tickets = {}
        for sha, subject in commits:
            for t in set(TICKET_RE.findall(subject)):
                tickets.setdefault(t, []).append(subject)
                all_tickets.add(t)

        if not tickets:
            print(f"  ({len(commits)} commits, no ticket references found)")
        else:
            print(f"  ({len(commits)} commits, {len(tickets)} unique tickets)")
            for t in sorted(tickets):
                linked = hyperlink(t, f"{JIRA_BASE_URL}{t}", links_enabled)
                print(f"  - {linked}")
                if args.verbose:
                    seen = set()
                    for s in tickets[t]:
                        if s not in seen:
                            print(f"      {s}")
                            seen.add(s)

        base_lib_versions, base_lib_source = read_library_versions_at_ref(repo, base_ref)
        head_lib_versions, head_lib_source = read_library_versions_at_ref(repo, head_ref)
        source_mismatch = base_lib_source and head_lib_source and base_lib_source != head_lib_source
        if source_mismatch:
            base_items = sorted(base_lib_versions.items())
            head_items = sorted(head_lib_versions.items())
            if base_items or head_items:
                print(f"\n  Library versions (source changed: {base_lib_source} -> {head_lib_source}):")
                lk_w = max((len(k) for k, _ in base_items), default=0)
                lv_w = max((len(v) for _, v in base_items), default=0)
                rk_w = max((len(k) for k, _ in head_items), default=0)
                col_w = lk_w + lv_w + 4
                header_l = f"Baseline ({base_lib_source})"
                header_r = f"Target ({head_lib_source})"
                print(f"    {header_l:<{col_w}}  {header_r}")
                print(f"    {'-' * col_w}  {'-' * (rk_w + max((len(v) for _, v in head_items), default=0) + 4)}")
                for (lk, lv), (rk, rv) in zip(base_items, head_items):
                    left = f"{lk:<{lk_w}}  {lv}"
                    print(f"    {left:<{col_w}}  {rk:<{rk_w}}  {rv}")
                for (lk, lv) in base_items[len(head_items):]:
                    print(f"    {lk:<{lk_w}}  {lv}")
                for (rk, rv) in head_items[len(base_items):]:
                    print(f"    {'':<{col_w}}  {rk:<{rk_w}}  {rv}")
        else:
            lib_changes = diff_library_versions(base_lib_versions, head_lib_versions)
            if lib_changes:
                source_label = head_lib_source or base_lib_source or "unknown"
                print(f"\n  Library version changes ({source_label}):")
                key_width = max(len(k) for k, _, _ in lib_changes)
                for lib_key, old_ver, new_ver in lib_changes:
                    old_str = old_ver if old_ver is not None else "(new)"
                    new_str = new_ver if new_ver is not None else "(removed)"
                    print(f"    {lib_key:<{key_width}}  {old_str}  ->  {new_str}")

                    lib_info = LIBRARY_VERSION_REPO_MAP.get(lib_key)
                    if not lib_info or old_ver is None or new_ver is None:
                        continue
                    lib_repo = slug_to_repo.get(lib_info["slug"].lower())
                    if lib_repo is None:
                        print(f"      (no local clone of {lib_info['slug']} found; skipping ticket diff)")
                        continue
                    lib_base_ref, lib_base_warn = resolve_ref(
                        lib_repo, old_ver, dep_key=lib_key, auth_header=artifactory_auth,
                        artifact_path=lib_info["artifact_path"], artifact_ext=lib_info["artifact_ext"],
                    )
                    lib_head_ref, lib_head_warn = resolve_ref(
                        lib_repo, new_ver, dep_key=lib_key, auth_header=artifactory_auth,
                        artifact_path=lib_info["artifact_path"], artifact_ext=lib_info["artifact_ext"],
                    )
                    for w in (lib_base_warn, lib_head_warn):
                        if w:
                            print(f"      WARN: {w}")
                    if not lib_base_ref or not lib_head_ref:
                        print(f"      could not resolve both refs; skipping ticket diff")
                        continue
                    lib_commits, lib_err = commits_between(lib_repo, lib_base_ref, lib_head_ref)
                    if lib_err:
                        print(f"      git error: {lib_err}")
                        continue
                    lib_tickets = {}
                    for sha, subject in lib_commits:
                        for t in set(TICKET_RE.findall(subject)):
                            lib_tickets.setdefault(t, []).append(subject)
                            all_tickets.add(t)
                    if not lib_tickets:
                        print(f"      ({len(lib_commits)} commits, no ticket references found)")
                    else:
                        print(f"      ({len(lib_commits)} commits, {len(lib_tickets)} unique tickets)")
                        for t in sorted(lib_tickets):
                            linked = hyperlink(t, f"{JIRA_BASE_URL}{t}", links_enabled)
                            print(f"      - {linked}")
                            if args.verbose:
                                seen = set()
                                for s in lib_tickets[t]:
                                    if s not in seen:
                                        print(f"          {s}")
                                        seen.add(s)

        print()

    for key, baseline_version, target_version, slug in ungrouped:
        print(f"## {key}: {baseline_version or '(new)'} -> {target_version}")
        if slug == "__unknown__":
            print(f"  WARN: unknown dependency key '{key}'; add to REPO_MAP")
        else:
            print("  external dependency; no local repo")
        print()

    print(f"Total unique tickets across all apps: {len(all_tickets)}")


if __name__ == "__main__":
    main()
