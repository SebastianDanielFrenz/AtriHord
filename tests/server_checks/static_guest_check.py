"""serve_static's gate for signed-in non-admins (guests, users) and anonymous visitors, out of a
STAGED server.py's AST over a temp web_files tree: every signed-in account gets the front end's
scripts (js/ incl. js/vendor/ - the media browser's and the VR viewer's modules), still gets the
redirect to /media.html for admin pages and the pages not on the list, a path trick through the
js/ prefix (.. / backslash / non-.js) gets nothing, and an anonymous visitor still gets only the
public share's scripts and css.

P8 V0: /vr.html (the standalone VR entry page) is on GUEST_ALLOWED_FILES, so every signed-in account
gets it, js/vr/*.js come with the js/ rule, and anonymous visitors still get the login redirect.

  venv/bin/python static_guest_check.py /tmp/.../server.py     (via writeguard.py in run_all.sh)
"""
import ast
import os
import shutil
import sys
import tempfile

BASE = os.environ.get("ATRIHORD_DIR") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SERVER = os.path.abspath(sys.argv[1])
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.dirname(SERVER))
tmp = tempfile.mkdtemp(prefix="static_guest_check_")
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append(bool(ok))
    print(("PASS " if ok else "FAIL ") + name + (f" - {detail}" if detail and not ok else ""))


tree = ast.parse(open(SERVER, encoding="utf-8").read())
defs = {}
ns = {"__name__": "harness", "__file__": os.path.join(BASE, "server.py")}
for node in tree.body:
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        try:
            exec(compile(ast.Module(body=[node], type_ignores=[]), SERVER, "exec"), ns)
        except Exception:
            pass
    elif isinstance(node, (ast.FunctionDef, ast.ClassDef)):
        defs[node.name] = node
    elif isinstance(node, ast.Assign):
        for t in node.targets:
            if isinstance(t, ast.Name):
                defs.setdefault(t.id, node)


def load(name):
    code = compile(ast.Module(body=[defs[name]], type_ignores=[]), SERVER, "exec")
    exec(code, ns)


WEB = os.path.join(tmp, "web_files")
files = {"index.html": "admin", "settings.html": "admin", "media.html": "m", "music.html": "m", "genres.html": "g",
         "music_share.html": "s", "public_share.html": "p", "js/app.js": "a", "js/settings.js": "s",
         "js/media.js": "m", "js/stereo3d.js": "3", "js/media-select.js": "ms", "js/vr-viewer.js": "v",
         "js/vr-logic.js": "l", "vr.html": "vr", "js/vr/app.js": "va", "js/vr/host.js": "vh", "js/vr/page.js": "vp", "js/vendor/three.module.min.js": "t", "js/vendor/three.LICENSE.txt": "lic",
         "js/media.js.bak_sourceid": "bak", "js/public_share.js": "ps", "css/main.css": "c", "downloads/x.jpg": "d"}
for rel, text in files.items():
    full = os.path.join(WEB, rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    open(full, "w").write(text)

for n in ("app", "GUEST_ALLOWED_FILES", "guest_script_ok", "media_page_response",
          "FILTER_OPTIONS_PLACEHOLDER", "serve_static", "is_admin"):
    load(n)
ns["WEB_FILES_DIR"] = WEB
USER = {"cur": None}
ns["get_authenticated_user"] = lambda: USER["cur"]
ns["is_music_host"] = lambda: False
ns["static_filter_options"] = lambda: {}
ns["login_redirect_for"] = lambda url: ns["redirect"]("/login-elsewhere")
ns["send_from_directory"] = lambda d, p: ns["app"].response_class(open(os.path.join(d, p)).read(), 200)
app = ns["app"]


def get(filename, user):
    USER["cur"] = user
    with app.test_request_context("/" + filename):
        resp = ns["serve_static"](filename)
        if isinstance(resp, tuple):
            return resp[1], None
        return resp.status_code, resp.headers.get("Location")


GUEST = {"username": "g", "role": "guest"}
USERR = {"username": "u", "role": "user"}
ADMIN = {"username": "a", "role": "admin"}
SCRIPTS = ["js/stereo3d.js", "js/media-select.js", "js/vr-viewer.js", "js/vr-logic.js",
           "js/vendor/three.module.min.js", "js/media.js", "js/public_share.js",
           "js/vr/app.js", "js/vr/host.js", "js/vr/page.js"]
for who, u in (("guest", GUEST), ("user", USERR)):
    check("%s: every front-end script incl. js/vendor/ is 200" % who, all(get(s, u)[0] == 200 for s in SCRIPTS),
          [(s, get(s, u)) for s in SCRIPTS if get(s, u)[0] != 200])
    check("%s: the listed pages and css/downloads still work" % who,
          all(get(s, u)[0] == 200 for s in ("media.html", "music.html", "genres.html", "css/main.css", "downloads/x.jpg")))
    check("%s: vr.html (the standalone VR page) is served" % who, get("vr.html", u)[0] == 200, get("vr.html", u))
    check("%s: vr.html's look-alikes are not: another page, a path trick and a directory" % who,
          all(get(s, u) == (302, "/media.html") for s in ("vr.htm", "vr.html/", "VR.html", "js/vr.html", "vr")),
          [(s, get(s, u)) for s in ("vr.htm", "vr.html/", "VR.html", "js/vr.html", "vr")])
    check("%s: admin HTML pages are still redirected to /media.html" % who,
          all(get(s, u) == (302, "/media.html") for s in ("index.html", "settings.html", "music_share.html", "public_share.html")),
          [(s, get(s, u)) for s in ("index.html", "settings.html")])
    check("%s: a .bak, a .txt or a non-js file under js/ is not served" % who,
          all(get(s, u) == (302, "/media.html") for s in ("js/media.js.bak_sourceid", "js/vendor/three.LICENSE.txt", "js/")))
    tricks = ["js/../settings.html", "js/vendor/../../settings.html", "js/../index.html", "js/./media.js",
              "js//media.js", "js\\..\\settings.html", "js/..%2fsettings.html", "js/../js/../settings.html",
              "js/../../server.py"]
    got = [(t, get(t, u)) for t in tricks]
    check("%s: path tricks through the js/ prefix get nothing (redirect, never the file)" % who,
          all(r[0] == 302 for _, r in got), got)
check("guest_script_ok is a pure path rule", ns["guest_script_ok"]("js/a.js") and not ns["guest_script_ok"]("a.js")
      and not ns["guest_script_ok"]("css/a.js") and not ns["guest_script_ok"]("js/a.html") and not ns["guest_script_ok"]("js/../a.js"))
check("admin: everything incl. admin pages, scripts and vendor, and vr.html",
      all(get(s, ADMIN)[0] == 200 for s in ("index.html", "settings.html", "js/app.js", "js/vendor/three.module.min.js", "vr.html")))
check("anonymous: the VR scripts, vendor and admin pages get the login redirect, nothing served",
      all(get(s, None) == (302, "/login-elsewhere") for s in
          ("js/vr-viewer.js", "js/vr-logic.js", "js/stereo3d.js", "js/vendor/three.module.min.js", "js/media-select.js",
           "index.html", "settings.html", "media.html", "js/app.js", "vr.html", "js/vr/app.js")))
check("anonymous: the public share's own scripts and css are unchanged",
      all(get(s, None)[0] == 200 for s in ("js/public_share.js", "css/main.css")))
shutil.rmtree(tmp, ignore_errors=True)
print("ALL PASS" if all(RESULTS) else f"{RESULTS.count(False)} FAILED")
