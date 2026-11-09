"""Instagram: the link classifier, the info panel's source links, the ban and
de-duplication lists and the download command, out of a STAGED server.py's AST;
the file name template through gallery-dl's own formatter.

  venv/bin/python instagram_check.py <staged server.py>

image_dedup.py is read from beside the staged server.py. On a server.py without
the feature the checks FAIL.
"""
import ast
import os
import re
import sys

SERVER = os.path.abspath(sys.argv[1])
sys.path.insert(0, os.path.dirname(SERVER))
ok = True


def check(label, cond, detail=""):
    global ok
    ok = ok and bool(cond)
    print(("PASS " if cond else "FAIL ") + label + (f"  ({detail})" if detail and not cond else ""), flush=True)


src = open(SERVER, encoding="utf-8").read()
tree = ast.parse(src)
funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
ns = {"re": re, "download_category_of": lambda root, rel: rel.split("/")[0],
      "pixiv_image_id_for": lambda name: None}
for n in tree.body:
    if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") in (
            "_TWITTER_POST_IN_NAME", "_PIXIV_ARTIST_FOLDER", "_INSTAGRAM_POST_IN_NAME",
            "GALLERY_DL_CATEGORIES", "ALL_CATEGORIES"):
        exec(compile(ast.Module([n], []), "s", "exec"), ns)
for f in ("classify_url", "source_links_for"):
    exec(compile(ast.Module([funcs[f]], []), "s", "exec"), ns)

c = ns["classify_url"]
for url, want in (("https://www.instagram.com/some_user/", "instagram"),
                  ("https://instagram.com/p/AbC-_d1/", "instagram"),
                  ("https://www.instagram.com/reel/AbC/", "instagram"),
                  ("https://instagr.am/p/AbC/", "instagram"),
                  ("https://www.instagram.com/max.com/", "instagram"),
                  ("https://x.com/someone", "twitter"), ("@someone", "twitter"),
                  ("https://www.youtube.com/watch?v=abc", "youtube")):
    check(f"classify {url}", c(url) == want, c(url))

check("in ALL_CATEGORIES", "instagram" in ns.get("ALL_CATEGORIES", ()))
check("banned by tombstone", "instagram" in ns.get("GALLERY_DL_CATEGORIES", ()))
import image_dedup  # noqa: E402
check("image dedup covers it", "instagram" in image_dedup.CATEGORIES)
check("the post id groups a post", image_dedup.post_token("instagram/@a/3456_2_AbC-_d.jpg") == "3456")


def links(rel):
    try:
        return ns["source_links_for"]("downloads", rel)
    except Exception as e:  # an unpatched server.py falls into the pixiv branch
        return {"error": repr(e)}


out = links("instagram/@some.user_1/3456_2_AbC-_d.jpg") or {}
check("artist link", out.get("artist_url") == "https://www.instagram.com/some.user_1/", out)
check("artist name", out.get("artist_name") == out.get("artist_id") == "@some.user_1", out)
check("post link", out.get("post_url") == "https://www.instagram.com/p/AbC-_d/", out)
check("a name that does not fit has no post link",
      (links("instagram/@a/x.jpg") or {}).get("artist_id") == "@a" and "post_url" not in links("instagram/@a/x.jpg"))
check("a folder that does not fit has no links", links("instagram/not a handle/1_1_a.jpg") is None)
check("twitter unchanged", (links("twitter/@a/123456_1.jpg") or {}).get("post_url") == "https://x.com/a/status/123456")

body = ast.get_source_segment(src, funcs["execute_download_task"])
m = re.search(r'elif category == "instagram":.*?elif category == "deviantart"', body, re.S)
branch = m.group(0) if m else ""
check("refuses without cookies, before any command",
      -1 < branch.find("cookies_instagram_path)") < branch.find("cmd = ["))
check("passes the cookies", '"--cookies", cookies_instagram_path' in branch)
t = re.search(r'"filename=([^"]+)"', branch)
from gallery_dl import formatter  # noqa: E402
meta = {"post_id": 3456789012345678901, "num": 2, "post_shortcode": "AbC-_d", "extension": "mp4", "username": "a"}
name = formatter.parse(t.group(1) if t else "x").format_map(meta)
check("file name", name == "3456789012345678901_2_AbC-_d.mp4", name)
check("the name yields the post link",
      (links("instagram/@a/" + name) or {}).get("post_url") == "https://www.instagram.com/p/AbC-_d/")
print("ALL PASS" if ok else "SOME CHECKS FAILED")
sys.exit(0 if ok else 1)
