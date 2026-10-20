# --------------------------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License.txt in the project root for license information.
# --------------------------------------------------------------------------------------------

import base64
import contextlib
import io
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

import yaml
from knack.util import CLIError

from azure.cli.core.azclierror import InvalidArgumentValueError, MutuallyExclusiveArgumentError

from azext_fleet.custom import (
    _build_member_credential_request,
    list_member_credentials,
    list_namespace_credentials,
    list_namespace_member_credentials,
)
from azext_fleet.vendored_sdks.v2026_11_02_preview.models import (
    FleetManagedNamespaceCredentialResult,
    FleetManagedNamespaceCredentialResults,
    FleetMemberCredentialRequest,
)


class _FakeCmd:
    def get_models(self, name, **_kwargs):
        if name != "FleetMemberCredentialRequest":
            raise AssertionError(f"unexpected model requested: {name}")
        return FleetMemberCredentialRequest


class CredentialCommandsTestCase(unittest.TestCase):
    def setUp(self):
        self.cmd = _FakeCmd()
        self.client = Mock()

    def test_builds_request_for_selected_members(self):
        request = _build_member_credential_request(self.cmd, ["member-1", "member-2"])

        self.assertEqual(request.member_names, ["member-1", "member-2"])
        self.assertIsNone(request.continuation_token)
        self.assertEqual(request.serialize(), {
            "memberNames": ["member-1", "member-2"],
        })

    def test_builds_request_for_continuation_token(self):
        request = _build_member_credential_request(
            self.cmd,
            continuation_token="next-page"
        )

        self.assertIsNone(request.member_names)
        self.assertEqual(request.continuation_token, "next-page")
        self.assertEqual(request.serialize(), {
            "continuationToken": "next-page",
        })

    def test_builds_empty_request_for_all_members(self):
        request = _build_member_credential_request(self.cmd)

        self.assertEqual(request.serialize(), {})

    def test_rejects_members_with_continuation_token(self):
        with self.assertRaises(MutuallyExclusiveArgumentError):
            _build_member_credential_request(
                self.cmd,
                ["member-1"],
                "next-page"
            )

    def test_rejects_more_than_100_members(self):
        with self.assertRaises(InvalidArgumentValueError):
            _build_member_credential_request(
                self.cmd,
                [f"member-{index}" for index in range(101)]
            )

    def test_rejects_duplicate_members(self):
        with self.assertRaises(InvalidArgumentValueError):
            _build_member_credential_request(
                self.cmd,
                ["member-1", "member-1"]
            )

    def test_lists_fleet_member_credentials(self):
        list_member_credentials(
            self.cmd,
            self.client,
            "resource-group",
            "fleet",
            member_names=["member-1"]
        )

        request = self.client.list_member_credentials.call_args.kwargs["body"]
        self.assertEqual(request.member_names, ["member-1"])
        self.client.list_member_credentials.assert_called_once_with(
            resource_group_name="resource-group",
            fleet_name="fleet",
            body=request
        )

    def test_lists_namespace_member_credentials(self):
        list_namespace_member_credentials(
            self.cmd,
            self.client,
            "resource-group",
            "fleet",
            "namespace",
            continuation_token="next-page"
        )

        request = self.client.list_member_credentials.call_args.kwargs["body"]
        self.assertEqual(request.continuation_token, "next-page")
        self.client.list_member_credentials.assert_called_once_with(
            resource_group_name="resource-group",
            fleet_name="fleet",
            managed_namespace_name="namespace",
            body=request
        )


class NamespaceCredentialsTestCase(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.config = {
            "apiVersion": "v1",
            "kind": "Config",
            "current-context": "hub",
            "clusters": [{"name": "hub", "cluster": {"server": "https://hub.example.com"}}],
            "contexts": [{"name": "hub", "context": {
                "cluster": "hub", "user": "hub-user", "namespace": "namespace",
            }}],
            "users": [{"name": "hub-user", "user": {"exec": {
                "apiVersion": "client.authentication.k8s.io/v1beta1",
                "command": "kubelogin",
                "args": ["get-token", "--login", "devicecode"],
            }}}],
        }
        credential = FleetManagedNamespaceCredentialResult()
        credential.value = base64.b64encode(yaml.safe_dump(self.config).encode("utf-8")).decode("ascii")
        response = FleetManagedNamespaceCredentialResults()
        response.kubeconfigs = [credential]
        self.client.list_credentials.return_value = response
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = os.path.join(directory.name, "config")

    def _get_credentials(self, **kwargs):
        return list_namespace_credentials(
            _FakeCmd(), self.client, "resource-group", "fleet", "namespace", **kwargs
        )

    @patch("azext_fleet.custom._convert_kubeconfig_to_azurecli")
    @patch("azext_fleet.custom.print_or_merge_credentials")
    def test_saves_to_default_path_then_converts(self, merge, convert):
        operations = Mock()
        operations.attach_mock(merge, "merge")
        operations.attach_mock(convert, "convert")

        self.assertIsNone(self._get_credentials())

        self.client.list_credentials.assert_called_once_with(
            resource_group_name="resource-group",
            fleet_name="fleet",
            managed_namespace_name="namespace"
        )
        default_path = os.path.join(os.path.expanduser("~"), ".kube", "config")
        merge.assert_called_once_with(
            default_path, yaml.safe_dump(self.config), False, None
        )
        convert.assert_called_once_with(default_path)
        self.assertEqual([call[0] for call in operations.mock_calls], ["merge", "convert"])

    @patch("azext_fleet.custom._convert_kubeconfig_to_azurecli")
    def test_merges_existing_config_and_sets_custom_context(self, convert):
        existing = {
            "apiVersion": "v1", "kind": "Config", "current-context": "existing",
            "clusters": [{"name": "existing", "cluster": {"server": "https://existing.example.com"}}],
            "contexts": [{"name": "existing", "context": {"cluster": "existing", "user": "existing"}}],
            "users": [{"name": "existing", "user": {"token": "test-token"}}],
        }
        with open(self.path, "w", encoding="utf-8") as stream:
            yaml.safe_dump(existing, stream)
        os.chmod(self.path, 0o600)

        self.assertIsNone(self._get_credentials(path=self.path, context_name="custom"))

        with open(self.path, encoding="utf-8") as stream:
            config = yaml.safe_load(stream)
        self.assertEqual(config["current-context"], "custom")
        self.assertIn(existing["contexts"][0], config["contexts"])
        self.assertIn(existing["clusters"][0], config["clusters"])
        self.assertIn(existing["users"][0], config["users"])
        context = next(item for item in config["contexts"] if item["name"] == "custom")
        self.assertEqual(context["context"]["namespace"], "namespace")
        convert.assert_called_once_with(self.path)

    @patch("azext_fleet.custom._convert_kubeconfig_to_azurecli")
    def test_overwrites_existing_credentials_without_prompting(self, _convert):
        self._get_credentials(path=self.path)
        self.config["clusters"][0]["cluster"]["server"] = "https://updated.example.com"
        self.client.list_credentials.return_value.kubeconfigs[0].value = (
            base64.b64encode(yaml.safe_dump(self.config).encode("utf-8")).decode("ascii")
        )

        with patch("azext_fleet._helpers.prompt_y_n") as prompt:
            self._get_credentials(path=self.path, overwrite_existing=True)
        prompt.assert_not_called()
        with open(self.path, encoding="utf-8") as stream:
            config = yaml.safe_load(stream)
        self.assertEqual(config["clusters"], self.config["clusters"])

    @patch("azext_fleet.custom.shutil.which", return_value="kubelogin")
    @patch("azext_fleet.custom.subprocess.run")
    def test_converts_saved_config_to_azurecli(self, run, _which):
        def convert(*args, **_kwargs):
            self.assertEqual(args[0], [
                "kubelogin", "convert-kubeconfig", "-l", "azurecli",
                "--kubeconfig", self.path,
            ])
            with open(self.path, encoding="utf-8") as stream:
                config = yaml.safe_load(stream)
            config["users"][0]["user"]["exec"]["args"][-1] = "azurecli"
            with open(self.path, "w", encoding="utf-8") as stream:
                yaml.safe_dump(config, stream)

        run.side_effect = convert
        self._get_credentials(path=self.path)
        run.assert_called_once()
        with open(self.path, encoding="utf-8") as stream:
            config = yaml.safe_load(stream)
        self.assertEqual(config["users"][0]["user"]["exec"]["args"][-1], "azurecli")
        self.assertEqual(config["contexts"][0]["context"]["namespace"], "namespace")
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    @patch("azext_fleet.custom.subprocess.run")
    def test_stdout_prints_yaml_without_json_or_conversion(self, run):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = self._get_credentials(path="-")
        self.assertIsNone(result)
        self.assertEqual(yaml.safe_load(output.getvalue()), self.config)
        run.assert_not_called()

    @patch("azext_fleet.custom._convert_kubeconfig_to_azurecli")
    def test_rejects_missing_credentials(self, convert):
        for response in (None, Mock(kubeconfigs=None), Mock(kubeconfigs=[]),
                         Mock(kubeconfigs=[Mock(value=None)])):
            with self.subTest(response=response):
                self.client.list_credentials.return_value = response
                with self.assertRaisesRegex(CLIError, "No Kubernetes credentials found"):
                    self._get_credentials(path=self.path)
        self.assertFalse(os.path.exists(self.path))
        convert.assert_not_called()

    def test_rejects_invalid_utf8(self):
        self.client.list_credentials.return_value.kubeconfigs[0].value = "/w=="
        with self.assertRaisesRegex(CLIError, "decode"):
            self._get_credentials(path=self.path)
        self.assertFalse(os.path.exists(self.path))

    def test_rejects_invalid_base64(self):
        self.client.list_credentials.return_value.kubeconfigs[0].value = "not base64!"
        with self.assertRaisesRegex(CLIError, "decode"):
            self._get_credentials(path=self.path)
        self.assertFalse(os.path.exists(self.path))


if __name__ == "__main__":
    unittest.main()
