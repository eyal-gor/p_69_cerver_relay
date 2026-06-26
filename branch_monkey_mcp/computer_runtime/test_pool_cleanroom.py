"""Pool clean-room secret-isolation test. Run: python3 test_pool_cleanroom.py

Imports the real build_process_env (stubbing the 3 sibling modules so it loads
without the full relay deps), plants host + vault secrets, and asserts a pooled
session (pool_session=True) inherits NONE of them while a normal session does.
"""
import sys, os, types, tempfile, importlib.util

PKG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cli_runtime.py")

def fake(name, **a):
    m = types.ModuleType(name); [setattr(m, k, v) for k, v in a.items()]; sys.modules[name] = m

fake("branch_monkey_mcp"); fake("branch_monkey_mcp.computer_runtime")
fake("branch_monkey_mcp.bridge_and_local_actions")
fake("branch_monkey_mcp.bridge_and_local_actions.cli_providers", CliProvider=object, get_provider=lambda *a, **k: None)
fake("branch_monkey_mcp.infisical_client", get_secrets_sync=lambda: {"VAULT_SECRET": "contributor-vault"}, is_configured=lambda: True)
fake("branch_monkey_mcp.computer_runtime.agent_environment", get_path_for_subprocess=lambda: os.environ.get("PATH", ""))

spec = importlib.util.spec_from_file_location("branch_monkey_mcp.computer_runtime.cli_runtime", PKG)
mod = importlib.util.module_from_spec(spec); sys.modules[spec.name] = mod; spec.loader.exec_module(mod)
build_process_env = mod.build_process_env

class CliCmd: env_inject = env_overrides = env_finalize = None

os.environ["OWNER_PRIVATE_SECRET"] = "owner-only"
tmp = tempfile.mkdtemp(prefix="cr-")
proxy = {"ANTHROPIC_BASE_URL": "https://gateway.cerver.ai/v2/proxy/anthropic", "ANTHROPIC_API_KEY": "EPHEMERAL"}
cr = build_process_env(CliCmd(), extra_env=proxy, pool_session=True, clean_home=tmp)
normal = build_process_env(CliCmd(), pool_session=False)

assert cr.get("HOME") == tmp, "HOME not isolated"
assert cr.get("ANTHROPIC_API_KEY") == "EPHEMERAL", "token not applied / host key leaked"
assert "OWNER_PRIVATE_SECRET" not in cr, "LEAK: host secret in clean-room"
assert "VAULT_SECRET" not in cr, "LEAK: vault secret in clean-room"
assert cr.get("PATH"), "no PATH"
assert normal.get("OWNER_PRIVATE_SECRET") == "owner-only", "regression: normal lost host env"
assert normal.get("VAULT_SECRET") == "contributor-vault", "regression: normal lost vault"
print("pool clean-room: PASS (host + vault isolated; normal mode unchanged)")
