"""Guest fence test. Run: python3 test_guest_fence.py

Runs real commands through the real shell sandbox, as a guest (pool_session)
and as the owner, and checks:
  - a guest can't read or list the owner's home, or a planted secret file in it
  - a guest can't write outside its workspace, or see the owner's env vars
  - a guest ignores the working folder it asked for
  - a guest's tools still work (sh, python3, git) and it can write its workspace
  - the owner's own sessions are unchanged
Plants one file in the owner's home for the test and removes it after.
"""
import asyncio, importlib.util, os, sys, types, uuid

HERE = os.path.dirname(os.path.abspath(__file__))


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec); sys.modules[name] = mod; spec.loader.exec_module(mod)
    return mod


# Load the two modules without the relay's heavy package imports.
for pkg in ("branch_monkey_mcp", "branch_monkey_mcp.computer_runtime", "branch_monkey_mcp.cerver_compute"):
    sys.modules[pkg] = types.ModuleType(pkg); sys.modules[pkg].__path__ = []
fence = load("branch_monkey_mcp.computer_runtime.guest_fence", os.path.join(HERE, "guest_fence.py"))
sys.modules["branch_monkey_mcp.computer_runtime"].guest_fence = fence
shell = load("branch_monkey_mcp.cerver_compute.shell_sandbox", os.path.join(HERE, "..", "cerver_compute", "shell_sandbox.py"))

ok, how = fence.fence_status()
if not ok:
    print(f"guest fence: SKIP ({how})"); sys.exit(0)

home = os.path.expanduser("~")
secret_path = os.path.join(home, f".cerver-fence-test-{uuid.uuid4().hex[:8]}")
with open(secret_path, "w") as f:
    f.write("owner-secret-value\n")
os.environ["OWNER_ONLY_TOKEN"] = "owner-token-value"
probe_write = os.path.join(home, f".cerver-fence-write-{uuid.uuid4().hex[:8]}")


async def run(sb, cmd):
    r = await shell.run_in_shell_sandbox(sb["sandbox_id"], cmd, 30)
    return r["exit_code"], r["stdout"] + r["stderr"]


async def main():
    fails = []

    def check(cond, msg):
        print(("  ok   " if cond else "  FAIL ") + msg)
        if not cond:
            fails.append(msg)

    g = shell.create_shell_sandbox({"pool_session": True, "working_dir": home}, None)
    ws = shell.get_shell_sandbox(g["sandbox_id"])["working_dir"]
    print(f"guest fence ({how}), workspace {ws}")
    check(not ws.startswith(home), "guest ignores the folder it asked for (got a fresh workspace)")
    code, out = await run(g, f"cat {secret_path}")
    check(code != 0 and "owner-secret-value" not in out, "guest can't read a file in the owner's home")
    code, out = await run(g, f"ls {home}")
    check(code != 0, "guest can't list the owner's home")
    code, out = await run(g, f"touch {probe_write}")
    check(code != 0 and not os.path.exists(probe_write), "guest can't write in the owner's home")
    code, out = await run(g, "touch /etc/cerver-fence-probe")
    check(code != 0, "guest can't write system folders")
    code, out = await run(g, "env")
    check("owner-token-value" not in out, "guest doesn't see the owner's environment variables")
    code, out = await run(g, "echo $HOME")
    check(out.strip() == ws, "guest HOME is its workspace")
    code, out = await run(g, "echo hi > a.txt && cat a.txt && python3 -c 'print(6*7)' && git --version")
    check(code == 0 and "hi" in out and "42" in out and "git version" in out, "guest tools work and it can write its workspace")
    shell.delete_shell_sandbox(g["sandbox_id"])
    check(not os.path.exists(ws), "guest workspace removed with the session")

    o = shell.create_shell_sandbox({"working_dir": home}, None)
    code, out = await run(o, f"cat {secret_path} && echo $OWNER_ONLY_TOKEN")
    check(code == 0 and "owner-secret-value" in out and "owner-token-value" in out, "owner's own session unchanged")
    shell.delete_shell_sandbox(o["sandbox_id"])
    return fails


try:
    fails = asyncio.run(main())
finally:
    for p in (secret_path, probe_write):
        try:
            os.remove(p)
        except FileNotFoundError:
            pass

print("guest fence: " + ("PASS" if not fails else f"FAIL ({len(fails)})"))
sys.exit(1 if fails else 0)
