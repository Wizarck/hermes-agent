"""Tests for the bundled observability/langfuse plugin."""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_DIR = REPO_ROOT / "plugins" / "observability" / "langfuse"


# ---------------------------------------------------------------------------
# Manifest + layout
# ---------------------------------------------------------------------------

class TestManifest:
    def test_plugin_directory_exists(self):
        assert PLUGIN_DIR.is_dir()
        assert (PLUGIN_DIR / "plugin.yaml").exists()
        assert (PLUGIN_DIR / "__init__.py").exists()

    def test_manifest_fields(self):
        data = yaml.safe_load((PLUGIN_DIR / "plugin.yaml").read_text())
        assert data["name"] == "langfuse"
        assert data["version"]
        # All six hooks the plugin implements.
        assert set(data["hooks"]) == {
            "pre_api_request", "post_api_request",
            "pre_llm_call", "post_llm_call",
            "pre_tool_call", "post_tool_call",
        }
        # Required env vars are the user-facing HERMES_ prefixed keys.
        assert "HERMES_LANGFUSE_PUBLIC_KEY" in data["requires_env"]
        assert "HERMES_LANGFUSE_SECRET_KEY" in data["requires_env"]


# ---------------------------------------------------------------------------
# Cost-attribution tags (Wizarck/hermes-agent fork extension)
# ---------------------------------------------------------------------------

class TestCostAttributionTags:
    """The fork injects `application` + `consumer` into Langfuse metadata so
    the eligia-core cost-by-tag dashboard can attribute Hermes-driven spend
    to the `hermes-bot` bucket instead of collapsing into `untagged`."""

    def _fresh_plugin(self):
        mod_name = "plugins.observability.langfuse"
        sys.modules.pop(mod_name, None)
        return importlib.import_module(mod_name)

    def test_application_tag_defaults_to_hermes_bot(self, monkeypatch):
        for k in ("HERMES_LANGFUSE_APPLICATION", "AIPLAYBOOK_APPLICATION"):
            monkeypatch.delenv(k, raising=False)
        mod = self._fresh_plugin()
        assert mod._application_tag() == "hermes-bot"

    def test_application_tag_reads_hermes_var_first(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGFUSE_APPLICATION", "hermes-staging")
        monkeypatch.setenv("AIPLAYBOOK_APPLICATION", "should-lose")
        mod = self._fresh_plugin()
        assert mod._application_tag() == "hermes-staging"

    def test_application_tag_falls_back_to_aiplaybook_env(self, monkeypatch):
        monkeypatch.delenv("HERMES_LANGFUSE_APPLICATION", raising=False)
        monkeypatch.setenv("AIPLAYBOOK_APPLICATION", "hermes-bot-canary")
        mod = self._fresh_plugin()
        assert mod._application_tag() == "hermes-bot-canary"

    def test_consumer_tag_defaults_to_hermes(self, monkeypatch):
        monkeypatch.delenv("HERMES_LANGFUSE_CONSUMER", raising=False)
        mod = self._fresh_plugin()
        assert mod._consumer_tag() == "HERMES"

    def test_consumer_tag_env_override(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGFUSE_CONSUMER", "HERMES_DEV")
        mod = self._fresh_plugin()
        assert mod._consumer_tag() == "HERMES_DEV"

    def test_root_trace_metadata_carries_both_tags(self, monkeypatch):
        """The trace-level metadata dict built by _start_root_trace MUST
        include both `application` and `consumer` keys.

        Trace-level tags are the defensive fallback when an observation
        doesn't carry its own metadata block. The eligia-core aggregator
        reads `obs.metadata.<dim>` first, `obs.trace.metadata.<dim>` second.
        """
        monkeypatch.setenv("HERMES_LANGFUSE_APPLICATION", "hermes-bot")
        monkeypatch.setenv("HERMES_LANGFUSE_CONSUMER", "HERMES")
        mod = self._fresh_plugin()

        captured = {}

        class FakeRootSpan:
            def set_trace_io(self, *_, **__): pass

        class FakeCtx:
            def __enter__(self_inner):
                return FakeRootSpan()
            def __exit__(self_inner, *_a): pass

        class FakeClient:
            def create_trace_id(self_inner, seed=""):
                return "fake-trace-id"
            def start_as_current_observation(self_inner, **kw):
                captured.update(kw)
                return FakeCtx()

        # propagate_attributes optional — easier to assert without it.
        monkeypatch.setattr(mod, "propagate_attributes", None)

        mod._start_root_trace(
            "task-key",
            task_id="t",
            session_id="s",
            platform="telegram",
            provider="anthropic",
            model="claude-haiku-4-5",
            api_mode="messages",
            messages=[{"role": "user", "content": "hi"}],
            client=FakeClient(),
        )
        metadata = captured.get("metadata") or {}
        assert metadata.get("application") == "hermes-bot"
        assert metadata.get("consumer") == "HERMES"
        assert metadata.get("source") == "hermes"  # existing tag preserved

    def test_generation_metadata_carries_both_tags(self, monkeypatch):
        """The PER-OBSERVATION metadata dict built by on_pre_llm_request MUST
        include both tags. This is the dimension the eligia-core cost-by-tag
        aggregator groups by; trace-level metadata is just a safety net.
        """
        monkeypatch.setenv("HERMES_LANGFUSE_PUBLIC_KEY", "pk-test")
        monkeypatch.setenv("HERMES_LANGFUSE_SECRET_KEY", "sk-test")
        monkeypatch.setenv("HERMES_LANGFUSE_APPLICATION", "hermes-bot")
        monkeypatch.setenv("HERMES_LANGFUSE_CONSUMER", "HERMES")
        mod = self._fresh_plugin()

        observed = {}

        class FakeRootSpan:
            def start_observation(self_inner, **kw):
                observed.update(kw)
                return object()

        class FakeState:
            root_span = FakeRootSpan()
            generations = {}
            tools = {}
            turn_tool_calls = []
            last_updated_at = 0.0

        # Pre-populate state so on_pre_llm_request takes the fast path
        # (no root-trace creation).
        state = FakeState()
        mod._TRACE_STATE[mod._trace_key("t", "s")] = state

        # Provide a fake client so _get_langfuse short-circuits to it.
        monkeypatch.setattr(mod, "_LANGFUSE_CLIENT", object())

        mod.on_pre_llm_request(
            task_id="t",
            session_id="s",
            platform="telegram",
            model="claude-haiku-4-5",
            provider="anthropic",
            api_mode="messages",
            api_call_count=1,
            messages=[],
        )
        metadata = observed.get("metadata") or {}
        assert metadata.get("application") == "hermes-bot"
        assert metadata.get("consumer") == "HERMES"
        # Existing tags preserved.
        assert metadata.get("provider") == "anthropic"

        # Cleanup so other tests don't see this state.
        mod._TRACE_STATE.clear()


# ---------------------------------------------------------------------------
# Plugin discovery: langfuse is opt-in (not loaded unless explicitly enabled).
# This guards against someone accidentally re-introducing a per-hook
# load_config() gate or making the plugin auto-load.
# ---------------------------------------------------------------------------

class TestDiscovery:
    def test_plugin_is_discovered_as_standalone_opt_in(self, tmp_path, monkeypatch):
        """Scanner should find the plugin but NOT load it by default."""
        from hermes_cli import plugins as plugins_mod

        # Isolated HERMES_HOME so we don't read the developer's config.yaml.
        home = tmp_path / ".hermes"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        manager = plugins_mod.PluginManager()
        manager.discover_and_load()

        # observability/langfuse appears in the plugin registry …
        loaded = manager._plugins.get("observability/langfuse")
        assert loaded is not None, "plugin not discovered"
        # … but is not loaded (opt-in default → no config.yaml means nothing enabled)
        assert loaded.enabled is False
        assert "not enabled" in (loaded.error or "").lower()


# ---------------------------------------------------------------------------
# Runtime gate: _get_langfuse() returns None and caches _INIT_FAILED when
# credentials are missing. Guards against regressing toward the rejected
# per-hook load_config() design.
# ---------------------------------------------------------------------------

class TestRuntimeGate:
    def _fresh_plugin(self):
        """Import the plugin module fresh (clears any cached client)."""
        mod_name = "plugins.observability.langfuse"
        sys.modules.pop(mod_name, None)
        return importlib.import_module(mod_name)

    def test_get_langfuse_returns_none_without_credentials(self, monkeypatch):
        for k in (
            "HERMES_LANGFUSE_PUBLIC_KEY", "HERMES_LANGFUSE_SECRET_KEY",
            "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY",
        ):
            monkeypatch.delenv(k, raising=False)

        langfuse_plugin = self._fresh_plugin()
        assert langfuse_plugin._get_langfuse() is None

    def test_get_langfuse_caches_failure_no_config_load(self, monkeypatch):
        """A miss must be cached — no per-hook config.yaml reads, no env re-reads."""
        for k in (
            "HERMES_LANGFUSE_PUBLIC_KEY", "HERMES_LANGFUSE_SECRET_KEY",
            "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY",
        ):
            monkeypatch.delenv(k, raising=False)

        langfuse_plugin = self._fresh_plugin()

        # Prime the cache with one call.
        assert langfuse_plugin._get_langfuse() is None

        # Now block os.environ.get — a correctly-cached plugin must not
        # touch env again.
        import os
        called = {"n": 0}
        real_get = os.environ.get

        def tracking_get(key, default=None):
            if key.startswith(("HERMES_LANGFUSE_", "LANGFUSE_")):
                called["n"] += 1
            return real_get(key, default)

        monkeypatch.setattr(os.environ, "get", tracking_get)

        for _ in range(20):
            assert langfuse_plugin._get_langfuse() is None

        assert called["n"] == 0, (
            f"_get_langfuse() re-read env {called['n']} times after cache miss — "
            "it should short-circuit via _INIT_FAILED"
        )

    def test_get_langfuse_does_not_import_hermes_config(self, monkeypatch):
        """The plugin must not re-read config.yaml per hook."""
        for k in (
            "HERMES_LANGFUSE_PUBLIC_KEY", "HERMES_LANGFUSE_SECRET_KEY",
            "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY",
        ):
            monkeypatch.delenv(k, raising=False)

        # Drop any cached import of hermes_cli.config.
        sys.modules.pop("hermes_cli.config", None)

        langfuse_plugin = self._fresh_plugin()
        for _ in range(20):
            langfuse_plugin._get_langfuse()

        assert "hermes_cli.config" not in sys.modules, (
            "langfuse plugin imported hermes_cli.config — regression toward "
            "the rejected per-hook load_config() design"
        )


# ---------------------------------------------------------------------------
# Hooks are inert when the client is unavailable.
# ---------------------------------------------------------------------------

class TestHooksInert:
    def test_hooks_noop_without_client(self, monkeypatch):
        """All 6 hooks must return without raising when _get_langfuse() is None."""
        for k in (
            "HERMES_LANGFUSE_PUBLIC_KEY", "HERMES_LANGFUSE_SECRET_KEY",
            "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY",
        ):
            monkeypatch.delenv(k, raising=False)

        sys.modules.pop("plugins.observability.langfuse", None)
        import importlib
        mod = importlib.import_module("plugins.observability.langfuse")

        # Each hook should just return; no exceptions.
        mod.on_pre_llm_call(task_id="t", session_id="s", messages=[{"role": "user", "content": "hi"}])
        mod.on_pre_llm_request(task_id="t", session_id="s", api_call_count=1, messages=[])
        mod.on_post_llm_call(task_id="t", session_id="s", api_call_count=1)
        mod.on_pre_tool_call(tool_name="read_file", args={}, task_id="t", session_id="s")
        mod.on_post_tool_call(tool_name="read_file", args={}, result="ok", task_id="t", session_id="s")
