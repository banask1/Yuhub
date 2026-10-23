"""智能应用控制开关自检（**只读**，绝不改注册表）。

⚠️ 本自检**不调用** ``set_state`` / ``disable`` / ``enable`` 的写路径
—— 那会真的改写系统安全策略，可能让本机再也开不回来。它只验证：

* 状态常量与注册表路径/值名是否正确；
* ``state_text`` 三态都有中文；
* ``read_state`` 在本机不抛异常（允许"没有 SAC / 需要管理员"）；
* ``CiTool.exe`` 路径格式；
* 非法状态在**碰注册表之前**就被拒绝（只调 ``set_state(99)``，走的是
  "非法状态"分支，不会写盘）。

用法： ``Yuhub.exe --sac-selftest <结果json路径>``
"""

import json
import os


def _dump(result, out_path):
    try:
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=2)
    except OSError:
        pass


def run(out_path):
    result = {"ok": True, "checks": []}

    def chk(name, passed, detail=""):
        result["checks"].append(
            {"name": name, "passed": bool(passed), "detail": str(detail)})
        if not passed:
            result["ok"] = False

    try:
        import sac
    except Exception as exc:                                   # noqa: BLE001
        result["ok"] = False
        result["error"] = repr(exc)
        _dump(result, out_path)
        return 1

    # ---- 常量与路径 ----
    chk("状态常量 0/1/2",
        (sac.STATE_OFF, sac.STATE_ON, sac.STATE_EVAL) == (0, 1, 2),
        (sac.STATE_OFF, sac.STATE_ON, sac.STATE_EVAL))
    chk("注册表路径含 Control\\CI\\Policy",
        "Control\\CI\\Policy" in sac.KEY_PATH, sac.KEY_PATH)
    chk("注册表值名",
        sac.VALUE_NAME == "VerifiedAndReputablePolicyState", sac.VALUE_NAME)

    for s in (sac.STATE_OFF, sac.STATE_ON, sac.STATE_EVAL):
        chk("state_text(%d) 有中文" % s,
            sac.state_text(s) != "未知", sac.state_text(s))

    chk("CiTool 路径以 CiTool.exe 结尾",
        sac.citool_path().lower().endswith("citool.exe"), sac.citool_path())

    # ---- 失败分类常量（UI 靠 kind 分流提示，不许匹配 err 文本）----
    chk("失败分类常量 ok/absent/denied/error",
        (sac.KIND_OK, sac.KIND_ABSENT, sac.KIND_DENIED, sac.KIND_ERROR)
        == ("ok", "absent", "denied", "error"),
        (sac.KIND_OK, sac.KIND_ABSENT, sac.KIND_DENIED, sac.KIND_ERROR))

    # ---- 只读探测（允许没有 SAC / 需要管理员，但不能抛异常）----
    state, err, kind = sac.read_state_ex()
    chk("read_state_ex 不抛异常", True,
        "state=%r err=%r kind=%r" % (state, err, kind))
    chk("read_state_ex 返回三件套且 kind 已知",
        kind in (sac.KIND_OK, sac.KIND_ABSENT, sac.KIND_DENIED, sac.KIND_ERROR),
        kind)
    chk("read_state_ex 状态类型", state is None or isinstance(state, int),
        repr(state))
    if state is not None:
        chk("读到的是已知状态", state in (0, 1, 2), state)
        chk("读到状态时 kind 必须是 ok", kind == sac.KIND_OK, kind)
    # 二值旧接口必须与三值接口一致（selftest 自身也用 read_state 复核）
    s2, e2 = sac.read_state()
    chk("read_state 与 read_state_ex 一致", (s2, e2) == (state, err),
        (s2, e2, state, err))

    # ---- 非法状态必须在碰注册表之前被拒绝 ----
    ok, msg = sac.set_state(99, refresh=False)
    chk("非法状态被拒且未写注册表", ok is False, msg)

    _dump(result, out_path)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    import sys
    sys.exit(run(sys.argv[1] if len(sys.argv) > 1 else "sac_selftest.json"))
