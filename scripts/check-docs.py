"""Check public documentation links and the paths used by the Codex marketplace."""
import json
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1]


def check_links(document):
    for link in re.findall(r"\]\(([^)\s]+)\)", document.read_text(encoding="utf-8")):
        url = urlsplit(link)
        if url.scheme or url.netloc or not url.path:
            continue
        path = (document.parent / unquote(url.path)).resolve()
        if not path.is_relative_to(ROOT) or not path.exists():
            raise ValueError(f"{document.relative_to(ROOT)}: missing local target {link}")


def main():
    docs = [ROOT / filename for filename in ("README.md", "QUICKSTART.md", "CONTRIBUTING.md", "SECURITY.md", "SUPPORT.md")]
    marketplace = json.loads((ROOT / ".agents/plugins/marketplace.json").read_text())
    for item in marketplace["plugins"]:
        package = (ROOT / item["source"]["path"]).resolve()
        if not package.is_relative_to(ROOT / "plugins"):
            raise ValueError(f"Plugin outside active packages: {package}")
        manifest = json.loads((package / ".codex-plugin/plugin.json").read_text())
        if manifest["name"] != item["name"]:
            raise ValueError(f"Marketplace and manifest disagree about {package.name}")
        skills = list((package / manifest["skills"]).glob("*/SKILL.md"))
        if not skills:
            raise ValueError(f"No skills in {package.name}")
        docs.extend(skills)
        docs.extend(package.glob("skills/*/references/*.md"))
    for document in docs:
        check_links(document)
    print(f"Checked paths for {len(marketplace['plugins'])} packages and {len(docs)} documents.")


if __name__ == "__main__":
    main()
