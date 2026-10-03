import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock

spec = importlib.util.spec_from_file_location('deploy', Path(__file__).resolve().parents[1] / 'scripts/deploy.py')
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)


class DeploymentTest(unittest.TestCase):
    def test_ci_requires_successful_latest_push_for_exact_commit(self):
        good = dict(id=1, head_sha='abc', head_branch='main', event='push',
                    status='completed', conclusion='success')
        cases = [([], False), ([good], True),
                 ([dict(good, head_sha='other')], False),
                 ([dict(good, event='pull_request')], False),
                 ([dict(good, status='in_progress', conclusion=None)], False),
                 ([good, dict(good, id=2, conclusion='failure')], False)]
        for runs, expected in cases:
            response = Mock()
            response.__enter__ = Mock(return_value=response)
            response.__exit__ = Mock(return_value=False)
            response.read.return_value = json.dumps({'workflow_runs': runs})
            with self.subTest(runs=runs), patch.object(deploy.urllib.request, 'urlopen', return_value=response):
                self.assertEqual(deploy.ci_passed('abc'), expected)

    def test_failed_health_rolls_back_and_blocks_repeated_deploy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'jev_router.py').write_text('pass\n')

            def git(*args):
                if args[0] == 'branch':
                    return 'main'
                if args[0] == 'rev-parse':
                    return 'old' if args[1] == 'HEAD' else 'new'
                if args[0] == 'diff':
                    return 'jev_router.py'
                return ''

            with patch.object(deploy, 'REPO', root), patch.object(deploy, 'STATE', root), \
                    patch.object(deploy, 'git', side_effect=git) as git_mock, \
                    patch.object(deploy, 'ci_passed', return_value=True), \
                    patch.object(deploy, 'health'), patch.object(deploy, 'restart') as restart, \
                    patch.object(deploy, 'wait_healthy', side_effect=[RuntimeError('unhealthy'), None]):
                with self.assertRaisesRegex(RuntimeError, 'unhealthy'):
                    deploy.deploy()
                git_mock.assert_any_call('reset', '--keep', 'old')
                self.assertEqual(restart.call_count, 2)
                self.assertEqual((root / 'failed-commit').read_text().strip(), 'new')
                self.assertFalse((root / 'deployed-commit').exists())
                with self.assertRaisesRegex(RuntimeError, 'failed deployment before'):
                    deploy.deploy()

    def test_unapproved_commit_does_not_modify_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            responses = ['main', '', '', 'old', 'new', '', 'dashboard.html']
            with patch.object(deploy, 'STATE', Path(directory)), \
                    patch.object(deploy, 'git', side_effect=responses) as git_mock, \
                    patch.object(deploy, 'ci_passed', return_value=False), \
                    patch.object(deploy, 'restart') as restart:
                deploy.deploy()
                self.assertFalse(any(call.args[0] in ('merge', 'reset') for call in git_mock.call_args_list))
                restart.assert_not_called()
