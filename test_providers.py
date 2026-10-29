import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
from providers import CodexProvider, OpenAIProvider, QuotaError, parse_decision

class ProviderTests(unittest.IsolatedAsyncioTestCase):
    def test_web_search_is_explicit_and_does_not_enable_shell_or_plugins(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch("providers.shutil.which", return_value="C:/codex.exe"):
                provider = CodexProvider(folder, web_search="live")
                args = provider.command(Path(folder))
                self.assertIn('web_search="live"', args)
                self.assertEqual(args[args.index("code_mode_host")-1], "--enable")
                for feature in ("shell_tool", "apps", "plugins", "computer_use"):
                    self.assertEqual(args[args.index(feature)-1], "--disable")
                self.assertIn('web_search="disabled"', CodexProvider(folder).command(Path(folder)))
                with self.assertRaises(ValueError):
                    CodexProvider(folder, web_search="invented")

    def test_decision_rejects_invalid_or_extra_actions(self):
        for raw in ['null', '{"action":"shell","text":"rm"}', '{"action":"reply","text":""}',
                    '{"action":"reply","text":"ok","command":"bad"}', '{"action":"reply","text":3}']:
            with self.assertRaises(ValueError):
                parse_decision(raw)
        self.assertEqual(parse_decision('{"action":"silence","text":""}'), ("silence", ""))
        self.assertEqual(len(parse_decision(json.dumps({"action":"reply","text":"a"*3000}))[1]), 1900)

    async def test_codex_stdin_environment_schema_and_cleanup(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch("providers.shutil.which", return_value="C:/codex.exe"):
                provider = CodexProvider(folder)
            async def spawn(*args, **kwargs):
                self.assertIn("--ignore-user-config", args)
                self.assertIn("--ignore-rules", args)
                self.assertIn("--ephemeral", args)
                self.assertNotIn("DISCORD_BOT_TOKEN", kwargs["env"])
                self.assertNotIn("OPENAI_API_KEY", kwargs["env"])
                self.assertEqual(args[-1], "-")
                self.assertNotIn("user content", args)
                output = Path(args[args.index("--output-last-message")+1])
                async def communicate(data):
                    self.assertIn(b"user content", data)
                    output.write_text('{"action":"reply","text":"ok"}', encoding="utf-8")
                return NS(communicate=communicate, returncode=0)
            with patch.dict("os.environ", {"DISCORD_BOT_TOKEN":"secret", "OPENAI_API_KEY":"secret"}):
                with patch("providers.asyncio.create_subprocess_exec", side_effect=spawn):
                    self.assertEqual(await provider.complete("persona", {"text":"user content"}), ("reply","ok"))
            self.assertEqual(list(Path(folder).iterdir()), [])

    async def test_timeout_kills_process_and_never_falls_back(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch("providers.shutil.which", return_value="C:/codex.exe"):
                provider = CodexProvider(folder, timeout=0.01)
            async def communicate(data):
                await asyncio.Event().wait()
            process = NS(communicate=communicate, returncode=None, wait=AsyncMock())
            def kill():
                process.returncode = -1
            process.kill = kill
            with patch("providers.asyncio.create_subprocess_exec", AsyncMock(return_value=process)):
                with self.assertRaises(asyncio.TimeoutError):
                    await provider.complete("persona", {})
            self.assertEqual(process.returncode, -1)
            process.wait.assert_awaited_once()

    async def test_api_reserves_settles_and_disables_storage(self):
        usage = NS(input_tokens=100, output_tokens=20)
        api = NS(responses=NS(create=AsyncMock(return_value=NS(
            usage=usage, output_text='{"action":"reply","text":"ok"}'))))
        from unittest.mock import Mock
        budget = NS(reserve=Mock(return_value=5), settle=Mock())
        provider = OpenAIProvider(api, "gpt-6-luna", budget)
        self.assertEqual(await provider.complete("persona", {}), ("reply","ok"))
        budget.settle.assert_called_once_with(5, usage)
        self.assertFalse(api.responses.create.call_args.kwargs["store"])
        self.assertTrue(api.responses.create.call_args.kwargs["text"]["format"]["strict"])
        budget.reserve.return_value = None
        with self.assertRaises(QuotaError):
            await provider.complete("persona", {})
        self.assertEqual(api.responses.create.await_count, 1)

