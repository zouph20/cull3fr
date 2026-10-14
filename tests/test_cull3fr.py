"""cull3fr 回归测试。

    python3 tests/test_cull3fr.py              # 全部
    python3 tests/test_cull3fr.py <测试名> ...  # 只跑这几个

不需要任何真实照片或 .phos：.phos 用程序里内置的 Phocus 基线现场生成，3FR / HEIC 用很小的假文件。
浏览器测试需要 playwright（没装会跳过）。设置 CULL3FR_SAMPLES=<放真实 X2D II 3FR / HEIC / .phos 的文件夹> 会多跑一项真实文件测试（只在本机读，不会上传）。
"""
import base64, importlib.util, json, os, plistlib, shutil, struct, sys, tempfile, threading, time, traceback
import urllib.request, urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.environ.get("CULL3FR_SCRIPT") or os.path.join(HERE, "..", "cull3fr.py")
SAMPLES = os.environ.get("CULL3FR_SAMPLES")
spec = importlib.util.spec_from_file_location("hb", SCRIPT)
hb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hb)
from http.server import ThreadingHTTPServer

TINY_JPEG = base64.b64decode("/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAA0JCgsKCA0LCgsODg0PEyAVExISEyccHhcgLikxMC4pLSwzOko+MzZGNywtQFdBRkxOUlNSMj5aYVpQYEpRUk//2wBDAQ4ODhMREyYVFSZPNS01T09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT0//wAARCAAwAEADASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDk6KKK6jAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigD//2Q==")
XMP0 = (b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        b'<rdf:Description rdf:about="" xmlns:xap="http://ns.adobe.com/xap/1.0/"><xap:Rating>0</xap:Rating>'
        b'</rdf:Description></rdf:RDF></x:xmpmeta>')


class Skip(Exception):
    pass


def phos_file(kind="3fr", approval=1, rating=None, edited=False):
    """按 Phocus 4.x 的格式现场生成一个 .phos。edited=True 时多一条「调过参数」的记录（CurrentIx 1）。"""
    tpl = hb.embedded_template(kind)
    s0 = tpl["ImageSettings0"]
    sets = [s0]
    if edited:
        s1 = dict(s0, Action=3, Name="")
        s1["ImageCorrection"] = dict(s0["ImageCorrection"], ColorTemperature=5000, Tint=-0.007, ShadowFill=10)
        s1["Created"] = dict(s0["Created"], Year=2026, Month=1, Day=1, Hour=12, GMT=0)
        sets.append(s1)
    d = {"Approval": approval, "CurrentIx": len(sets) - 1, "GUID": "00000000-0000-4000-8000-000000000001",
         "ImageParameterCache": tpl["ImageParameterCache"], "ImageSettings": sets, "Version": tpl["Version"]}
    if rating is not None:
        d["XMPData"] = hb.PHOCUS_XMP % rating
    return hb.dumps_phocus(d)


def put(path, data):
    with open(path, "wb") as f:
        f.write(data)


# 三种常见状态
PHOS_RAW_RED = lambda: phos_file("3fr", 2, None, True)          # 3FR：红，没打星，调过参数
PHOS_RAW_RED_3 = lambda: phos_file("3fr", 2, 3, True)           # 3FR：红，3 星，调过参数
PHOS_HEIC_GREEN_2 = lambda: phos_file("heic", 0, 2)             # HEIC：绿，2 星


def fake_3fr(path):
    """和 X2D II 3FR 同样的布局：IFD0 是一张内嵌 JPEG（压缩 7）+ XMP（xap:Rating 0）。"""
    entries = [(0x103, 3, 1, 7), (0x111, 4, 1, 0), (0x112, 3, 1, 1), (0x117, 4, 1, len(TINY_JPEG)),
               (0x2BC, 1, len(XMP0), 0)]
    ifd_len = 2 + 12 * len(entries) + 4
    jpg_off = 8 + ifd_len
    xmp_off = jpg_off + len(TINY_JPEG)
    out = b"II*\x00" + struct.pack("<I", 8) + struct.pack("<H", len(entries))
    for tag, typ, cnt, val in entries:
        if tag == 0x111:
            val = jpg_off
        if tag == 0x2BC:
            val = xmp_off
        out += struct.pack("<HHI", tag, typ, cnt) + (struct.pack("<HH", val, 0) if typ == 3 else struct.pack("<I", val))
    out += struct.pack("<I", 0) + TINY_JPEG + XMP0
    with open(path, "wb") as f:
        f.write(out)


def fake_heic(path):
    with open(path, "wb") as f:
        f.write(struct.pack(">I4s4sI", 16, b"ftyp", b"heix", 0))

RESULTS = []
TMP = tempfile.mkdtemp(prefix="cull3fr-test-")


def test(fn):
    def run():
        try:
            fn()
            RESULTS.append((fn.__name__, "PASS", ""))
        except Skip as ex:
            RESULTS.append((fn.__name__, "SKIP", str(ex)))
        except Exception as ex:
            RESULTS.append((fn.__name__, "FAIL", "%s: %s\n%s" % (type(ex).__name__, ex, traceback.format_exc(limit=3))))
    run.__name__ = fn.__name__
    return run


def mkdir(name):
    p = os.path.join(TMP, name)
    shutil.rmtree(p, ignore_errors=True)
    os.makedirs(p)
    return p


def shoot(name, phos=True, later=False):
    """B0000001 / B0000002 两组 3FR + HEIC（假文件）+ Phocus 格式的 .phos。"""
    d = mkdir(name)
    for s in ("B0000001", "B0000002"):
        fake_3fr(os.path.join(d, s + ".3FR"))
        fake_heic(os.path.join(d, s + ".HEIC"))
    if phos:
        put(os.path.join(d, "B0000001.3FR.phos"), PHOS_RAW_RED())
        put(os.path.join(d, "B0000002.3FR.phos"), PHOS_RAW_RED_3())
        put(os.path.join(d, "B0000001.HEIC.phos"), PHOS_HEIC_GREEN_2())
    if later:
        os.makedirs(os.path.join(d, "later"))
        fake_3fr(os.path.join(d, "later", "B0000009.3FR"))
        fake_heic(os.path.join(d, "later", "B0000009.HEIC"))
    return d


def read(p):
    with open(p, "rb") as f:
        return f.read()


def changed_lines(a, b):
    import difflib
    return [l for l in difflib.unified_diff(a.decode().splitlines(), b.decode().splitlines(), lineterm="", n=0)
            if l[:1] in "+-" and l[:3] not in ("+++", "---")]


# ---------------------------------------------------------------- .phos 格式
@test
def phocus_real_number_format():
    # Phocus（Apple plist）写实数的方式：0 写 0.0，整数不带 .0，其他用 17 位有效数字
    out = hb.dumps_phocus({"a": 0.0, "b": 2.0, "c": -0.007, "d": 100.0, "e": 0.5})
    for want in (b"<real>0.0</real>", b"<real>2</real>", b"<real>-0.0070000000000000001</real>",
                 b"<real>100</real>", b"<real>0.5</real>"):
        assert want in out, (want, out)
    for data in (PHOS_RAW_RED(), PHOS_RAW_RED_3(), PHOS_HEIC_GREEN_2()):
        assert hb.dumps_phocus(plistlib.loads(data)) == data


@test
def patch_changes_only_target_values():
    old = PHOS_RAW_RED_3()                          # Approval 2, XMPData 3★
    new = hb.phos_patch(old, rating=5, approval=0)
    ch = changed_lines(old, new)
    assert len(ch) == 4 and "&lt;xap:Rating&gt;5&lt;/xap:Rating&gt;" in "".join(ch), ch
    assert hb.phos_info_bytes(new) == {"approval": 0, "rating": 5}


@test
def patch_adds_xmpdata_exactly_like_phocus():
    old = PHOS_RAW_RED()                            # 打星前
    after_phocus = PHOS_RAW_RED_3()                 # Phocus 打 3 星后：只多一个 XMPData 键
    assert hb.phos_patch(old, rating=3) == after_phocus


@test
def patch_refuses_unknown_format():
    d = plistlib.dumps({"Approval": 1, "X": 1.0})   # 不是 Phocus 的写法（1.0）
    try:
        hb.phos_patch(d, approval=0)
        raise AssertionError("应当拒绝")
    except hb.PhosUnsupported:
        pass


@test
def nested_rating_strings_are_ignored():
    d = plistlib.loads(PHOS_RAW_RED_3())
    d["ImageSettings"][0]["Info"] = "<xap:Rating>1</xap:Rating>"
    raw = hb.dumps_phocus(d)
    assert hb.phos_info_bytes(raw)["rating"] == 3           # 只认顶层 XMPData
    out = plistlib.loads(hb.phos_patch(raw, rating=5))
    assert out["ImageSettings"][0]["Info"] == "<xap:Rating>1</xap:Rating>"


@test
def new_phos_matches_phocus_baseline():
    tpl = hb.embedded_template()
    ref = plistlib.loads(PHOS_RAW_RED())
    n = plistlib.loads(hb.phos_new(tpl, 0, 4))
    assert n["ImageSettings"] == [ref["ImageSettings"][0]] and n["ImageParameterCache"] == ref["ImageParameterCache"]
    assert n["Version"] == 31 and n["Approval"] == 0 and n["CurrentIx"] == 0
    assert n["XMPData"] == plistlib.loads(PHOS_RAW_RED_3())["XMPData"].replace(">3<", ">4<")


# ---------------------------------------------------------------- 会话：写入 / 撤销
@test
def write_and_undo_real_files():
    d = shoot("w1", later=True)
    orig = {f: read(os.path.join(d, f)) for f in os.listdir(d) if f.endswith(".phos")}
    S = hb.Session(d)
    assert [i["key"] for i in S.items] == ["B0000001", "B0000002", "later/B0000009"]
    S.set_mark("B0000001", {"r": 4})
    S.set_mark("B0000002", {"r": 5, "l": "yellow"})
    S.set_mark("later/B0000009", {"l": "green", "r": 3})
    assert S.plan()["files"] == 3
    res = S.write_all(True)
    assert "已写入 3 张" in res["text"], res["text"]
    assert hb.phos_info(os.path.join(d, "B0000001.3FR.phos")) == {"approval": 2, "rating": 4}
    assert hb.phos_info(os.path.join(d, "B0000002.3FR.phos")) == {"approval": 1, "rating": 5}
    assert hb.phos_info(os.path.join(d, "later/B0000009.3FR.phos")) == {"approval": 0, "rating": 3}
    assert S.plan()["files"] == 0
    S.undo()
    for f, b in orig.items():
        assert read(os.path.join(d, f)) == b, f
    assert not os.path.exists(os.path.join(d, "later/B0000009.3FR.phos"))
    assert S.plan()["files"] == 3   # 标记还在，可以再写


@test
def two_quick_writes_keep_separate_backups():      # 评审 M3
    d = shoot("w2")
    first = read(os.path.join(d, "B0000002.3FR.phos"))
    S = hb.Session(d)
    assert S.set_mark("B0000002", {"l": "green"}); S.write_all(True)
    assert S.set_mark("B0000002", {"r": 4}); S.write_all(True)
    assert hb.phos_info(os.path.join(d, "B0000002.3FR.phos")) == {"approval": 0, "rating": 4}
    S.undo(); S.undo()
    assert read(os.path.join(d, "B0000002.3FR.phos")) == first


@test
def externally_changed_phos_is_not_overwritten():
    d = shoot("w3")
    S = hb.Session(d)
    S.set_mark("B0000002", {"l": "green"})
    orig_build = S._build

    def build_then_phocus_touches(t, tpl):
        out = orig_build(t, tpl)
        p = os.path.join(d, "B0000002.3FR.phos")
        data = plistlib.loads(read(p)); data["CurrentIx"] = 0
        open(p, "wb").write(hb.dumps_phocus(data))   # 模拟 Phocus 在中间改了文件
        return out
    S._build = build_then_phocus_touches
    res = S.write_all(True)
    assert "刚被别的程序改过" in res["text"], res["text"]
    assert plistlib.loads(read(os.path.join(d, "B0000002.3FR.phos")))["Approval"] == 2


@test
def unknown_approval_not_reset():                  # 评审 L3
    d = shoot("w4")
    p = os.path.join(d, "B0000002.3FR.phos")
    data = plistlib.loads(read(p)); data["Approval"] = 3
    open(p, "wb").write(hb.dumps_phocus(data))
    S = hb.Session(d)
    S.set_mark("B0000002", {"r": 1})
    S.write_all(True)
    assert hb.phos_info(p) == {"approval": 3, "rating": 1}


@test
def marks_survive_raw_arriving_later():            # 评审 M4
    d = mkdir("late")
    fake_heic(d + "/B0001.HEIC")
    S = hb.Session(d)
    S.set_mark("B0001", {"r": 5, "l": "green"})
    fake_3fr(d + "/B0001.3FR")
    S2 = hb.Session(d)
    it = S2.item_json(S2.by_key["B0001"])
    assert it["r"] == 5 and it["l"] == "green" and it["p"], it


@test
def old_state_keys_are_migrated():
    d = shoot("mig")
    json.dump({"version": 2, "ov": {"B0000001.3FR": {"r": 2}, "x/../../evil.3FR": {"r": 9}, "B0000002.3FR": {"l": "purple"}},
               "written": {}}, open(d + "/.hb_cull.json", "w"))
    S = hb.Session(d)
    assert S.ov == {"B0000001": {"r": 2}}, S.ov


@test
def corrupt_state_file_is_kept_aside():
    d = shoot("bad")
    open(d + "/.cull3fr.json", "w").write("{not json")
    S = hb.Session(d)
    assert S.warn and any(f.startswith(".cull3fr.json.bad-") for f in os.listdir(d))


@test
def legacy_hb_cull_files_are_picked_up():
    d = shoot("legacy")
    json.dump({"version": 2, "ov": {"B0000001.3FR": {"r": 3, "l": "red"}}, "written": {}}, open(d + "/.hb_cull.json", "w"))
    S = hb.Session(d)
    assert S.ov == {"B0000001": {"r": 3, "l": "red"}}, S.ov
    assert os.path.exists(d + "/.cull3fr.json") and os.path.exists(d + "/.hb_cull.json")


@test
def save_failure_reverts_memory():                 # 评审 L8
    d = shoot("sf")
    S = hb.Session(d)
    S._save_state = lambda: (_ for _ in ()).throw(OSError(28, "No space left on device"))
    try:
        S.set_mark("B0000001", {"r": 3})
        raise AssertionError("应当失败")
    except hb.SaveError:
        pass
    assert "B0000001" not in S.ov


@test
def move_with_blocker_logs_and_undoes():           # 评审 L5
    d = shoot("mv")
    open(os.path.join(d, "_red"), "w").write("x")   # 挡路的同名文件
    S = hb.Session(d)
    S.set_mark("B0000001", {"l": "green"}); S.set_mark("B0000002", {"l": "red"})
    res = S.move_to_folders()
    assert "绿 1" in res["text"] and "不是文件夹" in res["text"], res["text"]
    assert os.path.exists(os.path.join(d, "_green", "B0000001.3FR"))
    assert S.ov.get("_green/B0000001") == {"l": "green"}
    S.undo()
    assert os.path.exists(os.path.join(d, "B0000001.3FR")) and not os.path.exists(os.path.join(d, "_green"))
    assert S.ov.get("B0000001") == {"l": "green"}


@test
def undo_refuses_paths_outside_folder():           # 评审 L6
    d = shoot("ev")
    victim = mkdir("victim"); open(victim + "/keep.txt", "w").write("x")
    json.dump([{"id": "x", "time": "t", "mode": "folders",
                "ops": [{"op": "move", "src": "../victim/moved.txt", "dst": "../victim/keep.txt"}]}],
              open(d + "/.hb_cull_log.json", "w"))
    S = hb.Session(d)
    S.undo()
    assert os.path.exists(victim + "/keep.txt") and not os.path.exists(victim + "/moved.txt")


@test
def malformed_heic_is_fast():                      # 评审 L4
    p = os.path.join(mkdir("badheic"), "B0001.HEIC")
    iloc = struct.pack(">I4sBBBBBBI", 18, b"iloc", 2, 0, 0, 0, 0x44, 0x00, 0xFFFFFFFF)
    meta = struct.pack(">I4sI", 8 + 4 + len(iloc), b"meta", 0) + iloc
    open(p, "wb").write(struct.pack(">I4s4sI", 16, b"ftyp", b"heic", 0) + meta)
    th = threading.Thread(target=lambda: (hb.heif_rating(p), hb.heif_preview(p)), daemon=True)
    th.start(); th.join(3)
    assert not th.is_alive(), "解析卡住超过 3 秒"


# ---------------------------------------------------------------- 本地服务：安全
def start_server(folder):
    S = hb.Session(folder)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), hb.Handler)
    port = srv.server_address[1]
    srv.daemon_threads = True; srv.sess = S; srv.preview_px = 3000; srv.last_seen = time.monotonic()
    srv.token = "testtoken123"; srv.allowed_hosts = {"127.0.0.1:%d" % port, "localhost:%d" % port}
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, port


def req(port, path, body=None, headers=None, method=None):
    h = dict(headers or {})
    data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
    r = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


@test
def server_rejects_foreign_requests():             # 评审 H1
    d = shoot("sec")
    srv, port = start_server(d)
    try:
        J = {"Content-Type": "application/json"}
        T = {"X-Cull3fr-Token": "testtoken123"}
        # 1) 网页常见的「简单请求」：text/plain、没有口令
        st, _ = req(port, "/api/set", b'{"k":"B0000001","r":5}', {"Content-Type": "text/plain", "Origin": "http://evil.example"})
        assert st == 403, st
        # 2) 有口令但不是 JSON
        st, _ = req(port, "/api/set", b'{}', dict(T, **{"Content-Type": "text/plain"}))
        assert st == 415, st
        # 3) DNS 重绑定：Host 不是本机
        st, _ = req(port, "/", headers={"Host": "evil.example:%d" % port})
        assert st == 403, st
        st, _ = req(port, "/api/items", headers=dict(T, Host="evil.example"))
        assert st == 403, st
        # 4) 没口令拿不到列表和图片
        assert req(port, "/api/items")[0] == 403
        assert req(port, "/img/p?k=B0000001&v=1")[0] == 403
        # 5) 首页里带口令，正常请求可以用
        st, page = req(port, "/")
        assert st == 200 and b"testtoken123" in page
        st, body = req(port, "/api/items", headers=T)
        assert st == 200 and json.loads(body)["items"]
        assert req(port, "/img/p?k=B0000001&v=1&t=testtoken123")[0] == 200
        assert srv.sess.ov == {}
    finally:
        srv.shutdown()


@test
def stale_session_is_rejected():                   # 评审 M2
    d1, d2 = shoot("s1"), shoot("s2")
    srv, port = start_server(d1)
    try:
        H = {"Content-Type": "application/json", "X-Cull3fr-Token": "testtoken123"}
        old_sid = srv.sess.sid
        st, body = req(port, "/api/open", {"path": d2}, H)
        assert json.loads(body)["ok"]
        st, body = req(port, "/api/set", {"k": "B0000001", "r": 5, "sid": old_sid}, H)
        assert json.loads(body).get("stale") and srv.sess.ov == {}
        st, body = req(port, "/api/set", {"k": "B0000001", "r": 5, "sid": srv.sess.sid}, H)
        assert json.loads(body)["r"] == 5
    finally:
        srv.shutdown()


# ---------------------------------------------------------------- 浏览器：快速打标、注入
@test
def browser_fast_marking_and_xss():                # 评审 H2 / L1
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise Skip("没装 playwright")
    d = mkdir("race")
    jpg = TINY_JPEG
    for i in range(6):
        open("%s/B%04d.3FR" % (d, i), "wb").write(b"II*\x00" + struct.pack("<I", 8) + b"\x00" * 6)
        open("%s/B%04d.JPG" % (d, i), "wb").write(jpg)
    # 一个会让 plistlib 报错、报错信息里带 HTML 的 .phos
    open(d + "/B0005.3FR.phos", "wb").write(b'<?xml version="1.0" encoding="UTF-8"?>\n<plist version="1.0"><dict><key>Approval</key><integer>&lt;img src=x onerror=&quot;document.title=\'PWNED\'&quot;&gt;</integer></dict></plist>')
    srv, port = start_server(d)
    S = srv.sess
    orig = S._save_state
    S._save_state = lambda: (time.sleep(0.15), orig())[1]    # 模拟慢盘
    try:
        with sync_playwright() as p:
            b = p.chromium.launch()
            pg = b.new_page()
            pg.goto("http://127.0.0.1:%d/" % port)
            pg.wait_for_function("document.querySelector('#name').textContent==='B0000'")
            pg.keyboard.press("Digit5"); time.sleep(0.08); pg.keyboard.press("Digit8")
            time.sleep(1.5)
            assert S.ov.get("B0000") == {"r": 5}, S.ov
            assert S.ov.get("B0001") == {"l": "red"}, S.ov
            for _ in range(4):
                pg.keyboard.press("ArrowRight"); time.sleep(0.3)
            time.sleep(1)
            assert pg.title() != "PWNED"
            b.close()
    finally:
        srv.shutdown()



# ---------------------------------------------------------------- 第二轮评审（N1–N7）
@test
def marks_give_way_after_write_and_come_back_on_undo():   # N4
    d = shoot("n4")
    S = hb.Session(d)
    S.set_mark("B0000002", {"l": "green", "r": 4})
    S.write_all(True)
    assert "B0000002" not in S.ov, S.ov                   # 写成功后让位给文件
    p = os.path.join(d, "B0000002.3FR.phos")
    data = plistlib.loads(read(p)); data["Approval"] = 2  # 之后在 Phocus 里改回红色
    open(p, "wb").write(hb.dumps_phocus(data))
    S2 = hb.Session(d)
    it = S2.item_json(S2.by_key["B0000002"])
    assert it["l"] == "red" and not it["p"] and S2.plan()["files"] == 0, it
    # 撤销时把让位的标记放回来（文件已被 Phocus 改过，不动文件）
    d2 = shoot("n4b")
    S3 = hb.Session(d2)
    S3.set_mark("B0000002", {"l": "green"}); S3.write_all(True)
    S3.undo()
    assert S3.ov.get("B0000002") == {"l": "green"}, S3.ov


@test
def undo_with_missing_backup_does_not_get_stuck():        # N3
    d = shoot("n3")
    first = read(os.path.join(d, "B0000002.3FR.phos"))
    S = hb.Session(d)
    S.set_mark("B0000002", {"l": "green"}); S.write_all(True)
    S.set_mark("B0000002", {"r": 1}); S.write_all(True)
    log = S._read_log()
    os.remove(S.apath(log[-1]["ops"][0]["backup"], S.sdir))   # 删掉最新一次的备份
    r1 = S.undo()
    assert "撤销不了" in r1["text"], r1["text"]
    r2 = S.undo()                                                # 不会卡住：第二次撤销处理更早那次
    assert "之后又被改过" in r2["text"], r2["text"]               # 文件在那之后又被改过，按规则不覆盖
    assert not S._read_log()


@test
def undo_counts_only_real_restores():                     # N7
    d = shoot("n7")
    S = hb.Session(d)
    S.set_mark("B0000001", {"l": "green"}); S.write_all(True)
    p = os.path.join(d, "B0000001.3FR.phos")
    open(p, "wb").write(read(S.apath(S._read_log()[-1]["ops"][0]["backup"], S.sdir)))  # 已经手动还原
    r = S.undo()
    assert "还原 0 个文件" in r["text"], r["text"]


@test
def stale_full_render_is_skipped():                        # N5
    d = shoot("n5")
    S = hb.Session(d)
    calls = []
    hb_render = hb.render
    def slow_render(src, dst, max_px):
        calls.append(src); time.sleep(0.5); open(dst, "wb").write(b"x")
    hb.render = slow_render
    try:
        its = S.items
        res = {}
        def go(i, name):
            try:
                S.ensure(its[i], "f"); res[name] = "ok"
            except hb.StaleRequest:
                res[name] = "stale"
        t1 = threading.Thread(target=go, args=(0, "a")); t1.start(); time.sleep(0.1)
        t2 = threading.Thread(target=go, args=(1, "b")); t2.start(); time.sleep(0.05)
        t3 = threading.Thread(target=go, args=(0, "c")); t3.start()
        for t in (t1, t2, t3):
            t.join(5)
        assert res["b"] == "stale" and res["c"] in ("ok", "stale") and len(calls) <= 2, (res, calls)
    finally:
        hb.render = hb_render


@test
def page_cannot_be_framed():                               # N6
    d = shoot("n6")
    srv, port = start_server(d)
    try:
        r = urllib.request.urlopen("http://127.0.0.1:%d/" % port, timeout=5)
        assert r.headers.get("X-Frame-Options") == "DENY" and "frame-ancestors" in r.headers.get("Content-Security-Policy", "")
    finally:
        srv.shutdown()


@test
def browser_focus_reload_does_not_misplace_marks():        # N1
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise Skip("没装 playwright")
    d = mkdir("n1")
    jpg = TINY_JPEG
    for i in range(200):
        open("%s/B%04d.3FR" % (d, i), "wb").write(b"II*\x00" + struct.pack("<I", 8) + b"\x00" * 6)
        open("%s/B%04d.JPG" % (d, i), "wb").write(jpg)
    srv, port = start_server(d)
    S = srv.sess
    orig = S.items_json
    S.items_json = lambda: (time.sleep(0.4), orig())[1]    # 让 /api/items 慢一点，制造竞争
    try:
        with sync_playwright() as p:
            b = p.chromium.launch()
            pg = b.new_page()
            pg.goto("http://127.0.0.1:%d/" % port)
            pg.wait_for_function("document.querySelector('#name').textContent==='B0000'")
            time.sleep(0.6)
            pg.evaluate("window.dispatchEvent(new Event('focus'))")
            time.sleep(0.05)
            pg.keyboard.press("Digit5"); time.sleep(0.05); pg.keyboard.press("Digit4")
            time.sleep(2)
            assert S.ov.get("B0000") == {"r": 5} and S.ov.get("B0001") == {"r": 4}, S.ov
            assert pg.inner_text("#name") == "B0002", pg.inner_text("#name")
            b.close()
    finally:
        srv.shutdown()


@test
def browser_reloads_after_server_restart():                # N2
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise Skip("没装 playwright")
    d = shoot("n2")
    srv, port = start_server(d)
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page()
        pg.goto("http://127.0.0.1:%d/" % port)
        pg.wait_for_function("document.querySelector('#name').textContent!==''")
        srv.shutdown(); srv.server_close()
        pg.keyboard.press("Digit3"); time.sleep(1)
        assert pg.evaluate("document.querySelector('#lost').classList.contains('show')")
        b.close()


# ---------------------------------------------------------------- 只有 HEIC 的照片
@test
def heic_only_with_existing_phos_is_patched():
    d = mkdir("heic1")
    fake_heic(d + "/B0001.HEIC")
    put(d + "/B0001.HEIC.phos", PHOS_HEIC_GREEN_2())   # Approval 0，2★
    before = read(d + "/B0001.HEIC.phos")
    S = hb.Session(d)
    it = S.item_json(S.by_key["B0001"])
    assert it["r"] == 2 and it["l"] == "green", it
    S.set_mark("B0001", {"l": "red", "r": 5})
    assert "将写入 1 张" in S.plan()["text"]
    S.write_all(True)
    after = read(d + "/B0001.HEIC.phos")
    assert hb.phos_info_bytes(after) == {"approval": 2, "rating": 5}
    assert len(changed_lines(before, after)) == 4
    S.undo()
    assert read(d + "/B0001.HEIC.phos") == before


@test
def heic_only_without_phos_is_created():
    d = mkdir("heic2")
    fake_heic(d + "/B0000003.HEIC")
    S = hb.Session(d)
    S.set_mark("B0000003", {"l": "red", "r": 1})
    plan = S.plan()["text"]
    assert "只有 HEIC" in plan, plan
    S.write_all(True)
    p = d + "/B0000003.HEIC.phos"
    n = plistlib.loads(read(p))
    ref = plistlib.loads(PHOS_HEIC_GREEN_2())
    s0 = n["ImageSettings"][0]
    assert s0["Name"] == ";;Initial" and s0["ImageCorrection"]["ColorTemperature"] == 0 and s0["ImageCorrection"]["Tint"] == 0
    rc = dict(ref["ImageSettings"][0]["ImageCorrection"]); nc = dict(s0["ImageCorrection"])
    for k in ("ColorTemperature", "Tint"):
        rc.pop(k); nc.pop(k)
    assert rc == nc
    assert hb.phos_info(p) == {"approval": 2, "rating": 1}
    assert hb.dumps_phocus(n) == read(p)


@test
def pair_still_writes_raw_not_heic():
    d = shoot("pair")
    heic_phos = read(d + "/B0000001.HEIC.phos")
    S = hb.Session(d)
    S.set_mark("B0000001", {"r": 4})
    S.write_all(True)
    assert read(d + "/B0000001.HEIC.phos") == heic_phos
    assert hb.phos_info(d + "/B0000001.3FR.phos")["rating"] == 4


# ---------------------------------------------------------------- 第三轮评审
@test
def heic_marks_follow_when_raw_arrives_later():
    d = mkdir("carry")
    fake_heic(d + "/B0001.HEIC")
    S = hb.Session(d)
    S.set_mark("B0001", {"r": 5, "l": "green"})
    S.write_all(True)
    assert hb.phos_info(d + "/B0001.HEIC.phos") == {"approval": 0, "rating": 5}
    fake_3fr(d + "/B0001.3FR")              # 3FR 后来才拷进来
    S2 = hb.Session(d)
    it = S2.item_json(S2.by_key["B0001"])
    assert it["r"] == 5 and it["l"] == "green" and it["p"] and not it["mm"], it
    S2.write_all(True)
    assert hb.phos_info(d + "/B0001.3FR.phos") == {"approval": 0, "rating": 5}
    S3 = hb.Session(d)
    assert not S3.item_json(S3.by_key["B0001"])["p"] and not S3.carry


@test
def undo_does_not_bring_back_marks_over_later_phocus_edits():
    d = shoot("m2")
    S = hb.Session(d)
    S.set_mark("B0000002", {"r": 4}); S.write_all(True)
    p = d + "/B0000002.3FR.phos"
    S.ov.clear()
    newdata = hb.phos_patch(read(p), rating=5)                     # 之后在 Phocus 里改成 5★
    open(p, "wb").write(newdata)
    S.undo()
    S2 = hb.Session(d)
    assert S2.plan()["files"] == 0 and hb.phos_info(p)["rating"] == 5, (S2.ov, S2.plan())


@test
def full_render_survives_back_and_forth():
    d = shoot("n5b")
    S = hb.Session(d)
    hb_render = hb.render
    def slow_render(src, dst, max_px):
        time.sleep(0.4); open(dst, "wb").write(b"x")
    hb.render = slow_render
    try:
        A, B = S.items[0], S.items[1]
        res = {}
        def go(it, name):
            try:
                S.ensure(it, "f"); res[name] = "ok"
            except hb.StaleRequest:
                res[name] = "stale"
            except Exception as ex:
                res[name] = "error " + str(ex)
        ts = []
        for it, name in ((A, "A1"), (B, "B1"), (A, "A2"), (B, "B2")):
            t = threading.Thread(target=go, args=(it, name)); t.start(); ts.append(t); time.sleep(0.05)
        for t in ts:
            t.join(10)
        assert res["B2"] == "ok", res                            # 最后停在 B：B 必须能看到
        assert not any(v.startswith("error") for v in res.values()), res
    finally:
        hb.render = hb_render


# ---------------------------------------------------------------- 可选：真实 X2D II 文件
@test
def real_media_previews_and_ratings():
    if not SAMPLES:
        raise Skip("没有设置 CULL3FR_SAMPLES")
    found = 0
    for f in sorted(os.listdir(SAMPLES)):
        p = os.path.join(SAMPLES, f)
        e = os.path.splitext(f)[1].lower()
        if e == ".3fr":
            with open(p, "rb") as fh:
                assert hb.tiff_rating(fh) is not None, f
            j = hb.raw_preview(p)
            assert j and hb._jpeg_size(j), f
            found += 1
        elif e in (".heic", ".heif"):
            j = hb.heif_preview(p)
            assert j and hb._jpeg_size(j), f
            found += 1
        elif e == ".phos":
            d = read(p)
            assert hb.dumps_phocus(plistlib.loads(d)) == d, "%s 不能逐字节还原" % f
            found += 1
    assert found, "文件夹里没有 3FR / HEIC / .phos"

# ---------------------------------------------------------------- 单实例
@test
def single_instance_hands_over_folder():           # 评审 M1
    import subprocess
    env = dict(os.environ, CULL3FR_CACHE=mkdir("inst_cache"))
    d1, d2 = shoot("i1"), shoot("i2")
    p1 = subprocess.Popen([sys.executable, SCRIPT, d1, "--no-browser", "--port", "18950"], env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        time.sleep(2.5)
        out = subprocess.run([sys.executable, SCRIPT, d2, "--no-browser", "--port", "18950"], env=env,
                             capture_output=True, text=True, timeout=60).stdout
        assert "已经在运行" in out, out
        reg = json.load(open(os.path.join(env["CULL3FR_CACHE"], "server.json")))
        st, body = req(reg["port"], "/api/ping", headers={"X-Cull3fr-Token": reg["token"]})
        assert json.loads(body)["name"] == "i2", body
        st, _ = req(reg["port"], "/api/quit", {"sid": ""}, {"Content-Type": "application/json", "X-Cull3fr-Token": reg["token"]})
        p1.wait(timeout=10)
        assert not os.path.exists(os.path.join(env["CULL3FR_CACHE"], "server.json"))
    finally:
        if p1.poll() is None:
            p1.kill()


@test
def install_app_makes_real_bundle_with_icon():
    """osacompile 只有在 -o 以 .app 结尾时才生成 App；0.5 的临时名不是，生成的「App」打不开。"""
    import subprocess as sp
    import zlib
    home = mkdir("fakehome")
    apps = os.path.join(home, "Applications")
    os.makedirs(apps)
    put(os.path.join(apps, "cull3fr.app"), b"flat compiled script left by 0.5")
    calls = []

    def fake_run(args, **kw):
        calls.append(list(args))
        if args[0] == "osacompile":
            out = args[args.index("-o") + 1]
            if out.endswith(".app"):
                os.makedirs(os.path.join(out, "Contents", "Resources"))
                put(os.path.join(out, "Contents", "Info.plist"), plistlib.dumps(
                    {"CFBundleExecutable": "applet", "CFBundleIconFile": "applet", "CFBundleIconName": "applet"},
                    fmt=plistlib.FMT_BINARY))
                put(os.path.join(out, "Contents", "Resources", "Assets.car"), b"car")
                put(os.path.join(out, "Contents", "Resources", "applet.icns"), b"default")
            else:
                put(out, b"compiled script")  # 其他扩展名：真 osacompile 只生成脚本文件
        return sp.CompletedProcess(args, 0, "", "")

    saved = (sys.platform, hb.subprocess.run, os.environ.get("HOME"))
    sys.platform, hb.subprocess.run, os.environ["HOME"] = "darwin", fake_run, home
    try:
        app = hb.install_app()
    finally:
        sys.platform, hb.subprocess.run = saved[0], saved[1]
        if saved[2] is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved[2]
    assert app == os.path.join(apps, "Cull3fr.app") and os.path.isdir(app), app
    assert os.listdir(apps) == ["Cull3fr.app"], os.listdir(apps)  # 旧的小写 App 和临时目录都已清理
    osa = next(c for c in calls if c[0] == "osacompile")
    assert osa[osa.index("-o") + 1].endswith("/Cull3fr.app"), osa
    script = "\n".join(osa[i + 1] for i, a in enumerate(osa) if a == "-e")
    assert "--launch" in script and os.path.abspath(SCRIPT) in script, script
    info = plistlib.load(open(os.path.join(app, "Contents", "Info.plist"), "rb"))
    assert "CFBundleIconName" not in info and info["CFBundleIconFile"] == "applet", info
    assert not os.path.exists(os.path.join(app, "Contents", "Resources", "Assets.car"))
    icns = read(os.path.join(app, "Contents", "Resources", "applet.icns"))
    assert icns[:4] == b"icns" and struct.unpack(">I", icns[4:8])[0] == len(icns)
    pos, seen = 8, {}
    while pos < len(icns):
        tag, ln = icns[pos:pos + 4], struct.unpack(">I", icns[pos + 4:pos + 8])[0]
        png = icns[pos + 8:pos + ln]
        assert png[:8] == b"\x89PNG\r\n\x1a\n", tag
        seen[tag] = struct.unpack(">II", png[16:24])
        if tag == b"ic12":  # 64×64：检查几个像素
            raw = zlib.decompress(png[png.index(b"IDAT") + 4:png.index(b"IEND") - 8])
            px = lambda x, y: tuple(raw[y * 257 + 1 + 4 * x:y * 257 + 5 + 4 * x])
            assert px(0, 0)[3] == 0, px(0, 0)                      # 方块外透明
            assert px(32, 32) == (0xff, 0xcc, 0x00, 255), px(32, 32)  # 中间是黄点
            assert px(18, 32) == (0x34, 0xc7, 0x59, 255), px(18, 32)  # 左边绿点
            assert px(46, 32) == (0xff, 0x45, 0x3a, 255), px(46, 32)  # 右边红点
            assert px(32, 12) == hb.ICON_BG + (255,), px(32, 12)   # 方块本身
        pos += ln
    assert seen == {t: (n, n) for t, n in hb.ICNS_TYPES}, seen
    assert any(c[0] == "codesign" for c in calls), calls


@test
def readme_icon_matches_app_icon():
    """README 里的 docs/icon.png 要和 App 图标出自同一段代码（改了图标记得重新生成这张图）。"""
    p = os.path.join(os.path.dirname(os.path.abspath(SCRIPT)), "docs", "icon.png")
    assert read(p) == hb.icon_png(256), "docs/icon.png 过期了：用 icon_png(256) 重新生成"


if __name__ == "__main__":
    only = sys.argv[1:]
    for name, fn in list(globals().items()):
        if callable(fn) and getattr(fn, "__name__", "") == name and name not in ("test", "mkdir", "shoot", "read",
                                                                                     "changed_lines", "start_server", "req", "fake_3fr", "fake_heic") \
                and not name.startswith("_") and fn.__module__ == "__main__" and name not in ("hb",):
            if only and name not in only:
                continue
            if name[0].islower() and fn.__code__.co_argcount == 0:
                fn()
    w = max(len(n) for n, _, _ in RESULTS)
    for n, s, msg in RESULTS:
        print("%-*s  %s" % (w, n, s))
        if msg:
            print("    " + msg.replace("\n", "\n    "))
    print("\n%d passed, %d failed, %d skipped" % (sum(s == "PASS" for _, s, _ in RESULTS),
                                                 sum(s == "FAIL" for _, s, _ in RESULTS), sum(s == "SKIP" for _, s, _ in RESULTS)))
    shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(1 if any(s == "FAIL" for _, s, _ in RESULTS) else 0)
