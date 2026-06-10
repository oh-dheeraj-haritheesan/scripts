#!/usr/bin/env python3
"""
what-shipped.py — list Jira tickets that went into a blueprint release.

Reads the target blueprint's dependency manifest, compares it against a baseline
(by default the numerically previous blueprint in the same product family), and
for each app whose pinned version changed, walks the sibling app repo's git log
between the two versions and extracts ticket keys from commit subjects.

Patch resolution is best-effort: tries an exact tag match first, falls back to
the tip of `release/<major>.<minor>` and warns. Run `git fetch` in the relevant
repos first if your local refs may be stale.

Usage:
  scripts/what-shipped.py WRN_2_17_Android
  scripts/what-shipped.py WRN_2_17_Android --baseline WRN_2_15_Android
  scripts/what-shipped.py WRN_2_17_Android --verbose
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

ARTIFACTORY_BASE = os.environ.get("ARTIFACTORY_URL", "https://outcomehealth.jfrog.io/artifactory").rstrip("/")
MAVEN_REPO = "tfm-maven"
ARTIFACT_FILE_EXT = "zip"

# >>> EDIT THIS <<<
# Absolute path to your local dmm-tools clone (the repo containing `dependencies/`).
# Example: "/Users/yourname/PP_Projects/dmm-tools"
# App repos (android-system-ops, wrn2, ipr2, ...) are assumed to be siblings of this path.
# Leave as None to fall back to this script's parent directory (works only if this file
# lives inside dmm-tools/scripts/).
DMM_TOOLS_PATH = None

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


def resolve_sha_via_artifactory(dep_key: str, version: str, auth_header: str):
    """Return (sha, warning) for a published artifact version via Artifactory."""
    artifact_path = ARTIFACT_PATH_MAP.get(dep_key)
    if not artifact_path:
        return None, "no artifact path configured for this app"
    artifact_id = artifact_path.rsplit("/", 1)[-1]

    props_url = (
        f"{ARTIFACTORY_BASE}/api/storage/{MAVEN_REPO}/{artifact_path}/"
        f"{version}/{artifact_id}-{version}.{ARTIFACT_FILE_EXT}?properties"
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


def resolve_ref(repo: Path, version: str, dep_key: str = None, auth_header: str = None):
    """Return (ref, warning) for a version string.

    Resolution order:
      1. Artifactory build info (exact SHA per published build) — when auth + dep_key supplied.
      2. Local git tag matching the version.
      3. Tip of `release/<major>.<minor>` (may misreport patch version differences).
    """
    if auth_header and dep_key:
        sha, err = resolve_sha_via_artifactory(dep_key, version, auth_header)
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
    else:
        dmm_tools = Path(__file__).resolve().parent.parent
        if not (dmm_tools / "dependencies").exists():
            sys.exit(
                "error: DMM_TOOLS_PATH is not set and this script is not inside dmm-tools/scripts/.\n"
                "Open this file and set DMM_TOOLS_PATH near the top to the absolute path of your\n"
                "local dmm-tools clone (e.g. \"/Users/you/PP_Projects/dmm-tools\")."
            )
    if not (dmm_tools / "dependencies").exists():
        sys.exit(f"error: {dmm_tools} does not look like a dmm-tools clone (no `dependencies/` directory).")

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

    all_tickets = set()
    for key, target_version in target_deps.items():
        baseline_version = baseline_deps.get(key)
        if baseline_version == target_version:
            continue

        header = f"## {key}: {baseline_version or '(new)'} -> {target_version}"
        print(header)

        if baseline_version is None:
            print("  app is new in target; skipping ticket diff")
            print()
            continue

        if key not in REPO_MAP:
            print(f"  WARN: unknown dependency key '{key}'; add to REPO_MAP")
            print()
            continue
        slug = REPO_MAP[key]
        if slug is None:
            print("  external dependency; no local repo")
            print()
            continue

        repo = slug_to_repo.get(slug.lower())
        if repo is None:
            print(f"  WARN: no local clone of {slug} found under {projects_root}")
            print()
            continue

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
        print()

    print(f"Total unique tickets across all apps: {len(all_tickets)}")


if __name__ == "__main__":
    main()
